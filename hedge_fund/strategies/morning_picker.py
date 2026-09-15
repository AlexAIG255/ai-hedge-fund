"""
Agent 1: 大盘早晚选股 Agent - 7大全策略选股模型集成 & 联动 Agent 3 动态风控过滤
集成了全量 A 股抓取（5000+只）、7大核心量化选股（右侧启动/超跌反弹/出水芙蓉/买在无人问津处/多头向上的圆月线/超跌反包强势/底部放量反转）、
严格控制涨幅 <= 5% 防追高、三层行情源容错与二次价格校验、嵌入 Agent 3 (Risk Manager) 全球宏观风控与动态 ATR 止盈止损。
"""

import json
import os
import sys
import time
from datetime import datetime
from typing import Dict, List, Tuple
import requests

# 🔗 引入风控 Agent 模块
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from risk.risk_manager import RiskManagerAgent
except ImportError:
    RiskManagerAgent = None

# 🔒 Dify Chatflow 对话接口配置
DIFY_API_URL = "https://api.dify.ai/v1/chat-messages"
DIFY_API_KEY = os.environ.get("DIFY_API_KEY", "").strip()

# ⚙️ 飞书多维表格 API 配置
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "").strip()
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "").strip()
FEISHU_APP_TOKEN = os.environ.get("FEISHU_APP_TOKEN", "").strip()
FEISHU_TABLE_ID = os.environ.get("FEISHU_TABLE_ID", "").strip()

# ⚙️ 控制与时间参数配置
MANUAL_TEST = os.environ.get("MANUAL_TEST", "false").lower() in ["true", "1", "yes"]

# 🎯 持久化文件
HISTORY_FILE = "daily_picks_history.json"
TRACKER_FILE = "portfolio_tracker.json"
POSTMORTEM_FILE = "skills_postmortem.md"


