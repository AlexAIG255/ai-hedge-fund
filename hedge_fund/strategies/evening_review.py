"""
Agent 2: 晚间复盘 Agent（优化版）
=====================================================================
优化点（相对原版）：
  1. 【修复】飞书同步与本地跟踪池字段统一：统一用 entry_date / recommend_date 双写，
            解决原版『entry_date vs recommend_date』不一致导致持股天数算错的问题。
  2. 【统一】止盈/止损比例、观察周期改由 strategy_config 集中管理。
  3. 【新增】追踪止损：浮盈达标后止损线抬升至成本（保本）+ 跟随阶段峰值回撤，
            解决『涨上去又跌回来』的利润回吐。
  4. 【强化】明确的 🚨 抛售提醒：止盈/止损/到期触发时，在推送中单列『建议抛售清单』。
  5. 【新增】盘中实时监控模式（--mode intraday）：盘中定时运行，一旦触碰止损/止盈线，
            立即推送 🚨 抛售提醒，不必等到晚上。每日每只限提醒一次（节流）。

用法：
  python evening_review.py            # 默认：晚间飞书复盘 + 收盘判定
  python evening_review.py --mode intraday   # 盘中：实时监控止损/止盈并提醒抛售
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from typing import Dict, List, Tuple
import requests

# 📁 确保 strategy_config.py（与 evening_review.py 同目录）无论在哪运行都能被找到
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 📝 自动加载 .env（cron/手动运行都需；否则 WECHAT_WEBHOOK 等为空导致静默不推送）
try:
    from _env import load_env
except ImportError:
    from hedge_fund.strategies._env import load_env
load_env()

# 🔧 引入集中配置
try:
    from strategy_config import (
        strategy_name, is_triple_volume,
        compute_stop_loss, compute_take_profit, get_observation_days,
        compute_effective_stop, to_market_code,
    )
except ImportError:
    def strategy_name(s): return str(s) if s else "综合选股"
    def is_triple_volume(s): return "三倍量" in (str(s) or "")
    def compute_stop_loss(e, s): return round(e * 0.95, 2)
    def compute_take_profit(e, s): return round(e * 1.08, 2)
    def get_observation_days(s): return 45
    def compute_effective_stop(e, pk, base, ret): return round(base, 2)
    def to_market_code(c): 
        c = str(c).zfill(6)
        if c.startswith(("60", "688")): return f"sh{c}"
        return f"sz{c}"

# 🔧 引入飞书字段适配层（按真实列名 + 字段类型自动对齐，缺列自动跳过）
try:
    from feishu_sync import (
        fetch_tenant_token, fetch_field_map, build_fields, resolve_column,
    )
except ImportError:
    fetch_tenant_token = fetch_field_map = build_fields = resolve_column = None

# 🔬 引入个股多维分析（财报/股息/资金面/舆情 + LLM 点评，含降级）
try:
    from stock_analyzer import (
        enrich_stock, generate_analysis_text, build_risk_hint,
        summarize_for_feishu, split_wechat_message,
    )
except ImportError:
    enrich_stock = generate_analysis_text = build_risk_hint = None
    summarize_for_feishu = split_wechat_message = None

# 🧠 引入持仓分析的经验记忆（自动进化 Skill）：复盘结果沉淀为经验，降低误判
try:
    from holding_analyzer import ExperienceMemory
except ImportError:
    ExperienceMemory = None

# 🧠 引入推送策略 Skill 引擎（真实胜率 + 归因 + 自动升级迭代）
try:
    from push_skill_engine import evolve_skill as _evolve_skill, render_push_summary as _render_skill_summary
except ImportError:
    try:
        from hedge_fund.strategies.push_skill_engine import (
            evolve_skill as _evolve_skill, render_push_summary as _render_skill_summary)
    except ImportError:
        _evolve_skill = _render_skill_summary = None

# 🌍 引入市场状态自适应引擎（晚间复盘展示当前时势 + 策略适配提示）
try:
    from market_regime_engine import detect_regime as _detect_regime, regime_brief as _regime_brief
except ImportError:
    try:
        from hedge_fund.strategies.market_regime_engine import (
            detect_regime as _detect_regime, regime_brief as _regime_brief)
    except ImportError:
        _detect_regime = _regime_brief = None

# 🏛️ 引入多 Agent 议会（看多/看空/大市/风控 交锋，风控终裁中肯结论）
try:
    from agent_council import run_council as _council_run, council_brief
except ImportError:
    try:
        from hedge_fund.strategies.agent_council import (
            run_council as _council_run, council_brief)
    except ImportError:
        _council_run = None
        council_brief = None

# 🏛️ 晚间议会开关（默认开启；关闭则逐只持仓不附议会结论，省 LLM）
EVENING_COUNCIL = os.environ.get("EVENING_COUNCIL", "true").lower() in ("true", "1", "yes")

# 📰 新闻-股价归因开关（布林线+甄别消息+相对大盘），默认开
ATTRIBUTION_ENABLED = os.environ.get("ATTRIBUTION_ENABLED", "true").lower() in ("true", "1", "yes")

# ⚙️ 飞书多维表格 API 配置
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "").strip()
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "").strip()
FEISHU_APP_TOKEN = os.environ.get("FEISHU_APP_TOKEN", "").strip()
FEISHU_TABLE_ID = os.environ.get("FEISHU_TABLE_ID", "").strip()

WECHAT_WEBHOOK = os.environ.get("WECHAT_WEBHOOK", "").strip()

# 🎯 持久化文件路径
TRACKER_FILE = "portfolio_tracker.json"
POSTMORTEM_FILE = "skills_postmortem.md"

# 🛑 终态集合（已结案：止盈/止损/到期/手动平仓）。这些状态一律不再复盘、不再推送，
#    避免"已止损"股票被反复扫描、反复推送浪费信息（用户明确要求：止损后即停止追踪推送）。
TERMINAL_STATUSES = {"WIN", "LOSS", "WIN_CLOSE", "LOSS_CLOSE", "CLOSED"}
# 🛑 止损类终态（用于"止损后停止推送"的精确判定）
STOPLOSS_STATUSES = {"LOSS", "LOSS_CLOSE"}

# 📋 止损整合开关：止损个股统一在【每周五】做一次整合清单推送，
#    周一~周四不再逐日重复罗列止损清单；此后静默，直到该股重新满足选股策略再次入选。
STOPLOSS_FRIDAY_DIGEST = os.environ.get("STOPLOSS_FRIDAY_DIGEST", "true").lower() in ("true", "1", "yes")


def is_terminal(status) -> bool:
    """是否为已结案终态。终态标的不再追踪/复盘/推送。"""
    return str(status or "").strip().upper() in TERMINAL_STATUSES


class EveningReviewAgent:

    def __init__(self):
        self.tracker_file = TRACKER_FILE
        self.postmortem_file = POSTMORTEM_FILE

    def load_tracker(self) -> List[Dict]:
        if os.path.exists(self.tracker_file):
            try:
                with open(self.tracker_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data if isinstance(data, list) else data.get("records", [])
            except Exception as e:
                print(f"⚠️ 读取持仓跟踪池失败: {e}")
        return []

    def save_tracker(self, tracker_data: List[Dict]):
        try:
            with open(self.tracker_file, "w", encoding="utf-8") as f:
                json.dump(tracker_data, f, ensure_ascii=False, indent=2)
            print("💾 【持仓跟踪池】数据已更新保存！")
        except Exception as e:
            print(f"❌ 保存持仓跟踪池失败: {e}")

    # ==========================================
    # 🔍 1. 从飞书同步全量持仓（修复分页与记录覆盖问题）
    # ==========================================
    def sync_from_feishu_and_get_records(self) -> Tuple[str, Dict[str, str], List[Dict]]:
        if not (FEISHU_APP_ID and FEISHU_APP_SECRET and FEISHU_APP_TOKEN and FEISHU_TABLE_ID):
            print("⚠️ 飞书环境变量不完整，跳过飞书同步。")
            return "", {}, []

        auth_url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        try:
            res_auth = requests.post(
                auth_url,
                json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
                timeout=10,
            )
            access_token = res_auth.json().get("tenant_access_token", "")
            if not access_token:
                print(f"❌ 获取飞书 Access Token 失败: {res_auth.text}")
                return "", {}, []
        except Exception as e:
            print(f"❌ 飞书鉴权网络请求异常: {e}")
            return "", {}, []

        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {access_token}",
        }

        # 🔧 拉取真实字段结构，用于按真实列名（策略归属 / 建仓价格 …）读取
        self.feishu_field_map = fetch_field_map(FEISHU_APP_TOKEN, FEISHU_TABLE_ID, access_token) if fetch_field_map else {}
        strategy_col = resolve_column("strategy", self.feishu_field_map) or "策略归属"
        entry_col = resolve_column("entry_price", self.feishu_field_map) or "建仓价格"

        record_map = {}
        feishu_active_stocks = []
        list_url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{FEISHU_TABLE_ID}/records"

        has_more = True
        page_token = ""

        try:
            while has_more:
                params = {"page_size": 500}  # 扩大单页容量，防止最新记录被漏掉
                if page_token:
                    params["page_token"] = page_token

                res_list = requests.get(list_url, headers=headers, params=params, timeout=10)
                if res_list.status_code != 200:
                    break

                res_json = res_list.json()
                data_body = res_json.get("data", {})
                items = data_body.get("items", [])
                has_more = data_body.get("has_more", False)
                page_token = data_body.get("page_token", "")

                for r in items:
                    rec_id = r.get("record_id")
                    fields = r.get("fields", {})

                    # 解析股票代码
                    code_val = fields.get("股票代码") or fields.get("代码") or ""
                    if isinstance(code_val, list) and len(code_val) > 0:
                        code_val = code_val[0].get("text", "") if isinstance(code_val[0], dict) else str(code_val[0])
                    elif isinstance(code_val, dict):
                        code_val = code_val.get("text", "")

                    code = str(code_val).strip()
                    if code and code.isdigit():
                        code = code.zfill(6)

                    status = str(fields.get("状态", "持仓中")).strip()
                    name = fields.get("股票名称") or fields.get("名称") or f"标的_{code}"
                    entry_price = float(fields.get(entry_col) or 0.0)

                    strategy = strategy_name(fields.get(strategy_col) or "综合选股")

                    # 🔧 止盈/止损比例统一走 config（原版：三倍量 0.96/1.10，其余 0.95/1.08）
                    default_target = compute_take_profit(entry_price, strategy)
                    default_stop = compute_stop_loss(entry_price, strategy)

                    target_price = float(fields.get("目标价") or fields.get("止盈价") or default_target)
                    stop_loss = float(fields.get("止损价") or default_stop)

                    # 推荐日期解析
                    rec_date_val = fields.get("推荐日期") or fields.get("建仓日期") or fields.get("复盘日期")
                    recommend_date = time.strftime("%Y-%m-%d")
                    if rec_date_val:
                        if isinstance(rec_date_val, (int, float)):
                            recommend_date = datetime.fromtimestamp(rec_date_val / 1000.0).strftime("%Y-%m-%d")
                        elif isinstance(rec_date_val, str) and len(rec_date_val) >= 10:
                            recommend_date = rec_date_val[:10]

                    if code and rec_id:
                        # 🛠️ 修复映射覆盖：如果状态是“持仓中”，强制优先绑定该 record_id，确保最新行被更新
                        if code not in record_map or status in ["持仓中", "TRACKING"]:
                            record_map[code] = rec_id

                        if status in ["持仓中", "TRACKING"]:
                            feishu_active_stocks.append({
                                "code": code,
                                "name": name,
                                "entry_price": entry_price,
                                "target_price": target_price,
                                "stop_loss": stop_loss,
                                "strategy": strategy,
                                "status": "TRACKING",
                                "recommend_date": recommend_date,
                                # 🔧 观察周期按策略写入（统一口径）
                                "observation_window": get_observation_days(strategy),
                            })

            print(f"📊 飞书表格拉取完成：当前活跃跟踪股票数量为 {len(feishu_active_stocks)} 只。")
        except Exception as e:
            print(f"⚠️ 获取飞书多维表格记录列表异常: {e}")

        return access_token, record_map, feishu_active_stocks

    # ==========================================
    # 📈 2. 核心行情复盘与状态更新（含追踪止损）
    # ==========================================
    def run_evening_review(self):
        access_token, record_map, feishu_stocks = self.sync_from_feishu_and_get_records()
        local_tracker = self.load_tracker()

        # 1. 深度合并本地 JSON 与飞书数据
        #    同一 code 可能存在多轮历史记录（反复入选/结案），按「TRACKING 优先 + entry_date 最新」
        #    取唯一代表，避免复盘/推送取到旧记录造成个股信息串扰或重复。
        tracker_map: Dict[str, Dict] = {}
        for item in local_tracker:
            code = str(item["code"]).zfill(6)
            cur = tracker_map.get(code)
            if cur is None:
                tracker_map[code] = item
                continue
            # 新记录更优的判定：TRACKING 状态 > 非TRACKING；同状态则 entry_date 更新者胜出
            cur_track = str(cur.get("status")) == "TRACKING"
            new_track = str(item.get("status")) == "TRACKING"
            if (new_track and not cur_track) or (
                new_track == cur_track and str(item.get("entry_date") or "") > str(cur.get("entry_date") or "")
            ):
                tracker_map[code] = item

        for fs_item in feishu_stocks:
            code = fs_item["code"]
            if code not in tracker_map:
                # 飞书新增但本地没有 → 补全统一字段
                fs_item.setdefault("entry_date", fs_item.get("recommend_date"))
                fs_item.setdefault("days_tracked", 0)
                fs_item.setdefault("peak_price", fs_item["entry_price"])
                fs_item.setdefault("last_price", fs_item["entry_price"])
                fs_item.setdefault("observation_window", get_observation_days(fs_item["strategy"]))
                fs_item.setdefault("last_alert_date", "")
                fs_item.setdefault("pending_signal", "")
                tracker_map[code] = fs_item
            else:
                # 🛑 关键修复：本地已结案（WIN/LOSS/CLOSED/WIN_CLOSE/LOSS_CLOSE）的标的，
                #    一律不再被飞书"持仓中"状态重新激活为 TRACKING。
                #    旧代码只判断 ["WIN","LOSS"]，导致 CLOSED/LOSS_CLOSE 等被反复重置→反复推送止损。
                if not is_terminal(tracker_map[code].get("status")):
                    tracker_map[code]["status"] = "TRACKING"
                    tracker_map[code]["entry_price"] = fs_item["entry_price"]
                    tracker_map[code]["target_price"] = fs_item["target_price"]
                    tracker_map[code]["stop_loss"] = fs_item["stop_loss"]
                    tracker_map[code]["strategy"] = fs_item["strategy"]
                    tracker_map[code]["observation_window"] = fs_item.get(
                        "observation_window", get_observation_days(fs_item["strategy"]))
                    # 🔧 修复：统一使用 entry_date，缺失时回退 recommend_date
                    if "entry_date" not in tracker_map[code]:
                        tracker_map[code]["entry_date"] = fs_item.get("recommend_date", time.strftime("%Y-%m-%d"))
                    if "recommend_date" not in tracker_map[code]:
                        tracker_map[code]["recommend_date"] = tracker_map[code]["entry_date"]

        tracker_data = list(tracker_map.values())

        active_tracking = [item for item in tracker_data if item.get("status") == "TRACKING"]
        if not active_tracking:
            print("💡 当前没有需要更新复盘的活跃股票。")
            self.save_tracker(tracker_data)
            return

        # 3. 批量获取腾讯最新行情
        tc_codes = [to_market_code(i["code"]) for i in active_tracking]
        headers = {"User-Agent": "Mozilla/5.0"}
        price_map = {}

        try:
            res = requests.get(f"http://qt.gtimg.cn/q={','.join(tc_codes)}", headers=headers, timeout=6)
            if res.status_code == 200:
                matches = re.findall(r'v_(sh|sz|bj)(\d{6})="([^"]+)"', res.text)
                for market, code, data_str in matches:
                    fields = data_str.split("~")
                    if len(fields) > 3:
                        trade_price = float(fields[3]) if fields[3] and fields[3] != "-" else 0.0
                        if trade_price > 0:
                            price_map[code] = trade_price
        except Exception as e:
            print(f"❌ 拉取实时收盘价异常: {e}")

        review_summary = []
        today_date = datetime.now().date()
        today_str = time.strftime("%Y-%m-%d")
        today_timestamp = int(time.time() * 1000)

        # 4. 遍历活跃持仓计算持仓天数、盈亏、追踪止损与平仓判定
        for item in active_tracking:
            code = str(item["code"]).zfill(6)
            entry_price = float(item["entry_price"])
            curr_price = price_map.get(code, item.get("last_price", entry_price))

            rec_date_str = item.get("entry_date") or item.get("recommend_date", today_str)
            try:
                rec_dt = datetime.strptime(rec_date_str[:10], "%Y-%m-%d").date()
                days = (today_date - rec_dt).days + 1
                if days < 1:
                    days = 1
            except Exception:
                days = item.get("days_tracked", 1)

            item["days_tracked"] = days
            item["last_price"] = curr_price
            item["peak_price"] = max(item.get("peak_price", entry_price), curr_price)

            ret_pct = ((curr_price - entry_price) / entry_price) * 100 if entry_price > 0 else 0.0
            ret_str = f"{ret_pct:+.2f}%"

            strategy = item.get("strategy", "综合选股")
            status = "持仓中"
            strength = "🔥 强势" if ret_pct >= 0 else "⚠️ 弱势"

            target_price = float(item.get("target_price") or compute_take_profit(entry_price, strategy))
            base_stop = float(item.get("stop_loss") or compute_stop_loss(entry_price, strategy))
            observation_window = int(item.get("observation_window") or get_observation_days(strategy))

            # 🔧 追踪止损：保本线 + 跟随峰值回撤
            effective_stop = compute_effective_stop(entry_price, item["peak_price"], base_stop, ret_pct)

            just_closed_today = False
            sell_reason = ""

            if curr_price >= target_price and entry_price > 0:
                # 🚀 止盈触发 → 抛售
                item["status"] = "WIN"
                item["close_date"] = today_str
                sell_reason = f"达标止盈 [{strategy}]: 突破目标价 {target_price}元，获利离场"
                status = "已止盈"
                strength = "🚀 强达标"
                just_closed_today = True
            elif curr_price <= effective_stop and entry_price > 0:
                # 🛑 止损触发（含追踪止损保护）→ 抛售
                item["status"] = "LOSS"
                item["close_date"] = today_str
                if strategy == "三倍量战法":
                    sell_reason = f"触及止损 [三倍量战法]: 跌破放量风控价 {effective_stop}元（基础止损 {base_stop}元），防守离场"
                else:
                    sell_reason = f"触及止损 [{strategy}]: 跌破有效止损价 {effective_stop}元（基础止损 {base_stop}元），防守离场"
                status = "已止损"
                strength = "❌ 弱触损"
                just_closed_today = True
            elif days >= observation_window:
                # ⏳ 观察期满 → 到期清算
                item["status"] = "WIN" if ret_pct > 0 else "LOSS"
                item["close_date"] = today_str
                sell_reason = f"到期清算: 达到 {observation_window} 日观察上限"
                status = "已平仓"
                just_closed_today = True

            item["reason"] = sell_reason or "持仓观察中"
            item["pending_signal"] = ""  # 收盘已处理，清空盘中待抛信号

            review_summary.append({
                "code": code,
                "name": item["name"],
                "strategy": strategy,
                "entry_price": entry_price,
                "curr_price": curr_price,
                "ret_pct": ret_pct,
                "ret_str": ret_str,
                "days": days,
                "status": status,
                "strength": strength,
                "reason": item["reason"],
                "target_price": target_price,
                "effective_stop": effective_stop,
                "just_closed_today": just_closed_today,
                "is_active": item["status"] == "TRACKING"
            })

            # 2️⃣ 精准对应写入飞书最新行
            if access_token and code in record_map:
                record_id = record_map[code]
                update_url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{FEISHU_TABLE_ID}/records/{record_id}"
                headers_fs = {
                    "Content-Type": "application/json; charset=utf-8",
                    "Authorization": f"Bearer {access_token}",
                }
                # 🔧 用适配层按真实列名写入（自动跳过表格不存在的列）
                fm = getattr(self, "feishu_field_map", None) or {}
                if not fm and fetch_field_map and access_token:
                    fm = self.feishu_field_map = fetch_field_map(FEISHU_APP_TOKEN, FEISHU_TABLE_ID, access_token)
                raw_fields = {
                    "review_date": today_timestamp,
                    "close_price": curr_price,
                    "profit_pct": ret_str,
                    "hold_days": days,
                    "status": status,
                    "strategy": strategy,
                    "eff_stop": effective_stop,
                    "attribution": f"[{strength}] {item['reason']}",
                }
                payload_fs = {"fields": build_fields(raw_fields, fm)}
                try:
                    res_patch = requests.patch(update_url, headers=headers_fs, json=payload_fs, timeout=5)
                    if res_patch.status_code == 200:
                        print(f"✅ 飞书更新成功 [{code} {item['name']}]: 收盘价 {curr_price}元, 状态 [{status}]")
                    else:
                        print(f"❌ 飞书更新失败 [{code}]: {res_patch.text}")
                except Exception as e:
                    print(f"❌ 更新飞书 Record ({code}) 网络异常: {e}")

        self.save_tracker(tracker_data)
        self.update_skills_postmortem(tracker_data)
        # 🧠 自动升级推送策略 Skill（真实胜率 + 盈亏归因 + 参数迭代），失败不影响推送
        self._evolve_push_skill(tracker_data)
        self.push_wechat_summary(review_summary, tracker_data)

    # ==========================================
    # 🚨 3. 盘中实时监控：止损/止盈 抛售提醒（不收盘，仅提醒）
    # ==========================================
    def run_intraday_alert(self):
        """
        盘中运行：监控仍处于 TRACKING 的持仓，一旦触碰止损/止盈线，
        立即推送 🚨 抛售提醒。每日每只限提醒一次（按 last_alert_date 节流）。
        建议通过定时任务在交易时段（如 09:35 / 10:30 / 13:30 / 14:30）调用。
        """
        access_token, record_map, feishu_stocks = self.sync_from_feishu_and_get_records()
        tracker = self.load_tracker()
        active = [i for i in tracker if i.get("status") == "TRACKING"]
        if not active:
            print("💡 当前没有需要盘中监控的活跃持仓。")
            return

        tc_codes = [to_market_code(i["code"]) for i in active]
        price_map = {}
        try:
            res = requests.get(f"http://qt.gtimg.cn/q={','.join(tc_codes)}", headers={"User-Agent": "Mozilla/5.0"}, timeout=6)
            if res.status_code == 200:
                for market, code, data_str in re.findall(r'v_(sh|sz|bj)(\d{6})="([^"]+)"', res.text):
                    fields = data_str.split("~")
                    if len(fields) > 3:
                        p = float(fields[3]) if fields[3] and fields[3] != "-" else 0.0
                        if p > 0:
                            price_map[code] = p
        except Exception as e:
            print(f"❌ 盘中行情拉取异常: {e}")
            return

        today_str = time.strftime("%Y-%m-%d")
        alerts = []

        for item in active:
            code = str(item["code"]).zfill(6)
            entry_price = float(item["entry_price"])
            curr_price = price_map.get(code, item.get("last_price", entry_price))
            strategy = item.get("strategy", "综合选股")
            target_price = float(item.get("target_price") or compute_take_profit(entry_price, strategy))
            base_stop = float(item.get("stop_loss") or compute_stop_loss(entry_price, strategy))
            item["peak_price"] = max(item.get("peak_price", entry_price), curr_price)
            ret_pct = ((curr_price - entry_price) / entry_price) * 100 if entry_price > 0 else 0.0
            effective_stop = compute_effective_stop(entry_price, item["peak_price"], base_stop, ret_pct)

            signal = ""
            if curr_price >= target_price and entry_price > 0:
                signal = "SELL_TAKE_PROFIT"
            elif curr_price <= effective_stop and entry_price > 0:
                signal = "SELL_STOP_LOSS"

            # 节流：同一天已提醒过相同信号则不再重复推送
            if signal and (item.get("last_alert_date") != today_str or item.get("pending_signal") != signal):
                item["last_alert_date"] = today_str
                item["pending_signal"] = signal
                item["last_price"] = curr_price
                direction = "止盈线" if signal == "SELL_TAKE_PROFIT" else "止损线"
                alerts.append({
                    "code": code,
                    "name": item["name"],
                    "strategy": strategy,
                    "curr_price": curr_price,
                    "entry_price": entry_price,
                    "target_price": target_price,
                    "effective_stop": effective_stop,
                    "ret_str": f"{ret_pct:+.2f}%",
                    "direction": direction,
                    "signal": signal,
                })
            elif not signal:
                # 已回到安全区间，清除待抛信号，避免误报
                item["pending_signal"] = ""

        self.save_tracker(tracker)

        if alerts:
            self.push_sell_alert(alerts)
        else:
            print("✅ 盘中监控：所有持仓均在安全区间内，无需抛售提醒。")

    def _send_markdown_parts(self, content: str) -> bool:
        """企业微信分段发送：单条超长自动按 1800 字拆分，避免风控封锁。"""
        url = WECHAT_WEBHOOK
        if not url:
            return False
        parts = split_wechat_message(content, 1800) if split_wechat_message else [content]
        ok = True
        for p in parts:
            payload = {"msgtype": "markdown", "markdown": {"content": p.replace("```", "")}}
            try:
                res = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=10)
                if res.json().get("errcode") != 0:
                    ok = False
            except Exception:
                ok = False
            time.sleep(1)
        return ok

    def push_sell_alert(self, alerts: List[Dict]):
        """专门推送 🚨 抛售提醒（企业微信）。"""
        if not WECHAT_WEBHOOK:
            print("⚠️ 未配置 WECHAT_WEBHOOK，跳过抛售提醒推送。")
            return

        today_str = time.strftime("%Y-%m-%d")
        lines = [
            f"🚨 **【盘中抛售提醒】** ({today_str})",
            f"-----------------------------------",
            f"以下标的已触碰止损/止盈线，请考虑择机卖出：",
        ]
        for a in alerts:
            icon = "🟢 止盈" if a["signal"] == "SELL_TAKE_PROFIT" else "🔴 止损"
            lines.append(
                f"{icon} **{a['name']}** (`{a['code']}`) | 策略: {a['strategy']}\n"
                f"• 现价 `{a['curr_price']:.2f}元` | 建仓 `{a['entry_price']:.2f}元` | 收益 `{a['ret_str']}`\n"
                f"• 触发 `{a['direction']}`（止盈 `{a['target_price']:.2f}` / 有效止损 `{a['effective_stop']:.2f}`）\n"
                f"• 👉 **建议操作：卖出 / 减仓**"
            )
        lines.append("-----------------------------------")
        lines.append("💡 风险提示：抛售提醒仅供参考，请结合自身风险偏好与盘面决策。")

        self._send_markdown_parts("\n".join(lines))
        print(f"🚨 盘中抛售提醒已推送（共 {len(alerts)} 只）！")

    def _evolve_push_skill(self, tracker_data: List[Dict]):
        """自动升级推送策略 Skill：真实胜率→盈亏归因→参数迭代→版本化文档。"""
        if _evolve_skill is None:
            return
        try:
            _evolve_skill(tracker_data)
        except Exception as e:
            print(f"⚠️ 推送 Skill 自动升级失败（不影响主流程）: {e}")

    def update_skills_postmortem(self, tracker_data: List[Dict]):
        completed = [i for i in tracker_data if i.get("status") in ["WIN", "LOSS"]]
        total = len(completed)
        win_count = sum(1 for i in completed if i.get("status") == "WIN")
        win_rate = (win_count / total * 100) if total > 0 else 0.0

        # ── 自动进化 Skill：把今日结案的持仓沉淀为案例，并按胜率沉淀教训 ──
        if ExperienceMemory is not None:
            try:
                mem = ExperienceMemory()
                today_str = time.strftime("%Y-%m-%d")
                # 仅记录今日新结案的标的，避免重复写入
                for it in completed:
                    if str(it.get("close_date", "")) == today_str:
                        decision = "持有" if it.get("status") == "WIN" else "止损/离场"
                        outcome = "盈利" if it.get("status") == "WIN" else "亏损"
                        mem.add_case(
                            code=str(it.get("code", "")).zfill(6),
                            decision=decision,
                            outcome=outcome,
                            note=str(it.get("reason", ""))[:80],
                        )
                # 按策略统计胜率，自动沉淀低胜率教训
                by_strategy: Dict[str, Dict[str, int]] = {}
                for it in completed:
                    s = it.get("strategy", "综合选股")
                    d = by_strategy.setdefault(s, {"total": 0, "win": 0})
                    d["total"] += 1
                    if it.get("status") == "WIN":
                        d["win"] += 1
                mem.auto_evolve(by_strategy)
            except Exception as e:
                print(f"⚠️ 经验记忆更新失败（不影响主流程）: {e}")

        content = (
            f"# 🤖 Agent 2 晚间复盘 Skill 策略迭代日志\n\n"
            f"- **更新时间**: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"- **历史已结案样本数**: `{total}`\n"
            f"- **历史总胜率**: `{win_rate:.2f}%`\n"
        )
        try:
            with open(self.postmortem_file, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            print(f"❌ 写入 Skill 日志失败: {e}")

    # ==========================================
    # 📋 本周止损整合清单（周五一次性推送，避免逐日重复刷屏）
    # ==========================================
    def _weekly_stoploss_digest(self, all_tracker_data: List[Dict], today: "datetime | None" = None) -> List[str]:
        """汇总【本周一~今天】触发的止损个股，仅在周五推送一次。

        设计依据（用户需求）：
          1) 止损个股统一在当周周五做一次整合推送，不再逐日重复罗列；
          2) 下周一起对这些个股静默，不再重复推送止损；
          3) 只有当该股重新满足选股策略、被早盘重新注册为 TRACKING 时，才恢复提示。
        """
        if not STOPLOSS_FRIDAY_DIGEST or (today or datetime.now()).weekday() != 4:
            return []
        today = today or datetime.now()
        monday = (today - timedelta(days=today.weekday())).strftime("%Y-%m-%d")
        today_s = today.strftime("%Y-%m-%d")

        seen, rows = set(), []
        for x in all_tracker_data or []:
            st = str(x.get("status", "")).upper()
            if st not in STOPLOSS_STATUSES:
                continue
            cd = str(x.get("close_date") or "")[:10]
            if not cd or cd < monday or cd > today_s:
                continue
            code = str(x.get("code", "")).zfill(6)
            if code in seen:
                continue
            seen.add(code)
            try:
                entry = float(x.get("entry_price") or 0)
                last = float(x.get("last_price") or 0)
            except (TypeError, ValueError):
                entry = last = 0.0
            ret = ((last - entry) / entry * 100) if entry > 0 else 0.0
            rows.append({
                "code": code, "name": x.get("name", ""),
                "strategy": x.get("strategy", "综合选股"),
                "entry": entry, "last": last, "ret": ret, "date": cd,
            })

        if not rows:
            return []

        rows.sort(key=lambda r: r["ret"])  # 亏损最大的排前面，便于复盘
        out = [
            f"📋 **【本周止损整合清单】** ({monday} ~ {today_s}，共 **{len(rows)}** 只)",
            "-----------------------------------",
        ]
        for i, r in enumerate(rows, 1):
            out.append(
                f"{i}. **{r['name']}** (`{r['code']}`) | {r['strategy']} | "
                f"成本 `{r['entry']:.2f}` → `{r['last']:.2f}` (`{r['ret']:+.2f}%`) | 触发 {r['date']}"
            )
        out.append("")
        out.append("💡 *以上个股已归档静默：下周一起不再重复推送止损；若重新满足选股策略入选，将重新提示。*")
        return out

    def push_wechat_summary(self, summary_list: List[Dict], all_tracker_data: List[Dict]):
        if not WECHAT_WEBHOOK or not summary_list:
            return

        today_str = time.strftime("%Y-%m-%d")
        chunk_size = 3
        total_chunks = (len(summary_list) + chunk_size - 1) // chunk_size

        # 🔬 晚间复盘给持仓股补多维分析（财报/盘面/资金面/消息面，带降级；默认开启）
        do_analysis = os.environ.get("EVENING_ANALYSIS", "true").lower() in ["true", "1", "yes"]
        if do_analysis and enrich_stock and generate_analysis_text:
            print("🔬 晚间复盘补充持仓股多维分析...")
            for s in summary_list:
                try:
                    snap = enrich_stock(str(s["code"]).zfill(6), s["name"], float(s["curr_price"]))
                    s["analysis"] = generate_analysis_text(s["code"], s["name"], float(s["curr_price"]), snap, s["strategy"], max_chars=220)
                    s["news_attrib"] = (snap.get("attribution") or "").strip() if ATTRIBUTION_ENABLED else ""
                except Exception:
                    s["analysis"] = ""
                    s["news_attrib"] = ""

        still_active = [s for s in summary_list if s["is_active"]]
        strong_stocks = [s for s in still_active if s["ret_pct"] >= 0]
        weak_stocks = [s for s in still_active if s["ret_pct"] < 0]

        avg_ret = (sum(s["ret_pct"] for s in still_active) / len(still_active)) if still_active else 0.0

        # 🧠 真实胜率/归因统一由 Skill 引擎计算（修复旧逻辑只数 WIN 状态、胜率虚低到 7.5% 的 bug）
        if _render_skill_summary is not None:
            try:
                skill_summary = _render_skill_summary(all_tracker_data, with_attribution=True)
            except Exception:
                skill_summary = ""
        else:
            skill_summary = ""

        # 🚨 今日触发离场的标的（需抛售清单）
        closed_today = [s for s in summary_list if s["just_closed_today"]]

        for page, i in enumerate(range(0, len(summary_list), chunk_size), 1):
            chunk = summary_list[i:i + chunk_size]
            # 📌 段首显式列出本段个股，避免多段推送内容相似、分不清是哪只
            seg_names = " ｜ ".join(f"{x['name']}(`{x['code']}`)" for x in chunk)
            lines = [
                f"🌙 **【晚间复盘总览】** ({today_str}) [{page}/{total_chunks}]",
                f"📌 本段个股：{seg_names}",
                f"-----------------------------------",
            ]

            for si, s in enumerate(chunk):
                icon = "🔴" if s["ret_pct"] > 0 else ("🟢" if s["ret_pct"] < 0 else "⚪")
                notice = " 🚨 **[今日触发离场，建议抛售]**" if s["just_closed_today"] else ""
                seq = f"【{i + si + 1}/{len(summary_list)}】"  # 全局序号，避免拆段后个股混淆

                line = (
                    f"{seq}{icon} **{s['name']}** (`{s['code']}`){notice}\n"
                    f"• 策略: **{s['strategy']}** | 评级: **{s['strength']}**\n"
                    f"• 最新价: `{s['curr_price']:.2f}元` | 收益: `{s['ret_str']}`\n"
                    f"• 状态: **{s['status']}** | 持仓: `{s['days']}天`\n"
                    f"• 止盈 `{s['target_price']:.2f}` / 有效止损 `{s['effective_stop']:.2f}`\n"
                )
                if s.get("analysis"):
                    line += f"• 📊 多维分析: {s['analysis']}\n"
                else:
                    line += f"• 归因: {s['reason']}\n"
                # 📰 新闻-股价归因：消息驱动 or 大市波动（布林线+甄别消息+相对大盘超额）
                if s.get("news_attrib"):
                    _at = s["news_attrib"]
                    if len(_at) > 220:
                        _at = _at[:220].rstrip() + "…"
                    line += f"• {_at}\n"
                # 🏛️ 多 Agent 议会中肯结论（看多/看空/大市/风控 交锋 + 风控终裁，含多空双方辩论）
                if EVENING_COUNCIL and _council_run and council_brief:
                    try:
                        _cp = float(s.get("curr_price") or 0)
                        _ep = float(s.get("entry_price") or 0)
                        _ret = float(s.get("ret_pct") or 0)
                        _holding = {"cost_price": _ep, "pnl_pct": _ret} if _ep > 0 else None
                        _rep = _council_run(str(s["code"]).zfill(6), s["name"], _cp,
                                           s.get("strategy", "综合选股"), _holding)
                        line += council_brief(_rep) + "\n"
                    except Exception as e:
                        print(f"⚠️ 议会分析 {s.get('code')} 失败，跳过: {e}")
                lines.append(line)

            lines.append("-----------------------------------")

            if page == total_chunks:
                # 🛑 止损清单不再逐日重复罗列（旧逻辑每日列几十只，重复刷屏）。
                #    改为：周五统一推「本周止损整合清单」，周一~周四静默；
                #    止损当天仅在本段正文以「🚨 今日触发离场」标注一次。
                weekly_digest = self._weekly_stoploss_digest(all_tracker_data)
                if weekly_digest:
                    lines.extend(weekly_digest)
                    lines.append("")
                elif closed_today:
                    lines.append(
                        f"🚨 **今日新增离场 {len(closed_today)} 只**（已在正文标注）："
                        + "、".join(f"{s['name']}(`{s['code']}`)" for s in closed_today)
                    )
                    lines.append("💡 *本周止损将于周五统一整合推送，期间不再逐日重复。*")
                    lines.append("")

                # 📊 持仓强弱：仅列 Top5 + 计数，避免与正文重复罗列几十只股票名
                def _top(lst, n=5):
                    lst = sorted(lst, key=lambda x: -abs(float(x.get("ret_pct") or 0)))[:n]
                    return "、".join(f"{x['name']}(`{x['code']}`)" for x in lst) or "无"

                lines.append("📊 **【持仓强弱筛选】**\n")
                lines.append(f"• 📈 **当前活跃持仓平均收益率**: `{avg_ret:+.2f}%`")
                lines.append(f"• 💪 **强势 {len(strong_stocks)} 只**（前5）: {_top(strong_stocks)}")
                lines.append(f"• ⚠️ **弱势 {len(weak_stocks)} 只**（前5）: {_top(weak_stocks)}\n")

                # 🧠 真实胜率 + 数据驱动归因（来自自动升级的 Skill 引擎）
                if skill_summary:
                    lines.append(skill_summary)
                else:
                    lines.append("🎯 历史胜率统计暂不可用（Skill 引擎未加载）")

                # 🌍 当前时势 + 策略自适应提示（与早盘共用同一判定缓存）
                if _regime_brief:
                    try:
                        lines.append("")
                        lines.append(_regime_brief(_detect_regime(), with_recommend=True))
                    except Exception:
                        pass

                lines.append("\n💡 *数据已精准同步回填至飞书多维表格*")

            full_msg = "\n".join(lines)
            if self._send_markdown_parts(full_msg):
                print(f"🎉 晚间复盘报告 [{page}/{total_chunks}] 已成功推送！")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="晚间复盘 / 盘中抛售监控 Agent")
    parser.add_argument("--mode", choices=["evening", "intraday"], default="evening",
                        help="evening=晚间复盘收盘判定（默认）；intraday=盘中止损止盈抛售提醒")
    args = parser.parse_args()

    agent = EveningReviewAgent()
    if args.mode == "intraday":
        print("==================================================")
        print("🚨 Agent 2 [盘中抛售监控] 启动，监控止损/止盈线...")
        print("==================================================")
        agent.run_intraday_alert()
    else:
        print("==================================================")
        print("🌙 Agent 2 [晚间复盘 Agent] 启动，开始数据结算与推送...")
        print("==================================================")
        agent.run_evening_review()
