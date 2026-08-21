"""
Agent 1: 大盘早晚选股 Agent (Morning Stock Picker Agent) - 进阶风控与复盘版
集成了全量 A 股抓取、腾讯实时校准、3日去重熔断、7大选股策略、TrendIQ 智能评分、
1-5星风险风控拦截、操盘指引以及 5-10 日建仓跟踪归因复盘系统。
"""

import json
import os
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Tuple
import requests

# 🔒 Dify Chatflow 对话接口配置
DIFY_API_URL = "https://api.dify.ai/v1/chat-messages"
DIFY_API_KEY = os.environ.get("DIFY_API_KEY", "").strip()

# ⚙️ 控制与时间参数配置
MANUAL_TEST = (
    os.environ.get("MANUAL_TEST", "false").lower() in ["true", "1", "yes"]
)
TARGET_SUCCESS_COUNT = 1 if MANUAL_TEST else 2  # 手动测试1次，正式2次
FAILURE_WAIT_SECONDS = 300  # 失败等待 5 分钟
SUCCESS_WAIT_SECONDS = 600  # 成功间隔 10 分钟
MAX_TOTAL_ATTEMPTS = 15  # 最大轮询上限
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
        """读取每日选股历史记录"""
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"⚠️ 读取历史记录文件失败: {e}")
        return {}

    def save_history(self, history_data: Dict):
        """保存每日选股历史记录"""
        try:
            with open(self.history_file, "w", encoding="utf-8") as f:
                json.dump(history_data, f, ensure_ascii=False, indent=2)
            print("💾 【每日选股历史】已成功更新保存！")
        except Exception as e:
            print(f"❌ 保存选股历史失败: {e}")

    def filter_three_day_duplicates(
        self, candidate_items: List[Dict]
    ) -> Tuple[List[Dict], Dict]:
        """去重逻辑：如果某只股票在过去连续 2 个交易日均被推荐，第 3 天自动取消推送"""
        history = self.load_history()
        today_str = time.strftime("%Y-%m-%d")

        past_dates = sorted([d for d in history.keys() if d < today_str])
        blocked_codes = set()

        if len(past_dates) >= 2:
            d_minus_1 = past_dates[-1]
            d_minus_2 = past_dates[-2]
            
            # 安全提取前两天的股票代码，兼顾列表或字典结构
            def extract_codes(day_data):
                if isinstance(day_data, dict):
                    records = day_data.get("records", [])
                elif isinstance(day_data, list):
                    records = day_data
                else:
                    records = []
                return set(item["code"] for item in records if isinstance(item, dict) and "code" in item)

            codes_d1 = extract_codes(history.get(d_minus_1, []))
            codes_d2 = extract_codes(history.get(d_minus_2, []))
            blocked_codes = codes_d1.intersection(codes_d2)

            if blocked_codes:
                print(
                    f"🛡️ 检测到前两日 ({d_minus_2}, {d_minus_1}) 均推荐的标的: {blocked_codes}，第3日触发熔断屏蔽！"
                )

        filtered_items = []
        for item in candidate_items:
            if item["code"] in blocked_codes:
                print(
                    f"🚫 剔除重复标的: [{item['code']} | {item['name']}] (已连续2日推荐)"
                )
            else:
                filtered_items.append(item)

        return filtered_items, history

    def update_today_history(self, selected_items: List[Dict]):
        """存入今日精选标的（增加 run_count 运行计数，支持同日推荐最多 3 次）"""
        history = self.load_history()
        today_str = time.strftime("%Y-%m-%d")
        
        today_entry = history.get(today_str, {})
        if isinstance(today_entry, list):
            run_count = 1
        elif isinstance(today_entry, dict):
            run_count = today_entry.get("run_count", 0) + 1
        else:
            run_count = 1

        today_records = []

        for item in selected_items:
            # 清理字符串格式，统一转成浮点数值和字符串
            try:
                pick_price = float(str(item.get("price", "0")).replace("元", ""))
            except ValueError:
                pick_price = 0.0

            try:
                stop_loss = float(str(item.get("stop_loss", "0")).replace("元", ""))
            except ValueError:
                stop_loss = item.get("raw_stop_loss", round(pick_price * 0.95, 2))

            try:
                target_price = float(str(item.get("target_price", "0")).replace("元", ""))
            except ValueError:
                target_price = item.get("raw_target", round(pick_price * 1.08, 2))

            today_records.append({
                "code": item["code"],
                "name": item["name"],
                "strategy": item["strategy"],
                "pick_price": pick_price,
                "stop_loss": stop_loss,
                "target_price": target_price,
                "entry_range": item.get("entry_range", f"{pick_price}~{pick_price}元"),
                "trend_iq": item.get("trend_iq", 80),
                "risk_stars": item.get("risk_stars", 1),
                "pct_at_pick": item.get("pct", "0.00%")
            })

        history[today_str] = {
            "run_count": run_count,
            "records": today_records
        }
        self.save_history(history)

    # ==========================================
    # 📐 2. TrendIQ 智能评分与 1-5 星风险拦截模块
    # ==========================================
    def calculate_trend_iq_and_risk(
        self, price_val: float, pct_val: float, turnover_val: float, pct_60d_val: float, vol_ratio_val: float
    ) -> Dict:
        """计算 TrendIQ 评分、风险星级及建仓指导操作方案"""
        # 风险星级评分 (基于换手率、波动幅度、超跌/暴涨程度)
        risk_stars = 1
        if turnover_val > 12.0 or abs(pct_60d_val) > 30.0:
            risk_stars += 1
        if turnover_val > 20.0 or abs(pct_60d_val) > 50.0:
            risk_stars += 1
        if pct_val < -5.0 or pct_val > 7.5:
            risk_stars += 1
        if price_val < 2.5:
            risk_stars += 1

        risk_stars = min(5, max(1, risk_stars))

        # TrendIQ 综合量化评分 (60-99分)
        trend_iq = int(
            80 + (pct_val * 1.2) - (risk_stars * 2.5) + min(10, turnover_val * 0.3) + (vol_ratio_val * 1.5)
        )
        trend_iq = min(99, max(60, trend_iq))

        # 操盘指导参数设置
        entry_low = round(price_val * 0.985, 2)   # 回踩 1.5% 试仓
        entry_high = round(price_val * 1.005, 2)  # 上浮 0.5% 限价建仓
        stop_loss = round(price_val * 0.95, 2)    # 5% 硬止损
        target_price = round(price_val * 1.08, 2) # 8% 阶段止盈

        return {
            "risk_stars": risk_stars,
            "risk_display": "⭐" * risk_stars,
            "trend_iq": trend_iq,
            "entry_range": f"{entry_low}~{entry_high}元",
            "stop_loss": f"{stop_loss:.2f}元",
            "target_price": f"{target_price:.2f}元",
            "raw_stop_loss": stop_loss,
            "raw_target": target_price,
            "pass_risk": risk_stars < 4  # 4星及以上高风险标的自动排除
        }

    # ==========================================
    # 📈 3. 5-10 日跟踪与归因复盘引擎
    # ==========================================
    def load_tracker(self) -> List[Dict]:
        if os.path.exists(self.tracker_file):
            try:
                with open(self.tracker_file, "r", encoding="utf-8") as f:
                    return json.load(f)
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
        """将今日推送的标的注册进 5-10 日建仓跟踪池"""
        tracker_data = self.load_tracker()
        today_str = time.strftime("%Y-%m-%d")

        for item in selected_items:
            # 避免重复跟踪
            if not any(r["code"] == item["code"] and r["status"] == "TRACKING" for r in tracker_data):
                try:
                    price_val = float(str(item["price"]).replace("元", ""))
                    stop_loss_val = float(str(item["stop_loss"]).replace("元", ""))
                    target_val = float(str(item["target_price"]).replace("元", ""))
                except Exception:
                    continue

                tracker_data.append({
                    "code": item["code"],
                    "name": item["name"],
                    "strategy": item.get("strategy", "默认策略"),
                    "entry_date": today_str,
                    "entry_price": price_val,
                    "stop_loss": stop_loss_val,
                    "target_price": target_val,
                    "days_tracked": 0,
                    "status": "TRACKING",  # TRACKING / WIN_CLOSE / LOSS_CLOSE / TIMEOUT
                    "history": []
                })

        self.save_tracker(tracker_data)

    def run_daily_tracking_and_postmortem(self, market_quotes_map: Dict[str, float]) -> List[str]:
        """对在跟踪标的进行收盘收益核算与归因总结"""
        tracker_data = self.load_tracker()
        today_str = time.strftime("%Y-%m-%d")
        postmortem_logs = []

        for item in tracker_data:
            if item["status"] != "TRACKING":
                continue

            code = item["code"]
            if code not in market_quotes_map:
                continue

            cur_price = market_quotes_map[code]
            item["days_tracked"] += 1
            ret_pct = round((cur_price - item["entry_price"]) / item["entry_price"] * 100, 2)
            item["history"].append({"date": today_str, "close": cur_price, "return_pct": ret_pct})

            # 场景 1：跌破止损位 -> 触发止损归因
            if cur_price <= item["stop_loss"]:
                item["status"] = "LOSS_CLOSE"
                log = f"⚠️ **【止损归因】** **{item['name']}({code})** 追踪第 {item['days_tracked']} 天跌破止损价 ({item['stop_loss']}元)，收盘价 `{cur_price}元`，累计收益: `{ret_pct}%`。**归因总结**：突破后跟风资金不足，受大盘/板块调头拖累，触发风控平仓。"
                postmortem_logs.append(log)

            # 场景 2：达到止盈目标 -> 触发止盈归因
            elif cur_price >= item["target_price"]:
                item["status"] = "WIN_CLOSE"
                log = f"🎉 **【止盈归因】** **{item['name']}({code})** 追踪第 {item['days_tracked']} 天达到目标价 ({item['target_price']}元)，收盘价 `{cur_price}元`，累计收益: `+{ret_pct}%`。**归因总结**：形态突破有效，多头动能强劲，主力持续拉升。"
                postmortem_logs.append(log)

            # 场景 3：满 10 天观察期届满 -> 到期总结
            elif item["days_tracked"] >= 10:
                item["status"] = "TIMEOUT"
                log = f"📌 **【到期归因】** **{item['name']}({code})** 满 10 日观察期，当前收盘 `{cur_price}元`，累计收益: `{ret_pct}%`。**归因总结**：筹码高位震荡消化，缺乏增量资金打板，动能衰减退出观察池。"
                postmortem_logs.append(log)

        self.save_tracker(tracker_data)

        # 写入 Skill 知识库复盘日志
        if postmortem_logs:
            try:
                with open(self.postmortem_file, "a", encoding="utf-8") as f:
                    f.write(f"\n### 🗓️ {today_str} 选股建仓复盘归因日志\n")
                    for log in postmortem_logs:
                        f.write(f"- {log}\n")
            except Exception as e:
                print(f"❌ 写入复盘知识库失败: {e}")

        return postmortem_logs

    # ==========================================
    # 🌐 4. 行情采集与动态量比校准
    # ==========================================
    def fetch_sina_market_data(self, scan_target=2500) -> List[Dict]:
        """新浪/腾讯实时行情拉取"""
        all_diff = []
        page_size = 100
        total_pages = scan_target // page_size
        session = requests.Session()
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/128.0.0.0"
            ),
            "Referer": "http://vip.stock.finance.sina.com.cn/",
        }

        print(f"📡 正在开启多源实时扫描，目标穿透 {scan_target} 只股票...")
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
                            # 过滤：仅主板（60/00），排除科创(688)/创业板(300/301)/北交所
                            if not (code.startswith("60") or code.startswith("00")):
                                continue

                            all_diff.append({
                                "f12": code,
                                "f14": item.get("name", ""),
                                "f2": float(item.get("trade", 0) or 0),
                                "f3": float(item.get("changepercent", 0) or 0),
                                "f8": float(item.get("turnoverratio", 0) or 0),
                                "f10": 1.2,  # 初始预估，后续由腾讯 HQ 第 49 位校准真实量比
                                "f24": float(item.get("changepercent", 0) or 0) * 2.5,
                            })
            except Exception:
                time.sleep(0.1)

        if not all_diff:
            print("⚠️ 新浪源返回空，启动腾讯 HQ 备用节点...")
            all_diff = self._fetch_tencent_backup()

        print(f"✅ 行情采集完成！共计 {len(all_diff)} 只主板标的。")
        return all_diff

    def _fetch_tencent_backup(self) -> List[Dict]:
        all_diff = []
        code_list = [f"sh60{i:04d}" for i in range(2000)] + [
            f"sz00{i:04d}" for i in range(2000)
        ]
        batch_size = 800
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        }

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

                            # 解析腾讯 HQ 第 49 位动态量比
                            real_vol_ratio = float(fields[49]) if len(fields) > 49 and fields[49] else 1.2

                            all_diff.append({
                                "f12": code,
                                "f14": fields[1],
                                "f2": float(fields[3]),
                                "f3": float(fields[32] or 0),
                                "f8": float(fields[38] or 0),
                                "f10": real_vol_ratio,
                                "f24": float(fields[32] or 0) * 2.5,
                            })
            except Exception:
                continue
        return all_diff

    def calibrate_items(self, items_list: List[Dict]) -> List[Dict]:
        """腾讯 HQ 毫秒级价格与【真实量比】校准"""
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
                            # 提取腾讯接口第 49 位真实的【量比】数据
                            real_vol_ratio = float(f[49]) if len(f) > 49 and f[49] else 1.2
                            tc_data[f[2]] = {
                                "price": f"{float(f[3]):.2f}元",
                                "raw_price": float(f[3]),
                                "pct": f"{float(f[32] or 0):+.2f}%",
                                "raw_pct": float(f[32] or 0),
                                "turnover": f"{float(f[38] or 0):.2f}%",
                                "raw_turnover": float(f[38] or 0),
                                "vol_ratio": f"{real_vol_ratio:.2f}",
                                "raw_vol_ratio": real_vol_ratio
                            }
                for item in items_list:
                    if item["code"] in tc_data:
                        t_data = tc_data[item["code"]]
                        item["price"] = t_data["price"]
                        item["pct"] = t_data["pct"]
                        item["turnover"] = t_data["turnover"]
                        item["vol_ratio"] = t_data["vol_ratio"]

                        # 重新计算风控与 TrendIQ
                        eval_res = self.calculate_trend_iq_and_risk(
                            price_val=t_data["raw_price"],
                            pct_val=t_data["raw_pct"],
                            turnover_val=t_data["raw_turnover"],
                            pct_60d_val=float(item.get("raw_pct_60d", 0)),
                            vol_ratio_val=t_data["raw_vol_ratio"]
                        )
                        item.update(eval_res)

        except Exception:
            pass
        return items_list

    # ==========================================
    # 📊 5. 核心策略选股筛选引擎
    # ==========================================
    def run_strategy_pipeline(self) -> Tuple[List[Dict], str]:
        """选股管道：运行 7 大策略算法，进行风险过滤，并联动复盘引擎"""
        raw_diff = self.fetch_sina_market_data()
        if not raw_diff:
            return [], ""

        # 生成全市场实时价格字典，用于复盘引擎
        market_quotes_map = {item["f12"]: float(item["f2"]) for item in raw_diff if float(item.get("f2", 0)) > 0}
        
        # 执行每日复盘归因总结
        postmortem_logs = self.run_daily_tracking_and_postmortem(market_quotes_map)

        # 7 大策略分类容器
        strategy_lotus = []
        strategy_fanbao = []
        strategy_oversold = []
        strategy_right_side = []
        strategy_quiet_bottom = []
        strategy_duck_head = []
        strategy_bottom_shrink = []

        for item in raw_diff:
            code, name = str(item.get("f12", "")), str(item.get("f14", ""))
            price, pct = item.get("f2", "-"), item.get("f3", "-")
            turnover, vol_ratio = item.get("f8", "-"), item.get("f10", "-")
            pct_60d = item.get("f24", "-")

            # 严格过滤：仅主板股票(60/00)，排除 ST、*ST、退市、次新股 (N/C 标识)
            if not (code.startswith("60") or code.startswith("00")):
                continue
            if (
                price in ["-", 0]
                or pct == "-"
                or any(k in name.upper() for k in ["ST", "退", "N", "C"])
            ):
                continue

            try:
                price_val = float(price)
                pct_val = float(pct)
                turnover_val = float(turnover) if turnover != "-" else 0.0
                vol_ratio_val = float(vol_ratio) if vol_ratio != "-" else 0.0
                pct_60d_val = float(pct_60d) if pct_60d != "-" else 0.0

                if pct_val < -5.0 or pct_val > 7.5:
                    continue

                # 计算 TrendIQ 与 1-5 星风险拦截
                eval_res = self.calculate_trend_iq_and_risk(
                    price_val, pct_val, turnover_val, pct_60d_val, vol_ratio_val
                )

                # 🛡️ 强风控门禁：风险等级 >= 4 星直接拦截剔除
                if not eval_res["pass_risk"]:
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
                }
                item_obj.update(eval_res)

                # 🎯 策略 1：【🌸 出水芙蓉突破】
                if (
                    -10.0 <= pct_60d_val <= 15.0
                    and 1.8 <= pct_val <= 7.2
                    and vol_ratio_val >= 1.3
                    and turnover_val >= 2.5
                ):
                    item_obj["strategy"] = "🌸 出水芙蓉突破"
                    strategy_lotus.append(item_obj)

                # 🎯 策略 2：【🔄 强劲反包蓄势】
                elif (
                    -15.0 <= pct_60d_val <= 10.0
                    and -3.0 <= pct_val <= 7.5
                    and vol_ratio_val >= 1.2
                    and turnover_val >= 2.5
                ):
                    item_obj["strategy"] = "🔄 强劲反包蓄势"
                    strategy_fanbao.append(item_obj)

                # 🎯 策略 3：【⚡ 急跌反抽企稳】
                elif (
                    pct_60d_val <= -15.0
                    and -5.0 <= pct_val <= 5.0
                    and vol_ratio_val >= 1.1
                    and turnover_val >= 2.0
                ):
                    item_obj["strategy"] = "⚡ 急跌反抽企稳"
                    strategy_oversold.append(item_obj)

                # 🎯 策略 4：【🚀 右侧刚启动】
                elif (
                    0.0 <= pct_60d_val <= 35.0
                    and 0.5 <= pct_val <= 6.5
                    and vol_ratio_val >= 1.2
                    and turnover_val >= 2.8
                ):
                    item_obj["strategy"] = "🚀 右侧刚启动"
                    strategy_right_side.append(item_obj)

                # 🎯 策略 5：【🤫 买在无人问津】
                elif (
                    -25.0 <= pct_60d_val <= 0.0
                    and -2.5 <= pct_val <= 3.0
                    and 1.0 <= turnover_val <= 3.0
                    and vol_ratio_val >= 1.0
                ):
                    item_obj["strategy"] = "🤫 买在无人问津"
                    strategy_quiet_bottom.append(item_obj)

                # 🎯 策略 6：【🦆 老鸭头突破】
                elif (
                    8.0 <= pct_60d_val <= 30.0
                    and 1.2 <= pct_val <= 6.8
                    and vol_ratio_val >= 1.2
                    and turnover_val >= 2.2
                ):
                    item_obj["strategy"] = "🦆 老鸭头突破"
                    strategy_duck_head.append(item_obj)

                # 🎯 策略 7：【📉 底部超跌缩量】
                elif (
                    -40.0 <= pct_60d_val <= -18.0
                    and -2.5 <= pct_val <= 2.5
                    and 0.5 <= turnover_val <= 2.0
                    and vol_ratio_val <= 1.1
                ):
                    item_obj["strategy"] = "📉 底部超跌缩量"
                    strategy_bottom_shrink.append(item_obj)

            except ValueError:
                continue

        # 汇总候选（各策略取 Top 2）
        candidate_items = (
            strategy_lotus[:2]
            + strategy_fanbao[:2]
            + strategy_oversold[:2]
            + strategy_right_side[:2]
            + strategy_quiet_bottom[:2]
            + strategy_duck_head[:2]
            + strategy_bottom_shrink[:2]
        )

        # 兜底安全性标的
        if not candidate_items:
            for item in raw_diff:
                code, name = str(item.get("f12", "")), str(item.get("f14", ""))
                pct_val = float(item.get("f3", 0) or 0)
                price_val = float(item.get("f2", 0) or 0)
                if (
                    code.startswith(("60", "00"))
                    and not any(k in name.upper() for k in ["ST", "退", "N", "C"])
                    and -5.0 <= pct_val <= 7.0
                ):
                    eval_res = self.calculate_trend_iq_and_risk(
                        price_val, pct_val, 1.5, 0.0, 1.0
                    )
                    if eval_res["pass_risk"]:
                        obj = {
                            "strategy": "⭐ 低位安全资金标的",
                            "code": code,
                            "name": name,
                            "price": f"{price_val:.2f}元",
                            "pct": f"{pct_val:+.2f}%",
                            "pct_60d": "-",
                            "vol_ratio": "1.00",
                            "turnover": f"{item.get('f8',0)}%",
                        }
                        obj.update(eval_res)
                        candidate_items.append(obj)
                if len(candidate_items) >= 6:
                    break

        # 腾讯 HQ 毫秒级价格与【动态量比】校准
        candidate_items = self.calibrate_items(candidate_items)
        
        # 3日连续推荐去重熔断
        final_items, history_data = self.filter_three_day_duplicates(candidate_items)

        if not final_items:
            print("⚠️ 去重熔断后，今日无新标的可推送。")
            return [], ""

        # 更新历史并加入 5-10 日跟踪池
        self.update_today_history(final_items)
        self.register_to_tracker(final_items)

        # 生成全新维度的选股 Markdown 表格
        header = "| 策略 | 代码 | 名称 | 现价 | TrendIQ | 风险等级 | 建仓范围 | 止损位 | 目标位 |\n| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |"
        rows = [
            f"| {i['strategy']} | `{i['code']}` | **{i['name']}** | {i['price']} | **{i['trend_iq']}** | {i['risk_display']} | {i['entry_range']} | {i['stop_loss']} | {i['target_price']} |"
            for i in final_items
        ]
        table_text = header + "\n" + "\n".join(rows)

        # 历史记录汇总
        h_header = "| 日期 | 当日推荐精选标的清单 |\n| :--- | :--- |"
        h_rows = []
        for d in sorted(history_data.keys(), reverse=True)[:5]:
            day_val = history_data[d]
            if isinstance(day_val, dict):
                picks = day_val.get("records", [])
            elif isinstance(day_val, list):
                picks = day_val
            else:
                picks = []

            pick_str_list = []
            for p in picks:
                if isinstance(p, dict):
                    pick_str_list.append(f"`{p.get('code', '')}` **{p.get('name', '')}**")
                else:
                    pick_str_list.append(str(p))
            h_rows.append(f"| {d} | " + ", ".join(pick_str_list))

        h_text = h_header + "\n" + "\n".join(h_rows) if h_rows else "暂无历史记录"

        # 拼接复盘总结部分
        postmortem_text = ""
        if postmortem_logs:
            postmortem_text = "\n---\n### 📝 历史建仓 5-10 日归因复盘总结\n" + "\n".join(postmortem_logs) + "\n"

        report_markdown = (
            "早上选股\n【沪深主板 - 精选量化建仓研报】\n\n"
            f"### 🎯 今日精选推荐标的表格\n{table_text}\n"
            f"{postmortem_text}\n---\n"
            f"### 📋 近期历史选股记录汇总（已启用第3日重复剔除）\n{h_text}"
        )

        return final_items, report_markdown

    # ==========================================
    # 📱 6. 企业微信机器人直连推送 (分段防拦截版)
    # ==========================================
    def push_to_wechat_work(self, report_markdown: str) -> bool:
        """按段落切分并发送 Markdown 研报至企业微信，防止长文本被拦截"""
        wechat_url = os.environ.get("WECHAT_WEBHOOK", "").strip()

        # 严格校验 URL 合法性，防止 Invalid URL 抛错
        if not wechat_url or not (wechat_url.startswith("http://") or wechat_url.startswith("https://")):
            print("⚠️ 未配置有效的 WECHAT_WEBHOOK (需以 https:// 开头)，跳过企业微信推送。")
            return False

        MAX_CHUNK_SIZE = 1800
        paragraphs = report_markdown.split("\n\n")
        chunks = []
        current_chunk = ""

        for p in paragraphs:
            if len(current_chunk) + len(p) + 2 > MAX_CHUNK_SIZE:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                current_chunk = p
            else:
                current_chunk = f"{current_chunk}\n\n{p}" if current_chunk else p

        if current_chunk:
            chunks.append(current_chunk.strip())

        total_chunks = len(chunks)
        print(f"📡 研报全长 {len(report_markdown)} 字，已自动切分为 {total_chunks} 段推送到企业微信...")

        success_all = True
        for idx, chunk in enumerate(chunks, 1):
            page_prefix = f"📄 **【选股研报 ({idx}/{total_chunks})】**\n\n" if total_chunks > 1 else ""
            payload = {
                "msgtype": "markdown",
                "markdown": {
                    "content": page_prefix + chunk
                }
            }

            try:
                res = requests.post(wechat_url, json=payload, timeout=10)
                res_json = res.json()
                if res_json.get("errcode") == 0:
                    print(f"🎉 第 ({idx}/{total_chunks}) 段推送成功！")
                else:
                    print(f"❌ 第 ({idx}/{total_chunks}) 段推送失败: {res_json}")
                    success_all = False
            except Exception as e:
                print(f"❌ 第 ({idx}/{total_chunks}) 段推送异常: {e}")
                success_all = False

            if idx < total_chunks:
                time.sleep(1)

        return success_all

    # ==========================================
    # 📡 7. 远程 Dify 对接 (可选)
    # ==========================================
    def push_to_dify(self, report_markdown: str) -> bool:
        """提交至 Dify API 节点"""
        if not DIFY_API_KEY:
            print("⚠️ DIFY_API_KEY 未设置，跳过 Dify API 推送。")
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
            "query": f"根据以下精选主板表格生成风控研报并推送至微信：\n{report_markdown}",
            "response_mode": "blocking",
            "user": "github-actions-bot",
        }

        try:
            print("📡 正在提交分析研报给 Dify Agent...")
            res = requests.post(
                DIFY_API_URL, headers=headers, json=payload, timeout=60
            )
            if res.status_code == 200:
                print("==================================================")
                print("⚡ Dify 返回响应成功！")
                print("==================================================")
                return True
            else:
                print(f"❌ Dify 返回错误: {res.text}")
                return False
        except Exception as e:
            print(f"❌ 连接 Dify 失败: {e}")
            return False