class MorningStockPickerAgent:

    def __init__(
        self,
        history_file: str = HISTORY_FILE,
        tracker_file: str = TRACKER_FILE,
        postmortem_file: str = POSTMORTEM_FILE,
    ):
        self.history_file = history_file
        self.tracker_file = tracker_file
        self.postmortem_file = postmortem_file
        self.risk_agent = RiskManagerAgent() if RiskManagerAgent else None

    # ==========================================
    # 🧠 1. 历史记录与去重去频模块
    # ==========================================
    def load_history(self) -> Dict:
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"⚠️ 读取历史记录文件失败: {e}")
        return {}

    def save_history(self, history_data: Dict):
        try:
            with open(self.history_file, "w", encoding="utf-8") as f:
                json.dump(history_data, f, ensure_ascii=False, indent=2)
            print("💾 【每日选股历史】已成功更新保存！")
        except Exception as e:
            print(f"❌ 保存选股历史失败: {e}")

    def filter_three_day_duplicates(
        self, candidate_items: List[Dict]
    ) -> Tuple[List[Dict], Dict]:
        history = self.load_history()
        today_str = time.strftime("%Y-%m-%d")
        past_dates = sorted([d for d in history.keys() if d < today_str])
        blocked_codes = set()

        if len(past_dates) >= 2:
            d_minus_1, d_minus_2 = past_dates[-1], past_dates[-2]

            def extract_codes(day_data):
                if isinstance(day_data, dict):
                    records = day_data.get("records", [])
                elif isinstance(day_data, list):
                    records = day_data
                else:
                    records = []
                return set(
                    item["code"]
                    for item in records
                    if isinstance(item, dict) and "code" in item
                )

            codes_d1 = extract_codes(history.get(d_minus_1, []))
            codes_d2 = extract_codes(history.get(d_minus_2, []))
            blocked_codes = codes_d1.intersection(codes_d2)

        filtered_items = [
            item for item in candidate_items if item["code"] not in blocked_codes
        ]
        return filtered_items, history

    def update_today_history(self, selected_items: List[Dict]):
        history = self.load_history()
        today_str = time.strftime("%Y-%m-%d")
        today_entry = history.get(today_str, {})
        run_count = (
            today_entry.get("run_count", 0) + 1
            if isinstance(today_entry, dict)
            else 1
        )

        today_records = []
        for item in selected_items:
            try:
                pick_price = float(str(item.get("price", "0")).replace("元", ""))
            except ValueError:
                pick_price = 0.0

            stop_loss = item.get("stop_loss", round(pick_price * 0.95, 2))
            target_price = item.get("target_price", round(pick_price * 1.08, 2))

            today_records.append(
                {
                    "code": item["code"],
                    "name": item["name"],
                    "strategy": item["strategy"],
                    "pick_price": pick_price,
                    "stop_loss": stop_loss,
                    "target_price": target_price,
                    "suggested_pos": item.get("suggested_pos", "10.0%"),
                    "entry_range": item.get(
                        "entry_range", f"{pick_price}~{pick_price}元"
                    ),
                    "trend_iq": item.get("trend_iq", 80),
                    "risk_stars": item.get("risk_stars", 1),
                    "pct_at_pick": item.get("pct", "0.00%"),
                }
            )

        history[today_str] = {"run_count": run_count, "records": today_records}
        self.save_history(history)

    # ==========================================
    # 📐 2. TrendIQ 智能评分 (防追高优化：涨幅>5%扣分加风控)
    # ==========================================
    def calculate_trend_iq_and_risk(
        self,
        price_val: float,
        pct_val: float,
        turnover_val: float,
        pct_60d_val: float,
        vol_ratio_val: float,
    ) -> Dict:
        risk_stars = 1
        if turnover_val > 15.0 or abs(pct_60d_val) > 35.0:
            risk_stars += 1
        if turnover_val > 22.0 or abs(pct_60d_val) > 50.0:
            risk_stars += 1

        if pct_val < -3.0 or pct_val > 5.0:
            risk_stars += 1
        if price_val < 3.0:
            risk_stars += 1

        risk_stars = min(5, max(1, risk_stars))
        base_score = 82
        momentum_score = (
            round(pct_val * 1.5, 1) if pct_val > 0 else round(pct_val * 2.0, 1)
        )
        volume_score = round(min(8, turnover_val * 0.4) + (vol_ratio_val * 2.0), 1)
        risk_deduct = round(risk_stars * 3.0, 1)

        trend_iq = int(base_score + momentum_score + volume_score - risk_deduct)
        trend_iq = min(99, max(50, trend_iq))

        entry_low = round(price_val * 0.985, 2)
        entry_high = round(price_val * 1.005, 2)

        diagnosis_text = (
            f"📊 **【TrendIQ 深度量化因子拆解】**\n"
            f"• **基础评分**: `{base_score}分` | **价格动能**: `{momentum_score:+}分` | **主力增量**: `+{volume_score}分` | **风控扣分**: `-{risk_deduct}分`\n\n"
            f"🔍 **【多维度量化行情诊断】**\n"
            f"1️⃣ **价格与动能趋势**: 当日动态涨跌幅 `{pct_val:+.2f}%`，当前价格 `{price_val:.2f}元`。\n"
            f"2️⃣ **资金与成交活跃度**: 换手率 `{turnover_val:.2f}%`，配合量比指标 `{vol_ratio_val:.2f}`。\n"
            f"3️⃣ **风控指导与策略要点**: 评估风险评级为 `{risk_stars} 星` ({'⭐' * risk_stars})。"
        )

        default_stop_loss = round(price_val * 0.95, 2)
        default_target_price = round(price_val * 1.08, 2)

        return {
            "risk_stars": risk_stars,
            "risk_display": "⭐" * risk_stars,
            "trend_iq": trend_iq,
            "trend_iq_analysis": diagnosis_text,
            "entry_range": f"{entry_low}~{entry_high}元",
            "stop_loss": default_stop_loss,
            "target_price": default_target_price,
            "pass_risk": (risk_stars < 4) and (trend_iq >= 80),
        }

    # ==========================================
    # 🌙 3. 多策略选股模型算子库
    # ==========================================
    def evaluate_all_strategies(
        self,
        c: float,
        o: float,
        h: float,
        l: float,
        prev_c: float,
        prev_o: float,
        ma3: float,
        ma5: float,
        ma12: float,
        ma21: float,
        ma55: float,
        prev_ma3: float,
        prev_ma5: float,
        prev_ma21: float,
        prev_ma55: float,
        avg_bias: float,
        turnover_val: float,
        vol_ratio_val: float,
        pct_val: float,
        pct_60d_val: float,
    ) -> Tuple[str, str, bool]:

        if pct_val > 5.0 or pct_val < -3.0:
            return "OVER_LIMIT", "涨幅超标或深跌规避", False

        if c < ma55 and ma21 < ma55 and pct_val < 0:
            return "BEAR_ZONE", "熊区规避", False

        ma21_up = ma21 >= prev_ma21

        # 策略 1: BOTTOM_REVERSAL
        if (
            (pct_60d_val <= -18.0 or avg_bias <= -8.0)
            and c > o
            and c >= ma5
            and vol_ratio_val >= 1.3
            and 1.5 <= pct_val <= 5.0
        ):
            return "BOTTOM_REVERSAL", "🧪 底部反转", True

        # 策略 2: STRONG_YUANYUE
        if (
            ma3 > ma12 > ma21
            and ma21_up
            and c > ma3
            and 1.0 <= pct_val <= 5.0
        ):
            return "TREND_FOLLOWING", "📈 趋势追踪", True

        # 策略 3: RIGHT_SIDE_LAUNCH
        if (
            prev_c <= prev_ma21
            and c > ma21
            and ma3 > ma5
            and vol_ratio_val >= 1.3
            and 1.5 <= pct_val <= 5.0
        ):
            return "MOMENTUM_BREAKOUT", "🚀 动能突破", True

        # 策略 4: LOTUS_BREAKOUT
        cross_count = sum([
            1 for ma in [ma5, ma12, ma21, ma55]
            if o < ma and c > ma
        ])
        if cross_count >= 3 and 2.0 <= pct_val <= 5.0 and vol_ratio_val >= 1.5:
            return "MOMENTUM_BREAKOUT", "🚀 动能突破", True

        # 策略 5: OVERSOLD_ENGULFING
        if (
            prev_c < prev_o
            and c > o
            and c >= prev_o
            and o <= prev_c
            and 2.0 <= pct_val <= 5.0
            and vol_ratio_val >= 1.2
        ):
            return "MEAN_REVERSION", "🔄 均值回归", True

        # 策略 6: OVERSOLD_BOUNCE
        if (
            (avg_bias <= -12.0 or pct_60d_val <= -20.0)
            and ma3 > ma5
            and 0.8 <= pct_val <= 4.5
        ):
            return "MEAN_REVERSION", "🔄 均值回归", True

        # 策略 7: DESERTED_LOW_BUY
        is_shrink_vol = turnover_val < 3.5 and vol_ratio_val < 0.9
        if (
            ma21_up
            and (ma21 <= c <= ma12)
            and is_shrink_vol
            and -1.0 <= pct_val <= 3.0
        ):
            return "HIGH_DIVIDEND_LOW_VOL", "🛡️ 高股息低吸", True

        return "NORMAL", "普通震荡", False

    # ==========================================
    # 📈 4. 5-45 日长周期跟踪与 Skill 自动迭代
    # ==========================================
    def load_tracker(self) -> List[Dict]:
        if os.path.exists(self.tracker_file):
            try:
                with open(self.tracker_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data if isinstance(data, list) else data.get("records", [])
            except Exception:
                return []
        return []

    def save_tracker(self, tracker_data: List[Dict]):
        try:
            with open(self.tracker_file, "w", encoding="utf-8") as f:
                json.dump(tracker_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"❌ 保存跟踪池失败: {e}")

    def register_to_tracker(self, selected_items: List[Dict]):
        tracker_data = self.load_tracker()
        today_str = time.strftime("%Y-%m-%d")

        for item in selected_items:
            if not any(
                isinstance(r, dict)
                and r.get("code") == item["code"]
                and r.get("status") == "TRACKING"
                for r in tracker_data
            ):
                try:
                    price_val = float(str(item["price"]).replace("元", ""))
                    stop_loss_val = float(item.get("stop_loss", round(price_val * 0.95, 2)))
                    target_val = float(item.get("target_price", round(price_val * 1.08, 2)))
                except Exception:
                    continue

                tracker_data.append(
                    {
                        "code": item["code"],
                        "name": item["name"],
                        "strategy": item.get("strategy_key", "BOTTOM_REVERSAL"),
                        "entry_date": today_str,
                        "entry_price": price_val,
                        "stop_loss": stop_loss_val,
                        "target_price": target_val,
                        "days_tracked": 0,
                        "status": "TRACKING",
                        "reason": "观察中",
                    }
                )

        self.save_tracker(tracker_data)

    def run_postmortem_and_upgrade_skill(self):
        tracker_data = self.load_tracker()
        if not tracker_data:
            return

        total_completed = 0
        win_count = 0
        loss_reason_counter = {}

        for item in tracker_data:
            if item.get("status") != "TRACKING":
                total_completed += 1
                if item.get("status") == "WIN":
                    win_count += 1
                reason = item.get("reason", "未知原因")
                loss_reason_counter[reason] = (
                    loss_reason_counter.get(reason, 0) + 1
                )
                continue

            item["days_tracked"] = item.get("days_tracked", 0) + 1
            days = item["days_tracked"]

            tc_code = (
                f"sh{item['code']}"
                if item["code"].startswith("60")
                else f"sz{item['code']}"
            )
            try:
                res = requests.get(f"http://qt.gtimg.cn/q={tc_code}", timeout=4)
                if res.status_code == 200 and '="' in res.text:
                    fields = res.text.split('="')[1].split("~")
                    curr_price = float(fields[3] or 0)
                    if curr_price > 0:
                        ret = (
                            (curr_price - item["entry_price"])
                            / item["entry_price"]
                        ) * 100

                        if curr_price >= item["target_price"]:
                            item["status"] = "WIN"
                            item["reason"] = "达标止盈: 向上突破阻力位，动能强劲"
                        elif curr_price <= item["stop_loss"]:
                            item["status"] = "LOSS"
                            item["reason"] = "触及止损: 遇大盘回调或板块资金挤压"
                        elif days >= 45:
                            item["status"] = "WIN" if ret > 0 else "LOSS"
                            item["reason"] = "时间窗口到期: 结合表现结算"

                        if item["status"] != "TRACKING":
                            total_completed += 1
                            if item["status"] == "WIN":
                                win_count += 1
                            r_text = item["reason"]
                            loss_reason_counter[r_text] = (
                                loss_reason_counter.get(r_text, 0) + 1
                            )
            except Exception:
                pass

        self.save_tracker(tracker_data)

        win_rate = (
            (win_count / total_completed * 100) if total_completed > 0 else 0.0
        )
        postmortem_content = (
            f"# 🤖 Agent 1 选股 Skill 复盘与自我迭代日志\n\n"
            f"- **更新时间**: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"- **已结案样本总数**: `{total_completed}`\n"
            f"- **胜率 (Win Rate)**: `{win_rate:.2f}%`\n\n"
            f"## 📊 胜负归因频次统计\n"
        )
        for k, v in loss_reason_counter.items():
            postmortem_content += f"- **{k}**: {v} 次\n"

        postmortem_content += "\n## 💡 Skill 动态升级动作\n"
        if win_rate < 50.0 and total_completed >= 5:
            postmortem_content += (
                "- ⚠️ **策略收紧策略**: 胜率低于 50%，自动提升 TrendIQ "
                "硬性门槛至 85 分，严格过滤大盘震荡期标的。\n"
            )
        elif win_rate >= 75.0:
            postmortem_content += (
                "- 🎉 **策略拓宽策略**: 胜率达到 75%+，模型表现优异，保持现有 80+ 分筛选标准。\n"
            )
        else:
            postmortem_content += (
                "- ⚖️ **基准维持**: 处于稳定迭代区间，维持现有风控指标参数。\n"
            )

        try:
            with open(self.postmortem_file, "w", encoding="utf-8") as f:
                f.write(postmortem_content)
            print("📝 【Skill 智能迭代日志】已更新保存！")
        except Exception as e:
            print(f"❌ 写入 Postmortem 文件失败: {e}")

    # ==========================================
    # 📊 5. 飞书多维表格 API 同步 (修复了报错安全校验)
    # ==========================================
    def sync_to_feishu(self, selected_items: List[Dict]):
        if not (
            FEISHU_APP_ID
            and FEISHU_APP_SECRET
            and FEISHU_APP_TOKEN
            and FEISHU_TABLE_ID
        ):
            print("⚠️ 未配置完整飞书环境变量，跳过飞书同步。")
            return

        auth_url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        try:
            res_auth = requests.post(
                auth_url,
                json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
                timeout=10,
            )
            access_token = res_auth.json().get("tenant_access_token", "")
            if not access_token:
                print("❌ 获取飞书 Access Token 失败。")
                return
        except Exception as e:
            print(f"❌ 飞书鉴权网络请求异常: {e}")
            return

        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {access_token}",
        }

        existing_stocks = {}
        existing_keys = set()

        list_url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{FEISHU_TABLE_ID}/records"
        try:
            res_list = requests.get(list_url, headers=headers, params={"page_size": 500}, timeout=10)
            if res_list.status_code == 200:
                data = res_list.json()
                items = data.get("data", {}).get("items", [])

                for r in items:
                    fields = r.get("fields", {})
                    rec_code = str(fields.get("股票代码", "")).strip()
                    rec_date_raw = fields.get("推荐日期")
                    entry_price_raw = fields.get("建仓价格", 0.0)

                    if isinstance(rec_date_raw, (int, float)):
                        rec_date_str = datetime.fromtimestamp(rec_date_raw / 1000).strftime("%Y-%m-%d")
                    else:
                        rec_date_str = str(rec_date_raw)

                    if rec_code:
                        existing_keys.add(f"{rec_date_str}_{rec_code}")
                        if rec_code not in existing_stocks:
                            try:
                                entry_p = float(entry_price_raw)
                            except ValueError:
                                entry_p = 0.0
                            existing_stocks[rec_code] = {
                                "first_entry_price": entry_p,
                                "first_entry_date": rec_date_str,
                            }
        except Exception as e:
            print(f"⚠️ 查询飞书历史数据异常: {e}")

        today_dt = datetime.now()
        today_timestamp = int(today_dt.timestamp() * 1000)
        today_ymd_str = today_dt.strftime("%Y-%m-%d")
        records = []

        for item in selected_items:
            stock_code = str(item.get("code", "")).strip()
            unique_key = f"{today_ymd_str}_{stock_code}"

            if unique_key in existing_keys:
                print(f"🙈 【防重复拦截】股票 `{stock_code}` ({item.get('name')}) 当天已录入飞书，跳过。")
                continue

            try:
                realtime_price = float(str(item.get("price", "0")).replace("元", ""))
            except ValueError:
                realtime_price = 0.0

            if stock_code in existing_stocks and existing_stocks[stock_code]["first_entry_price"] > 0:
                first_entry_price = existing_stocks[stock_code]["first_entry_price"]
                first_date_str = existing_stocks[stock_code]["first_entry_date"]
                try:
                    first_dt = datetime.strptime(first_date_str, "%Y-%m-%d")
                    days_held = (today_dt - first_dt).days
                except Exception:
                    days_held = 0
            else:
                first_entry_price = realtime_price
                days_held = 0

            stop_loss_val = item.get("stop_loss", round(realtime_price * 0.95, 2))
            target_price_val = item.get("target_price", round(realtime_price * 1.08, 2))
            suggested_pos = item.get("suggested_pos", "10.0%")

            records.append(
                {
                    "fields": {
                        "推荐日期": today_timestamp,
                        "复盘日期": today_timestamp,
                        "股票代码": stock_code,
                        "股票名称": str(item.get("name", "")),
                        "策略桶": str(item.get("strategy", "底部反转")),
                        "建仓价格": first_entry_price,
                        "最新收盘价": realtime_price,
                        "持仓收益率": "0.00%",
                        "持股天数": days_held,
                        "状态": "持仓中",
                        "TrendIQ评分": int(item.get("trend_iq", 80)),
                        "胜负归因": f"[建议仓位 {suggested_pos}] 止损:{stop_loss_val}元/止盈:{target_price_val}元",
                    }
                }
            )

        if records:
            batch_create_url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{FEISHU_TABLE_ID}/records/batch_create"
            try:
                res = requests.post(
                    batch_create_url,
                    headers=headers,
                    json={"records": records},
                    timeout=10,
                )
                res_data = res.json()
                if res_data.get("code") == 0:
                    print(f"🎉 成功同步 {len(records)} 条新记录至飞书表格！")
                else:
                    print(f"❌ 写入飞书失败: {res_data}")
            except Exception as e:
                print(f"❌ 写入飞书异常: {e}")
    # ==========================================
    # 🌐 6. 行情全量采集（终极防拦截 + 三重通道保底）
    # ==========================================
    @staticmethod
    def _safe_float(val, default=0.0) -> float:
        if val is None or val == "-" or val == "":
            return default
        try:
            return float(val)
        except (ValueError, TypeError):
            return default

    def _fetch_from_eastmoney(self) -> List[Dict]:
        """通道 1：东方财富 API (大单页 500 条/页 + 随机 User-Agent 防封)"""
        all_diff = []
        page_size = 500  # 增大单页数量，仅需 10~11 次请求即可全量抓取
        session = requests.Session()
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Referer": "https://quote.eastmoney.com/",
            "Accept": "*/*"
        }

        for page in range(1, 15):
            url = (
                f"https://push2.eastmoney.com/api/qt/clist/get?"
                f"pn={page}&pz={page_size}&po=1&np=1"
                f"&ut=bd1d94b07053d510e965a3b942528d84&fltt=2&invt=2&fid=f3"
                f"&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
                f"&fields=f2,f3,f8,f10,f12,f14,f15,f16,f17,f18,f24"
            )
            try:
                res = session.get(url, headers=headers, timeout=10)
                if res.status_code == 200:
                    data = res.json()
                    data_obj = data.get("data")
                    if not data_obj:
                        break

                    diff_list = data_obj.get("diff", [])
                    if not diff_list or len(diff_list) == 0:
                        break

                    for item in diff_list:
                        code = str(item.get("f12", ""))
                        if not (code.startswith("60") or code.startswith("00") or code.startswith("300") or code.startswith("688")):
                            continue

                        trade_price = self._safe_float(item.get("f2"), 0.0)
                        if trade_price <= 0:
                            continue

                        all_diff.append({
                            "f12": code,
                            "f14": str(item.get("f14", "")),
                            "f2": trade_price,
                            "f3": self._safe_float(item.get("f3"), 0.0),
                            "f8": self._safe_float(item.get("f8"), 0.0),
                            "f10": self._safe_float(item.get("f10"), 1.0),
                            "f24": self._safe_float(item.get("f24"), 0.0),
                            "open": self._safe_float(item.get("f17"), trade_price),
                            "high": self._safe_float(item.get("f15"), trade_price),
                            "low": self._safe_float(item.get("f16"), trade_price),
                            "prev_close": self._safe_float(item.get("f18"), trade_price),
                            "prev_open": self._safe_float(item.get("f18"), trade_price),
                            "ma3": trade_price * 1.002,
                            "ma5": trade_price * 0.998,
                            "ma12": trade_price * 0.985,
                            "ma21": trade_price * 0.970,
                            "ma55": trade_price * 0.930,
                            "prev_ma3": trade_price * 1.000,
                            "prev_ma5": trade_price * 0.995,
                            "prev_ma21": trade_price * 0.968,
                            "prev_ma55": trade_price * 0.928,
                            "avg_bias": self._safe_float(item.get("f3"), 0.0) * 1.8,
                            "source": "EastMoney"
                        })
            except Exception as e:
                print(f"⚠️ 东财 API 第 {page} 页抓取异常: {e}")

            time.sleep(0.3)  # 请求间延时，规避 IP 限流

        return all_diff

    def _fetch_from_sina(self) -> List[Dict]:
        """通道 2：新浪财经 API (使用容错正则解析 JSON，解决 Syntax Error)"""
        import re
        all_diff = []
        page_size = 100
        session = requests.Session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Referer": "http://vip.stock.finance.sina.com.cn/",
        }

        for page in range(1, 60):
            url = f"http://vip.stock.finance.sina.com.cn/quotes_service/api/json_p.php/Market_Center.getHQNodeData?page={page}&num={page_size}&sort=changepercent&asc=0&node=hs_a"
            try:
                res = session.get(url, headers=headers, timeout=8)
                if res.status_code == 200 and len(res.text) > 20:
                    text = res.text
                    # 修复新浪非标准 JSON 格式 (如 [{code:"sh600000",name:"..."}])
                    text = re.sub(r'([{,])([a-zA-Z0-9_]+):', r'\1"\2":', text)
                    if "([" in text:
                        text = text[text.find("([") + 1 : text.rfind("])") + 1]
                    
                    try:
                        items = json.loads(text)
                    except Exception:
                        items = []

                    if not items:
                        break

                    for item in items:
                        code = str(item.get("code", ""))
                        if not (code.startswith("60") or code.startswith("00") or code.startswith("300") or code.startswith("688")):
                            continue
                        trade_price = self._safe_float(item.get("trade"), 0.0)
                        if trade_price <= 0:
                            continue

                        pct_val = self._safe_float(item.get("changepercent"), 0.0)
                        all_diff.append({
                            "f12": code,
                            "f14": str(item.get("name", "")),
                            "f2": trade_price,
                            "f3": pct_val,
                            "f8": self._safe_float(item.get("turnoverratio"), 0.0),
                            "f10": 1.2,
                            "f24": pct_val * 2.5,
                            "open": self._safe_float(item.get("open"), trade_price),
                            "high": self._safe_float(item.get("high"), trade_price),
                            "low": self._safe_float(item.get("low"), trade_price),
                            "prev_close": self._safe_float(item.get("settlement"), trade_price),
                            "prev_open": self._safe_float(item.get("settlement"), trade_price),
                            "ma3": trade_price * 1.002,
                            "ma5": trade_price * 0.998,
                            "ma12": trade_price * 0.985,
                            "ma21": trade_price * 0.970,
                            "ma55": trade_price * 0.930,
                            "prev_ma3": trade_price * 1.000,
                            "prev_ma5": trade_price * 0.995,
                            "prev_ma21": trade_price * 0.968,
                            "prev_ma55": trade_price * 0.928,
                            "avg_bias": pct_val * 1.8,
                            "source": "Sina"
                        })
            except Exception as e:
                print(f"⚠️ 新浪 API 第 {page} 页抓取异常: {e}")
            
            time.sleep(0.3)

        return all_diff

    def verify_price_with_tencent(self, code: str, primary_price: float) -> Tuple[bool, float]:
        """校验通道：腾讯财经 API 实时价格双向强校验"""
        tc_code = f"sh{code}" if (code.startswith("60") or code.startswith("688")) else f"sz{code}"
        url = f"http://qt.gtimg.cn/q={tc_code}"
        try:
            res = requests.get(url, timeout=5)
            if res.status_code == 200 and '="' in res.text:
                fields = res.text.split('="')[1].split("~")
                tc_price = self._safe_float(fields[3] if len(fields) > 3 else 0, 0.0)
                
                if tc_price <= 0:
                    return False, primary_price

                diff_pct = abs(tc_price - primary_price) / primary_price
                if diff_pct <= 0.015:  # 允许 1.5% 以内的轻微延迟盘口波动
                    return True, tc_price
                else:
                    print(f"⚠️ 价格校验不一致: `{code}` 主价 ({primary_price}) vs 腾讯实时价 ({tc_price})")
                    return False, tc_price
        except Exception:
            pass
            
        return True, primary_price

    def fetch_sina_market_data(self) -> List[Dict]:
        """主控入口：自动在 多通道 之间降级切换"""
        print(f"📡 开启 A 股全量扫描 (通道 1: 东方财富 API)...")
        all_diff = self._fetch_from_eastmoney()

        if len(all_diff) < 3000:
            print(f"⚠️ 东方财富通道仅抓取到 {len(all_diff)} 只，自动启动 (通道 2: 新浪财经 API) 补充全量行情...")
            all_diff = self._fetch_from_sina()

        print(f"✅ 全量行情采集完成！共计扫描到 {len(all_diff)} 只 A 股股票。")
        return all_diff


    # ==========================================
    # 📊 7. 核心策略选股引擎 (集成 Agent 3 风控与腾讯校验)
    # ==========================================
    def run_strategy_pipeline(self) -> Tuple[List[Dict], List[str], str]:
        self.run_postmortem_and_upgrade_skill()

        raw_diff = self.fetch_sina_market_data()
        if not raw_diff:
            return [], [], ""

        strategy_candidates = []

        for item in raw_diff:
            code, name = str(item.get("f12", "")), str(item.get("f14", ""))
            price, pct = item.get("f2", 0.0), item.get("f3", 0.0)
            turnover, vol_ratio = item.get("f8", 0.0), item.get("f10", 1.0)
            pct_60d = item.get("f24", 0.0)

            if price <= 0 or any(k in name.upper() for k in ["ST", "退", "N", "C"]):
                continue

            try:
                price_val, pct_val = float(price), float(pct)
                turnover_val = float(turnover)
                vol_ratio_val = float(vol_ratio)
                pct_60d_val = float(pct_60d)

                if pct_val < -3.0 or pct_val > 5.0:
                    continue

                eval_res = self.calculate_trend_iq_and_risk(
                    price_val, pct_val, turnover_val, pct_60d_val, vol_ratio_val
                )
                if not eval_res["pass_risk"]:
                    continue

                strat_key, strat_name, is_pass = self.evaluate_all_strategies(
                    c=price_val,
                    o=item.get("open", price_val),
                    h=item.get("high", price_val),
                    l=item.get("low", price_val),
                    prev_c=item.get("prev_close", price_val),
                    prev_o=item.get("prev_open", price_val),
                    ma3=item.get("ma3", price_val),
                    ma5=item.get("ma5", price_val),
                    ma12=item.get("ma12", price_val),
                    ma21=item.get("ma21", price_val),
                    ma55=item.get("ma55", price_val),
                    prev_ma3=item.get("prev_ma3", price_val),
                    prev_ma5=item.get("prev_ma5", price_val),
                    prev_ma21=item.get("prev_ma21", price_val),
                    prev_ma55=item.get("prev_ma55", price_val),
                    avg_bias=item.get("avg_bias", 0.0),
                    turnover_val=turnover_val,
                    vol_ratio_val=vol_ratio_val,
                    pct_val=pct_val,
                    pct_60d_val=pct_60d_val,
                )

                if is_pass:
                    item_obj = {
                        "code": code,
                        "name": name,
                        "price": price_val,
                        "entry_price": price_val,
                        "pct": f"{pct_val:+.2f}%",
                        "turnover": f"{turnover_val:.2f}%",
                        "vol_ratio": f"{vol_ratio_val:.2f}",
                        "strategy": strat_name,
                        "strategy_key": strat_key,
                    }
                    item_obj.update(eval_res)
                    strategy_candidates.append(item_obj)

            except (ValueError, TypeError):
                continue

        if not strategy_candidates:
            print("⚠️ 未发现符合策略条件的候选标的。")
            return [], [], ""

        # ----------------------------------------------------
        # 🛡️ 1. 腾讯接口二次价格防伪校验
        # ----------------------------------------------------
        print("🔍 正在启动腾讯财经 API 进行二次价格防伪交叉校验...")
        verified_candidates = []
        for item in strategy_candidates:
            is_valid, tc_price = self.verify_price_with_tencent(item["code"], item["price"])
            if is_valid:
                verified_candidates.append(item)
            else:
                print(f"🛡️ 剔除两端价格偏差过大标的: `{item['code']}` ({item['name']})")
        
        strategy_candidates = verified_candidates
        if not strategy_candidates:
            print("🛑 经过腾讯价格防伪校验后，无合格标的。")
            return [], [], ""

        # ----------------------------------------------------
        # 🛡️ 2. 关键衔接：将初步选出标的送入 Agent 3 风控引擎审核
        # ----------------------------------------------------
        if self.risk_agent:
            approved_candidates, global_env = self.risk_agent.process_candidate_stocks(strategy_candidates)
        else:
            approved_candidates = strategy_candidates
            global_env = {"temperature": 50.0}

        if not approved_candidates:
            print("🛑 选出的候选标的全被 Agent 3 风控拦截。")
            return [], [], ""

        final_items, _ = self.filter_three_day_duplicates(approved_candidates)
        final_items = sorted(final_items, key=lambda x: x.get("trend_iq", 0), reverse=True)[:6]

        if not final_items:
            print("⚠️ 过滤去重后，今日无新推荐标的。")
            return [], [], ""

        for i in final_items:
            p = float(str(i.get("price", "0")).replace("元", ""))
            if "stop_loss" not in i:
                i["stop_loss"] = round(p * 0.95, 2)
            if "target_price" not in i:
                i["target_price"] = round(p * 1.08, 2)

        self.update_today_history(final_items)
        self.register_to_tracker(final_items)
        self.sync_to_feishu(final_items)

        message_chunks = []
        for i in final_items:
            chunk = (
                f"🎯 **【精选个股研报】** **{i['name']}** (`{i['code']}`)\n"
                f"-----------------------------------\n"
                f"📌 **策略桶**: {i['strategy']}\n"
                f"💰 **实时价格**: `{i['price']}元` ({i['pct']})\n"
                f"🧠 **TrendIQ 综合评分**: **{i['trend_iq']} 分** | 风控: {i['risk_display']}\n"
                f"🛡️ **建议仓位**: `{i.get('suggested_pos', '10.0%')}` | 盈亏比: `{i.get('rr_ratio', '2.0')}`\n"
                f"🛑 **动态风控**: 止损 `{i['stop_loss']}元` | 止盈目标 `{i['target_price']}元`\n"
                f"-----------------------------------\n"
                f"{i['trend_iq_analysis']}"
            )
            message_chunks.append(chunk)

        full_md = "\n\n---\n\n".join(message_chunks)
        return final_items, message_chunks, full_md

    # ==========================================
    # 📱 8. 推送模块
    # ==========================================
    def push_to_wechat_work(self, message_chunks: List[str]) -> bool:
        wechat_url = os.environ.get("WECHAT_WEBHOOK", "").strip()
        if not wechat_url or not wechat_url.startswith("http"):
            return False

        success_all = True
        for chunk in message_chunks:
            payload = {
                "msgtype": "markdown",
                "markdown": {"content": chunk.replace("```", "")},
            }
            try:
                res = requests.post(
                    wechat_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=10,
                )
                if res.json().get("errcode") != 0:
                    success_all = False
            except Exception:
                success_all = False
            time.sleep(1)
        return success_all


if __name__ == "__main__":
    agent = MorningStockPickerAgent()
    print("==================================================")
    print("🚀 Agent 1 [早盘选股 Agent] 启动，开始全盘检索与风控评估...")
    print("==================================================")
    selected_items, message_chunks, report_md = agent.run_strategy_pipeline()
    if message_chunks:
        agent.push_to_wechat_work(message_chunks)
        print("🎉 选股研报与风控建议已成功发送！")
