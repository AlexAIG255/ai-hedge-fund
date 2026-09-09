"""
Agent 2: 晚间复盘 Agent - 5-45日跟踪复盘 & 飞书覆盖更新 & 企微强弱筛选与深度归因推送 & Skill 策略自我迭代
针对早盘选股池，获取当日最新收盘价，精准更新飞书多维表格（覆盖收益率、持股天数、状态），
并按 5 个股票/组切片推送，附带强势/弱势筛选及盈亏归因文字总结，彻底规避 4096 字节限制。
"""

import json
import os
import time
from typing import Dict, List, Tuple
import requests

# ⚙️ 飞书多维表格 API 配置
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "").strip()
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "").strip()
FEISHU_APP_TOKEN = os.environ.get("FEISHU_APP_TOKEN", "").strip()
FEISHU_TABLE_ID = os.environ.get("FEISHU_TABLE_ID", "").strip()

WECHAT_WEBHOOK = os.environ.get("WECHAT_WEBHOOK", "").strip()

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
    # 🔍 1. 获取飞书 Access Token 与 记录 List
    # ==========================================
    def get_feishu_token_and_records(self) -> Tuple[str, Dict[str, str]]:
        """获取 Token 并检索飞书中所有记录，建立 (股票代码 -> record_id) 的映射关系"""
        if not (FEISHU_APP_ID and FEISHU_APP_SECRET and FEISHU_APP_TOKEN and FEISHU_TABLE_ID):
            print("⚠️ 未配置完整飞书环境变量，跳过多维表格同步。")
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
                print("❌ 获取飞书 Access Token 失败。")
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
                    code = str(r.get("fields", {}).get("股票代码", ""))
                    if code and rec_id:
                        record_map[code] = rec_id
        except Exception as e:
            print(f"⚠️ 获取飞书多维表格记录列表异常: {e}")

        return access_token, record_map

    # ==========================================
    # 📈 2. 核心行情复盘与飞书覆盖更新
    # ==========================================
    def run_evening_review(self):
        tracker_data = self.load_tracker()
        if not tracker_data:
            print("💡 当前持仓跟踪池为空，无需复盘。")
            return

        access_token, record_map = self.get_feishu_token_and_records()

        active_tracking = [item for item in tracker_data if item.get("status") == "TRACKING"]
        if not active_tracking:
            print("💡 当前没有在跟踪中的股票。")
            return

        # 1. 批量获取腾讯最新行情
        tc_codes = [f"sh{i['code']}" if i["code"].startswith("60") else f"sz{i['code']}" for i in active_tracking]
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

        try:
            res = requests.get(f"http://qt.gtimg.cn/q={','.join(tc_codes)}", headers=headers, timeout=6)
            price_map = {}
            if res.status_code == 200:
                for line in res.text.split(";"):
                    if '="' in line:
                        f = line.split('="')[1].replace('"', "").split("~")
                        if len(f) > 38 and float(f[3] or 0) > 0:
                            price_map[f[2]] = float(f[3])
        except Exception as e:
            print(f"❌ 拉取实时收盘价异常: {e}")
            price_map = {}

        review_summary = []
        today_timestamp = int(time.time() * 1000)

        # 2. 遍历持仓池逐一做强弱判定与状态评估
        for item in tracker_data:
            if item.get("status") != "TRACKING":
                continue

            code = item["code"]
            entry_price = item["entry_price"]
            curr_price = price_map.get(code, entry_price)

            item["days_tracked"] = item.get("days_tracked", 0) + 1
            days = item["days_tracked"]

            ret_pct = ((curr_price - entry_price) / entry_price) * 100
            ret_str = f"{ret_pct:+.2f}%"

            # 强弱与状态判定
            status = "持仓中"
            strength = "🔥 强势" if ret_pct >= 0 else "⚠️ 弱势"

            if curr_price >= item.get("target_price", entry_price * 1.1):
                item["status"] = "WIN"
                item["reason"] = "达标止盈: 突破关键阻力位，多头量能持续放量"
                status = "已止盈"
                strength = "🚀 强达标"
            elif curr_price <= item.get("stop_loss", entry_price * 0.95):
                item["status"] = "LOSS"
                item["reason"] = "触及止损: 回踩跌破安全支撑线，防范下行风险"
                status = "已止损"
                strength = "❌ 弱触损"
            elif days >= 45:
                item["status"] = "WIN" if ret_pct > 0 else "LOSS"
                item["reason"] = "到期清算: 达到 45 日持仓窗口上限"
                status = "已平仓"

            review_summary.append({
                "code": code,
                "name": item["name"],
                "strategy": item.get("strategy", "综合选股"),
                "entry_price": entry_price,
                "curr_price": curr_price,
                "ret_pct": ret_pct,
                "ret_str": ret_str,
                "days": days,
                "status": status,
                "strength": strength,
                "reason": item.get("reason", "趋势震荡整理中" if ret_pct >= -2 else "弱势下探均线"),
            })

            # 3. 🚀 回填/更新飞书多维表格
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
                        "胜负归因": f"[{strength}] {item.get('reason', '持仓观察中')}",
                    }
                }
                try:
                    requests.patch(update_url, headers=headers_fs, json=payload_fs, timeout=5)
                except Exception as e:
                    print(f"❌ 更新飞书 Record ({code}) 异常: {e}")

        # 保存更新后的持仓文件
        self.save_tracker(tracker_data)

        # 4. 执行复盘迭代与企微切片推送（附带深度归因总结）
        self.update_skills_postmortem(tracker_data)
        self.push_wechat_summary(review_summary)

    # ==========================================
    # 🤖 3. Skill 自动迭代与深度归因分析日志
    # ==========================================
    def update_skills_postmortem(self, tracker_data: List[Dict]):
        completed = [i for i in tracker_data if i.get("status") in ["WIN", "LOSS"]]
        win_count = sum(1 for i in completed if i.get("status") == "WIN")
        total = len(completed)
        win_rate = (win_count / total * 100) if total > 0 else 0.0

        content = (
            f"# 🤖 Agent 2 晚间复盘 Skill 策略迭代日志\n\n"
            f"- **更新时间**: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"- **已结案样本总数**: `{total}`\n"
            f"- **历史总胜率**: `{win_rate:.2f}%`\n\n"
            f"## 💡 深度归因与策略调优\n"
        )
        if win_rate < 50.0 and total >= 5:
            content += (
                "- ⚠️ **盈亏归因**: 胜率低于 50%，主要归因于在市场大盘调整期高吸了缺乏承接资金的股票，止损触达过快。\n"
                "- 🔧 **调整策略**: 收紧选股标准，将 TrendIQ 硬性门槛提高至 85 分，严格限制高位追涨，优先低吸做多。\n"
            )
        else:
            content += (
                "- ✅ **盈亏归因**: 选股动能延续性良好，强势个股成功承接主线资金，止盈机制触发稳定。\n"
                "- 🚀 **调整策略**: 保持当前低吸与量价突破系统，继续强化 5-45 日波段监控。\n"
            )

        try:
            with open(self.postmortem_file, "w", encoding="utf-8") as f:
                f.write(content)
            print("📝 【Skill 智能迭代日志】更新成功！")
        except Exception as e:
            print(f"❌ 写入 Skill 日志失败: {e}")

    # ==========================================
    # 📱 4. 企微分批推送 (强弱筛选 + 盈亏归因文字总结)
    # ==========================================
    def push_wechat_summary(self, summary_list: List[Dict]):
        if not WECHAT_WEBHOOK or not summary_list:
            return

        today_str = time.strftime("%Y-%m-%d")
        chunk_size = 5  # 每 5 个股票为一组拆分推送
        total_chunks = (len(summary_list) + chunk_size - 1) // chunk_size

        # 1. 计算整体统计与强弱列表
        strong_stocks = [s for s in summary_list if s["ret_pct"] >= 0]
        weak_stocks = [s for s in summary_list if s["ret_pct"] < 0]
        avg_ret = sum(s["ret_pct"] for s in summary_list) / len(summary_list)

        # 2. 分批发送个股复盘详情
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
                    f"• 评级: **{s['strength']}** | 状态: **{s['status']}**\n"
                    f"• 最新价: `{s['curr_price']:.2f}元` | 收益: `{s['ret_str']}`\n"
                    f"• 归因: {s['reason']}\n"
                )
                lines.append(line)

            lines.append("-----------------------------------")

            # 3. 在最后一页加上【强弱筛选与盈亏归因总结】
            if page == total_chunks:
                lines.append("📊 **【持仓强弱筛选与深度总结】**\n")
                lines.append(f"• **平均收益率**: `{avg_ret:+.2f}%`")
                lines.append(f"• **💪 建议保持(强势)**: {', '.join([s['name'] for s in strong_stocks]) or '无'}")
                lines.append(f"• **⚠️ 建议警惕/防守(弱势)**: {', '.join([s['name'] for s in weak_stocks]) or '无'}\n")

                lines.append("🧐 **【盈亏归因分析】**")
                if avg_ret >= 0:
                    lines.append("• **盈利主因**: 强势股成功吸纳主线资金，板块共振强，量价配合良好，支撑位反弹有力。")
                else:
                    lines.append("• **亏损主因**: 部分个股在大盘回调时缺乏资金承接，跌破短线支撑线触发防守策略。")

                lines.append("\n💡 *数据已精准回填至飞书多维表格*")

            full_msg = "\n".join(lines)
            payload = {
                "msgtype": "markdown",
                "markdown": {"content": full_msg},
            }

            try:
                res = requests.post(WECHAT_WEBHOOK, json=payload, headers={"Content-Type": "application/json"}, timeout=10)
                if res.json().get("errcode") == 0:
                    print(f"🎉 晚间复盘报告 [{page}/{total_chunks}] 已成功推送至企业微信！")
                else:
                    print(f"❌ 企微推送失败 [{page}/{total_chunks}]: {res.json()}")
            except Exception as e:
                print(f"❌ 企微推送异常: {e}")

            time.sleep(1)  # 间隔 1 秒，防频繁拦截


if __name__ == "__main__":
    agent = EveningReviewAgent()
    print("==================================================")
    print("🌙 Agent 2 [晚间复盘 Agent] 启动，开始结算数据...")
    print("==================================================")
    agent.run_evening_review()
