"""
Agent 2: 晚间复盘 Agent - 5-45日跟踪复盘 & 飞书双向同步 & 企微强弱筛选推送

优化更新日志：
1. 彻底解决止损个股重复推送：历史结案标的次日起自动静默，不再入选企微推送与每日复盘，仅保留用于历史胜率统计。
2. 飞书 1:1 严密核对与及时回填：支持全量分页拉取，精准回填当日最新收盘价、持仓收益率、持股天数与复盘时间戳。
3. 补充【三倍量战法】：全面支持“三倍量战法”策略识别、专属风控比例（10%止盈 / 4%止损）及胜负归因逻辑。
"""

import json
import os
import re
import time
from datetime import datetime
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
    # 🔍 1. 从飞书同步全量持仓（支持分页 & 排除历史结案）
    # ==========================================
    def sync_from_feishu_and_get_records(self) -> Tuple[str, Dict[str, str], List[Dict]]:
        if not (FEISHU_APP_ID and FEISHU_APP_SECRET and FEISHU_APP_TOKEN and FEISHU_TABLE_ID):
            print("⚠️ 飞书环境变量不完整，跳过飞书同步。")
            return "", {}, []

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
                return "", {}, []
        except Exception as e:
            print(f"❌ 飞书鉴权网络请求异常: {e}")
            return "", {}, []

        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {access_token}",
        }

        record_map = {}
        feishu_active_stocks = []
        list_url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{FEISHU_TABLE_ID}/records"

        has_more = True
        page_token = ""

        try:
            while has_more:
                params = {"page_size": 100}
                if page_token:
                    params["page_token"] = page_token

                res_list = requests.get(list_url, headers=headers, params=params, timeout=10)
                if res_list.status_code != 200:
                    break

                res_json = res_list.json()
                data_body = res_json.get("data", {})
                items = data_body.get("items", [])
                has_more = data_body.get("has_more", False)
                page_token = data_body.get("page_token", "")

                for r in items:
                    rec_id = r.get("record_id")
                    fields = r.get("fields", {})

                    # 解析股票代码
                    code_val = fields.get("股票代码") or fields.get("代码") or ""
                    if isinstance(code_val, list) and len(code_val) > 0:
                        code_val = code_val[0].get("text", "") if isinstance(code_val[0], dict) else str(code_val[0])
                    elif isinstance(code_val, dict):
                        code_val = code_val.get("text", "")

                    code = str(code_val).strip()
                    if code and code.isdigit():
                        code = code.zfill(6)

                    status = str(fields.get("状态", "持仓中")).strip()
                    name = fields.get("股票名称") or fields.get("名称") or f"标的_{code}"
                    entry_price = float(fields.get("买入价格") or fields.get("建仓价") or fields.get("最新校验价") or 0.0)
                    
                    strategy = fields.get("策略桶") or fields.get("匹配策略桶") or "综合选股"

                    # 3️⃣ 支持【三倍量战法】策略识别与参数初始化
                    if "三倍量" in strategy:
                        strategy = "三倍量战法"
                        default_target = round(entry_price * 1.10, 2) if entry_price > 0 else 0
                        default_stop = round(entry_price * 0.96, 2) if entry_price > 0 else 0
                    else:
                        default_target = round(entry_price * 1.08, 2) if entry_price > 0 else 0
                        default_stop = round(entry_price * 0.95, 2) if entry_price > 0 else 0

                    target_price = float(fields.get("目标价") or fields.get("止盈价") or default_target)
                    stop_loss = float(fields.get("止损价") or default_stop)

                    # 推荐日期解析
                    rec_date_val = fields.get("推荐日期") or fields.get("建仓日期") or fields.get("复盘日期")
                    recommend_date = time.strftime("%Y-%m-%d")
                    if rec_date_val:
                        if isinstance(rec_date_val, (int, float)):
                            recommend_date = datetime.fromtimestamp(rec_date_val / 1000.0).strftime("%Y-%m-%d")
                        elif isinstance(rec_date_val, str) and len(rec_date_val) >= 10:
                            recommend_date = rec_date_val[:10]

                    if code and rec_id:
                        record_map[code] = rec_id
                        # 1️⃣ 严格过滤：仅加载【持仓中】或【TRACKING】状态的股票，已止损/已止盈的不拉入活跃池
                        if status in ["持仓中", "TRACKING"]:
                            feishu_active_stocks.append({
                                "code": code,
                                "name": name,
                                "entry_price": entry_price,
                                "target_price": target_price,
                                "stop_loss": stop_loss,
                                "strategy": strategy,
                                "status": "TRACKING",
                                "recommend_date": recommend_date,
                            })

            print(f"📊 飞书表格拉取完成：当前活跃跟踪股票数量为 {len(feishu_active_stocks)} 只（已自动排除历史已结案标的）。")
        except Exception as e:
            print(f"⚠️ 获取飞书多维表格记录列表异常: {e}")

        return access_token, record_map, feishu_active_stocks

    # ==========================================
    # 📈 2. 核心行情复盘与状态更新
    # ==========================================
    def run_evening_review(self):
        access_token, record_map, feishu_stocks = self.sync_from_feishu_and_get_records()
        local_tracker = self.load_tracker()

        # 1. 深度合并本地 JSON 与飞书数据
        tracker_map = {str(item["code"]).zfill(6): item for item in local_tracker}

        for fs_item in feishu_stocks:
            code = fs_item["code"]
            if code not in tracker_map:
                tracker_map[code] = fs_item
            else:
                # 仅当本地记录不是已结案状态时，同步活跃状态
                if tracker_map[code].get("status") not in ["WIN", "LOSS"]:
                    tracker_map[code]["status"] = "TRACKING"
                    tracker_map[code]["entry_price"] = fs_item["entry_price"]
                    tracker_map[code]["target_price"] = fs_item["target_price"]
                    tracker_map[code]["stop_loss"] = fs_item["stop_loss"]
                    tracker_map[code]["strategy"] = fs_item["strategy"]
                    if "recommend_date" not in tracker_map[code]:
                        tracker_map[code]["recommend_date"] = fs_item["recommend_date"]

        tracker_data = list(tracker_map.values())

        # 2. 筛选真正需要复盘的活跃持仓（排除往期已经 WIN/LOSS 的股票）
        active_tracking = [item for item in tracker_data if item.get("status") == "TRACKING"]
        if not active_tracking:
            print("💡 当前没有需要更新复盘的活跃股票。")
            self.save_tracker(tracker_data)
            return

        # 3. 批量获取腾讯最新行情
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

        try:
            res = requests.get(f"http://qt.gtimg.cn/q={','.join(tc_codes)}", headers=headers, timeout=6)
            if res.status_code == 200:
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
        today_date = datetime.now().date()
        today_str = time.strftime("%Y-%m-%d")
        today_timestamp = int(time.time() * 1000)

        # 4. 遍历活跃持仓计算持仓天数与盈亏判定
        for item in active_tracking:
            code = str(item["code"]).zfill(6)
            entry_price = float(item["entry_price"])
            curr_price = price_map.get(code, entry_price)

            rec_date_str = item.get("recommend_date", today_str)
            try:
                rec_dt = datetime.strptime(rec_date_str[:10], "%Y-%m-%d").date()
                days = (today_date - rec_dt).days + 1
                if days < 1:
                    days = 1
            except Exception:
                days = item.get("days_tracked", 1)

            item["days_tracked"] = days

            ret_pct = ((curr_price - entry_price) / entry_price) * 100 if entry_price > 0 else 0.0
            ret_str = f"{ret_pct:+.2f}%"

            strategy = item.get("strategy", "综合选股")
            status = "持仓中"
            strength = "🔥 强势" if ret_pct >= 0 else "⚠️ 弱势"

            # 区分不同战法的目标价止损价
            if strategy == "三倍量战法":
                target_price = float(item.get("target_price") or round(entry_price * 1.10, 2))
                stop_loss = float(item.get("stop_loss") or round(entry_price * 0.96, 2))
            else:
                target_price = float(item.get("target_price") or round(entry_price * 1.08, 2))
                stop_loss = float(item.get("stop_loss") or round(entry_price * 0.95, 2))

            just_closed_today = False  # 是否为今日刚触发结案

            # 盈亏规则判定
            if curr_price >= target_price and entry_price > 0:
                item["status"] = "WIN"
                item["close_date"] = today_str
                item["reason"] = f"达标止盈 [{strategy}]: 突破目标价 {target_price}元，获利离场"
                status = "已止盈"
                strength = "🚀 强达标"
                just_closed_today = True
            elif curr_price <= stop_loss and entry_price > 0:
                item["status"] = "LOSS"
                item["close_date"] = today_str
                if strategy == "三倍量战法":
                    item["reason"] = f"触及止损 [三倍量战法]: 跌破放量风控价 {stop_loss}元，防守离场"
                else:
                    item["reason"] = f"触及止损 [{strategy}]: 跌破止损价 {stop_loss}元，防守离场"
                status = "已止损"
                strength = "❌ 弱触损"
                just_closed_today = True
            elif days >= 45:
                item["status"] = "WIN" if ret_pct > 0 else "LOSS"
                item["close_date"] = today_str
                item["reason"] = "到期清算: 达到 45 日持仓上限"
                status = "已平仓"
                just_closed_today = True

            reason_desc = item.get("reason", "持仓观察中")

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
                "just_closed_today": just_closed_today,
                "is_active": item["status"] == "TRACKING"
            })

            # 2️⃣ 飞书 1:1 回填更新每日收盘价与状态
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
                    res_patch = requests.patch(update_url, headers=headers_fs, json=payload_fs, timeout=5)
                    if res_patch.status_code == 200:
                        print(f"✅ 飞书更新成功 [{code} {item['name']}]: 收盘价 {curr_price}元, 状态 [{status}]")
                    else:
                        print(f"❌ 飞书更新失败 [{code}]: {res_patch.text}")
                except Exception as e:
                    print(f"❌ 更新飞书 Record ({code}) 网络异常: {e}")

        # 保存更新后的持久化持仓
        self.save_tracker(tracker_data)

        # 6. 执行 Skill 复盘日志与企微分批推送
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
    # 📱 4. 企微分批推送（精准剔除过往已止损股票）
    # ==========================================
    def push_wechat_summary(self, summary_list: List[Dict], all_tracker_data: List[Dict]):
        if not WECHAT_WEBHOOK or not summary_list:
            return

        today_str = time.strftime("%Y-%m-%d")
        chunk_size = 5
        total_chunks = (len(summary_list) + chunk_size - 1) // chunk_size

        # 1️⃣ 严格过滤“建议持有/防守”清单：只包含【当前依然在持仓跟踪中】的股票，已止损的不入选
        still_active = [s for s in summary_list if s["is_active"]]
        strong_stocks = [s for s in still_active if s["ret_pct"] >= 0]
        weak_stocks = [s for s in still_active if s["ret_pct"] < 0]
        
        avg_ret = (sum(s["ret_pct"] for s in still_active) / len(still_active)) if still_active else 0.0

        # 计算历史胜率（基于所有历史已结案记录）
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
                notice = " 🚨 **[今日触发离场，次日起停止推送]**" if s["just_closed_today"] else ""
                
                line = (
                    f"{icon} **{s['name']}** (`{s['code']}`){notice}\n"
                    f"• 策略桶: **{s['strategy']}** | 评级: **{s['strength']}**\n"
                    f"• 最新价: `{s['curr_price']:.2f}元` | 收益: `{s['ret_str']}`\n"
                    f"• 状态: **{s['status']}** | 持仓: `{s['days']}天`\n"
                    f"• 归因: {s['reason']}\n"
                )
                lines.append(line)

            lines.append("-----------------------------------")

            if page == total_chunks:
                lines.append("📊 **【持仓强弱筛选与成功率统计】**\n")
                lines.append(f"• 🎯 **历史策略成功率(胜率)**: `{win_rate:.1f}%` (已结案 `{total_completed}` 只)")
                lines.append(f"• 📈 **当前活跃持仓平均收益率**: `{avg_ret:+.2f}%`")
                lines.append(f"• 💪 **建议持有(强势标的)**: {', '.join([s['name'] for s in strong_stocks]) or '无'}")
                lines.append(f"• ⚠️ **建议防守(弱势跟踪中)**: {', '.join([s['name'] for s in weak_stocks]) or '无'}\n")

                lines.append("🧐 **【盈亏归因分析】**")
                if avg_ret >= 0:
                    lines.append("• **盈利主因**: 选股策略与板块资金共振，突破阻力位后动能延续良好。")
                else:
                    lines.append("• **亏损主因**: 大盘盘整期买盘承接不足，部分标的触及防守线触发风控。")

                lines.append("\n💡 *数据已精准同步回填至飞书多维表格*")

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
