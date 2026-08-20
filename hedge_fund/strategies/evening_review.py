"""
Agent 2: 晚间复盘 Agent (Evening Review Agent)
自动读取早盘选股历史，抓取最新收盘价，计算 5-10 日观察期收益，并推送企微。
"""

import json
import os
import time
from datetime import datetime
import requests

# 锁定相对路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(BASE_DIR, "daily_picks_history.json")


class EveningReviewAgent:

    def __init__(self, history_file: str = HISTORY_FILE):
        self.history_file = history_file

    def load_history(self) -> dict:
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"❌ 读取历史选股文件失败: {e}")
        return {}

    def fetch_closing_prices(self, codes: list) -> dict:
        """从东方财富获取最新收盘价格"""
        if not codes:
            return {}
        
        prices = {}
        headers = {"User-Agent": "Mozilla/5.0"}
        url = "http://push2.eastmoney.com/api/qt/ulist/get"
        secids = [f"0.{c}" if c.startswith("00") else f"1.{c}" for c in set(codes)]
        params = {
            "fltt": "2",
            "fields": "f12,f14,f2,f3",
            "secids": ",".join(secids)
        }
        
        try:
            res = requests.get(url, headers=headers, params=params, timeout=10)
            if res.status_code == 200:
                diff = res.json().get("data", {}).get("diff", [])
                for item in diff:
                    prices[str(item["f12"])] = {
                        "name": item.get("f14", ""),
                        "close_price": item.get("f2", 0.0),
                        "pct_change": item.get("f3", 0.0)
                    }
        except Exception as e:
            print(f"❌ 获取收盘价失败: {e}")
            
        return prices

    def generate_review_report(self) -> str:
        history = self.load_history()
        if not history:
            return "⚠️ 当前无选股历史记录，无法进行 5-10 日复盘。"

        all_tracked_codes = []
        today_date = datetime.now()

        # 筛选近 10 个自然日内的推荐标的
        recent_picks = {}
        for date_str, picks in history.items():
            pick_date = datetime.strptime(date_str, "%Y-%m-%d")
            days_diff = (today_date - pick_date).days
            if 0 <= days_diff <= 10:  # 5-10日观察期
                recent_picks[date_str] = picks
                for p in picks:
                    all_tracked_codes.append(p["code"])

        if not all_tracked_codes:
            return "📊 近 10 日内暂无需要跟踪的标的。"

        closing_data = self.fetch_closing_prices(all_tracked_codes)

        # 构建 Markdown 复盘研报
        report_lines = [
            "🌙 **【沪深主板 - 晚间复盘与 5-10 日观察期跟踪】**",
            f"📅 复盘时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n",
            "| 推荐日期 | 代码 | 名称 | 今日收盘价 | 今日涨跌幅 | 策略定位 |",
            "| :--- | :--- | :--- | :--- | :--- | :--- |"
        ]

        for date_str in sorted(recent_picks.keys(), reverse=True):
            for item in recent_picks[date_str]:
                code = item["code"]
                mkt_info = closing_data.get(code, {})
                c_price = f"{mkt_info.get('close_price', '-')}元"
                pct = f"{mkt_info.get('pct_change', 0.0):+.2f}%"
                report_lines.append(
                    f"| {date_str} | `{code}` | **{item['name']}** | {c_price} | {pct} | {item.get('strategy', '选股推荐')} |"
                )

        return "\n".join(report_lines)

    def push_to_wechat(self, markdown_text: str) -> bool:
        wechat_url = os.environ.get("WECHAT_WEBHOOK", "").strip()
        if not wechat_url:
            print("⚠️ 未设置 WECHAT_WEBHOOK，取消微信推送。")
            return False

        payload = {
            "msgtype": "markdown",
            "markdown": {"content": markdown_text}
        }
        try:
            res = requests.post(wechat_url, json=payload, timeout=10)
            if res.json().get("errcode") == 0:
                print("🎉 晚间复盘已成功推送至微信！")
                return True
            print(f"❌ 推送微信失败: {res.text}")
        except Exception as e:
            print(f"❌ 推送异常: {e}")
        return False


def main():
    agent = EveningReviewAgent()
    print("==================================================")
    print("🚀 Agent 2 [晚间复盘 Agent] 启动...")
    print("==================================================")
    report = agent.generate_review_report()
    print("\n" + report + "\n")
    agent.push_to_wechat(report)


if __name__ == "__main__":
    main()
