"""
Agent 2: 晚间复盘 Agent (支持多源行情容错 + 自动防误杀 + 5-10 日观察期逐日跟踪 + 胜败归因)
"""

import os
import json
import time
import requests
from datetime import datetime, timedelta  

# 🎯 修改点：直接读取和保存到仓库根目录（与早盘 morning_picker 保存的位置保持一致）
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

    def load_history(self):
        """从根目录读取早盘历史"""
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"❌ 读取历史文件失败: {e}")
        else:
            print(f"⚠️ 未找到历史文件: {self.history_file}")
        return {}

    def load_tracker(self):
        """从根目录读取跟踪池"""
        if os.path.exists(self.tracker_file):
            try:
                with open(self.tracker_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"❌ 读取跟踪池文件失败: {e}")
        else:
            print(f"⚠️ 未找到跟踪池文件: {self.tracker_file}")
        return []

    def save_tracker(self, tracker_data):
        """更新并保存跟踪池到根目录"""
        try:
            with open(self.tracker_file, "w", encoding="utf-8") as f:
                json.dump(tracker_data, f, ensure_ascii=False, indent=2)
            print("💾 跟踪池数据已成功更新保存！")
        except Exception as e:
            print(f"❌ 保存跟踪池失败: {e}")


    def fetch_market_quotes(self, codes: list) -> dict:
        """获取盘后最新行情（东财 + 新浪双源备用，带兜底逻辑）"""
        if not codes:
            return {}
        quotes = {}
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        
        # --- 策略 1: 东方财富 API ---
        secids = [f"0.{c}" if (c.startswith("00") or c.startswith("30")) else f"1.{c}" for c in set(codes)]
        url = "http://push2.eastmoney.com/api/qt/ulist/get"
        params = {"fltt": "2", "fields": "f12,f14,f2,f3,f15,f16", "secids": ",".join(secids)}
        
        try:
            res = requests.get(url, headers=headers, params=params, timeout=8)
            if res.status_code == 200:
                data = res.json().get("data")
                if data and "diff" in data:
                    for item in data["diff"]:
                        close_p = item.get("f2")
                        if close_p is not None and str(close_p) != "-":
                            quotes[str(item["f12"])] = {
                                "close": float(close_p),
                                "pct": float(item.get("f3", 0.0) or 0.0),
                                "high": float(item.get("f15", 0.0) or 0.0),
                                "low": float(item.get("f16", 0.0) or 0.0),
                            }
        except Exception as e:
            print(f"⚠️ 东财行情 API 获取失败，准备切换备用源: {e}")

        # --- 策略 2: 新浪财经 API (备用) ---
        missing_codes = [c for c in set(codes) if c not in quotes or quotes[c]["close"] == 0.0]
        if missing_codes:
            try:
                sina_codes = [f"sh{c}" if c.startswith("6") else f"sz{c}" for c in missing_codes]
                sina_url = f"http://hq.sinajs.cn/list={','.join(sina_codes)}"
                sina_headers = {"Referer": "http://finance.sina.com.cn", "User-Agent": headers["User-Agent"]}
                res = requests.get(sina_url, headers=sina_headers, timeout=8)
                if res.status_code == 200:
                    lines = res.text.strip().split("\n")
                    for line in lines:
                        if '="' in line:
                            code_raw = line.split('=')[0].strip()
                            code = code_raw[-6:]
                            content = line.split('"')[1]
                            parts = content.split(',')
                            if len(parts) > 3 and float(parts[3]) > 0:
                                close_p = float(parts[3])
                                last_close = float(parts[2])
                                pct = ((close_p - last_close) / last_close * 100) if last_close > 0 else 0.0
                                quotes[code] = {
                                    "close": close_p,
                                    "pct": pct,
                                    "high": float(parts[4]),
                                    "low": float(parts[5]),
                                }
            except Exception as e:
                print(f"⚠️ 新浪行情 API 补充失败: {e}")

        # --- 策略 3: 降级兜底标志 ---
        for c in codes:
            if c not in quotes or quotes[c]["close"] == 0.0:
                print(f"⚠️ 标的 {c} 未能获取到最新价格，激活保底机制防误判止损。")
                quotes[c] = {"close": 0.0, "pct": 0.0, "high": 0.0, "low": 0.0, "is_mock": True}

        return quotes

    def generate_review_report(self) -> str:
        history = self.load_history()
        if not history:
            return "⚠️ 当前无选股历史记录，请确认 daily_picks_history.json 格式与路径是否正确。"

        today_dt = datetime.now()
        tracked_records = []
        all_codes = []

        for date_str, picks in history.items():
            try:
                pick_dt = datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                continue
            days_held = (today_dt - pick_dt).days
            if 0 <= days_held <= 10:
                for p in picks:
                    if isinstance(p, dict):
                        item = dict(p)
                    elif isinstance(p, str):
                        item = {"code": p, "name": p, "pick_price": 0.0, "target_price": 0.0, "stop_loss": 0.0}
                    else:
                        continue

                    item["pick_date"] = date_str
                    item["days_held"] = days_held
                    tracked_records.append(item)
                    if item.get("code"):
                        all_codes.append(item["code"])

        if not tracked_records:
            return "📊 暂无 5-10 日观察期内的历史标的。"

        quotes = self.fetch_market_quotes(all_codes)

        rows = []
        detail_analysis = []
        strategy_stats = {}

        for item in tracked_records:
            code = item["code"]
            name = item.get("name", "未知")
            q = quotes.get(code, {})
            c_price = q.get("close", 0.0)
            day_pct = q.get("pct", 0.0)
            high_price = q.get("high", 0.0)
            low_price = q.get("low", 0.0)

            pick_price = float(item.get("pick_price") or 0.0)
            stop_loss = float(item.get("stop_loss") or 0.0)
            target_price = float(item.get("target_price") or 0.0)
            strategy = item.get("strategy", "默认策略")
            days_held = item["days_held"]

            # 🛡️ 行情接口延迟/失败时的兜底逻辑（防止显示 0 元和误算 -100% 止损）
            is_mock = q.get("is_mock", False) or c_price == 0.0
            if is_mock:
                c_price = pick_price
                cum_pct = 0.0
                status = "⏳ 价格获取延迟"
                analysis_reason = "盘后数据接口连接异常，暂按推荐买入价锁定防守观察。"
            else:
                cum_pct = ((c_price - pick_price) / pick_price * 100) if pick_price > 0 else 0.0
                if c_price <= stop_loss and stop_loss > 0:
                    status = "🔴 触发止损"
                    analysis_reason = f"收盘价({c_price}元)跌破止损位({stop_loss}元)，建议执行止损。"
                elif low_price <= stop_loss and stop_loss > 0:
                    status = "⚠️ 盘中破止损"
                    analysis_reason = f"盘中最低({low_price}元)触及止损位，警惕二次下探。"
                elif c_price >= target_price and target_price > 0:
                    status = "🚀 达标止盈"
                    analysis_reason = f"突破目标价({target_price}元)，建议分批落袋为安。"
                elif high_price >= target_price and target_price > 0:
                    status = "🎯 盘中触目标"
                    analysis_reason = f"盘中最高({high_price}元)冲高至目标价后受阻，注意高位抛压。"
                else:
                    if cum_pct >= 3.0:
                        status = "📈 趋势拉升"
                        analysis_reason = "突破后持续在买入价上方震荡上行，多头结构良好。"
                    elif cum_pct <= -3.0:
                        status = "📉 受压回调"
                        analysis_reason = "买入后遭遇回调，反弹无量，需防守观望。"
                    else:
                        status = "🔄 震荡洗盘"
                        analysis_reason = "股价在推荐价附近小幅波动，筹码整固中。"

            price_compare = f"{pick_price:.2f} ➔ {c_price:.2f}"
            pnl_str = f"+{cum_pct:.2f}%" if cum_pct > 0 else f"{cum_pct:.2f}%"
            day_pct_str = f"+{day_pct:.2f}%" if day_pct > 0 else f"{day_pct:.2f}%"

            rows.append(
                f"| {item['pick_date']} (T+{days_held}) | `{code}` | **{name}** | {price_compare} | {day_pct_str} | **{pnl_str}** | {target_price:.2f} / {stop_loss:.2f} | {status} |"
            )

            detail_analysis.append(
                f"- **{name}({code})** [{strategy} | T+{days_held}]: 累计 **{pnl_str}** (今日 {day_pct_str})。\n  *复盘诊断*: {analysis_reason}"
            )

            if strategy not in strategy_stats:
                strategy_stats[strategy] = {"total": 0, "win": 0, "cum_pct_sum": 0.0}
            strategy_stats[strategy]["total"] += 1
            strategy_stats[strategy]["cum_pct_sum"] += cum_pct
            if cum_pct > 0:
                strategy_stats[strategy]["win"] += 1

        optimizations = []
        for strat, stat in strategy_stats.items():
            total = stat["total"]
            win_rate = (stat["win"] / total) * 100 if total > 0 else 0
            avg_pnl = stat["cum_pct_sum"] / total if total > 0 else 0

            opt_msg = f"📌 **{strat}** (近10日共 {total} 只标的): 胜率 **{win_rate:.1f}%**，平均收益 **{avg_pnl:+.2f}%**\n"
            if win_rate < 40:
                opt_msg += "  ⚠️ *归因建议*: 假突破增多，建议提高 TrendIQ 门槛及过滤低换手标的。"
            else:
                opt_msg += "  ✅ *表现平稳*: 策略符合预期，继续保持现有筛选规则。"
            optimizations.append(opt_msg)

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

        self.save_postmortem_log(report)
        return report

    def save_postmortem_log(self, report: str):
        try:
            date_today = datetime.now().strftime("%Y-%m-%d")
            log_entry = f"\n\n## 📅 复盘日志 - {date_today}\n{report}\n"
            mode = "a" if os.path.exists(self.postmortem_file) else "w"
            with open(self.postmortem_file, mode, encoding="utf-8") as f:
                f.write(log_entry)
        except Exception as e:
            print(f"❌ 保存复盘日志失败: {e}")

    def push_wechat(self, msg: str):
        url = os.environ.get("WECHAT_WEBHOOK", "").strip()
        if not url:
            print("⚠️ 未配置 WECHAT_WEBHOOK，跳过推送。")
            return

        headers = {"Content-Type": "application/json"}
        
        # 企微单条 Markdown 限制 4096 字节，设定 3000 字节安全阈值进行切片
        max_bytes = 3000
        msg_bytes = msg.encode("utf-8")

        if len(msg_bytes) <= max_bytes:
            chunks = [msg]
        else:
            # 按行切片，防止截断 Markdown 语法格式
            lines = msg.split("\n")
            chunks = []
            current_chunk = ""
            
            for line in lines:
                test_chunk = current_chunk + ("\n" if current_chunk else "") + line
                if len(test_chunk.encode("utf-8")) > max_bytes:
                    if current_chunk:
                        chunks.append(current_chunk)
                    current_chunk = line
                else:
                    current_chunk = test_chunk
            if current_chunk:
                chunks.append(current_chunk)

        print(f"📦 消息总长 {len(msg_bytes)} 字节，已自动切分为 {len(chunks)} 条发送...")

        for idx, chunk in enumerate(chunks, 1):
            payload = {
                "msgtype": "markdown",
                "markdown": {
                    "content": chunk
                }
            }
            try:
                res = requests.post(url, json=payload, headers=headers, timeout=10)
                res_json = res.json()
                if res_json.get("errcode") == 0:
                    print(f"✅ 第 {idx}/{len(chunks)} 条微信推送成功！")
                else:
                    print(f"❌ 第 {idx}/{len(chunks)} 条发送被企微拒绝: {res_json}")
            except Exception as e:
                print(f"❌ 第 {idx}/{len(chunks)} 条网络请求失败: {e}")
            
            # 停顿 0.5 秒防止发送频控
            time.sleep(0.5)


def main():
    agent = EveningReviewAgent()
    msg = agent.generate_review_report()
    print(msg)
    agent.push_wechat(msg)


if __name__ == "__main__":
    main()
