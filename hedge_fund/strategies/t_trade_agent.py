"""
做T机会分析 Agent（不止损前提下，通过日内高抛低吸摊低成本）
================================================================
核心目标：用户已持仓且浮亏/浅套、不愿止损时，量化判断「能不能做T、怎么做T、
做了能降多少成本」，并严格守住止损纪律（任何做T价位都不得诱导在止损下方补仓摊平）。

设计原则：
  1. 纯规则计算，不依赖 LLM/API Key —— 沙箱与用户机器行为一致、可复现。
  2. 只读取已抓取的日K与实时价，不触碰三个受保护价格节点。
  3. 正T(先买后卖) 与 倒T(先卖后买) 分开评估：
     - 浮亏且现价在布林下轨附近 → 优先「倒T」思路需谨慎(卖在低位)；
       更适合「正T」：盘中急跌到支撑低吸、反弹到压力高抛，收盘前卖出当日买入部分。
     - A股 T+1，真正的做T是「用已有底仓当日高抛 + 尾盘/回落低吸回补」(倒T)，
       或「先低吸+用底仓高抛」(正T)。本引擎按底仓可用股数给出可执行方案。
  4. 降本测算透明：做T一次净收益 / 总持股数 = 每股成本下降额。

关键量化：
  - 日均振幅 = mean((high-low)/pre_close)，反映日内波动空间
  - 近20日高低点、布林上下轨 → 作为低吸/高抛参考位
  - 做T可行性评分：振幅足够 + 震荡(non-trending) + 未破止损 → 才建议做
  - 降本额 = (高抛价 - 低吸价) * 做T股数 / 总持股数  （再扣手续费/滑点）
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional

try:
    from stock_analyzer import compute_technicals
except ImportError:  # 包内导入
    try:
        from hedge_fund.strategies.stock_analyzer import compute_technicals
    except ImportError:
        compute_technicals = None


# ── 可调参数（环境变量，无需改代码） ──────────────────────────
# 做T最低日均振幅：低于此值波动太小，做T不划算（默认 2.0%）
T_MIN_AMPLITUDE = float(os.environ.get("T_MIN_AMPLITUDE", "2.0"))
# 做T股数占底仓比例（默认 30%，别一次全做，留余地）
T_POSITION_RATIO = float(os.environ.get("T_POSITION_RATIO", "0.3"))
# 单次做T目标价差（低吸到高抛的幅度，默认 2.5%）
T_TARGET_SPREAD = float(os.environ.get("T_TARGET_SPREAD", "2.5"))
# 手续费+滑点成本（单边，默认 0.15%）
T_COST_RATE = float(os.environ.get("T_COST_RATE", "0.15"))
# 现价距止损位安全垫：低于此比例不建议做T(快破止损)，默认 3%
T_STOP_SAFETY = float(os.environ.get("T_STOP_SAFETY", "3.0"))


def _amplitude(kline: List[Dict]) -> Dict:
    """由日K计算日内振幅统计（近N日）。"""
    amps = []
    highs, lows, closes = [], [], []
    for b in (kline or []):
        try:
            h, l, c = float(b.get("high", 0)), float(b.get("low", 0)), float(b.get("close", 0))
        except (ValueError, TypeError):
            continue
        if c > 0:
            closes.append(c)
        if h > 0 and l > 0:
            highs.append(h)
            lows.append(l)
            if c > 0:
                amps.append((h - l) / c * 100)
    if not amps:
        return {"avg": 0.0, "recent": 0.0, "high20": 0.0, "low20": 0.0}
    recent = amps[-5:]
    return {
        "avg": round(sum(amps) / len(amps), 2),
        "recent": round(sum(recent) / len(recent), 2),
        "high20": round(max(highs[-20:]), 3) if highs else 0.0,
        "low20": round(min(lows[-20:]), 3) if lows else 0.0,
    }


def _trend_state(tech: Dict) -> str:
    """判断趋势/震荡状态：震荡市最适合做T，单边急跌不宜做。"""
    if not tech:
        return "unknown"
    week = tech.get("week_chg", 0)
    pos = tech.get("position", "")
    if week <= -8 or pos == "跌破下轨":
        return "downtrend"       # 单边下跌/破位
    if week >= 10 or pos == "突破上轨":
        return "uptrend"         # 单边上扬
    return "range"               # 震荡（做T黄金环境）


def analyze_t_opportunity(code: str, name: str, cost: float, cur: float,
                          qty: float, kline: List[Dict], stop_loss: float,
                          tech: Dict = None) -> Dict:
    """分析一只持仓的做T机会。

    返回 dict：
      feasible(bool), score(int 0-100), strategy(正T/倒T/不建议),
      buy_price, sell_price, t_shares, cost_reduction(每股降本元),
      cost_after(做T后成本), pnl_pct, amplitude, trend, reason, caution
    """
    code = str(code).zfill(6)
    cost = float(cost or 0)
    cur = float(cur or 0) or (float(kline[-1].get("close", 0)) if kline else 0)
    qty = float(qty or 0)
    if cost <= 0 or cur <= 0 or qty <= 0:
        return {"feasible": False, "score": 0, "strategy": "数据不足",
                "reason": "成本/现价/股数不完整，无法测算做T。"}

    pnl_pct = (cur - cost) / cost * 100
    amp = _amplitude(kline)
    if tech is None and compute_technicals:
        try:
            tech = compute_technicals(kline, cur)
        except Exception:
            tech = {}
    tech = tech or {}
    trend = _trend_state(tech)

    # ── 安全垫检查：现价距止损位太近 → 不做T（避免诱导破位补仓）──
    stop = float(stop_loss or 0) or round(cost * 0.92, 2)
    safety_pct = (cur - stop) / cur * 100 if cur > 0 else 0
    too_close_to_stop = safety_pct < T_STOP_SAFETY

    # ── 做T可行性评分 ──
    score = 0
    score += min(40, int(amp["avg"] / T_MIN_AMPLITUDE * 20)) if T_MIN_AMPLITUDE > 0 else 40
    if amp["recent"] >= T_MIN_AMPLITUDE:
        score += 10
    if trend == "range":
        score += 25
    elif trend == "uptrend":
        score += 10
    elif trend == "downtrend":
        score -= 20
    if pnl_pct > -8:            # 未达深套，尚有操作空间
        score += 10
    if too_close_to_stop:
        score -= 40
    score = max(0, min(100, score))

    feasible = (score >= 50 and amp["avg"] >= T_MIN_AMPLITUDE
                and not too_close_to_stop and qty >= 100)

    # ── 低吸/高抛价位（日内可操作为准，不用20日布林全带宽）──
    # 做T是日内行为：单日空间≈日均振幅。布林上下轨是20日跨度，直接用会导致
    # 价差过大(10%+)一天到不了。故以「现价 ± 日均振幅的一半」为日内目标区间，
    # 再用近期高低点/布林轨做软约束(不越过)，确保价位现实可达。
    day_half = amp["avg"] / 2 if amp["avg"] > 0 else T_TARGET_SPREAD / 2
    day_half = max(1.0, min(day_half, T_TARGET_SPREAD))  # 夹在 [1%, 目标价差%]
    buy_target = cur * (1 - day_half / 100)
    sell_target = cur * (1 + day_half / 100)
    upper = tech.get("upper") or (cur * (1 + T_TARGET_SPREAD / 100))
    lower = tech.get("lower") or (cur * (1 - T_TARGET_SPREAD / 100))
    high20, low20 = amp["high20"], amp["low20"]
    # 低吸价：日内目标 与 近期低点 取较高(不追过低)；不高于现价；不低于止损安全垫
    buy = max(buy_target, low20) if low20 else buy_target
    buy = min(buy, cur)
    buy = max(buy, stop * (1 + T_STOP_SAFETY / 100))
    # 高抛价：日内目标 与 布林上轨/近期高点 取较低(不贪高)；不低于现价
    sell = min(sell_target, upper) if upper else sell_target
    if high20:
        sell = min(sell, high20)
    sell = max(sell, cur)
    # 若低吸被止损垫顶到接近现价导致价差过小，放宽到 T_TARGET_SPREAD 的一半
    if buy >= cur and stop > 0:
        buy = max(stop * (1 + T_STOP_SAFETY / 100), cur * (1 - T_TARGET_SPREAD / 200))

    # ── 做T股数（底仓的 T_POSITION_RATIO，取整百）──
    t_shares = int(qty * T_POSITION_RATIO) // 100 * 100
    t_shares = max(100, min(t_shares, int(qty)))

    # ── 降本测算 ──
    gross = (sell - buy) * t_shares
    fee = (buy + sell) * t_shares * T_COST_RATE / 100
    net = gross - fee
    cost_reduction = net / qty if qty else 0.0     # 每股成本下降
    cost_after = max(0.0, cost - cost_reduction)

    # ── 策略类型与操作指示 ──
    if not feasible:
        if too_close_to_stop:
            strategy = "不建议做T"
            reason = f"现价距止损位仅 {safety_pct:.1f}%，安全垫不足，优先执行止损纪律而非做T。"
        elif amp["avg"] < T_MIN_AMPLITUDE:
            strategy = "不建议做T"
            reason = f"日均振幅仅 {amp['avg']:.1f}%（<{T_MIN_AMPLITUDE}%），波动太小，做T难以覆盖手续费。"
        elif trend == "downtrend":
            strategy = "不建议做T"
            reason = f"处于单边下跌/破位（近5日{tech.get('week_chg', 0):+.1f}%），做T易越补越套，应控仓。"
        else:
            strategy = "观望"
            reason = f"做T评分 {score} 分偏低，机会一般，建议等待更大波动。"
        return {
            "feasible": False, "score": score, "strategy": strategy,
            "buy_price": None, "sell_price": None, "t_shares": 0,
            "cost_reduction": 0.0, "cost_after": cost,
            "pnl_pct": round(pnl_pct, 2), "amplitude": amp["avg"], "trend": trend,
            "reason": reason, "caution": "",
        }

    # 正T vs 倒T：浮亏且现价偏低位 → 正T(低吸后高抛)；否则倒T(高抛后低吸回补)
    pos = tech.get("position", "")
    if pnl_pct < 0 and ("下轨" in pos or cur <= (tech.get("ma20") or cur)):
        strategy = "正T（先低吸后高抛）"
        howto = (f"盘中回落至 {buy:.2f} 附近低吸 {t_shares} 股，"
                 f"反弹至 {sell:.2f} 附近高抛同等股数（当日买入部分不可卖，需卖底仓、留新买）。")
    else:
        strategy = "倒T（先高抛后低吸）"
        howto = (f"盘中冲高至 {sell:.2f} 附近先卖出底仓 {t_shares} 股，"
                 f"回落至 {buy:.2f} 附近买回同等股数，赚取价差。")

    reason = (f"日均振幅 {amp['avg']:.1f}%、近5日 {amp['recent']:.1f}%，{_trend_cn(trend)}，"
              f"具备做T空间；现价距止损 {safety_pct:.1f}% 安全垫充足。")

    return {
        "feasible": True, "score": score, "strategy": strategy,
        "buy_price": round(buy, 2), "sell_price": round(sell, 2),
        "t_shares": t_shares,
        "spread_pct": round((sell - buy) / buy * 100, 2) if buy else 0,
        "gross_profit": round(gross, 2), "net_profit": round(net, 2),
        "cost_reduction": round(cost_reduction, 4),
        "cost_after": round(cost_after, 2),
        "pnl_pct": round(pnl_pct, 2), "amplitude": amp["avg"], "trend": trend,
        "reason": reason, "howto": howto,
        "caution": (f"⚠️ 纪律：做T价差 {((sell-buy)/buy*100) if buy else 0:.1f}% 需覆盖手续费；"
                    f"若跌破止损 {stop:.2f} 元立即停止做T、执行止损，不摊平。"
                    f"单次仅用 {int(T_POSITION_RATIO*100)}% 底仓，分批操作。"),
    }


def _trend_cn(t: str) -> str:
    return {"range": "震荡整理", "uptrend": "上升通道", "downtrend": "下跌通道",
            "unknown": "趋势不明"}.get(t, t)


def render_t_brief(result: Dict, name: str, code: str) -> str:
    """把做T分析结果渲染成推送用的紧凑文本卡片。"""
    r = result
    head = f"🔄 **【做T机会】{name}({code})** "
    if not r.get("feasible"):
        return (head + f"| 评分 {r.get('score', 0)}\n"
                f"• 结论：**{r.get('strategy', '观望')}**\n"
                f"• {r.get('reason', '')}")
    lines = [
        head + f"| 机会评分 **{r['score']}** | {_trend_cn(r.get('trend',''))}",
        f"• 策略：**{r['strategy']}**",
        f"• 低吸 `{r['buy_price']:.2f}` → 高抛 `{r['sell_price']:.2f}`"
        f" (价差 {r.get('spread_pct', 0):.1f}%)",
        f"• 做T股数：{r['t_shares']} 股 | 净收益约 `{r.get('net_profit', 0):+.0f}元`",
        f"• 🎯 成本从 `{r.get('cost_after', 0) + r.get('cost_reduction', 0):.2f}`"
        f" 降至 `{r['cost_after']:.2f}`（每股降本 `{r['cost_reduction']:.3f}`元）",
        f"• 操作：{r.get('howto', '')}",
        f"• {r.get('caution', '')}",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    # 自检：模拟一只浮亏震荡股
    k = [
        {"high": 10.8 + i % 3 * 0.2, "low": 10.2 - i % 2 * 0.15,
         "close": 10.5 + (i % 3 - 1) * 0.15, "volume": 1000000}
        for i in range(20)
    ]
    r = analyze_t_opportunity("600409", "三友化工", 11.0, 10.5, 1000, k, 10.12)
    print(render_t_brief(r, "三友化工", "600409"))
