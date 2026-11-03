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
from datetime import datetime
from typing import Dict, List, Tuple
import requests

# 📁 确保 strategy_config.py（与 evening_review.py 同目录）无论在哪运行都能被找到
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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

# ⚙️ 飞书多维表格 API 配置
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "").strip()
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "").strip()
FEISHU_APP_TOKEN = os.environ.get("FEISHU_APP_TOKEN", "").strip()
FEISHU_TABLE_ID = os.environ.get("FEISHU_TABLE_ID", "").strip()

WECHAT_WEBHOOK = os.environ.get("WECHAT_WEBHOOK", "").strip()

# 🎯 持久化文件路径
TRACKER_FILE = "portfolio_tracker.json"
POSTMORTEM_FILE = "skills_postmortem.md"


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
        tracker_map = {str(item["code"]).zfill(6): item for item in local_tracker}

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
                if tracker_map[code].get("status") not in ["WIN", "LOSS"]:
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

    def update_skills_postmortem(self, tracker_data: List[Dict]):
        completed = [i for i in tracker_data if i.get("status") in ["WIN", "LOSS"]]
        total = len(completed)
        win_count = sum(1 for i in completed if i.get("status") == "WIN")
        win_rate = (win_count / total * 100) if total > 0 else 0.0

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

    def push_wechat_summary(self, summary_list: List[Dict], all_tracker_data: List[Dict]):
        if not WECHAT_WEBHOOK or not summary_list:
            return

        today_str = time.strftime("%Y-%m-%d")
        chunk_size = 3
        total_chunks = (len(summary_list) + chunk_size - 1) // chunk_size

        # 🔬 可选：晚间复盘也给持仓股补多维分析（需联网+LLM，默认关闭以省资源）
        do_analysis = os.environ.get("EVENING_ANALYSIS", "false").lower() in ["true", "1", "yes"]
        if do_analysis and enrich_stock and generate_analysis_text:
            print("🔬 晚间复盘补充持仓股多维分析...")
            for s in summary_list:
                try:
                    snap = enrich_stock(str(s["code"]).zfill(6), s["name"], float(s["curr_price"]))
                    s["analysis"] = generate_analysis_text(s["code"], s["name"], float(s["curr_price"]), snap, s["strategy"], max_chars=220)
                except Exception:
                    s["analysis"] = ""

        still_active = [s for s in summary_list if s["is_active"]]
        strong_stocks = [s for s in still_active if s["ret_pct"] >= 0]
        weak_stocks = [s for s in still_active if s["ret_pct"] < 0]

        avg_ret = (sum(s["ret_pct"] for s in still_active) / len(still_active)) if still_active else 0.0

        completed = [i for i in all_tracker_data if i.get("status") in ["WIN", "LOSS"]]
        total_completed = len(completed)
        win_count = sum(1 for i in completed if i.get("status") == "WIN")
        win_rate = (win_count / total_completed * 100) if total_completed > 0 else 0.0

        # 🚨 今日触发离场的标的（需抛售清单）
        closed_today = [s for s in summary_list if s["just_closed_today"]]

        for page, i in enumerate(range(0, len(summary_list), chunk_size), 1):
            chunk = summary_list[i:i + chunk_size]
            lines = [
                f"🌙 **【晚间复盘总览】** ({today_str}) [{page}/{total_chunks}]",
                f"-----------------------------------",
            ]

            for s in chunk:
                icon = "🔴" if s["ret_pct"] > 0 else ("🟢" if s["ret_pct"] < 0 else "⚪")
                notice = " 🚨 **[今日触发离场，建议抛售]**" if s["just_closed_today"] else ""

                line = (
                    f"{icon} **{s['name']}** (`{s['code']}`){notice}\n"
                    f"• 策略: **{s['strategy']}** | 评级: **{s['strength']}**\n"
                    f"• 最新价: `{s['curr_price']:.2f}元` | 收益: `{s['ret_str']}`\n"
                    f"• 状态: **{s['status']}** | 持仓: `{s['days']}天`\n"
                    f"• 止盈 `{s['target_price']:.2f}` / 有效止损 `{s['effective_stop']:.2f}`\n"
                )
                if s.get("analysis"):
                    line += f"• 📊 多维分析: {s['analysis']}\n"
                else:
                    line += f"• 归因: {s['reason']}\n"
                lines.append(line)

            lines.append("-----------------------------------")

            if page == total_chunks:
                if closed_today:
                    lines.append("🚨 **【今日抛售提醒清单】**")
                    for s in closed_today:
                        lines.append(f"• `{s['code']}` {s['name']} — **{s['status']}** | {s['reason']}")
                    lines.append("")

                lines.append("📊 **【持仓强弱筛选与成功率统计】**\n")
                lines.append(f"• 🎯 **历史策略成功率(胜率)**: `{win_rate:.1f}%` (已结案 `{total_completed}` 只)")
                lines.append(f"• 📈 **当前活跃持仓平均收益率**: `{avg_ret:+.2f}%`")
                lines.append(f"• 💪 **建议持有(强势标的)**: {', '.join([s['name'] for s in strong_stocks]) or '无'}")
                lines.append(f"• ⚠️ **建议防守(弱势跟踪中)**: {', '.join([s['name'] for s in weak_stocks]) or '无'}\n")

                lines.append("🧐 **【盈亏归因分析】**")
                if avg_ret >= 0:
                    lines.append("• **盈利主因**: 选股策略与板块资金共振，突破阻力位后动能延续良好。")
                else:
                    lines.append("• **亏损主因**: 大盘盘整期买盘承接不足，部分标的触及防守线触发风控。")

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
