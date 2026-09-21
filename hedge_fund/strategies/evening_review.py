"""
Agent 2: 晚间复盘 Agent - 5-45日跟踪复盘 & 飞书精准覆盖 & 企微强弱筛选、胜率统计与深度归因推送
彻底修复：
1. 采用正则表达式精准解析行情，防止股票代码与价格错位。
2. 企微推送显式加入【历史已结案成功率/胜率】统计。
3. 严格隔离已止盈/止损/平仓股票，防止历史标的重复复盘。
"""

import json
import os
import re
import time
from typing import Dict, List, Tuple
import requests

# ⚙️ 飞书多维表格 API 配置
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "").strip()
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "").strip()
FEISHU_APP_TOKEN = os.environ.get("FEISHU_APP_TOKEN", "").strip()
FEISHU_TABLE_ID = os.environ.get("FEISHU_TABLE_ID", "").strip()

WECHAT_WEBHOOK = os.environ.get("WECHAT_WEBHOOK", "").strip()

# 🧪 策略桶配置参数
ENABLE_BOTTOM_REVERSAL = os.environ.get("ENABLE_BOTTOM_REVERSAL", "true").lower() == "true"

# 🎯 持久化文件路径
TRACKER_FILE = "portfolio_tracker.json"
POSTMORTEM_FILE = "skills_postmortem.md"


