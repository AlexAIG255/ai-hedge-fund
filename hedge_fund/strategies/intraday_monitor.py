"""
Agent 3: 盘中异动监控（仅异动提醒）
=====================================================================
需求：盘中监控【暂时只需要异动提醒】，不再做止盈/止损平仓（平仓唯一权威仍是晚间复盘）。

监控对象：本地持仓跟踪池（portfolio_tracker.json 中 status=TRACKING 的标的）。
异动维度（阈值集中在 strategy_config.ANOMALY_THRESHOLDS，统一调参）：
  - 强势异动：涨幅 ≥ strong_pct
  - 弱势异动（风险）：跌幅 ≤ weak_pct
  - 放量异动：量比 ≥ vol_ratio 或 换手率 ≥ turnover
  - 振幅异动：日内振幅 ≥ amplitude
命中任一即推送企业微信异动提醒（分段，防封锁）。

用法:
  python intraday_monitor.py          # 盘中异动扫描 + 推送
"""

import json
import os
import re
import sys
import time
from datetime import datetime
from typing import Dict, List

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 🔧 异动阈值（统一在 strategy_config 调参）
try:
    from strategy_config import to_market_code, get_anomaly_thresholds
except ImportError:
    def to_market_code(c):
        c = str(c).zfill(6)
        if c.startswith(("60", "68", "688")):
            return f"sh{c}"
        if c.startswith(("00", "30")):
            return f"sz{c}"
        return f"sh{c}"
    def get_anomaly_thresholds():
        return {"strong_pct": 5.0, "weak_pct": -5.0, "vol_ratio": 3.0, "turnover": 8.0, "amplitude": 6.0}

# 🔬 分段推送工具
try:
    from stock_analyzer import split_wechat_message
except ImportError:
    split_wechat_message = None

WECHAT_WEBHOOK = os.environ.get("WECHAT_WEBHOOK", "").strip()
TRACKER_FILE = os.environ.get("TRACKER_FILE", "portfolio_tracker.json")


def load_tracker() -> List[Dict]:
    if os.path.exists(TRACKER_FILE):
        try:
            with open(TRACKER_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else data.get("records", [])
        except Exception:
            return []
    return []


def fetch_realtime_quotes(codes: List[str]) -> Dict[str, Dict]:
    """批量拉取腾讯实时行情（盘中字段）。返回 {code: {...}}。"""
    if not codes:
        return {}
    tc_codes = [to_market_code(c) for c in codes]
    out: Dict[str, Dict] = {}
    try:
        res = requests.get(
            f"http://qt.gtimg.cn/q={','.join(tc_codes)}",
            headers={"User-Agent": "Mozilla/5.0", "Referer": "http://qt.gtimg.cn/"},
            timeout=6,
        )
        if res.status_code == 200:
            for line in res.text.strip().split(";\n"):
                if '="' not in line:
                    continue
                parts = line.split('="')
                pure = parts[0].replace("v_", "")[2:]
                fields = parts[1].split("~")
                if len(fields) < 40:
                    continue
                try:
                    price = float(fields[3] or 0)
                    prev_close = float(fields[4] or price)
                    open_p = float(fields[5] or price)
                    high = float(fields[33] or price)
                    low = float(fields[34] or price)
                    pct = float(fields[32] or 0)
                    turnover = float(fields[38] or 0)
                    vol_ratio = float(fields[49] or 1.0)
                except (ValueError, IndexError):
                    continue
                amplitude = ((high - low) / prev_close * 100) if prev_close > 0 else 0.0
                out[pure] = {
                    "price": price, "prev_close": prev_close, "open": open_p,
                    "high": high, "low": low, "pct": pct,
                    "turnover": turnover, "vol_ratio": vol_ratio, "amplitude": amplitude,
                }
    except Exception as e:
        print(f"❌ 盘中行情拉取异常: {e}")
    return out


def detect_anomalies(item: Dict, q: Dict, th: Dict) -> List[str]:
    """返回该标的命中的异动标签列表。"""
    flags = []
    pct = q.get("pct", 0.0)
    vol_ratio = q.get("vol_ratio", 1.0)
    turnover = q.get("turnover", 0.0)
    amplitude = q.get("amplitude", 0.0)

    if pct >= th["strong_pct"]:
        flags.append(f"强势上攻(涨{pct:+.2f}%)")
    if pct <= th["weak_pct"]:
        flags.append(f"弱势下挫(跌{pct:+.2f}%)")
    if vol_ratio >= th["vol_ratio"]:
        flags.append(f"显著放量(量比{vol_ratio:.1f})")
    if turnover >= th["turnover"]:
        flags.append(f"高换手({turnover:.1f}%)")
    if amplitude >= th["amplitude"]:
        flags.append(f"剧烈波动(振幅{amplitude:.1f}%)")
    return flags


def run_anomaly_scan() -> List[Dict]:
    th = get_anomaly_thresholds()
    tracker = load_tracker()
    active = [i for i in tracker if i.get("status") == "TRACKING"]
    if not active:
        print("💡 当前没有需要盘中监控的活跃持仓。")
        return []

    quotes = fetch_realtime_quotes([i["code"] for i in active])
    today_str = time.strftime("%Y-%m-%d")
    alerts = []

    for item in active:
        code = str(item["code"]).zfill(6)
        q = quotes.get(code)
        if not q:
            continue
        flags = detect_anomalies(item, q, th)
        if not flags:
            continue
        alerts.append({
            "code": code,
            "name": item.get("name", code),
            "strategy": item.get("strategy", "综合选股"),
            "price": q["price"],
            "pct": q["pct"],
            "flags": flags,
        })

    if alerts:
        push_anomaly_alert(alerts, today_str)
    else:
        print("✅ 盘中监控：所有活跃持仓均未触发异动，盘面平稳。")
    return alerts


def _send_markdown(content: str) -> bool:
    if not WECHAT_WEBHOOK:
        return False
    parts = split_wechat_message(content, 1800) if split_wechat_message else [content]
    ok = True
    for p in parts:
        payload = {"msgtype": "markdown", "markdown": {"content": p.replace("```", "")}}
        try:
            res = requests.post(WECHAT_WEBHOOK, json=payload,
                                headers={"Content-Type": "application/json"}, timeout=10)
            if res.json().get("errcode") != 0:
                ok = False
        except Exception:
            ok = False
        time.sleep(1)
    return ok


def push_anomaly_alert(alerts: List[Dict], today_str: str):
    lines = [
        f"📡 **【盘中异动提醒】** ({today_str})",
        f"-----------------------------------",
        f"以下活跃持仓触发异动，请关注盘面变化（非买卖指令）：",
    ]
    for a in alerts:
        flag_str = "、".join(a["flags"])
        lines.append(
            f"🔔 **{a['name']}** (`{a['code']}`) | 策略: {a['strategy']}\n"
            f"• 现价 `{a['price']:.2f}元` | 涨跌 `{a['pct']:+.2f}%`\n"
            f"• 异动: {flag_str}"
        )
    lines.append("-----------------------------------")
    lines.append("💡 异动提醒仅供参考，请结合自身交易计划与风控决策。")
    _send_markdown("\n".join(lines))
    print(f"📡 盘中异动提醒已推送（共 {len(alerts)} 只）！")


if __name__ == "__main__":
    print("==================================================")
    print("📡 Agent 3 [盘中异动监控] 启动，扫描活跃持仓异动...")
    print("==================================================")
    run_anomaly_scan()
