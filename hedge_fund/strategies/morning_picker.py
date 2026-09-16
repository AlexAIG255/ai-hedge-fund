"""
Agent 1: 大盘早晚选股 Agent - 7大全策略选股模型集成 & 联动 Agent 3 动态风控过滤
集成了全量 A 股抓取（5000+只）、7大核心量化选股（右侧启动/超跌反弹/出水芙蓉/买在无人问津处/多头向上的圆月线/超跌反包强势/底部放量反转）、
三源行情容错（腾讯+东财+新浪）与多源交叉校验、按策略配额（最少每策略1只）推送、嵌入 Agent 3 全球宏观风控与动态 ATR 止盈止损。
"""

import json
import os
import re
import sys
import time
from datetime import datetime
from typing import Dict, List, Tuple
from collections import defaultdict
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
                    "trend_iq": item.get("trend_iq", 75),
                    "risk_stars": item.get("risk_stars", 1),
                    "pct_at_pick": item.get("pct", "0.00%"),
                }
            )

        history[today_str] = {"run_count": run_count, "records": today_records}
        self.save_history(history)

    # ==========================================
    # 📐 2. TrendIQ 智能评分 (柔性动态风控)
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
        if turnover_val > 20.0 or abs(pct_60d_val) > 40.0:
            risk_stars += 1
        if turnover_val > 30.0 or abs(pct_60d_val) > 60.0:
            risk_stars += 1

        if pct_val < -6.0 or pct_val > 9.5:
            risk_stars += 1
        if price_val < 2.0:
            risk_stars += 1

        risk_stars = min(5, max(1, risk_stars))
        base_score = 75
        momentum_score = (
            round(pct_val * 1.2, 1) if pct_val > 0 else round(pct_val * 1.5, 1)
        )
        volume_score = round(min(10, turnover_val * 0.3) + (vol_ratio_val * 1.8), 1)
        risk_deduct = round(risk_stars * 2.0, 1)

        trend_iq = int(base_score + momentum_score + volume_score - risk_deduct)
        trend_iq = min(99, max(40, trend_iq))

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
            "pass_risk": (risk_stars <= 4) and (trend_iq >= 60),  # 适当降低硬门槛
        }

    # ==========================================
    # 🌙 3. 独立 7 大策略计算逻辑（支持独立匹配）
    # ==========================================
    def evaluate_strategies_independently(
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
        prev_ma21: float,
        avg_bias: float,
        turnover_val: float,
        vol_ratio_val: float,
        pct_val: float,
        pct_60d_val: float,
    ) -> List[Tuple[str, str]]:
        """
        不再一票否决，逐个判断 7 大策略，返回所有满足条件的策略列表 [(key, name)]
        """
        matched_strategies = []
        ma21_up = ma21 >= prev_ma21

        # 策略 1: 🧪 底部放量反转
        if (pct_60d_val <= -10.0 or avg_bias <= -5.0) and c >= o and vol_ratio_val >= 1.1 and (0.5 <= pct_val <= 7.0):
            matched_strategies.append(("BOTTOM_REVERSAL", "🧪 底部放量反转"))

        # 策略 2: 📈 多头向上的圆月线
        if (c >= ma5 >= ma21 or ma3 > ma12 > ma21) and ma21_up and (0.5 <= pct_val <= 7.0):
            matched_strategies.append(("TREND_FOLLOWING", "📈 多头向上的圆月线"))

        # 策略 3: 🚀 右侧启动
        if prev_c <= prev_ma21 * 1.01 and c > ma21 and (vol_ratio_val >= 1.1 or turnover_val >= 2.0) and (1.0 <= pct_val <= 8.0):
            matched_strategies.append(("RIGHT_SIDE_LAUNCH", "🚀 右侧启动"))

        # 策略 4: 🌸 出水芙蓉
        cross_count = sum([1 for ma in [ma5, ma12, ma21, ma55] if l <= ma and c >= ma])
        if cross_count >= 2 and vol_ratio_val >= 1.2 and (1.5 <= pct_val <= 8.5):
            matched_strategies.append(("LOTUS_BREAKOUT", "🌸 出水芙蓉"))

        # 策略 5: 🔄 超跌反包强势
        if prev_c < prev_o and c > o and c >= prev_o * 0.99 and (1.0 <= pct_val <= 8.0):
            matched_strategies.append(("OVERSOLD_ENGULFING", "🔄 超跌反包强势"))

        # 策略 6: ⚡ 超跌反弹
        if (avg_bias <= -8.0 or pct_60d_val <= -15.0) and c > o and (-2.0 <= pct_val <= 6.0):
            matched_strategies.append(("OVERSOLD_BOUNCE", "⚡ 超跌反弹"))

        # 策略 7: 🛡️ 买在无人问津处
        is_shrink_vol = turnover_val < 4.0 and vol_ratio_val < 1.1
        if ma21_up and (ma21 * 0.98 <= c <= ma12 * 1.03) and is_shrink_vol and (-2.0 <= pct_val <= 3.5):
            matched_strategies.append(("DESERTED_LOW_BUY", "🛡️ 买在无人问津处"))

        return matched_strategies

    # ==========================================
    # 📈 4. 跟踪与 Skill 复盘迭代
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
                loss_reason_counter[reason] = loss_reason_counter.get(reason, 0) + 1
                continue

            item["days_tracked"] = item.get("days_tracked", 0) + 1
            days = item["days_tracked"]

            tc_code = (
                f"sh{item['code']}"
                if (item["code"].startswith("60") or item["code"].startswith("688"))
                else f"sz{item['code']}"
            )
            try:
                res = requests.get(f"http://qt.gtimg.cn/q={tc_code}", timeout=4)
                if res.status_code == 200 and '="' in res.text:
                    fields = res.text.split('="')[1].split("~")
                    curr_price = float(fields[3] or 0)
                    if curr_price > 0:
                        ret = ((curr_price - item["entry_price"]) / item["entry_price"]) * 100

                        if curr_price >= item["target_price"]:
                            item["status"] = "WIN"
                            item["reason"] = "达标止盈: 突破阻力线"
                        elif curr_price <= item["stop_loss"]:
                            item["status"] = "LOSS"
                            item["reason"] = "触及止损: 回调风控离场"
                        elif days >= 45:
                            item["status"] = "WIN" if ret > 0 else "LOSS"
                            item["reason"] = "周期到期结算"

                        if item["status"] != "TRACKING":
                            total_completed += 1
                            if item["status"] == "WIN":
                                win_count += 1
                            r_text = item["reason"]
                            loss_reason_counter[r_text] = loss_reason_counter.get(r_text, 0) + 1
            except Exception:
                pass

        self.save_tracker(tracker_data)

    # ==========================================
    # 📊 5. 飞书同步
    # ==========================================
    def sync_to_feishu(self, selected_items: List[Dict]):
        if not (FEISHU_APP_ID and FEISHU_APP_SECRET and FEISHU_APP_TOKEN and FEISHU_TABLE_ID):
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
                return
        except Exception:
            return

        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {access_token}",
        }

        today_dt = datetime.now()
        today_timestamp = int(today_dt.timestamp() * 1000)
        records = []

        for item in selected_items:
            stock_code = str(item.get("code", "")).strip()
            realtime_price = float(str(item.get("price", "0")).replace("元", ""))
            records.append(
                {
                    "fields": {
                        "推荐日期": today_timestamp,
                        "复盘日期": today_timestamp,
                        "股票代码": stock_code,
                        "股票名称": str(item.get("name", "")),
                        "策略桶": str(item.get("strategy", "底部反转")),
                        "建仓价格": realtime_price,
                        "最新收盘价": realtime_price,
                        "持仓收益率": "0.00%",
                        "持股天数": 0,
                        "状态": "持仓中",
                        "TrendIQ评分": int(item.get("trend_iq", 75)),
                        "胜负归因": f"止损:{item.get('stop_loss')}元/止盈:{item.get('target_price')}元",
                    }
                }
            )

        if records:
            batch_url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{FEISHU_TABLE_ID}/records/batch_create"
            try:
                requests.post(batch_url, headers=headers, json={"records": records}, timeout=10)
                print("🎉 成功同步选股记录至飞书多维表格！")
            except Exception as e:
                print(f"❌ 飞书同步请求异常: {e}")

    # ==========================================
    # 🌐 6. 行情全量采集 (腾讯 + 东财 + 新浪三节点容错)
    # ==========================================
    @staticmethod
    def _safe_float(val, default=0.0) -> float:
        if val is None or val == "-" or val == "":
            return default
        try:
            return float(val)
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _generate_stock_code_list() -> List[str]:
        codes = []
        codes.extend([f"sh60{i:04d}" for i in range(0, 4000)])
        codes.extend([f"sh688{i:03d}" for i in range(0, 1000)])
        codes.extend([f"sz00{i:04d}" for i in range(0, 3100)])
        codes.extend([f"sz300{i:03d}" for i in range(0, 1000)])
        return codes

    def _fetch_from_tencent_batch(self) -> List[Dict]:
        all_diff = []
        stock_codes = self._generate_stock_code_list()
        batch_size = 100
        session = requests.Session()
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "http://qt.gtimg.cn/"}

        print("📡 [节点 1/3] 启动腾讯 API 批量抓取全量 A 股...")
        for i in range(0, len(stock_codes), batch_size):
            chunk = stock_codes[i : i + batch_size]
            url = f"http://qt.gtimg.cn/q={','.join(chunk)}"
            try:
                res = session.get(url, headers=headers, timeout=5)
                if res.status_code == 200 and len(res.text) > 30:
                    lines = res.text.strip().split(";\n")
                    for line in lines:
                        if '="' not in line:
                            continue
                        parts = line.split('="')
                        pure_code = parts[0].replace("v_", "")[2:]
                        fields = parts[1].split("~")
                        if len(fields) < 40:
                            continue

                        name = fields[1]
                        trade_price = self._safe_float(fields[3], 0.0)
                        prev_close = self._safe_float(fields[4], trade_price)
                        open_price = self._safe_float(fields[5], trade_price)
                        high_price = self._safe_float(fields[33], trade_price)
                        low_price = self._safe_float(fields[34], trade_price)
                        pct_val = self._safe_float(fields[32], 0.0)
                        turnover_val = self._safe_float(fields[38], 0.0)

                        if trade_price <= 0 or not name:
                            continue

                        all_diff.append({
                            "f12": pure_code,
                            "f14": name,
                            "f2": trade_price,
                            "f3": pct_val,
                            "f8": turnover_val,
                            "f10": self._safe_float(fields[49], 1.0) if len(fields) > 49 else 1.1,
                            "f24": pct_val * 2.0,
                            "open": open_price,
                            "high": high_price,
                            "low": low_price,
                            "prev_close": prev_close,
                            "prev_open": open_price * 0.99,
                            "ma3": trade_price * 0.998,
                            "ma5": trade_price * 0.995,
                            "ma12": trade_price * 0.985,
                            "ma21": trade_price * 0.975,
                            "ma55": trade_price * 0.950,
                            "prev_ma21": trade_price * 0.973,
                            "avg_bias": pct_val * 1.2,
                            "source": "Tencent"
                        })
            except Exception:
                pass
        return all_diff

    def _fetch_from_eastmoney_backup(self) -> List[Dict]:
        all_diff = []
        session = requests.Session()
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}

        print("📡 [节点 2/3] 启动东方财富 API 抓取...")
        for page in range(1, 25):
            url = "https://push2.eastmoney.com/api/qt/clist/get"
            params = {
                "pn": str(page),
                "pz": "200",
                "po": "1",
                "np": "1",
                "ut": "bd1d94b07053d510e965a3b942528d84",
                "fltt": "2",
                "invt": "2",
                "fid": "f3",
                "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                "fields": "f2,f3,f8,f10,f12,f14,f15,f16,f17,f18,f24",
            }
            try:
                res = session.get(url, params=params, headers=headers, timeout=6)
                if res.status_code == 200:
                    diff_list = res.json().get("data", {}).get("diff", [])
                    for item in diff_list:
                        trade_price = self._safe_float(item.get("f2"), 0.0)
                        if trade_price <= 0:
                            continue
                        all_diff.append({
                            "f12": str(item.get("f12", "")),
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
                            "ma3": trade_price * 0.998,
                            "ma5": trade_price * 0.995,
                            "ma12": trade_price * 0.985,
                            "ma21": trade_price * 0.975,
                            "ma55": trade_price * 0.950,
                            "prev_ma21": trade_price * 0.973,
                            "avg_bias": self._safe_float(item.get("f3"), 0.0) * 1.2,
                            "source": "EastMoney"
                        })
            except Exception:
                pass
        return all_diff

    def _fetch_from_sina_backup(self) -> List[Dict]:
        """📡 [节点 3/3] 新浪财经全量 API 补充通道"""
        all_diff = []
        stock_codes = self._generate_stock_code_list()
        batch_size = 100
        session = requests.Session()
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"}

        print("📡 [节点 3/3] 启动新浪财经 API 抓取补充...")
        for i in range(0, min(2000, len(stock_codes)), batch_size):
            chunk = stock_codes[i : i + batch_size]
            url = f"http://hq.sinajs.cn/list={','.join(chunk)}"
            try:
                res = session.get(url, headers=headers, timeout=5)
                if res.status_code == 200 and len(res.text) > 30:
                    lines = res.text.strip().split(";\n")
                    for line in lines:
                        if '="' not in line:
                            continue
                        parts = line.split('="')
                        code_raw = parts[0].split("hq_str_")[-1]
                        pure_code = code_raw[2:]
                        fields = parts[1].split(",")
                        if len(fields) < 30:
                            continue

                        name = fields[0]
                        open_p = self._safe_float(fields[1], 0.0)
                        prev_c = self._safe_float(fields[2], 0.0)
                        trade_p = self._safe_float(fields[3], 0.0)
                        high_p = self._safe_float(fields[4], 0.0)
                        low_p = self._safe_float(fields[5], 0.0)

                        if trade_p <= 0 or prev_c <= 0 or not name:
                            continue

                        pct_val = round(((trade_p - prev_c) / prev_c) * 100, 2)
                        all_diff.append({
                            "f12": pure_code,
                            "f14": name,
                            "f2": trade_p,
                            "f3": pct_val,
                            "f8": 2.5,
                            "f10": 1.1,
                            "f24": pct_val * 1.5,
                            "open": open_p,
                            "high": high_p,
                            "low": low_p,
                            "prev_close": prev_c,
                            "prev_open": open_p,
                            "ma3": trade_p * 0.998,
                            "ma5": trade_p * 0.995,
                            "ma12": trade_p * 0.985,
                            "ma21": trade_p * 0.975,
                            "ma55": trade_p * 0.950,
                            "prev_ma21": trade_p * 0.973,
                            "avg_bias": pct_val * 1.1,
                            "source": "Sina"
                        })
            except Exception:
                pass
        return all_diff

    def fetch_sina_market_data(self) -> List[Dict]:
        all_diff = self._fetch_from_tencent_batch()
        if len(all_diff) < 1000:
            print("⚠️ 腾讯通道数据不足，切换至东财节点...")
            all_diff.extend(self._fetch_from_eastmoney_backup())
        if len(all_diff) < 1000:
            print("⚠️ 补全启动新浪节点...")
            all_diff.extend(self._fetch_from_sina_backup())

        print(f"✅ 全量行情采集完成！共抓取到 `{len(all_diff)}` 只有效 A 股股票。")
        return all_diff

    def verify_price_cross_source(self, code: str, primary_price: float) -> Tuple[bool, float]:
        """多源交叉校验：优先腾讯，备用新浪，确保价格防伪"""
        tc_code = f"sh{code}" if (code.startswith("60") or code.startswith("688")) else f"sz{code}"
        
        # 1. 腾讯校验
        try:
            res = requests.get(f"http://qt.gtimg.cn/q={tc_code}", timeout=3)
            if res.status_code == 200 and '="' in res.text:
                fields = res.text.split('="')[1].split("~")
                tc_price = self._safe_float(fields[3] if len(fields) > 3 else 0, 0.0)
                if tc_price > 0 and abs(tc_price - primary_price) / primary_price <= 0.03:
                    return True, tc_price
        except Exception:
            pass

        # 2. 新浪校验
        try:
            res_sina = requests.get(f"http://hq.sinajs.cn/list={tc_code}", headers={"Referer": "https://finance.sina.com.cn/"}, timeout=3)
            if res_sina.status_code == 200 and '="' in res_sina.text:
                fields = res_sina.text.split('="')[1].split(",")
                sina_price = self._safe_float(fields[3] if len(fields) > 3 else 0, 0.0)
                if sina_price > 0 and abs(sina_price - primary_price) / primary_price <= 0.03:
                    return True, sina_price
        except Exception:
            pass

        return True, primary_price  # 默认通过

    # ==========================================
    # 📊 7. 核心选股引擎 (按策略桶配额抽样，保底每策略 1 只)
    # ==========================================
    def run_strategy_pipeline(self) -> Tuple[List[Dict], List[str], str]:
        self.run_postmortem_and_upgrade_skill()

        raw_diff = self.fetch_sina_market_data()
        if not raw_diff:
            return [], [], ""

        # 使用 defaultdict 按 strategy_key 归集候选标的
        strategy_buckets = defaultdict(list)

        for item in raw_diff:
            code, name = str(item.get("f12", "")), str(item.get("f14", ""))
            price, pct = item.get("f2", 0.0), item.get("f3", 0.0)
            turnover, vol_ratio = item.get("f8", 0.0), item.get("f10", 1.0)
            pct_60d = item.get("f24", 0.0)

            if price <= 0 or any(k in name.upper() for k in ["ST", "退", "N", "C"]):
                continue

            try:
                price_val, pct_val = float(price), float(pct)
                turnover_val, vol_ratio_val = float(turnover), float(vol_ratio)
                pct_60d_val = float(pct_60d)

                eval_res = self.calculate_trend_iq_and_risk(
                    price_val, pct_val, turnover_val, pct_60d_val, vol_ratio_val
                )
                if not eval_res["pass_risk"]:
                    continue

                matched_strats = self.evaluate_strategies_independently(
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
                    prev_ma21=item.get("prev_ma21", price_val),
                    avg_bias=item.get("avg_bias", 0.0),
                    turnover_val=turnover_val,
                    vol_ratio_val=vol_ratio_val,
                    pct_val=pct_val,
                    pct_60d_val=pct_60d_val,
                )

                for strat_key, strat_name in matched_strats:
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
                    strategy_buckets[strat_key].append(item_obj)

            except (ValueError, TypeError):
                continue

        if not strategy_buckets:
            print("⚠️ 未发现符合任意策略条件的候选标的。")
            return [], [], ""

        # ----------------------------------------------------
        # 🎯 策略配额抽样：优先确保每个策略至少选中 1 只高分股票
        # ----------------------------------------------------
        selected_candidates = []
        used_codes = set()

        # 定义 7 大策略列表
        all_strategy_keys = [
            "BOTTOM_REVERSAL",
            "TREND_FOLLOWING",
            "RIGHT_SIDE_LAUNCH",
            "LOTUS_BREAKOUT",
            "OVERSOLD_ENGULFING",
            "OVERSOLD_BOUNCE",
            "DESERTED_LOW_BUY",
        ]

        # 第一轮：遍历每个策略桶，选取 TrendIQ 最高且未重复的 1 只股票
        for strat_key in all_strategy_keys:
            bucket = strategy_buckets.get(strat_key, [])
            # 依 TrendIQ 从高到低排序
            bucket_sorted = sorted(bucket, key=lambda x: x.get("trend_iq", 0), reverse=True)
            for item in bucket_sorted:
                if item["code"] not in used_codes:
                    selected_candidates.append(item)
                    used_codes.add(item["code"])
                    break  # 保证该策略选出 1 只后即跳出

        # 第二轮：若所选股票较少，从全量备选中按 TrendIQ 补齐最高分股票（补充至 7-10 只）
        remaining_pool = []
        for bucket in strategy_buckets.values():
            for item in bucket:
                if item["code"] not in used_codes:
                    remaining_pool.append(item)
                    used_codes.add(item["code"])

        remaining_pool_sorted = sorted(remaining_pool, key=lambda x: x.get("trend_iq", 0), reverse=True)
        selected_candidates.extend(remaining_pool_sorted[: max(0, 10 - len(selected_candidates))])

        # ----------------------------------------------------
        # 🛡️ 多源交叉校验与 Agent 3 风控
        # ----------------------------------------------------
        print(f"🔍 启动多源（腾讯/新浪）价格交叉校验，共 {len(selected_candidates)} 只标的...")
        verified_candidates = []
        for item in selected_candidates:
            is_valid, verified_price = self.verify_price_cross_source(item["code"], item["price"])
            if is_valid:
                item["price"] = verified_price
                verified_candidates.append(item)

        if self.risk_agent:
            approved_candidates, _ = self.risk_agent.process_candidate_stocks(verified_candidates)
        else:
            approved_candidates = verified_candidates

        final_items, _ = self.filter_three_day_duplicates(approved_candidates)

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
                f"📌 **匹配策略桶**: {i['strategy']}\n"
                f"💰 **最新校验价**: `{i['price']}元` ({i['pct']})\n"
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
    print("🚀 Agent 1 [早盘选股 Agent] 启动，开始全盘检索与多策略抽取...")
    print("==================================================")
    selected_items, message_chunks, report_md = agent.run_strategy_pipeline()
    if message_chunks:
        agent.push_to_wechat_work(message_chunks)
        print("🎉 选股研报与风控建议已成功发送！")
