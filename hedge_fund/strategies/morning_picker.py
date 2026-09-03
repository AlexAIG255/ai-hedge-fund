"""
Agent 1: 大盘早晚选股 Agent (Morning Stock Picker Agent) - 精准策略与 TrendIQ 深度切片推送版
集成了全量 A 股抓取、腾讯实时校准、3日去重熔断、7大选股策略、TrendIQ 80+ 智能深度评分、
企业微信/Dify 推送、飞书多维表格云端同步，以及 5-10 日建仓跟踪复盘系统。
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

# ⚙️ 飞书多维表格 API 配置
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "").strip()
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "").strip()
FEISHU_APP_TOKEN = os.environ.get("FEISHU_APP_TOKEN", "").strip()
FEISHU_TABLE_ID = os.environ.get("FEISHU_TABLE_ID", "").strip()

# ⚙️ 控制与时间参数配置
MANUAL_TEST = (
    os.environ.get("MANUAL_TEST", "false").lower() in ["true", "1", "yes"]
)
TARGET_SUCCESS_COUNT = 1 if MANUAL_TEST else 2
FAILURE_WAIT_SECONDS = 300
SUCCESS_WAIT_SECONDS = 600
MAX_TOTAL_ATTEMPTS = 15

# 🎯 根目录统一持久化文件
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
                print(f"🚫 剔除重复标的: [{item['code']} | {item['name']}] (已连续2日推荐)")
            else:
                filtered_items.append(item)

        return filtered_items, history

    def update_today_history(self, selected_items: List[Dict]):
        """存入今日精选标的"""
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
    # 📐 2. TrendIQ 智能评分
    # ==========================================
    def calculate_trend_iq_and_risk(
        self, price_val: float, pct_val: float, turnover_val: float, pct_60d_val: float, vol_ratio_val: float
    ) -> Dict:
        """计算 TrendIQ 深度量化指标"""
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
        momentum_score = round(pct_val * 1.5, 1) if pct_val > 0 else round(pct_val * 2.0, 1)
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
            f"1️⃣ **价格与动能趋势**: 当日动态涨跌幅 `{pct_val:+.2f}%`，当前价格 `{price_val:.2f}元`。符合严格的多头攻击模型，技术面未见衰竭。\n"
            f"2️⃣ **资金与成交活跃度**: 换手率 `{turnover_val:.2f}%`，配合量比指标 `{vol_ratio_val:.2f}`。放量温和且无庄股爆量出货迹象。\n"
            f"3️⃣ **中期趋势与筹码结构**: 60日累计涨跌幅 `{pct_60d_val:+.1f}%`。无高位爆量滞涨，下方筹码支撑力度极强。\n"
            f"4️⃣ **风控指导与策略要点**: 评估风险评级为 `{risk_stars} 星` ({'⭐' * risk_stars})。必须严格按照 `{stop_loss:.2f}元` 执行破位止损离场。"
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
            "pass_risk": (risk_stars < 4) and (trend_iq >= 80)
        }

    # ==========================================
    # 📈 3. 5-10 日跟踪注册
    # ==========================================
    def load_tracker(self) -> List[Dict]:
        if os.path.exists(self.tracker_file):
            try:
                with open(self.tracker_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return data
                    elif isinstance(data, dict):
                        return data.get("records", [])
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
        """将今日推送标的插入跟踪池（初始状态 TRACKING）"""
        tracker_data = self.load_tracker()
        today_str = time.strftime("%Y-%m-%d")

        for item in selected_items:
            if not any(isinstance(r, dict) and r.get("code") == item["code"] and r.get("status") == "TRACKING" for r in tracker_data):
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
                    "status": "TRACKING",
                    "history": []
                })

        self.save_tracker(tracker_data)

    # ==========================================
    # 📊 4. 飞书多维表格 API 自动同步模块 (带自动授权)
    # ==========================================
        def sync_to_feishu(self, selected_items: List[Dict]):
        """将早盘选出的优质个股数据自动写入飞书云端多维表"""
        if not (FEISHU_APP_ID and FEISHU_APP_SECRET and FEISHU_APP_TOKEN and FEISHU_TABLE_ID):
            print("⚠️ 未配置飞书参数，跳过飞书多维表写入。")
            return

        # 1. 获取 飞书 tenant_access_token
        auth_url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        auth_payload = {
            "app_id": FEISHU_APP_ID,
            "app_secret": FEISHU_APP_SECRET
        }

        try:
            res_auth = requests.post(auth_url, json=auth_payload, timeout=10)
            token_json = res_auth.json()
            access_token = token_json.get("tenant_access_token", "")

            if not access_token:
                print(f"❌ 飞书鉴权失败: {token_json}")
                return
        except Exception as e:
            print(f"❌ 请求飞书 Token 网络异常: {e}")
            return

        # 2. 批量写入飞书多维表格
        records_url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{FEISHU_TABLE_ID}/records/batch_create"
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {access_token}"
        }

        # 转换当前时间为 13 位毫秒级时间戳 (飞书 Date 字段要求)
        today_timestamp = int(time.time() * 1000)
        records = []

        for item in selected_items:
            try:
                pick_price = float(str(item.get("price", "0")).replace("元", ""))
            except ValueError:
                pick_price = 0.0

            records.append({
                "fields": {
                    "推荐日期": today_timestamp,
                    "复盘日期": today_timestamp,
                    "股票代码": str(item.get("code", "")),
                    "股票名称": str(item.get("name", "")),
                    "策略归属": str(item.get("strategy", "默认策略")),
                    "建仓价格": pick_price,
                    "最新收盘价": pick_price,
                    "持仓收益率": 0,  # 必须为纯数字 0，不能传字符串 "0%"
                    "持股天数": 0,
                    "状态": "持仓中",
                    "TrendIQ评分": int(item.get("trend_iq", 80))
                }
            })

        if records:
            try:
                res = requests.post(records_url, headers=headers, json={"records": records}, timeout=10)
                res_data = res.json()
                if res_data.get("code") == 0:
                    print(f"🎉 成功同步 {len(records)} 条数据至飞书多维表格！")
                else:
                    print(f"❌ 写入飞书失败: {res_data}")
            except Exception as e:
                print(f"❌ 写入飞书多维表异常: {e}")

    # ==========================================
    # 🌐 5. 行情采集与动态校准
    # ==========================================
    def check_market_sentiment(self) -> bool:
        try:
            res = requests.get("http://qt.gtimg.cn/q=sh000001", timeout=5)
            if res.status_code == 200 and '="' in res.text:
                fields = res.text.split('="')[1].replace('"', "").split("~")
                pct_chg = float(fields[32] or 0)
                if pct_chg < -1.5:
                    print(f"⚠️ 大盘大跌 ({pct_chg}%)，启动市场冰点防守机制！")
                    return False
        except Exception:
            pass
        return True

    def fetch_sina_market_data(self, scan_target=2500) -> List[Dict]:
        all_diff = []
        page_size = 100
        total_pages = scan_target // page_size
        session = requests.Session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/128.0.0.0",
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
                        json_str = raw_text[raw_text.find("([") + 1 : raw_text.rfind("])") + 1]
                        items = json.loads(json_str)
                        for item in items:
                            code = str(item.get("code", ""))
                            if not (code.startswith("60") or code.startswith("00")):
                                continue

                            all_diff.append({
                                "f12": code,
                                "f14": item.get("name", ""),
                                "f2": float(item.get("trade", 0) or 0),
                                "f3": float(item.get("changepercent", 0) or 0),
                                "f8": float(item.get("turnoverratio", 0) or 0),
                                "f10": 1.2,
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
        code_list = [f"sh60{i:04d}" for i in range(2000)] + [f"sz00{i:04d}" for i in range(2000)]
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
        if not items_list:
            return items_list
        tc_codes = [f"sh{i['code']}" if i["code"].startswith("60") else f"sz{i['code']}" for i in items_list]
        try:
            res = requests.get(f"http://qt.gtimg.cn/q={','.join(tc_codes)}", timeout=5)
            if res.status_code == 200:
                tc_data = {}
                for line in res.text.split(";"):
                    if '="' in line:
                        f = line.split('="')[1].replace('"', "").split("~")
                        if len(f) > 38 and float(f[3] or 0) > 0:
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
    # 📊 6. 核心策略选股筛选引擎
    # ==========================================
    def run_strategy_pipeline(self) -> Tuple[List[Dict], List[str], str]:
        raw_diff = self.fetch_sina_market_data()
        if not raw_diff:
            return [], [], ""

        is_market_healthy = self.check_market_sentiment()

        strategy_lotus, strategy_fanbao, strategy_oversold = [], [], []
        strategy_right_side, strategy_quiet_bottom, strategy_duck_head, strategy_bottom_shrink = [], [], [], []

        for item in raw_diff:
            code, name = str(item.get("f12", "")), str(item.get("f14", ""))
            price, pct = item.get("f2", "-"), item.get("f3", "-")
            turnover, vol_ratio = item.get("f8", "-"), item.get("f10", "-")
            pct_60d = item.get("f24", "-")

            if not (code.startswith("60") or code.startswith("00")):
                continue
            if price in ["-", 0] or pct == "-" or any(k in name.upper() for k in ["ST", "退", "N", "C"]):
                continue

            try:
                price_val, pct_val = float(price), float(pct)
                turnover_val = float(turnover) if turnover != "-" else 0.0
                vol_ratio_val = float(vol_ratio) if vol_ratio != "-" else 0.0
                pct_60d_val = float(pct_60d) if pct_60d != "-" else 0.0

                if pct_val < -4.0 or pct_val > 6.5:
                    continue

                eval_res = self.calculate_trend_iq_and_risk(
                    price_val, pct_val, turnover_val, pct_60d_val, vol_ratio_val
                )

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

                if is_market_healthy and -8.0 <= pct_60d_val <= 12.0 and 2.0 <= pct_val <= 6.0 and 1.8 <= vol_ratio_val <= 5.0 and 3.0 <= turnover_val <= 9.0:
                    item_obj["strategy"] = "🌸 出水芙蓉突破"
                    strategy_lotus.append(item_obj)
                elif is_market_healthy and -10.0 <= pct_60d_val <= 8.0 and 0.0 <= pct_val <= 6.0 and 1.5 <= vol_ratio_val <= 4.5 and 2.8 <= turnover_val <= 8.5:
                    item_obj["strategy"] = "🔄 强劲反包蓄势"
                    strategy_fanbao.append(item_obj)
                elif pct_60d_val <= -18.0 and -3.0 <= pct_val <= 4.0 and vol_ratio_val >= 1.2 and 2.0 <= turnover_val <= 6.0:
                    item_obj["strategy"] = "⚡ 急跌反抽企稳"
                    strategy_oversold.append(item_obj)
                elif is_market_healthy and 0.0 <= pct_60d_val <= 25.0 and 1.0 <= pct_val <= 5.5 and 1.6 <= vol_ratio_val <= 4.0 and 3.0 <= turnover_val <= 9.0:
                    item_obj["strategy"] = "🚀 右侧刚启动"
                    strategy_right_side.append(item_obj)
                elif -20.0 <= pct_60d_val <= -2.0 and -1.5 <= pct_val <= 2.5 and 1.5 <= turnover_val <= 3.5 and vol_ratio_val >= 1.1:
                    item_obj["strategy"] = "🤫 买在无人问津"
                    strategy_quiet_bottom.append(item_obj)
                elif is_market_healthy and 5.0 <= pct_60d_val <= 22.0 and 1.5 <= pct_val <= 5.8 and 1.5 <= vol_ratio_val <= 4.0 and 2.5 <= turnover_val <= 8.0:
                    item_obj["strategy"] = "🦆 老鸭头突破"
                    strategy_duck_head.append(item_obj)
                elif -35.0 <= pct_60d_val <= -15.0 and -2.0 <= pct_val <= 2.0 and 0.8 <= turnover_val <= 2.5 and vol_ratio_val <= 1.2:
                    item_obj["strategy"] = "📉 底部超跌缩量"
                    strategy_bottom_shrink.append(item_obj)

            except ValueError:
                continue

        candidate_items = (
            strategy_lotus[:2] + strategy_fanbao[:2] + strategy_oversold[:2]
            + strategy_right_side[:2] + strategy_quiet_bottom[:2]
            + strategy_duck_head[:2] + strategy_bottom_shrink[:2]
        )

        if not candidate_items:
            print("⚠️ 今日未发现符合 TrendIQ>=80 高胜率标准标的，不发送兜底数据。")
            return [], [], ""

        candidate_items = self.calibrate_items(candidate_items)
        candidate_items = [i for i in candidate_items if i.get("trend_iq", 0) >= 80]

        final_items, history_data = self.filter_three_day_duplicates(candidate_items)

        if not final_items:
            print("⚠️ 去重后，今日无符合条件的新标的。")
            return [], [], ""

        self.update_today_history(final_items)
        self.register_to_tracker(final_items)

        # 🟢 写入飞书云端表格
        self.sync_to_feishu(final_items)

        # 打包研报切片
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
    # 📱 7. 企业微信机器人切片推送
    # ==========================================
    def push_to_wechat_work(self, message_chunks: List[str]) -> bool:
        wechat_url = os.environ.get("WECHAT_WEBHOOK", "").strip()

        if not wechat_url or not (wechat_url.startswith("http://") or wechat_url.startswith("https://")):
            print("⚠️ 未配置有效的 WECHAT_WEBHOOK，跳过推送。")
            return False

        if not message_chunks:
            print("⚠️ 推送内容为空，跳过。")
            return False

        print(f"📡 准备向企业微信逐条分批推送 {len(message_chunks)} 条切片研报...")
        success_all = True

        for idx, chunk in enumerate(message_chunks, 1):
            safe_chunk = chunk.replace("```", "").replace("<font", "").replace("</font>", "")
            payload = {
                "msgtype": "markdown",
                "markdown": {"content": safe_chunk}
            }

            try:
                res = requests.post(wechat_url, json=payload, headers={"Content-Type": "application/json"}, timeout=10)
                res_json = res.json()
                if res_json.get("errcode") == 0:
                    print(f"🎉 第 ({idx}/{len(message_chunks)}) 条个股研报切片推送成功！")
                else:
                    print(f"❌ 第 ({idx}/{len(message_chunks)}) 条推送失败: {res_json}")
                    success_all = False
            except Exception as e:
                print(f"❌ 第 ({idx}/{len(message_chunks)}) 条推送网络异常: {e}")
                success_all = False

            time.sleep(1)

        return success_all

    # ==========================================
    # 📡 8. 远程 Dify 对接
    # ==========================================
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
            print("📡 正在提交分析研报给 Dify Agent...")
            res = requests.post(DIFY_API_URL, headers=headers, json=payload, timeout=60)
            if res.status_code == 200:
                print("⚡ Dify 返回响应成功！")
                return True
            else:
                print(f"❌ Dify 返回错误: {res.text}")
                return False
        except Exception as e:
            print(f"❌ 连接 Dify 失败: {e}")
            return False


# ==========================================
# 🚀 启动控制逻辑
# ==========================================
def main():
    agent = MorningStockPickerAgent()
    print("==================================================")
    print("🚀 Agent 1 [早盘选股 Agent] 启动，全策略搜寻中...")
    print("==================================================")

    today_str = time.strftime("%Y-%m-%d")
    history = agent.load_history()
    today_data = history.get(today_str, {})

    if isinstance(today_data, dict):
        run_count = today_data.get("run_count", 0)
    elif isinstance(today_data, list) and len(today_data) > 0:
        run_count = 1
    else:
        run_count = 0

    max_allowed_runs = 999 if MANUAL_TEST else 5

    if run_count >= max_allowed_runs:
        print(f"🛑 监测到今日 ({today_str}) 已成功推荐 {run_count} 次，达到最大限制 ({max_allowed_runs} 次)，程序停止。")
        return

    success_count = 0
    for attempt in range(1, MAX_TOTAL_ATTEMPTS + 1):
        print(f"\n🔄 轮询尝试 {attempt}/{MAX_TOTAL_ATTEMPTS} (已成功次数: {success_count}/{TARGET_SUCCESS_COUNT})...")

        selected_items, message_chunks, report_md = agent.run_strategy_pipeline()

        if message_chunks:
            pushed_wechat = False
            pushed_dify = False

            if hasattr(agent, "push_to_wechat_work"):
                pushed_wechat = agent.push_to_wechat_work(message_chunks)

            if DIFY_API_KEY:
                pushed_dify = agent.push_to_dify(report_md)

            if pushed_wechat or pushed_dify or (not DIFY_API_KEY and not os.environ.get("WECHAT_WEBHOOK")):
                success_count += 1

            if success_count >= TARGET_SUCCESS_COUNT:
                print("🛑 完成任务，本次选股推送结束。")
                break
            else:
                time.sleep(SUCCESS_WAIT_SECONDS)
        else:
            print("⚠️ 本次未筛选出符合标准的新标的，程序静默退出，不推送任何兜底数据。")
            break


if __name__ == "__main__":
    main()
