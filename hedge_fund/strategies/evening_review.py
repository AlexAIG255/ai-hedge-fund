"""
Agent 2: 晚间复盘 Agent (支持 5-10 日观察期逐日跟踪 + 个股胜败归因 + 策略多日累积归因与进化)
"""

import json
import os
import time
from datetime import datetime
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 如果 daily_picks_history.json 在项目根目录，可向上调整路径；否则保持 BASE_DIR 对应路径
HISTORY_FILE = os.path.join(BASE_DIR, "daily_picks_history.json")
POSTMORTEM_FILE = os.path.join(BASE_DIR, "skills_postmortem.md")


class EveningReviewAgent:

    # 自动寻找根目录或当前目录下的 daily_picks_history.json
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.abspath(os.path.join(CURRENT_DIR, "../..")) # 向上退两级到根目录

    # 优先读取根目录文件
HISTORY_FILE = os.path.join(ROOT_DIR, "daily_picks_history.json")

         if not os.path.exists(HISTORY_FILE):
    # 备选：当前脚本同级目录
    HISTORY_FILE = os.path.join(CURRENT_DIR, "daily_picks_history.json")


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
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        # 0.表示深市，1.表示沪市
        secids = [f"0.{c}" if (c.startswith("00") or c.startswith("30")) else f"1.{c}" for c in set(codes)]
        url = "http://push2.eastmoney.com/api/qt/ulist/get"
        params = {"fltt": "2", "fields": "f12,f14,f2,f3,f15,f16", "secids": ",".join(secids)}
        
        try:
            res = requests.get(url, headers=headers, params=params, timeout=10)
            if res.status_code == 200:
                data = res.json().get("data")
                if data and "diff" in data:
                    for item in data["diff"]:
                        quotes[str(item["f12"])] = {
                            "close": float(item.get("f2", 0.0) or 0.0),
                            "pct": float(item.get("f3", 0.0) or 0.0),
                            "high": float(item.get("f15", 0.0) or 0.0),
                            "low": float(item.get("f16", 0.0) or 0.0),
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
            try:
                pick_dt = datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                continue
            days_held = (today_dt - pick_dt).days
            if 0 <= days_held <= 10:  # 5-10 日观察期
                for p in picks:
                    # 复制字典避免修改原历史
                    item = dict(p)
                    item["pick_date"] = date_str
                    item["days_held"] = days_held
                    tracked_records.append(item)
                    all_codes.append(item["code"])

        if not tracked_records:
            return "📊 暂无 5-10 日观察期内的历史标的。"

        quotes = self.fetch_market_quotes(all_codes)

        # 2. 逐日复盘与状态评估 + 个股总结
        rows = []
        detail_analysis = []
        strategy_stats = {}  # 策略多日累计归因统计

        for item in tracked_records:
            code = item["code"]
            name = item.get("name", "未知")
            q = quotes.get(code, {})
            c_price = q.get("close", 0.0)
            day_pct = q.get("pct", 0.0)
            high_price = q.get("high", 0.0)
            low_price = q.get("low", 0.0)

            pick_price = float(item.get("pick_price") or c_price)
            stop_loss = float(item.get("stop_loss") or 0.0)
            target_price = float(item.get("target_price") or 0.0)
            strategy = item.get("strategy", "默认策略")
            days_held = item["days_held"]

            # 累计涨跌幅计算
            cum_pct = ((c_price - pick_price) / pick_price * 100) if pick_price > 0 else 0.0
            
            # 判断状态与生成个股复盘总结 (成功 / 止损 / 观察)
            status = "🟢 正常持有"
            analysis_reason = ""

            if c_price <= stop_loss and stop_loss > 0:
                status = "🔴 触发止损"
                analysis_reason = f"收盘价({c_price}元)跌破止损位({stop_loss}元)，存在破位风险，建议严格执行止损。"
            elif low_price <= stop_loss and stop_loss > 0:
                status = "⚠️ 盘中破止损"
                analysis_reason = f"盘中最低({low_price}元)触及止损位，拉回收盘，说明下方支撑交投剧烈，警惕二次下探。"
            elif c_price >= target_price and target_price > 0:
                status = "🚀 达标止盈"
                analysis_reason = f"收盘价突破目标价({target_price}元)，多头动能强劲，可分批落袋为安或锁定利润。"
            elif high_price >= target_price and target_price > 0:
                status = "🎯 盘中触目标"
                analysis_reason = f"盘中最高({high_price}元)冲高至目标价后受阻，冲高回落，注意高位获利盘抛压。"
            else:
                if cum_pct >= 3.0:
                    status = "📈 趋势拉升"
                    analysis_reason = "突破后走势顺畅，持续在买入价上方震荡上行，多头结构良好。"
                elif cum_pct <= -3.0:
                    status = "📉 受压回调"
                    analysis_reason = "买入后遭遇大盘或板块拖累回调，虽未触及止损，但反弹无量，需防守观望。"
                else:
                    status = "🔄 震荡洗盘"
                    analysis_reason = "股价在推荐价附近小幅波动，主力筹码整固中，关注后续量能是否放大。"

            # 构建表格行 (增加买入价➔最新价的价格对比)
            price_compare = f"{pick_price:.2f} ➔ {c_price:.2f}"
            pnl_str = f"+{cum_pct:.2f}%" if cum_pct > 0 else f"{cum_pct:.2f}%"
            day_pct_str = f"+{day_pct:.2f}%" if day_pct > 0 else f"{day_pct:.2f}%"

            rows.append(
                f"| {item['pick_date']} (T+{days_held}) | `{code}` | **{name}** | {price_compare} | {day_pct_str} | **{pnl_str}** | {target_price:.2f} / {stop_loss:.2f} | {status} |"
            )

            # 收集个股详细复盘
            detail_analysis.append(
                f"- **{name}({code})** [{strategy} | T+{days_held}]: 累计 **{pnl_str}** (今日 {day_pct_str})。\n  *复盘诊断*: {analysis_reason}"
            )

            # 多日策略累计数据汇总（按策略计算胜率与累计均收益）
            if strategy not in strategy_stats:
                strategy_stats[strategy] = {
                    "total": 0,
                    "win": 0,
                    "loss": 0,
                    "stop_loss_hit": 0,
                    "target_hit": 0,
                    "cum_pct_sum": 0.0
                }
            strategy_stats[strategy]["total"] += 1
            strategy_stats[strategy]["cum_pct_sum"] += cum_pct
            if cum_pct > 0:
                strategy_stats[strategy]["win"] += 1
            else:
                strategy_stats[strategy]["loss"] += 1
            if "止损" in status:
                strategy_stats[strategy]["stop_loss_hit"] += 1
            if "止盈" in status or "目标" in status:
                strategy_stats[strategy]["target_hit"] += 1

        # 3. 策略多日累积归因与迭代优化逻辑
        optimizations = []
        for strat, stat in strategy_stats.items():
            total = stat["total"]
            win_rate = (stat["win"] / total) * 100 if total > 0 else 0
            avg_pnl = stat["cum_pct_sum"] / total if total > 0 else 0
            stop_rate = (stat["stop_loss_hit"] / total) * 100 if total > 0 else 0

            opt_msg = f"📌 **{strat}** (近10日共样本 {total} 只): 胜率 **{win_rate:.1f}%**，平均收益 **{avg_pnl:+.2f}%**\n"
            
            if win_rate < 40 or stop_rate >= 30:
                opt_msg += f"  ⚠️ *归因修正建议*: 该策略近期假突破增多/止损率偏高({stop_rate:.0f}%)。建议提高选股过滤门槛：\n" \
                           f"     1) 将 TrendIQ 门槛调高（例如由 80 提升至 85）；\n" \
                           f"     2) 过滤掉上影线过长或首日换手率低于 3% 的虚假突破标的。"
            elif win_rate >= 65 and avg_pnl > 3.0:
                opt_msg += f"  🔥 *表现优异*: 策略契合当前市场风格！可适当放宽买入建仓区间，或对于该策略标的提高目标止盈位。"
            else:
                opt_msg += f"  ✅ *表现平稳*: 策略总体表现符合预期，建议保持当前筛选参数，继续追踪后续表现。"
            
            optimizations.append(opt_msg)

        # 4. 拼装 Markdown 报告
        table_text = (
            "| 推荐日期(持仓) | 代码 | 名称 | 买入价 ➔ 最新价 | 今日涨跌 | 累计收益 | 目标/止损 | 当前状态 |\n"
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n" + "\n".join(rows)
        )
        detail_text = "\n".join(detail_analysis)
        opt_text = "\n\n".join(optimizations)

        report = (
            f"🌙 **【沪深主板 - 晚间复盘与 5-10 日观察期追踪报告】**\n\n"
            f"### 📋 1. 历史标的价格对比与跟踪明细\n{table_text}\n\n"
            f"### 🔍 2. 近期个股逐一复盘与胜败诊断\n{detail_text}\n\n---\n"
            f"### ⚙️ 3. 策略多日累积归因与参数迭代修补建议\n{opt_text}"
        )

        # 5. 保存复盘归因日志到 skills_postmortem.md 保持进化记录
        self.save_postmortem_log(report)

        return report

    def save_postmortem_log(self, report: str):
        """将每日归因与复盘结果追加记录到 Markdown 文件中"""
        try:
            date_today = datetime.now().strftime("%Y-%m-%d")
            log_entry = f"\n\n## 📅 复盘日志 - {date_today}\n{report}\n"
            
            mode = "a" if os.path.exists(self.postmortem_file) else "w"
            with open(self.postmortem_file, mode, encoding="utf-8") as f:
                f.write(log_entry)
            print(f"💾 复盘归因成功保存至 {self.postmortem_file}")
        except Exception as e:
            print(f"❌ 保存复盘归因日志失败: {e}")

    def push_wechat(self, msg: str):
        url = os.environ.get("WECHAT_WEBHOOK", "").strip()
        if url:
            try:
                res = requests.post(url, json={"msgtype": "markdown", "markdown": {"content": msg}}, timeout=10)
                if res.status_code == 200:
                    print("✅ 微信推送成功！")
                else:
                    print(f"❌ 微信推送返回异常: {res.text}")
            except Exception as e:
                print(f"❌ 微信推送失败: {e}")
        else:
            print("⚠️ 未配置 WECHAT_WEBHOOK 环境变量，跳过微信推送。")


def main():
    agent = EveningReviewAgent()
    msg = agent.generate_review_report()
    print(msg)
    agent.push_wechat(msg)


if __name__ == "__main__":
    main()
