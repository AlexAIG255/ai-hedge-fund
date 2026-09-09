"""
Agent 2: 晚间复盘 Agent - 5-45日跟踪复盘 & 飞书覆盖更新 & 企微防轰炸/防超限推送 & Skill 策略自我迭代
针对早盘选股池，获取当日最新收盘价，精准更新飞书多维表格（覆盖收益率、持股天数、状态），
并按 5 个股票/组自动切片分批推送企微复盘简报，规避 4096 字节长度限制。
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

        # 2. 遍历持仓池逐一做多维度统计与状态判定
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

            status = "持仓中"
            if curr_price >= item.get("target_price", entry_price * 1.1):
                item["status"] = "WIN"
                item["reason"] = "达标止盈: 突破阻力线，动能放量"
                status = "已止盈"
            elif curr_price <= item.get("stop_loss", entry_price * 0.95):
                item["status"] = "LOSS"
                item["reason"] = "触及止损: 回踩跌破安全防线"
                status = "已止损"
            elif days >= 45:
                item["status"] = "WIN" if ret_pct > 0 else "LOSS"
                item["reason"] = "到期清算: 达到 45 日窗口限制"
                status = "已平仓"

            review_summary.append({
                "code": code,
                "name": item["name"],
                "strategy": item.get("strategy", ""),
                "entry_price": entry_price,
                "curr_price": curr_price,
                "ret_str": ret_str,
                "days": days,
                "status": status,
                "reason": item.get("reason", "持仓观察中"),
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
                        "胜负归因": item.get("reason", "持仓观察中"),
                    }
                }
                try:
                    requests.patch(update_url, headers=headers_fs, json=payload_fs, timeout=5)
                except Exception as e:
                    print(f"❌ 更新飞书 Record ({code}) 异常: {e}")

        # 保存更新后的持仓文件
        self.save_tracker(tracker_data)

        # 4. 执行复盘迭代与企微信每 5 个一组切片推送
        self.update_skills_postmortem(tracker_data)
        self.push_wechat_summary(review_summary)

    # ==========================================
    # 🤖 3. Skill 自动迭代生成日志
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
            f"- **胜率 (Win Rate)**: `{win_rate:.2f}%`\n\n"
            f"## 💡 策略调优方向\n"
        )
        if win_rate < 50.0 and total >= 5:
            content += "- ⚠️ 胜率回落至 50% 以下，建议收紧买点，提高 TrendIQ 硬性门槛至 85 分。\n"
        else:
            content += "- ✅ 当前模型运行稳健，维持既有低吸/突破参数系统。\n"

        try:
            with open(self.postmortem_file, "w", encoding="utf-8") as f:
                f.write(content)
            print("📝 【Skill 智能迭代日志】更新成功！")
        except Exception as e:
            print(f"❌ 写入 Skill 日志失败: {e}")

    # ==========================================
    # 📱 4. 企微切片分批推送 (每 5 个股票一个消息)
    # ==========================================
    def push_wechat_summary(self, summary_list: List[Dict]):
        if not WECHAT_WEBHOOK or not summary_list:
            return

        today_str = time.strftime("%Y-%m-%d")
        chunk_size = 5  # 每 5 个股票拆分成一条消息发送
        total_chunks = (len(summary_list) + chunk_size - 1) // chunk_size

        for page, i in enumerate(range(0, len(summary_list), chunk_size), 1):
            chunk = summary_list[i:i + chunk_size]
            lines = [
                f"🌙 **【晚间复盘总览】** ({today_str}) [{page}/{total_chunks}]",
                f"-----------------------------------",
            ]

            for s in chunk:
                icon = "🔴" if "+" in s["ret_str"] else ("🟢" if "-" in s["ret_str"] else "⚪")
                line = (
                    f"{icon} **{s['name']}** (`{s['code']}`)\n"
                    f"• 最新价: `{s['curr_price']:.2f}元` | 收益: `{s['ret_str']}`\n"
                    f"• 天数: `{s['days']}天` | 状态: **{s['status']}** ({s['reason']})\n"
                )
                lines.append(line)

            lines.append("-----------------------------------")
            if page == total_chunks:
                lines.append("💡 *数据已同步回填至飞书多维表格*")

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

            time.sleep(1)  # 每次发送后间隔 1 秒，避免触发频控拦截


if __name__ == "__main__":
    agent = EveningReviewAgent()
    print("==================================================")
    print("🌙 Agent 2 [晚间复盘 Agent] 启动，开始结算数据...")
    print("==================================================")
    agent.run_evening_review()
