import os
import json
import time
import re
import requests
from datetime import datetime, timedelta

# 🎯 严格匹配根目录数据文件
HISTORY_FILE = "daily_picks_history.json"
TRACKER_FILE = "portfolio_tracker.json"
POSTMORTEM_FILE = "skills_postmortem.md"

class EveningReviewAgent:
    def __init__(
        self,
        history_file: str = HISTORY_FILE,
        tracker_file: str = TRACKER_FILE,
        postmortem_file: str = POSTMORTEM_FILE
    ):
        self.history_file = history_file
        self.tracker_file = tracker_file
        self.postmortem_file = postmortem_file

    def load_tracker(self) -> list:
        """从根目录读取跟踪池，兼容列表与字典结构"""
        if os.path.exists(self.tracker_file):
            try:
                with open(self.tracker_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return data
                    elif isinstance(data, dict):
                        # 兼容有 metadata 的 json 结构，提取真正的股票列表
                        return data.get("records", data.get("tracker", []))
            except Exception as e:
                print(f"❌ 读取跟踪池失败: {e}")
        return []

    def save_tracker(self, tracker_data: list):
        """保存更新后的跟踪池"""
        try:
            with open(self.tracker_file, "w", encoding="utf-8") as f:
                json.dump(tracker_data, f, ensure_ascii=False, indent=2)
            print("💾 【晚盘复盘】跟踪池数据已更新保存！")
        except Exception as e:
            print(f"❌ 保存跟踪池失败: {e}")

    def fetch_closing_quotes(self, stock_codes: list) -> dict:
        """从腾讯 API 获取收盘实时价格"""
        # 🛡️ 严格过滤非 6 位数字代码（防止把 run_count/records 当成股票）
        valid_codes = [str(c) for c in stock_codes if re.match(r"^\d{6}$", str(c))]
        if not valid_codes:
            return {}

        tc_codes = [f"sh{c}" if c.startswith("60") or c.startswith("68") else f"sz{c}" for c in valid_codes]
        quotes = {}
        try:
            url = f"http://qt.gtimg.cn/q={','.join(tc_codes)}"
            res = requests.get(url, timeout=10)
            if res.status_code == 200:
                for line in res.text.split(";"):
                    if '="' in line:
                        parts = line.split('="')
                        symbol = parts[0].strip().replace("v_sh", "").replace("v_sz", "")
                        fields = parts[1].replace('"', "").split("~")
                        if len(fields) > 3:
                            code = fields[2]
                            close_p = float(fields[3]) if fields[3] else 0.0
                            if close_p > 0:
                                quotes[code] = close_p
        except Exception as e:
            print(f"⚠️ 获收盘行情失败: {e}")
        return quotes

    def run_evening_review(self) -> str:
        tracker = self.load_tracker()
        today_str = datetime.now().strftime("%Y-%m-%d")

        # 🛡️ 过滤出真正处于跟踪期（未结案）且代码合规的股票
        active_items = [
            item for item in tracker 
            if isinstance(item, dict) 
            and item.get("status") in ["TRACKING", "HOLD", "ACTIVE", None]
            and re.match(r"^\d{6}$", str(item.get("code", "")))
        ]

        if not active_items:
            return f"🌆 **【晚盘复盘与持仓跟踪】({today_str})**\n-----------------------------------\n当前无处于活跃观察期的持仓标的。"

        active_codes = [item["code"] for item in active_items]
        quotes = self.fetch_closing_quotes(active_codes)

        review_logs = []
        table_rows = [
            "| 日期 | 代码 | 名称 | 建仓价 ➔ 今日收盘 | 今日涨跌 | 累计收益 | 目标/止损 | 状态诊断 |",
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |"
        ]

        for item in active_items:
            code = item["code"]
            name = item.get("name", "未知")
            entry_p = float(item.get("entry_price", item.get("buy_price", 0)))
            target_p = float(item.get("target_price", entry_p * 1.10))
            stop_p = float(item.get("stop_loss", entry_p * 0.95))
            days_tracked = item.get("days_tracked", 0) + 1
            item["days_tracked"] = days_tracked

            if code not in quotes or entry_p <= 0:
                table_rows.append(f"| {today_str} (T+{days_tracked}) | `{code}` | {name} | `{entry_p:.2f}` ➔ `N/A` | `0.00%` | `0.00%` | `{target_p:.2f}/{stop_p:.2f}` | ⌛ 行情延迟 |")
                continue

            close_p = quotes[code]
            total_ret = round((close_p - entry_p) / entry_p * 100, 2)
            today_chg = round(float(item.get("today_change", 0)), 2) # 可选今日涨跌

            # 🎯 核心需求 1：破位止损后立即结案，次日开始停止记录
            status_desc = ""
            if close_p <= stop_p:
                item["status"] = "CLOSED"  # 标记为已结案，下次复盘直接跳过
                item["close_reason"] = "破位止损"
                item["close_date"] = today_str
                status_desc = "🔴 触发止损 (停止后续记录)"
                review_logs.append(f"🚨 **破位止损警报**: **{name}** (`{code}`) 触及止损价 `{stop_p}元`，今日收盘 `{close_p}元` (`{total_ret}%`)，已移出跟踪池。")

            elif close_p >= target_p:
                item["status"] = "CLOSED"  # 止盈结案，下次跳过
                item["close_reason"] = "止盈达标"
                item["close_date"] = today_str
                status_desc = "🎉 止盈达标 (成功结案)"
                review_logs.append(f"🎉 **止盈达标喜报**: **{name}** (`{code}`) 达标目标价 `{target_p}元`，今日收盘 `{close_p}元` (`+{total_ret}%`)，成功结案。")

            elif days_tracked >= 10:
                item["status"] = "CLOSED"  # 10天满期结案
                item["close_reason"] = "观察期满"
                item["close_date"] = today_str
                status_desc = "📌 满10天移出"
                review_logs.append(f"📌 **观察期满**: **{name}** (`{code}`) 已跟踪 10 个交易日，累计收益 `{total_ret}%`，退出跟踪。")

            else:
                item["status"] = "TRACKING"  # 继续跟踪
                status_desc = "🔄 震荡持仓"

            ret_sign = f"+{total_ret}%" if total_ret > 0 else f"{total_ret}%"
            table_rows.append(
                f"| {today_str} (T+{days_tracked}) | `{code}` | {name} | `{entry_p:.2f}` ➔ `{close_p:.2f}` | `{today_chg}%` | `{ret_sign}` | `{target_p:.2f}/{stop_p:.2f}` | {status_desc} |"
            )

        # 保存更新后的 JSON，确保已止损标的状态变成 CLOSED
        self.save_tracker(tracker)

        # 拼装推送内容
        report_md = f"🌆 **【晚盘收盘复盘与持仓跟踪】({today_str})**\n\n"
        report_md += "\n".join(table_rows) + "\n\n"
        if review_logs:
            report_md += "--- \n**📋 盘后触发与归因警报**:\n" + "\n".join(review_logs)

        return report_md

    def push_to_wechat(self, content: str):
        wechat_url = os.environ.get("WECHAT_WEBHOOK", "").strip()
        if not wechat_url:
            print("⚠️ 未配置 WECHAT_WEBHOOK，跳过推送。")
            return
        payload = {"msgtype": "markdown", "markdown": {"content": content}}
        try:
            res = requests.post(wechat_url, json=payload, timeout=10)
            if res.json().get("errcode") == 0:
                print("🎉 晚盘复盘报告已成功推送至企业微信！")
        except Exception as e:
            print(f"❌ 推送失败: {e}")

def main():
    agent = EveningReviewAgent()
    report = agent.run_evening_review()
    print(report)
    agent.push_to_wechat(report)

if __name__ == "__main__":
    main()
