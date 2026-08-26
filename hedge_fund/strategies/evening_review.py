"""
Agent 2: 晚盘收盘复盘与持仓跟踪 Agent (Evening Review Agent)
- 精准读取 morning_picker 生成的持仓池，匹配推荐日期与策略
- 动态计算持股天数 (T+N)、持仓收益率、盈亏比 (Reward/Risk Ratio)
- 触发机制：止盈 / 止损 / 10日满期 自动结案
- 消息切片推送至企业微信，彻底解决长文本限制问题
"""

import os
import json
import time
import re
import requests
from datetime import datetime

# 文件路径配置
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
                        return data.get("records", data.get("tracker", []))
            except Exception as e:
                print(f"❌ 读取跟踪池失败: {e}")
        return []

    def save_tracker(self, tracker_data: list):
        """保存更新后的跟踪池数据"""
        try:
            with open(self.tracker_file, "w", encoding="utf-8") as f:
                json.dump(tracker_data, f, ensure_ascii=False, indent=2)
            print("💾 【晚盘复盘】跟踪池数据已更新保存！")
        except Exception as e:
            print(f"❌ 保存跟踪池失败: {e}")

    def fetch_closing_quotes(self, stock_codes: list) -> dict:
        """从腾讯 API 获取收盘实时价格与今日动态涨跌幅"""
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
                        fields = parts[1].replace('"', "").split("~")
                        if len(fields) > 32:
                            code = fields[2]
                            close_p = float(fields[3]) if fields[3] else 0.0
                            today_pct = float(fields[32]) if fields[32] else 0.0
                            if close_p > 0:
                                quotes[code] = {
                                    "close": close_p,
                                    "pct": today_pct
                                }
        except Exception as e:
            print(f"⚠️ 获取收盘行情失败: {e}")
        return quotes

    def run_evening_review(self) -> tuple[list, list]:
        tracker = self.load_tracker()
        today_str = datetime.now().strftime("%Y-%m-%d")

        # 🛡️ 筛选出处于跟踪期（未结案）且代码合规的标的
        active_items = [
            item for item in tracker 
            if isinstance(item, dict) 
            and item.get("status") in ["TRACKING", "HOLD", "ACTIVE", None]
            and re.match(r"^\d{6}$", str(item.get("code", "")))
        ]

        if not active_items:
            empty_msg = f"🌆 **【晚盘收盘复盘】({today_str})**\n-----------------------------------\n当前无处于活跃观察期的持仓标的。"
            return [], [empty_msg]

        active_codes = [item["code"] for item in active_items]
        quotes = self.fetch_closing_quotes(active_codes)

        review_logs = []
        message_chunks = []

        # 头部消息切片
        header_chunk = (
            f"🌆 **【晚盘收盘复盘与持仓跟踪】({today_str})**\n"
            f"-----------------------------------\n"
            f"今日监控活跃持仓标的：`{len(active_items)}` 只"
        )
        message_chunks.append(header_chunk)

        for item in active_items:
            code = item["code"]
            name = item.get("name", "未知")
            strategy = item.get("strategy", "深度量化选股")
            
            # 读取早盘推荐参数（若无记录则兜底取基础设定）
            entry_date = item.get("entry_date", item.get("pick_date", today_str))
            entry_p = float(item.get("entry_price", item.get("buy_price", item.get("pick_price", 0))))
            target_p = float(item.get("target_price", entry_p * 1.08))
            stop_p = float(item.get("stop_loss", entry_p * 0.95))
            
            # 模拟记录持股天数 (T+N)
            days_tracked = item.get("days_tracked", 0) + 1
            item["days_tracked"] = days_tracked

            if code not in quotes or entry_p <= 0:
                chunk = (
                    f"📊 **【复盘卡片】** **{name}** (`{code}`) | `T+{days_tracked}`\n"
                    f"• **状态**: ⌛ 暂未获取到收盘行情"
                )
                message_chunks.append(chunk)
                continue

            q_info = quotes[code]
            close_p = q_info["close"]
            today_chg = q_info["pct"]
            
            # 📈 核心量化指标计算：持仓收益率 & 收益风险比 (RRR)
            total_ret = round((close_p - entry_p) / entry_p * 100, 2)
            item["current_price"] = close_p
            item["total_return"] = total_ret

            risk = max(entry_p - stop_p, 0.01)
            reward = max(target_p - entry_p, 0.01)
            rrr = round(reward / risk, 2) # 计划盈亏比

            # 🎯 触发式结案诊断
            status_desc = ""
            if close_p <= stop_p:
                item["status"] = "CLOSED"
                item["close_reason"] = "破位止损"
                item["close_date"] = today_str
                status_desc = "🔴 **破位止损 (移除跟踪)**"
                review_logs.append(
                    f"🧧 **破位止损**: **{name}** (`{code}`) 触及止损价 `{stop_p:.2f}元`，收盘 `{close_p:.2f}元` (`{total_ret}%`)。"
                )

            elif close_p >= target_p:
                item["status"] = "CLOSED"
                item["close_reason"] = "止盈达标"
                item["close_date"] = today_str
                status_desc = "🎉 **止盈达标 (成功结案)**"
                review_logs.append(
                    f"🎉 **止盈达标**: **{name}** (`{code}`) 达标目标价 `{target_p:.2f}元`，收盘 `{close_p:.2f}元` (`+{total_ret}%`)。"
                )

            elif days_tracked >= 10:
                item["status"] = "CLOSED"
                item["close_reason"] = "观察期满"
                item["close_date"] = today_str
                status_desc = "📌 **满10天移出**"
                review_logs.append(
                    f"📌 **观察期满**: **{name}** (`{code}`) 已跟踪 10 个交易日，累计收益 `{total_ret}%`。"
                )

            else:
                item["status"] = "TRACKING"
                status_desc = "🔄 **持仓中 (继续跟踪)**"

            ret_sign = f"+{total_ret}%" if total_ret > 0 else f"{total_ret}%"
            today_chg_sign = f"+{today_chg:.2f}%" if today_chg > 0 else f"{today_chg:.2f}%"

            # 🧩 格式化推送卡片切片
            card_chunk = (
                f"📊 **【复盘卡片】** **{name}** (`{code}`) | `T+{days_tracked}`\n"
                f"-----------------------------------\n"
                f"📌 **推荐日期**: `{entry_date}` | **策略**: `{strategy}`\n"
                f"💵 **建仓 ➔ 收盘**: `{entry_p:.2f}元` ➔ `{close_p:.2f}元`\n"
                f"📈 **今日涨跌**: `{today_chg_sign}` | **持仓收益**: **{ret_sign}**\n"
                f"🛡️ **风控防线**: 目标 `{target_p:.2f}元` | 止损 `{stop_p:.2f}元` (盈亏比: `{rrr}`)\n"
                f"📋 **诊断状态**: {status_desc}"
            )
            message_chunks.append(card_chunk)

        # 拼接盘后总结警报切片
        if review_logs:
            alert_chunk = "🚨 **【盘后触发与结案警报】**\n-----------------------------------\n" + "\n".join(review_logs)
            message_chunks.append(alert_chunk)

        # 保存更新数据至 JSON 跟踪池
        self.save_tracker(tracker)

        return active_items, message_chunks

    def push_to_wechat(self, message_chunks: list):
        """分批推送切片，防止触发微信 4096 字符上限限制"""
        wechat_url = os.environ.get("WECHAT_WEBHOOK", "").strip()
        if not wechat_url:
            print("⚠️ 未配置 WECHAT_WEBHOOK，跳过推送。")
            return

        for idx, chunk in enumerate(message_chunks, 1):
            payload = {"msgtype": "markdown", "markdown": {"content": chunk}}
            try:
                res = requests.post(wechat_url, json=payload, timeout=10)
                if res.json().get("errcode") == 0:
                    print(f"🎉 第 ({idx}/{len(message_chunks)}) 条晚盘复盘卡片推送成功！")
            except Exception as e:
                print(f"❌ 推送失败: {e}")
            time.sleep(1)


def main():
    agent = EveningReviewAgent()
    _, message_chunks = agent.run_evening_review()
    agent.push_to_wechat(message_chunks)


if __name__ == "__main__":
    main()
