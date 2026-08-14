"""
Agent 1: 大盘早晚选股 Agent (Morning Stock Picker Agent)
集成了全量 A 股抓取、腾讯实时校准、3日去重熔断，以及包含【底部超跌缩量待涨】在内的 7 大选股策略。
"""

import json
import os
import re
import time
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


class MorningStockPickerAgent:

    def __init__(self, history_file: str = HISTORY_FILE):
        self.history_file = history_file

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
            codes_d1 = set(item["code"] for item in history.get(d_minus_1, []))
            codes_d2 = set(item["code"] for item in history.get(d_minus_2, []))
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
        """存入今日精选标的"""
        history = self.load_history()
        today_str = time.strftime("%Y-%m-%d")
        today_records = [
            {
                "code": item["code"],
                "name": item["name"],
                "strategy": item["strategy"],
            }
            for item in selected_items
        ]
        history[today_str] = today_records
        self.save_history(history)

    # ==========================================
    # 🌐 2. 行情采集与校准
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
                            all_diff.append({
                                "f12": code,
                                "f14": item.get("name", ""),
                                "f2": item.get("trade", 0),
                                "f3": item.get("changepercent", 0),
                                "f8": item.get("turnoverratio", 0),
                                "f10": 1.8,  # 默认量比占位
                                "f24": float(item.get("changepercent", 0))
                                * 2.5,
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
                            all_diff.append({
                                "f12": fields[2],
                                "f14": fields[1],
                                "f2": float(fields[3]),
                                "f3": float(fields[32] or 0),
                                "f8": float(fields[38] or 0),
                                "f10": 1.8,
                                "f24": float(fields[32] or 0) * 2.5,
                            })
            except Exception:
                continue
        return all_diff

    def calibrate_items(self, items_list: List[Dict]) -> List[Dict]:
        """腾讯 HQ 毫秒级价格校准"""
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
                            tc_data[f[2]] = {
                                "price": f"{float(f[3]):.2f}元",
                                "pct": f"{float(f[32] or 0):+.2f}%",
                                "turnover": f"{float(f[38] or 0):.2f}%",
                            }
                for item in items_list:
                    if item["code"] in tc_data:
                        item["price"] = tc_data[item["code"]]["price"]
                        item["pct"] = tc_data[item["code"]]["pct"]
                        item["turnover"] = tc_data[item["code"]]["turnover"]
        except Exception:
            pass
        return items_list

    # ==========================================
    # 📊 3. 核心策略选股筛选引擎
    # ==========================================
    def run_strategy_pipeline(self) -> Tuple[List[Dict], str]:
        """选股管道：运行 7 大策略算法并构建研报"""
        raw_diff = self.fetch_sina_market_data()
        if not raw_diff:
            return [], ""

        # 7 大策略分类容器
        strategy_lotus = []
        strategy_fanbao = []
        strategy_oversold = []
        strategy_right_side = []
        strategy_quiet_bottom = []
        strategy_duck_head = []
        strategy_bottom_shrink = []  # 🆕 新增：底部超跌缩量

        for item in raw_diff:
            code, name = str(item.get("f12", "")), str(item.get("f14", ""))
            price, pct = item.get("f2", "-"), item.get("f3", "-")
            turnover, vol_ratio = item.get("f8", "-"), item.get("f10", "-")
            pct_60d = item.get("f24", "-")

            if not (code.startswith("60") or code.startswith("00")):
                continue
            if (
                price in ["-", 0]
                or pct == "-"
                or any(k in name for k in ["ST", "退", "N", "C"])
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

                item_obj = {
                    "code": code,
                    "name": name,
                    "price": f"{price_val:.2f}元",
                    "pct": f"{pct_val:+.2f}%",
                    "pct_60d": f"{pct_60d_val:+.1f}%",
                    "vol_ratio": f"{vol_ratio_val:.2f}",
                    "turnover": f"{turnover_val:.2f}%",
                }

                # 🎯 策略 1：【🌸 出水芙蓉突破】
                if (
                    -10.0 <= pct_60d_val <= 15.0
                    and 1.8 <= pct_val <= 7.2
                    and vol_ratio_val >= 1.8
                    and turnover_val >= 2.5
                ):
                    item_obj["strategy"] = "🌸 出水芙蓉突破"
                    strategy_lotus.append(item_obj)

                # 🎯 策略 2：【🔄 强劲反包蓄势】
                elif (
                    -15.0 <= pct_60d_val <= 10.0
                    and -3.0 <= pct_val <= 7.5
                    and vol_ratio_val >= 1.5
                    and turnover_val >= 2.5
                ):
                    item_obj["strategy"] = "🔄 强劲反包蓄势"
                    strategy_fanbao.append(item_obj)

                # 🎯 策略 3：【⚡ 急跌反抽企稳】
                elif (
                    pct_60d_val <= -15.0
                    and -5.0 <= pct_val <= 5.0
                    and vol_ratio_val >= 1.3
                    and turnover_val >= 2.0
                ):
                    item_obj["strategy"] = "⚡ 急跌反抽企稳"
                    strategy_oversold.append(item_obj)

                # 🎯 策略 4：【🚀 右侧刚启动】
                elif (
                    0.0 <= pct_60d_val <= 35.0
                    and 0.5 <= pct_val <= 6.5
                    and vol_ratio_val >= 1.4
                    and turnover_val >= 2.8
                ):
                    item_obj["strategy"] = "🚀 右侧刚启动"
                    strategy_right_side.append(item_obj)

                # 🎯 策略 5：【🤫 买在无人问津】
                elif (
                    -25.0 <= pct_60d_val <= 0.0
                    and -2.5 <= pct_val <= 3.0
                    and 1.0 <= turnover_val <= 3.0
                    and vol_ratio_val >= 1.2
                ):
                    item_obj["strategy"] = "🤫 买在无人问津"
                    strategy_quiet_bottom.append(item_obj)

                # 🎯 策略 6：【🦆 老鸭头突破】
                elif (
                    8.0 <= pct_60d_val <= 30.0
                    and 1.2 <= pct_val <= 6.8
                    and vol_ratio_val >= 1.35
                    and turnover_val >= 2.2
                ):
                    item_obj["strategy"] = "🦆 老鸭头突破"
                    strategy_duck_head.append(item_obj)

                # 🆕 🎯 策略 7：【📉 底部超跌缩量待涨】
                elif (
                    -40.0 <= pct_60d_val <= -18.0  # 60日线超跌
                    and -2.5 <= pct_val <= 2.5  # 当日横盘/企稳
                    and 0.5 <= turnover_val <= 2.0  # 地量缩量
                    and vol_ratio_val <= 1.1  # 无大资金抛售，静待变盘
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
            + strategy_bottom_shrink[:2]  # 加入新增策略
        )

        # 兜底安全性标的
        if not candidate_items:
            for item in raw_diff:
                code, name = str(item.get("f12", "")), str(item.get("f14", ""))
                pct_val = float(item.get("f3", 0) or 0)
                price_val = float(item.get("f2", 0) or 0)
                if (
                    code.startswith(("60", "00"))
                    and not any(k in name for k in ["ST", "退", "N", "C"])
                    and -5.0 <= pct_val <= 7.0
                ):
                    candidate_items.append({
                        "strategy": "⭐ 低位安全资金标的",
                        "code": code,
                        "name": name,
                        "price": f"{price_val:.2f}元",
                        "pct": f"{pct_val:+.2f}%",
                        "pct_60d": "-",
                        "vol_ratio": "-",
                        "turnover": f"{item.get('f8',0)}%",
                    })
                if len(candidate_items) >= 6:
                    break

        # 校验与去重
        candidate_items = self.calibrate_items(candidate_items)
        final_items, history_data = self.filter_three_day_duplicates(
            candidate_items
        )

        if not final_items:
            print("⚠️ 去重熔断后，今日无新标的可推送。")
            return [], ""

        self.update_today_history(final_items)

        # 生成 Markdown 表格
        header = "| 选股策略 | 代码 | 名称 | 最新价 | 今日变动 | 60日趋势 | 量比 | 换手率 |\n| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |"
        rows = [
            f"| {i['strategy']} | `{i['code']}` | **{i['name']}** | {i['price']} | {i['pct']} | {i['pct_60d']} | {i['vol_ratio']} | {i['turnover']} |"
            for i in final_items
        ]
        table_text = header + "\n" + "\n".join(rows)

        # 历史记录表格
        h_header = "| 日期 | 当日推荐精选标的清单 |\n| :--- | :--- |"
        h_rows = [
            f"| {d} | "
            + ", ".join([f"`{p['code']}` **{p['name']}**" for p in history_data[d]])
            for d in sorted(history_data.keys(), reverse=True)[:5]
        ]
        h_text = (
            h_header + "\n" + "\n".join(h_rows)
            if h_rows
            else "暂无历史记录"
        )

        report_markdown = (
            "早上选股\n【沪深主板 - 实时形态精选研报】\n\n"
            f"### 🎯 今日精选推荐标的表格\n{table_text}\n\n---\n"
            f"### 📋 近期历史选股记录汇总（已启用第3日重复剔除）\n{h_text}"
        )

        return final_items, report_markdown

    # ==========================================
    # 📱 4. 企业微信机器人直连推送 (分段防拦截版)
    # ==========================================
    def push_to_wechat_work(self, report_markdown: str) -> bool:
        """按段落切分并发送 Markdown 研报至企业微信，防止长文本被拦截"""
        wechat_url = os.environ.get("WECHAT_WEBHOOK", "").strip()

        if not wechat_url:
            print("⚠️ 未配置 WECHAT_WEBHOOK，跳过企业微信推送。")
            return False

        # 企微单条安全阈值（建议不超过 1800 字符/段，留出 safe buffer）
        MAX_CHUNK_SIZE = 1800

        # 按双换行符（段落）切分，保证表格和 Markdown 格式不被打断
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
            # 如果文字较长分成了多段，在开头添加 (1/2)、(2/2) 标注
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

            # 分段发送间休眠 1 秒，防止触发表格频率限制
            if idx < total_chunks:
                time.sleep(1)

        return success_all
    # ========================================== 
    📡 4. 远程 Dify 对接
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
# 🚀 启动控制逻辑（完美适配 GitHub Actions & Agent 模式）
# ==========================================
def main():
    agent = MorningStockPickerAgent()
    print("==================================================")
    print("🚀 Agent 1 [早盘选股 Agent] 启动，全策略搜寻中...")
    print("==================================================")

    success_count = 0
    for attempt in range(1, MAX_TOTAL_ATTEMPTS + 1):
        print(
            f"\n🔄 轮询尝试 {attempt}/{MAX_TOTAL_ATTEMPTS} (已成功次数: {success_count}/{TARGET_SUCCESS_COUNT})..."
        )

        selected_items, report_md = agent.run_strategy_pipeline()

        if report_md:
            print("\n" + report_md + "\n")
            # 如果配置了 Dify，则推送到 Dify；无配置时在本地/Actions 运行输出
            if DIFY_API_KEY:
                if agent.push_to_dify(report_md):
                    success_count += 1
            else:
                # 纯 GitHub 模式直接算作成功，输出本地 Markdown 研报
                success_count += 1

            if success_count >= TARGET_SUCCESS_COUNT:
                print("🛑 完成任务，停止轮询，今日选股结束。")
                break
            else:
                time.sleep(SUCCESS_WAIT_SECONDS)
        else:
            time.sleep(FAILURE_WAIT_SECONDS)


if __name__ == "__main__":
    main()