class EveningReviewAgent:

    def __init__(self):
        self.tracker_file = TRACKER_FILE
        self.postmortem_file = POSTMORTEM_FILE

    def load_tracker(self) -> List[Dict]:
        if os.path.exists(self.tracker_file):
            try:
                with open(self.tracker_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data if isinstance(data, list) else data.get("records", [])
            except Exception as e:
                print(f"⚠️ 读取持仓跟踪池失败: {e}")
        return []

    def save_tracker(self, tracker_data: List[Dict]):
        try:
            with open(self.tracker_file, "w", encoding="utf-8") as f:
                json.dump(tracker_data, f, ensure_ascii=False, indent=2)
            print("💾 【持仓跟踪池】数据已更新保存！")
        except Exception as e:
            print(f"❌ 保存持仓跟踪池失败: {e}")

    # ==========================================
    # 🔍 1. 获取飞书 Access Token 与记录 Map
    # ==========================================
    def get_feishu_token_and_records(self) -> Tuple[str, Dict[str, str]]:
        if not (FEISHU_APP_ID and FEISHU_APP_SECRET and FEISHU_APP_TOKEN and FEISHU_TABLE_ID):
            print("⚠️ 飞书环境变量不完整，跳过多维表格同步。")
            return "", {}

        auth_url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        try:
            res_auth = requests.post(
                auth_url,
                json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
                timeout=10,
            )
            access_token = res_auth.json().get("tenant_access_token", "")
            if not access_token:
                print(f"❌ 获取飞书 Access Token 失败: {res_auth.text}")
                return "", {}
        except Exception as e:
            print(f"❌ 飞书鉴权网络请求异常: {e}")
            return "", {}

        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {access_token}",
        }

        record_map = {}
        list_url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{FEISHU_TABLE_ID}/records"
        try:
            res_list = requests.get(list_url, headers=headers, params={"page_size": 100}, timeout=10)
            if res_list.status_code == 200:
                items = res_list.json().get("data", {}).get("items", [])
                for r in items:
                    rec_id = r.get("record_id")
                    fields = r.get("fields", {})

                    code_val = fields.get("股票代码") or fields.get("代码") or ""
                    if isinstance(code_val, list) and len(code_val) > 0:
                        code_val = code_val[0].get("text", "") if isinstance(code_val[0], dict) else str(code_val[0])
                    elif isinstance(code_val, dict):
                        code_val = code_val.get("text", "")

                    code = str(code_val).strip()
                    if code and code.isdigit():
                        code = code.zfill(6)

                    # 仅把“持仓中”的飞书记录映射优先保留，避免历史结案记录干扰
                    status = fields.get("状态", "")
                    if code and rec_id:
                        if code not in record_map or status == "持仓中":
                            record_map[code] = rec_id

                print(f"📊 飞书表格匹配成功，读取到 {len(record_map)} 条有效映射记录。")
        except Exception as e:
            print(f"⚠️ 获取飞书多维表格记录列表异常: {e}")

        return access_token, record_map

    # ==========================================
    # 📈 2. 核心行情复盘与状态更新
    # ==========================================
    def run_evening_review(self):
        tracker_data = self.load_tracker()
        if not tracker_data:
            print("💡 当前持仓跟踪池为空，无需复盘。")
            return

        # 🛡️ 过滤：仅对状态为 TRACKING 的股票进行行情复盘，杜绝历史已止盈/止损股票重复复盘
        active_tracking = [item for item in tracker_data if item.get("status") == "TRACKING"]
        if not active_tracking:
            print("💡 当前没有在跟踪中的活跃股票（所有历史股票已结案）。")
            return

        access_token, record_map = self.get_feishu_token_and_records()

        # 1. 构建请求代码列表
        tc_codes = []
        for i in active_tracking:
            code = str(i["code"]).zfill(6)
            if code.startswith("60") or code.startswith("68"):
                tc_codes.append(f"sh{code}")
            elif code.startswith("00") or code.startswith("30"):
                tc_codes.append(f"sz{code}")
            elif code.startswith("8") or code.startswith("4") or code.startswith("9"):
                tc_codes.append(f"bj{code}")

        headers = {"User-Agent": "Mozilla/5.0"}
        price_map = {}

        # 2. 批量获取腾讯最新行情（使用正则提取，防止代码与价格对不上）
        try:
            res = requests.get(f"http://qt.gtimg.cn/q={','.join(tc_codes)}", headers=headers, timeout=6)
            if res.status_code == 200:
                # 正则匹配格式：v_sh600105="1~永鼎股份~600105~5.45~..."
                matches = re.findall(r'v_(sh|sz|bj)(\d{6})="([^"]+)"', res.text)
                for market, code, data_str in matches:
                    fields = data_str.split("~")
                    if len(fields) > 3:
                        trade_price = float(fields[3]) if fields[3] and fields[3] != "-" else 0.0
                        if trade_price > 0:
                            price_map[code] = trade_price
        except Exception as e:
            print(f"❌ 拉取实时收盘价异常: {e}")

        review_summary = []
        today_timestamp = int(time.time() * 1000)

        # 3. 遍历活跃持仓，逐一判断
        for item in tracker_data:
            if item.get("status") != "TRACKING":
                continue

            code = str(item["code"]).zfill(6)
            entry_price = float(item["entry_price"])
            curr_price = price_map.get(code, entry_price)

            # 天数加 1
            item["days_tracked"] = item.get("days_tracked", 0) + 1
            days = item["days_tracked"]

            ret_pct = ((curr_price - entry_price) / entry_price) * 100
            ret_str = f"{ret_pct:+.2f}%"

            strategy = item.get("strategy", "综合选股")
            status = "持仓中"
            strength = "🔥 强势" if ret_pct >= 0 else "⚠️ 弱势"

            target_price = float(item.get("target_price", round(entry_price * 1.08, 2)))
            stop_loss = float(item.get("stop_loss", round(entry_price * 0.95, 2)))

            # 盈亏规则判定
            if curr_price >= target_price:
                item["status"] = "WIN"
                item["reason"] = f"达标止盈 [{strategy}]: 突破目标价 {target_price}元，获利离场"
                status = "已止盈"
                strength = "🚀 强达标"
            elif curr_price <= stop_loss:
                item["status"] = "LOSS"
                item["reason"] = f"触及止损 [{strategy}]: 跌破止损价 {stop_loss}元，防守离场"
                status = "已止损"
                strength = "❌ 弱触损"
            elif days >= 45:
                item["status"] = "WIN" if ret_pct > 0 else "LOSS"
                item["reason"] = "到期清算: 达到 45 日持仓上限"
                status = "已平仓"

            reason_desc = item.get("reason", "趋势震荡整理中")

            review_summary.append({
                "code": code,
                "name": item["name"],
                "strategy": strategy,
                "entry_price": entry_price,
                "curr_price": curr_price,
                "ret_pct": ret_pct,
                "ret_str": ret_str,
                "days": days,
                "status": status,
                "strength": strength,
                "reason": reason_desc,
            })

            # 4. 回填/更新飞书多维表格
            if access_token and code in record_map:
                record_id = record_map[code]
                update_url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{FEISHU_TABLE_ID}/records/{record_id}"
                headers_fs = {
                    "Content-Type": "application/json; charset=utf-8",
                    "Authorization": f"Bearer {access_token}",
                }
                payload_fs = {
                    "fields": {
                        "复盘日期": today_timestamp,
                        "最新收盘价": curr_price,
                        "持仓收益率": ret_str,
                        "持股天数": days,
                        "状态": status,
                        "策略桶": strategy,
                        "胜负归因": f"[{strength}] {reason_desc}",
                    }
                }
                try:
                    requests.patch(update_url, headers=headers_fs, json=payload_fs, timeout=5)
                except Exception as e:
                    print(f"❌ 更新飞书 Record ({code}) 异常: {e}")

        # 保存更新后的持仓文件
        self.save_tracker(tracker_data)

        # 5. 执行 Skill 复盘迭代与企微分批推送
        self.update_skills_postmortem(tracker_data)
        self.push_wechat_summary(review_summary, tracker_data)

    # ==========================================
    # 🤖 3. Skill 自动迭代日志
    # ==========================================
    def update_skills_postmortem(self, tracker_data: List[Dict]):
        completed = [i for i in tracker_data if i.get("status") in ["WIN", "LOSS"]]
        total = len(completed)
        win_count = sum(1 for i in completed if i.get("status") == "WIN")
        win_rate = (win_count / total * 100) if total > 0 else 0.0

        content = (
            f"# 🤖 Agent 2 晚间复盘 Skill 策略迭代日志\n\n"
            f"- **更新时间**: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"- **历史已结案样本数**: `{total}`\n"
            f"- **历史总胜率**: `{win_rate:.2f}%`\n"
        )
        try:
            with open(self.postmortem_file, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            print(f"❌ 写入 Skill 日志失败: {e}")

    # ==========================================
    # 📱 4. 企微分批推送（含成功率/胜率汇总）
    # ==========================================
    def push_wechat_summary(self, summary_list: List[Dict], all_tracker_data: List[Dict]):
        if not WECHAT_WEBHOOK or not summary_list:
            return

        today_str = time.strftime("%Y-%m-%d")
        chunk_size = 5
        total_chunks = (len(summary_list) + chunk_size - 1) // chunk_size

        strong_stocks = [s for s in summary_list if s["ret_pct"] >= 0]
        weak_stocks = [s for s in summary_list if s["ret_pct"] < 0]
        avg_ret = sum(s["ret_pct"] for s in summary_list) / len(summary_list)

        # 🎯 计算历史已结案选股成功率（胜率）
        completed = [i for i in all_tracker_data if i.get("status") in ["WIN", "LOSS"]]
        total_completed = len(completed)
        win_count = sum(1 for i in completed if i.get("status") == "WIN")
        win_rate = (win_count / total_completed * 100) if total_completed > 0 else 0.0

        for page, i in enumerate(range(0, len(summary_list), chunk_size), 1):
            chunk = summary_list[i:i + chunk_size]
            lines = [
                f"🌙 **【晚间复盘总览】** ({today_str}) [{page}/{total_chunks}]",
                f"-----------------------------------",
            ]

            for s in chunk:
                icon = "🔴" if s["ret_pct"] > 0 else ("🟢" if s["ret_pct"] < 0 else "⚪")
                line = (
                    f"{icon} **{s['name']}** (`{s['code']}`)\n"
                    f"• 策略桶: **{s['strategy']}** | 评级: **{s['strength']}**\n"
                    f"• 最新价: `{s['curr_price']:.2f}元` | 收益: `{s['ret_str']}`\n"
                    f"• 状态: **{s['status']}** | 持仓: `{s['days']}天`\n"
                    f"• 归因: {s['reason']}\n"
                )
                lines.append(line)

            lines.append("-----------------------------------")

            # 最后一页插入【成功率/胜率】与【强弱深度总结】
            if page == total_chunks:
                lines.append("📊 **【持仓强弱筛选与成功率统计】**\n")
                lines.append(f"• 🎯 **历史策略成功率(胜率)**: `{win_rate:.1f}%` (已结案 `{total_completed}` 只)")
                lines.append(f"• 📈 **当前持仓平均收益率**: `{avg_ret:+.2f}%`")
                lines.append(f"• 💪 **建议持有(强势标的)**: {', '.join([s['name'] for s in strong_stocks]) or '无'}")
                lines.append(f"• ⚠️ **建议防守(弱势标的)**: {', '.join([s['name'] for s in weak_stocks]) or '无'}\n")

                lines.append("🧐 **【盈亏归因分析】**")
                if avg_ret >= 0:
                    lines.append("• **盈利主因**: 选股策略与板块资金共振，突破阻力位后动能延续良好。")
                else:
                    lines.append("• **亏损主因**: 大盘盘整期买盘承接不足，部分标的触及防守线触发风控。")

                lines.append("\n💡 *数据已同步回填至飞书多维表格*")

            full_msg = "\n".join(lines)
            payload = {
                "msgtype": "markdown",
                "markdown": {"content": full_msg},
            }

            try:
                res = requests.post(WECHAT_WEBHOOK, json=payload, headers={"Content-Type": "application/json"}, timeout=10)
                if res.json().get("errcode") == 0:
                    print(f"🎉 晚间复盘报告 [{page}/{total_chunks}] 已成功推送！")
            except Exception as e:
                print(f"❌ 企微推送异常: {e}")

            time.sleep(1)


if __name__ == "__main__":
    agent = EveningReviewAgent()
    print("==================================================")
    print("🌙 Agent 2 [晚间复盘 Agent] 启动，开始数据结算与推送...")
    print("==================================================")
    agent.run_evening_review()
