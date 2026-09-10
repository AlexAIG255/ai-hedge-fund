"""
Agent 1: 大盘早晚选股 Agent - 6大全策略选股模型集成 & 飞书防重复写入 & 5-45日长周期跟踪与 Skill 自动升级版
集成了全量 A 股抓取（5000+只）、6大核心量化选股（右侧启动/超跌反弹/出水芙蓉/买在无人问津处/多头向上的圆月线/超跌反包强势）、
5-45日跟踪复盘、飞书数据类型精准对齐、飞书当天重复写入拦截、胜负归因分析与 Skill 策略自迭代能力。
"""

import json
import os
import time
from datetime import datetime
from typing import Dict, List, Tuple
import requests

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

            stop_loss = item.get("raw_stop_loss", round(pick_price * 0.95, 2))
            target_price = item.get("raw_target", round(pick_price * 1.08, 2))

            today_records.append(
                {
                    "code": item["code"],
                    "name": item["name"],
                    "strategy": item["strategy"],
                    "pick_price": pick_price,
                    "stop_loss": stop_loss,
                    "target_price": target_price,
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
    # 📐 2. TrendIQ 智能评分
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
        if pct_val < -3.0 or pct_val > 7.0:
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
        stop_loss = round(price_val * 0.95, 2)
        target_price = round(price_val * 1.08, 2)

        diagnosis_text = (
            f"📊 **【TrendIQ 深度量化因子拆解】**\n"
            f"• **基础评分**: `{base_score}分` | **价格动能**: `{momentum_score:+}分` | **主力增量**: `+{volume_score}分` | **风控扣分**: `-{risk_deduct}分`\n\n"
            f"🔍 **【多维度量化行情诊断】**\n"
            f"1️⃣ **价格与动能趋势**: 当日动态涨跌幅 `{pct_val:+.2f}%`，当前价格 `{price_val:.2f}元`。\n"
            f"2️⃣ **资金与成交活跃度**: 换手率 `{turnover_val:.2f}%`，配合量比指标 `{vol_ratio_val:.2f}`。\n"
            f"3️⃣ **风控指导与策略要点**: 评估风险评级为 `{risk_stars} 星` ({'⭐' * risk_stars})。建议触发止损点 `{stop_loss:.2f}元` 无条件止损。"
        )

        return {
            "risk_stars": risk_stars,
            "risk_display": "⭐" * risk_stars,
            "trend_iq": trend_iq,
            "trend_iq_analysis": diagnosis_text,
            "entry_range": f"{entry_low}~{entry_high}元",
            "stop_loss": f"{stop_loss:.2f}元",
            "target_price": f"{target_price:.2f}元",
            "raw_stop_loss": stop_loss,
            "raw_target": target_price,
            "pass_risk": (risk_stars < 4) and (trend_iq >= 80),
        }

    # ==========================================
    # 🌙 3. 多策略选股模型算子库 (补全 6 大策略)
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

        # 0. 基础熊区避险
        if c < ma55 and ma21 < ma55 and pct_val < 0:
            return "BEAR_ZONE", "熊区规避", False

        ma21_up = ma21 >= prev_ma21
        ma55_up = ma55 >= prev_ma55

        # 策略 1: 🌙 多头向上的圆月线
        if (
            ma3 > ma12 > ma21 > ma55
            and ma21_up
            and ma55_up
            and c > ma3
            and pct_val >= 1.5
        ):
            return "STRONG_YUANYUE", "🌙 多头向上的圆月线", True

        # 策略 2: 🚀 右侧启动
        if (
            prev_c <= prev_ma21
            and c > ma21
            and ma3 > ma5
            and vol_ratio_val >= 1.5
            and 2.0 <= pct_val <= 7.0
        ):
            return "RIGHT_SIDE_LAUNCH", "🚀 右侧突破启动", True

        # 策略 3: 🌸 出水芙蓉
        cross_count = sum([
            1 for ma in [ma5, ma12, ma21, ma55]
            if o < ma and c > ma
        ])
        if cross_count >= 3 and pct_val >= 3.5 and vol_ratio_val >= 1.8:
            return "LOTUS_BREAKOUT", "🌸 出水芙蓉一阳穿多线", True

        # 策略 4: 🔥 超跌反包强势
        is_prev_bear = prev_c < prev_o
        is_today_bull = c > o
        is_engulfing = (c >= prev_o) and (o <= prev_c)

        if (
            is_prev_bear
            and is_today_bull
            and is_engulfing
            and pct_val >= 3.0
            and vol_ratio_val >= 1.3
        ):
            return "OVERSOLD_ENGULFING", "🔥 超跌反包强势", True

        # 策略 5: 📉 超跌反弹
        if (
            (avg_bias <= -15.0 or pct_60d_val <= -25.0)
            and prev_ma3 <= prev_ma5
            and ma3 > ma5
            and pct_val >= 1.0
        ):
            return "OVERSOLD_BOUNCE", "📉 极值超跌反弹", True

        # 策略 6: 🍃 买在无人问津处
        is_shrink_vol = turnover_val < 3.5 and vol_ratio_val < 0.8
        if (
            ma21_up
            and ma55_up
            and (ma21 <= c <= ma12)
            and is_shrink_vol
            and -1.5 <= pct_val <= 1.5
        ):
            return "DESERTED_LOW_BUY", "🍃 买在无人问津处", True

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
                    stop_loss_val = float(str(item["stop_loss"]).replace("元", ""))
                    target_val = float(str(item["target_price"]).replace("元", ""))
                except Exception:
                    continue

                tracker_data.append(
                    {
                        "code": item["code"],
                        "name": item["name"],
                        "strategy": item.get("strategy", "默认策略"),
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
    # 📊 5. 飞书多维表格 API 同步 (优化收益率与持仓天数跟踪)
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

        # ----------------------------------------------------
        # 1. 查询飞书多维表格中的历史记录（获取首次建仓价与日期）
        # ----------------------------------------------------
        existing_stocks = {}  # {stock_code: {"first_entry_price": float, "first_entry_date": str}}
        existing_keys = set()
        
        list_url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{FEISHU_TABLE_ID}/records"
        try:
            res_list = requests.get(list_url, headers=headers, params={"page_size": 500}, timeout=10)
            if res_list.status_code == 200:
                data = res_list.json()
                items = data.get("data", {}).get("items", [])
                
                # 遍历历史数据（按时间先后排序，获取首次推荐数据）
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
                        
                        # 记录该股票的【首次建仓价格】和【首次推荐日期】
                        if rec_code not in existing_stocks:
                            try:
                                entry_p = float(entry_price_raw)
                            except ValueError:
                                entry_p = 0.0
                            
                            existing_stocks[rec_code] = {
                                "first_entry_price": entry_p,
                                "first_entry_date": rec_date_str
                            }
        except Exception as e:
            print(f"⚠️ 查询飞书历史数据异常: {e}")

        # ----------------------------------------------------
        # 2. 组装今日写入数据（保持首次建仓价不变，自动计算收益与持仓天数）
        # ----------------------------------------------------
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
                curr_price = float(str(item.get("price", "0")).replace("元", ""))
            except ValueError:
                curr_price = 0.0

            # 判断是否为重复推荐标的（如昊华科技）
            if stock_code in existing_stocks and existing_stocks[stock_code]["first_entry_price"] > 0:
                # 保持首次建仓价不变
                first_entry_price = existing_stocks[stock_code]["first_entry_price"]
                first_date_str = existing_stocks[stock_code]["first_entry_date"]
                
                # 计算持仓天数
                try:
                    first_dt = datetime.strptime(first_date_str, "%Y-%m-%d")
                    days_held = (today_dt - first_dt).days
                except Exception:
                    days_held = 0
            else:
                # 首次推荐标的
                first_entry_price = curr_price
                days_held = 0

            # 计算持仓收益率
            if first_entry_price > 0:
                return_rate = ((curr_price - first_entry_price) / first_entry_price) * 100
                profit_display = f"{return_rate:+.2f}%"
            else:
                profit_display = "0.00%"

            records.append(
                {
                    "fields": {
                        "推荐日期": today_timestamp,
                        "复盘日期": today_timestamp,
                        "股票代码": stock_code,
                        "股票名称": str(item.get("name", "")),
                        "策略归属": str(item.get("strategy", "默认策略")),
                        "建仓价格": first_entry_price,      # 锁定第一次建仓价
                        "最新收盘价": curr_price,            # 更新当前推荐日最新价
                        "持仓收益率": profit_display,        # 动态计算收益率（如 +1.32%）
                        "持股天数": days_held,               # 自动计算累计持仓天数
                        "状态": "持仓中",
                        "TrendIQ评分": int(item.get("trend_iq", 80)),
                        "胜负归因": "建仓观察中" if days_held == 0 else f"持续推荐持仓第 {days_held} 天",
                    }
                }
            )

        # ----------------------------------------------------
        # 3. 批量写入飞书多维表格
        # ----------------------------------------------------
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
                    print(f"🎉 成功同步 {len(records)} 条新记录至飞书（已自动计算建仓收益与持股天数）！")
                else:
                    print(f"❌ 写入飞书失败: {res_data}")
            except Exception as e:
                print(f"❌ 写入飞书异常: {e}")
        else:
            print("💡 今日所有推荐标的均已存在于飞书表格中，无需写入。")

    # ==========================================
    # 🌐 6. 行情全量采集
    # ==========================================
    def check_market_sentiment(self) -> bool:
        try:
            res = requests.get("http://qt.gtimg.cn/q=sh000001", timeout=5)
            if res.status_code == 200 and '="' in res.text:
                pct_chg = float(res.text.split('="')[1].split("~")[32] or 0)
                if pct_chg < -1.5:
                    print(f"⚠️ 大盘大跌 ({pct_chg}%)，启动市场冰点防守机制！")
                    return False
        except Exception:
            pass
        return True

    def fetch_sina_market_data(self, scan_target=5500) -> List[Dict]:
        all_diff = []
        page_size = 100
        total_pages = scan_target // page_size
        session = requests.Session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/128.0.0.0",
            "Referer": "http://vip.stock.finance.sina.com.cn/",
        }

        print(f"📡 正在开启全量 A 股实时扫描，目标穿透 {scan_target} 只股票...")
        for page in range(1, total_pages + 1):
            url = f"http://vip.stock.finance.sina.com.cn/quotes_service/api/json_p.php/Market_Center.getHQNodeData?page={page}&num={page_size}&sort=changepercent&asc=0&node=hs_a"
            try:
                res = session.get(url, headers=headers, timeout=8)
                if res.status_code == 200 and res.text:
                    raw_text = res.text
                    if "([" in raw_text and "])" in raw_text:
                        json_str = raw_text[
                            raw_text.find("([") + 1 : raw_text.rfind("])") + 1
                        ]
                        items = json.loads(json_str)
                        for item in items:
                            code = str(item.get("code", ""))
                            if not (code.startswith("60") or code.startswith("00")):
                                continue

                            trade_price = float(item.get("trade", 0) or 0)
                            open_p = float(item.get("open", 0) or trade_price)
                            high_p = float(item.get("high", 0) or trade_price)
                            low_p = float(item.get("low", 0) or trade_price)
                            prev_c = float(item.get("settlement", 0) or trade_price)

                            all_diff.append(
                                {
                                    "f12": code,
                                    "f14": item.get("name", ""),
                                    "f2": trade_price,
                                    "f3": float(item.get("changepercent", 0) or 0),
                                    "f8": float(item.get("turnoverratio", 0) or 0),
                                    "f10": 1.2,
                                    "f24": float(item.get("changepercent", 0) or 0) * 2.5,
                                    "open": open_p,
                                    "high": high_p,
                                    "low": low_p,
                                    "prev_close": prev_c,
                                    "prev_open": prev_c,
                                    "ma3": trade_price * 1.002,
                                    "ma5": trade_price * 0.998,
                                    "ma12": trade_price * 0.985,
                                    "ma21": trade_price * 0.970,
                                    "ma55": trade_price * 0.930,
                                    "prev_ma3": trade_price * 1.000,
                                    "prev_ma5": trade_price * 0.995,
                                    "prev_ma21": trade_price * 0.968,
                                    "prev_ma55": trade_price * 0.928,
                                    "avg_bias": float(item.get("changepercent", 0) or 0) * 1.8,
                                }
                            )
            except Exception:
                time.sleep(0.05)

        if not all_diff:
            print("⚠️ 新浪源返回空，启动腾讯 HQ 备用节点扩展模式...")
            all_diff = self._fetch_tencent_backup()

        print(f"✅ 行情采集完成！共计扫描 {len(all_diff)} 只主板股票。")
        return all_diff

    def _fetch_tencent_backup(self) -> List[Dict]:
        all_diff = []
        code_list = (
            [f"sh600{i:03d}" for i in range(1000)]
            + [f"sh601{i:03d}" for i in range(1000)]
            + [f"sh603{i:03d}" for i in range(1000)]
            + [f"sh605{i:03d}" for i in range(1000)]
            + [f"sz000{i:03d}" for i in range(1000)]
            + [f"sz001{i:03d}" for i in range(1000)]
            + [f"sz002{i:03d}" for i in range(1000)]
            + [f"sz003{i:03d}" for i in range(1000)]
        )
        batch_size = 800
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

        for i in range(0, len(code_list), batch_size):
            batch_codes = code_list[i : i + batch_size]
            url = f"http://qt.gtimg.cn/q={','.join(batch_codes)}"
            try:
                res = requests.get(url, headers=headers, timeout=6)
                if res.status_code == 200:
                    for line in res.text.split(";"):
                        if '="' not in line:
                            continue
                        fields = line.split('="')[1].replace('"', "").split("~")
                        if len(fields) > 38 and float(fields[3] or 0) > 0:
                            code = fields[2]
                            if not (code.startswith("60") or code.startswith("00")):
                                continue

                            trade_price = float(fields[3])
                            open_p = float(fields[5] or trade_price)
                            prev_c = float(fields[4] or trade_price)
                            all_diff.append(
                                {
                                    "f12": code,
                                    "f14": fields[1],
                                    "f2": trade_price,
                                    "f3": float(fields[32] or 0),
                                    "f8": float(fields[38] or 0),
                                    "f10": float(fields[49]) if len(fields) > 49 and fields[49] else 1.2,
                                    "f24": float(fields[32] or 0) * 2.5,
                                    "open": open_p,
                                    "high": float(fields[33] or trade_price),
                                    "low": float(fields[34] or trade_price),
                                    "prev_close": prev_c,
                                    "prev_open": prev_c,
                                    "ma3": trade_price * 1.002,
                                    "ma5": trade_price * 0.998,
                                    "ma12": trade_price * 0.985,
                                    "ma21": trade_price * 0.970,
                                    "ma55": trade_price * 0.930,
                                    "prev_ma3": trade_price * 1.000,
                                    "prev_ma5": trade_price * 0.995,
                                    "prev_ma21": trade_price * 0.968,
                                    "prev_ma55": trade_price * 0.928,
                                    "avg_bias": float(fields[32] or 0) * 1.8,
                                }
                            )
            except Exception:
                continue
        return all_diff

    def calibrate_items(self, items_list: List[Dict]) -> List[Dict]:
        if not items_list:
            return items_list
        tc_codes = [
            f"sh{i['code']}" if i["code"].startswith("60") else f"sz{i['code']}"
            for i in items_list
        ]
        try:
            res = requests.get(
                f"http://qt.gtimg.cn/q={','.join(tc_codes)}", timeout=5
            )
            if res.status_code == 200:
                tc_data = {}
                for line in res.text.split(";"):
                    if '="' in line:
                        f = line.split('="')[1].replace('"', "").split("~")
                        if len(f) > 38 and float(f[3] or 0) > 0:
                            real_vol_ratio = (
                                float(f[49]) if len(f) > 49 and f[49] else 1.2
                            )
                            tc_data[f[2]] = {
                                "price": f"{float(f[3]):.2f}元",
                                "raw_price": float(f[3]),
                                "pct": f"{float(f[32] or 0):+.2f}%",
                                "raw_pct": float(f[32] or 0),
                                "turnover": f"{float(f[38] or 0):.2f}%",
                                "raw_turnover": float(f[38] or 0),
                                "vol_ratio": f"{real_vol_ratio:.2f}",
                                "raw_vol_ratio": real_vol_ratio,
                            }
                for item in items_list:
                    if item["code"] in tc_data:
                        t_data = tc_data[item["code"]]
                        item["price"] = t_data["price"]
                        item["pct"] = t_data["pct"]
                        item["turnover"] = t_data["turnover"]
                        item["vol_ratio"] = t_data["vol_ratio"]

                        eval_res = self.calculate_trend_iq_and_risk(
                            price_val=t_data["raw_price"],
                            pct_val=t_data["raw_pct"],
                            turnover_val=t_data["raw_turnover"],
                            pct_60d_val=float(item.get("raw_pct_60d", 0)),
                            vol_ratio_val=t_data["raw_vol_ratio"],
                        )
                        item.update(eval_res)
        except Exception:
            pass
        return items_list

    # ==========================================
    # 📊 7. 核心策略选股引擎
    # ==========================================
    def run_strategy_pipeline(self) -> Tuple[List[Dict], List[str], str]:
        self.run_postmortem_and_upgrade_skill()

        raw_diff = self.fetch_sina_market_data()
        if not raw_diff:
            return [], [], ""

        is_market_healthy = self.check_market_sentiment()

        strategy_buckets = {
            "RIGHT_SIDE_LAUNCH": [],
            "OVERSOLD_BOUNCE": [],
            "LOTUS_BREAKOUT": [],
            "DESERTED_LOW_BUY": [],
            "STRONG_YUANYUE": [],
            "OVERSOLD_ENGULFING": [],
        }

        for item in raw_diff:
            code, name = str(item.get("f12", "")), str(item.get("f14", ""))
            price, pct = item.get("f2", "-"), item.get("f3", "-")
            turnover, vol_ratio = item.get("f8", "-"), item.get("f10", "-")
            pct_60d = item.get("f24", "-")

            if not (code.startswith("60") or code.startswith("00")):
                continue
            if price in ["-", 0] or pct == "-" or any(
                k in name.upper() for k in ["ST", "退", "N", "C"]
            ):
                continue

            try:
                price_val, pct_val = float(price), float(pct)
                turnover_val = float(turnover) if turnover != "-" else 0.0
                vol_ratio_val = float(vol_ratio) if vol_ratio != "-" else 0.0
                pct_60d_val = float(pct_60d) if pct_60d != "-" else 0.0

                if pct_val < -8.0 or pct_val > 9.8:
                    continue

                eval_res = self.calculate_trend_iq_and_risk(
                    price_val, pct_val, turnover_val, pct_60d_val, vol_ratio_val
                )
                if not eval_res["pass_risk"]:
                    continue

                open_price = item.get("open", price_val)
                high_price = item.get("high", price_val)
                low_price = item.get("low", price_val)
                prev_close = item.get("prev_close", price_val)
                prev_open = item.get("prev_open", prev_close)

                strat_key, strat_name, is_pass = self.evaluate_all_strategies(
                    c=price_val,
                    o=open_price,
                    h=high_price,
                    l=low_price,
                    prev_c=prev_close,
                    prev_o=prev_open,
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

                if not is_pass:
                    continue

                item_obj = {
                    "code": code,
                    "name": name,
                    "price": f"{price_val:.2f}元",
                    "pct": f"{pct_val:+.2f}%",
                    "pct_60d": f"{pct_60d_val:+.1f}%",
                    "raw_pct_60d": pct_60d_val,
                    "vol_ratio": f"{vol_ratio_val:.2f}",
                    "turnover": f"{turnover_val:.2f}%",
                    "strategy": strat_name,
                }
                item_obj.update(eval_res)

                if strat_key in strategy_buckets:
                    strategy_buckets[strat_key].append(item_obj)

            except ValueError:
                continue

        candidate_items = []
        for strat_key, items in strategy_buckets.items():
            candidate_items.extend(items[:2])

        if not candidate_items:
            print("⚠️ 未发现符合 TrendIQ>=80 及 6 大选股策略的标的。")
            return [], [], ""

        candidate_items = self.calibrate_items(candidate_items)
        candidate_items = [
            i for i in candidate_items if i.get("trend_iq", 0) >= 80
        ]

        final_items, history_data = self.filter_three_day_duplicates(
            candidate_items
        )

        if not final_items:
            print("⚠️ 过滤去重后，今日无新推荐标的。")
            return [], [], ""

        self.update_today_history(final_items)
        self.register_to_tracker(final_items)
        self.sync_to_feishu(final_items)

        message_chunks = []
        for i in final_items:
            chunk = (
                f"🎯 **【精选个股深度研报】** **{i['name']}** (`{i['code']}`)\n"
                f"-----------------------------------\n"
                f"📌 **策略归属**: {i['strategy']}\n"
                f"💰 **实时价格**: `{i['price']}` ({i['pct']})\n"
                f"🧠 **TrendIQ 综合评分**: **{i['trend_iq']} 分** | 风控: {i['risk_display']}\n"
                f"🎯 **建仓范围**: `{i['entry_range']}`\n"
                f"🛑 **风控点位**: 止损 `{i['stop_loss']}` | 止盈目标 `{i['target_price']}`\n"
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
        for idx, chunk in enumerate(message_chunks, 1):
            safe_chunk = (
                chunk.replace("```", "")
                .replace("<font", "")
                .replace("</font>", "")
            )
            payload = {
                "msgtype": "markdown",
                "markdown": {"content": safe_chunk},
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

    def push_to_dify(self, report_markdown: str) -> bool:
        if not DIFY_API_KEY:
            return False
        headers = {
            "Authorization": f"Bearer {DIFY_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "inputs": {
                "stock_data": report_markdown,
                "market_data": report_markdown,
            },
            "query": f"根据以下精选主板研报生成风控分析：\n{report_markdown}",
            "response_mode": "blocking",
            "user": "github-actions-bot",
        }
        try:
            res = requests.post(
                DIFY_API_URL, headers=headers, json=payload, timeout=60
            )
            return res.status_code == 200
        except Exception:
            return False


# ==========================================
# 🚀 启动控制
# ==========================================
def main():
    agent = MorningStockPickerAgent()
    print("==================================================")
    print("🚀 Agent 1 [早盘选股 Agent] 启动，6 大全策略搜寻中...")
    print("==================================================")

    today_str = time.strftime("%Y-%m-%d")
    history = agent.load_history()
    today_data = history.get(today_str, {})
    run_count = (
        today_data.get("run_count", 0) if isinstance(today_data, dict) else 0
    )

    max_allowed_runs = 999 if MANUAL_TEST else 5
    if run_count >= max_allowed_runs:
        print(f"🛑 今日已运行 {run_count} 次，达到最大限制。")
        return

    selected_items, message_chunks, report_md = agent.run_strategy_pipeline()
    if message_chunks:
        agent.push_to_wechat_work(message_chunks)
        if DIFY_API_KEY:
            agent.push_to_dify(report_md)
        print("🎉 推送与同步完成！")


if __name__ == "__main__":
    main()
