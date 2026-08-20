"""
Agent 2: 晚间复盘 Agent (支持 5-10 日观察期逐日追踪 + 策略优劣自我迭代)
"""

import json
import os
import time
from datetime import datetime
import requests

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

    def fetch_market_quotes(self, codes: list) -> dict:
        """获取盘后最新行情（含收盘价、最高价、最低价、今日涨跌幅）"""
        if not codes:
            return {}
        quotes = {}
        headers = {"User-Agent": "Mozilla/5.0"}
        secids = [f"0.{c}" if c.startswith("00") else f"1.{c}" for c in set(codes)]
        url = "http://push2.eastmoney.com/api/qt/ulist/get"
        params = {"fltt": "2", "fields": "f12,f14,f2,f3,f15,f16", "secids": ",".join(secids)}
        
        try:
            res = requests.get(url, headers=headers, params=params, timeout=10)
            if res.status_code == 200:
                for item in res.json().get("data", {}).get("diff", []):
                    quotes[str(item["f12"])] = {
                        "close": float(item.get("f2", 0.0)),
                        "pct": float(item.get("f3", 0.0)),
                        "high": float(item.get("f15", 0.0)),
                        "low": float(item.get("f16", 0.0)),
                    }
        except Exception as e:
            print(f"❌ 获取收盘价失败: {e}")
        return quotes

    def generate_review_report(self) -> str:
        history = self.load_history()
        if not history:
            return "⚠️ 当前无选股历史记录，请确认早盘选股脚本已运行并提交 daily_picks_history.json 到 GitHub 仓库。"

        today_dt = datetime.now()
        tracked_records = []
        all_codes = []

        # 1. 过滤出 5-10 日观察期内的推荐标的
        for date_str, picks in history.items():
            pick_dt = datetime.strptime(date_str, "%Y-%m-%d")
            days_held = (today_dt - pick_dt).days
            if 0 <= days_held <= 10:  # 5-10 日观察期
                for p in picks:
                    p["pick_date"] = date_str
                    p["days_held"] = days_held
                    tracked_records.append(p)
                    all_codes.append(p["code"])

        if not tracked_records:
            return "📊 暂无 5-10 日观察期内的历史标的。"

        quotes = self.fetch_market_quotes(all_codes)

        # 2. 逐日复盘与状态评估
        rows = []
        strategy_stats = {}  # 用于自适应优化

        for item in tracked_records:
            code = item["code"]
            q = quotes.get(code, {})
            c_price = q.get("close", 0.0)
            day_pct = q.get("pct", 0.0)
            pick_price = item.get("pick_price", c_price)
            stop_loss = item.get("stop_loss", 0.0)
            target_price = item.get("target_price", 0.0)
            strategy = item.get("strategy", "默认策略")

            # 统计累计涨跌幅与走势诊断
            cum_pct = ((c_price - pick_price) / pick_price * 100) if pick_price else 0.0
            
            # 状态评估 (成功/触发止损/持仓观察)
            status = "🟢 正常持有"
            if c_price <= stop_loss and stop_loss > 0:
                status = "🔴 触发止损"
                reason = "跌破止损位"
            elif c_price >= target_price and target_price > 0:
                status = "🚀 达成目标"
                reason = "突破目标位"
            else:
                reason = "区间震荡" if abs(cum_pct) < 3 else ("趋势上涨" if cum_pct > 0 else "受压回落")

            rows.append(
                f"| {item['pick_date']} | `{code}` | **{item['name']}** | {c_price}元 | {day_pct:+.2f}% | {cum_pct:+.2f}% | {stop_loss}元 | {status} |"
            )

            # 归因数据收集（用于策略迭代）
            if strategy not in strategy_stats:
                strategy_stats[strategy] = {"total": 0, "win": 0}
            strategy_stats[strategy]["total"] += 1
            if cum_pct > 0:
                strategy_stats[strategy]["win"] += 1

        # 3. 自动诊断与策略技能进化逻辑
        optimizations = []
        for strat, stat in strategy_stats.items():
            win_rate = (stat["win"] / stat["total"]) * 100 if stat["total"] else 0
            if win_rate < 40:
                optimizations.append(f"⚠️ **{strat}** 胜率偏低 ({win_rate:.0f}%)：建议调高量比门槛（由 1.5 升至 2.0），并提高换手率要求。")
            else:
                optimizations.append(f"✅ **{strat}** 表现稳定 (胜率 {win_rate:.0f}%)：继续保持现有选股参数。")

        table_text = (
            "| 推荐日期 | 代码 | 名称 | 最新收盘价 | 今日涨跌 | 累计收益 | 止损位 | 当前状态 |\n"
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n" + "\n".join(rows)
        )
        opt_text = "\n".join(optimizations)

        report = (
            "🌙 **【沪深主板 - 晚间复盘与 5-10 日观察期跟踪】**\n\n"
            f"### 📋 5-10日跟踪标的表现明细\n{table_text}\n\n---\n"
            f"### ⚙️ 选股策略自动诊断与进化建议\n{opt_text}"
        )
        return report

    def push_wechat(self, msg: str):
        url = os.environ.get("WECHAT_WEBHOOK", "").strip()
        if url:
            requests.post(url, json={"msgtype": "markdown", "markdown": {"content": msg}})


def main():
    agent = EveningReviewAgent()
    msg = agent.generate_review_report()
    print(msg)
    agent.push_wechat(msg)


if __name__ == "__main__":
    main()