# ==========================================
# 🚀 启动控制逻辑（当日允许重复推荐 3 次防重版）
# ==========================================
def main():
    agent = MorningStockPickerAgent()
    print("==================================================")
    print("🚀 Agent 1 [早盘选股 Agent] 启动，全策略搜寻中...")
    print("==================================================")

    # 🛡️ 防重机制：检查今日已推荐次数（上限 3 次）
    today_str = time.strftime("%Y-%m-%d")
    history = agent.load_history()
    today_data = history.get(today_str, {})

    if isinstance(today_data, dict):
        run_count = today_data.get("run_count", 0)
    elif isinstance(today_data, list) and len(today_data) > 0:
        run_count = 1
    else:
        run_count = 0

    if run_count >= 3:
        print(f"🛑 监测到今日 ({today_str}) 已成功推荐 3 次，触发防重拦截机制，程序停止。")
        return

    print(f"📊 今日已推荐次数: {run_count}/3，准备执行本次选股及推送...")

    success_count = 0
    for attempt in range(1, MAX_TOTAL_ATTEMPTS + 1):
        print(
            f"\n🔄 轮询尝试 {attempt}/{MAX_TOTAL_ATTEMPTS} (已成功次数: {success_count}/{TARGET_SUCCESS_COUNT})..."
        )

        selected_items, report_md = agent.run_strategy_pipeline()

        if report_md:
            print("\n" + report_md + "\n")

            pushed_wechat = False
            pushed_dify = False

            # 1. 尝试企业微信推送
            if hasattr(agent, "push_to_wechat_work"):
                pushed_wechat = agent.push_to_wechat_work(report_md)

            # 2. 尝试 Dify 推送
            if DIFY_API_KEY:
                pushed_dify = agent.push_to_dify(report_md)

            # 只要任意一种方式推送成功（或纯 GitHub 本地模式），即计为成功
            if pushed_wechat or pushed_dify or (not DIFY_API_KEY and not os.environ.get("WECHAT_WEBHOOK")):
                success_count += 1

            if success_count >= TARGET_SUCCESS_COUNT:
                print("🛑 完成任务，本次选股推送结束。")
                break
            else:
                time.sleep(SUCCESS_WAIT_SECONDS)
        else:
            print("⚠️ 今日标的均触发去重熔断或高风险拦截，防止重复推送，停止轮询。")
            break


if __name__ == "__main__":
    main()
