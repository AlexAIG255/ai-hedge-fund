"""
Agent 2: 晚盘收盘复盘与持仓跟踪 Agent (Evening Review Agent) - 飞书防覆盖/精准对齐升级版
- 精准读取 morning_picker 生成的持仓池，匹配推荐日期与策略
- 动态计算持股天数 (T+N)、持仓收益率、盈亏比 (Reward/Risk Ratio)
- 触发机制：止盈 / 止损 / 10日满期 自动结案
- 自动更新与同步数据至飞书多维表格 (使用 '推荐日期_股票代码' 复合主键防误覆盖)
"""

import json
import os
import re
import time
from datetime import datetime
import requests

# 🎯 持久化文件路径
HISTORY_FILE = "daily_picks_history.json"
TRACKER_FILE = "portfolio_tracker.json"
POSTMORTEM_FILE = "skills_postmortem.md"

# ⚙️ 飞书多维表格 API 配置 (来自环境变量)
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "").strip()
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "").strip()
FEISHU_APP_TOKEN = os.environ.get("FEISHU_APP_TOKEN", "").strip()
FEISHU_TABLE_ID = os.environ.get("FEISHU_TABLE_ID", "").strip()


class EveningReviewAgent:

    def __init__(
        self,
        history_file: str = HISTORY_FILE,
        tracker_file: str = TRACKER_FILE,
        postmortem_file: str = POSTMORTEM_FILE,
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
        valid_codes = [
            str(c) for c in stock_codes if re.match(r"^\d{6}$", str(c))
        ]
        if not valid_codes:
            return {}

        tc_codes = [
            f"sh{c}" if c.startswith("60") or c.startswith("68") else f"sz{c}"
            for c in valid_codes
        ]
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
                            today_pct = (
                                float(fields[32]) if fields[32] else 0.0
                            )
                            if close_p > 0:
                                quotes[code] = {
                                    "close": close_p,
                                    "pct": today_pct,
                                }
        except Exception as e:
            print(f"⚠️ 获取收盘行情失败: {e}")
        return quotes

    # ==========================================
    # ⚙️ 飞书多维表格 API 精准更新/增量同步模块
    # ==========================================
    def get_feishu_tenant_token(self) -> str:
        """获取飞书 Tenant Access Token"""
        if not (FEISHU_APP_ID and FEISHU_APP_SECRET):
            return ""
        auth_url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        try:
            res = requests.post(
                auth_url,
                json={
                    "app_id": FEISHU_APP_ID,
                    "app_secret": FEISHU_APP_SECRET,
                },
                timeout=10,
            )
            data = res.json()
            if data.get("code") == 0:
                return data.get("tenant_access_token", "")
            else:
                print(f"⚠️ 飞书鉴权失败: {data}")
        except Exception as e:
            print(f"❌ 飞书 Token 获取异常: {e}")
        return ""

    def sync_to_feishu_sheet(self, active_items: list):
        """将晚盘复盘后的收盘价、持仓收益率、持股天数与状态精确更新至飞书多维表格"""
        access_token = self.get_feishu_tenant_token()
        if not access_token or not (FEISHU_APP_TOKEN and FEISHU_TABLE_ID):
            print("⚠️ 未配置完整飞书环境变量，跳过多维表格同步。")
            return

        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {access_token}",
        }

        # 1. 查询飞书现有记录（使用 推荐日期 + 股票代码 作为联合主键映射）
        search_url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{FEISHU_TABLE_ID}/records"
        existing_records = {}
        try:
            res_search = requests.get(
                search_url,
                headers=headers,
                params={"page_size": 200},
                timeout=10,
            )
            res_json = res_search.json()
            if res_json.get("code") == 0:
                recs = res_json.get("data", {}).get("items", [])
                for r in recs:
                    f = r.get("fields", {})
                    code = str(f.get("股票代码", ""))
                    rec_date_raw = f.get("推荐日期")

                    # 时间戳转 YYYY-MM-DD
                    if isinstance(rec_date_raw, (int, float)):
                        rec_date_str = datetime.fromtimestamp(
                            rec_date_raw / 1000
                        ).strftime("%Y-%m-%d")
                    else:
                        rec_date_str = str(rec_date_raw)

                    # 🛡️ 联合主键，彻底解决多日重复股票误覆盖的问题
                    if code and rec_date_str:
                        unique_key = f"{rec_date_str}_{code}"
                        existing_records[unique_key] = r.get("record_id")
        except Exception as e:
            print(f"⚠️ 查询飞书已有记录失败: {e}")

        # 2. 构建增量更新与新增 Payload 结构
        add_records = []
        update_records = []
        today_timestamp = int(time.time() * 1000)
        today_str = datetime.now().strftime("%Y-%m-%d")

        for item in active_items:
            code = str(item.get("code", ""))
            entry_date = str(
                item.get("entry_date", item.get("pick_date", today_str))
            )
            entry_p = float(
                item.get(
                    "entry_price",
                    item.get("buy_price", item.get("pick_price", 0)),
                )
            )
            close_p = float(item.get("current_price", entry_p))
            total_ret = float(item.get("total_return", 0.0))
            days_tracked = int(item.get("days_tracked", 1))

            status_str = "持仓中"
            if item.get("status") == "CLOSED":
                status_str = f"已结案({item.get('close_reason', '离场')})"

            fields_data = {
                "复盘日期": today_timestamp,
                "股票代码": code,
                "股票名称": str(item.get("name", "")),
                "策略归属": str(item.get("strategy", "量化选股")),
                "建仓价格": entry_p,
                "最新收盘价": close_p,
                "持仓收益率": round(total_ret / 100.0, 4),  # 匹配百分比格式
                "持股天数": days_tracked,
                "状态": status_str,
                "胜负归因": str(item.get("close_reason", "持仓跟踪中")),
            }

            lookup_key = f"{entry_date}_{code}"

            # 命中精准记录 ID，执行定向 Update
            if lookup_key in existing_records:
                update_records.append(
                    {
                        "record_id": existing_records[lookup_key],
                        "fields": fields_data,
                    }
                )
            else:
                # 若未找到记录（如补录场景），补齐推荐日期并新增
                try:
                    entry_dt_ts = int(
                        time.mktime(
                            time.strptime(entry_date, "%Y-%m-%d")
                        )
                        * 1000
                    )
                except Exception:
                    entry_dt_ts = today_timestamp

                fields_data["推荐日期"] = entry_dt_ts
                fields_data["TrendIQ评分"] = int(item.get("trend_iq", 80))
                add_records.append({"fields": fields_data})

        # 3. 提交至飞书 API
        try:
            if update_records:
                batch_update_url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{FEISHU_TABLE_ID}/records/batch_update"
                res_up = requests.post(
                    batch_update_url,
                    headers=headers,
                    json={"records": update_records},
                    timeout=10,
                )
                if res_up.json().get("code") == 0:
                    print(
                        f"🎉 成功同步更新 {len(update_records)} 条持仓复盘数据至飞书多维表格！"
                    )
                else:
                    print(f"❌ 飞书批量更新失败: {res_up.json()}")

            if add_records:
                batch_create_url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{FEISHU_TABLE_ID}/records/batch_create"
                res_add = requests.post(
                    batch_create_url,
                    headers=headers,
                    json={"records": add_records},
                    timeout=10,
                )
                if res_add.json().get("code") == 0:
                    print(
                        f"🎉 成功补齐插入 {len(add_records)} 条持仓记录至飞书多维表格！"
                    )
                else:
                    print(f"❌ 飞书批量插入失败: {res_add.json()}")

        except Exception as e:
            print(f"❌ 同步数据至飞书多维表格发生异常: {e}")

    # ==========================================
    # 🔄 晚盘核心复盘逻辑
    # ==========================================
    def run_evening_review(self) -> tuple[list, list]:
        tracker = self.load_tracker()
        today_str = datetime.now().strftime("%Y-%m-%d")

        active_items = [
            item
            for item in tracker
            if isinstance(item, dict)
            and item.get("status") in ["TRACKING", "HOLD", "ACTIVE", None]
            and re.match(r"^\d{6}$", str(item.get("code", "")))
        ]

        if not active_items:
            empty_msg = (
                f"🌆 **【晚盘收盘复盘】({today_str})**\n"
                f"-----------------------------------\n"
                f"当前无处于活跃观察期的持仓标的。"
            )
            return [], [empty_msg]

        active_codes = [item["code"] for item in active_items]
        quotes = self.fetch_closing_quotes(active_codes)

        review_logs = []
        message_chunks = []

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

            entry_date = item.get(
                "entry_date", item.get("pick_date", today_str)
            )
            entry_p = float(
                item.get(
                    "entry_price",
                    item.get("buy_price", item.get("pick_price", 0)),
                )
            )
            target_p = float(item.get("target_price", entry_p * 1.08))
            stop_p = float(item.get("stop_loss", entry_p * 0.95))

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

            total_ret = round((close_p - entry_p) / entry_p * 100, 2)
            item["current_price"] = close_p
            item["total_return"] = total_ret

            risk = max(entry_p - stop_p, 0.01)
            reward = max(target_p - entry_p, 0.01)
            rrr = round(reward / risk, 2)

            # 🎯 离场触发判定
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
                item["close_reason"] = "持仓观察中"
                status_desc = "🔄 **持仓中 (继续跟踪)**"

            ret_sign = f"+{total_ret}%" if total_ret > 0 else f"{total_ret}%"
            today_chg_sign = (
                f"+{today_chg:.2f}%" if today_chg > 0 else f"{today_chg:.2f}%"
            )

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

        if review_logs:
            alert_chunk = (
                "🚨 **【盘后触发与结案警报】**\n-----------------------------------\n"
                + "\n".join(review_logs)
            )
            message_chunks.append(alert_chunk)

        # 1. 保存本地跟踪 JSON
        self.save_tracker(tracker)

        # 2. 🚀 精准同步/更新至飞书多维表格
        self.sync_to_feishu_sheet(active_items)

        return active_items, message_chunks

    def push_to_wechat(self, message_chunks: list):
        """推送至企业微信"""
        wechat_url = os.environ.get("WECHAT_WEBHOOK", "").strip()
        if not wechat_url:
            print("⚠️ 未配置 WECHAT_WEBHOOK，跳过推送。")
            return

        for idx, chunk in enumerate(message_chunks, 1):
            payload = {"msgtype": "markdown", "markdown": {"content": chunk}}
            try:
                res = requests.post(wechat_url, json=payload, timeout=10)
                if res.json().get("errcode") == 0:
                    print(
                        f"🎉 第 ({idx}/{len(message_chunks)}) 条晚盘复盘卡片推送成功！"
                    )
            except Exception as e:
                print(f"❌ 推送失败: {e}")
            time.sleep(1)


def main():
    agent = EveningReviewAgent()
    _, message_chunks = agent.run_evening_review()
    agent.push_to_wechat(message_chunks)


if __name__ == "__main__":
    main()
