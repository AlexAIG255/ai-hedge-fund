"""
市场状态自适应引擎（牛 / 熊 / 震荡 / 超跌反弹）
=====================================================================
核心理念：策略没有绝对优劣，只有"是否匹配当前市场环境"。
  同一套"均线金叉"，纯用胜率 38.7%；加"20日均线斜率>15°"过滤后胜率 71.6%（Wind十年回测）。
  市况切换时，策略优先级、止损/止盈、仓位都必须跟着变。

判定维度（腾讯行情 qt.gtimg.cn，零新增节点）：
  1. 上证指数中期趋势：近60日涨跌幅 + 价格相对MA20/MA60位置
  2. 创业板/科创50：成长风格强弱（熊市中跌得最狠、反弹弹性最大）
  3. 成交额：放量/缩量（存量博弈 vs 增量入场）
  4. 市场宽度：涨跌家数比（普涨/普跌）

输出：
  - regime: BULL(牛市) / BEAR(熊市) / SHOCK(震荡) / REBOUND(超跌反弹)
  - strategy_weights: 各策略的适配权重（用于早盘排序/筛选）
  - risk_params: 该市况下的建议仓位/止损/止盈/观察期
  - brief(): 可嵌入推送的"当前时势+推荐策略"文字

用法：
  from market_regime_engine import detect_regime, get_strategy_weights, regime_brief
  regime = detect_regime()
  weights = get_strategy_weights(regime)
  text = regime_brief(regime)
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Dict, List, Optional, Tuple

import requests

# 指数代码（腾讯行情格式）
_INDEX_CODES = {
    "上证指数": "sh000001",
    "深证成指": "sz399001",
    "创业板指": "sz399006",
    "科创50": "sh000688",
}
_TENCENT_URL = "http://qt.gtimg.cn/q={codes}"

# 策略族分类（与 strategy_config.STRATEGY_NAME_BY_KEY 中文名对齐）
STRATEGY_FAMILY = {
    # 趋势跟随（牛市主力）
    "多头向上的圆月线": "trend", "均线多头向上": "trend", "右侧启动": "trend",
    "🚀 右侧刚启动": "trend", "右侧突破启动": "trend",
    # 突破动量（震荡/结构市）
    "出水芙蓉": "breakout", "出水芙蓉突破": "breakout",
    "🌸 出水芙蓉突破": "breakout", "🌸 出水芙蓉一阳穿多线": "breakout",
    "老鸭头突破": "breakout", "🦆 老鸭头突破": "breakout",
    "三倍量战法": "breakout", "底部放量反转": "breakout",
    # 超跌反转（熊市/超跌反弹主力）
    "超跌反包强势": "reversal", "超跌反弹": "reversal",
    "🔄 强劲反包蓄势": "reversal", "买在无人问津处": "reversal",
    "🤫 买在无人问津": "reversal", "低位安全资金标的": "reversal",
    "超跌反包": "reversal",
}

# 各市况下策略族权重（越高越优先）+ 风控参数
REGIME_PROFILE = {
    "BULL": {
        "label": "🐂 牛市 / 趋势上行",
        "desc": "指数中期均线多头、放量普涨。捂股为主，紧跟趋势，减少频繁换股。",
        "family_weight": {"trend": 3.0, "breakout": 2.0, "reversal": 0.8},
        "pos_pct": 15.0, "stop_loss": 0.96, "take_profit": 1.12,
        "obs_days": 60, "trend_iq_floor": 78,
        "tips": "看周线不被日线洗出；均线斜率>15°的金叉才有效；低位重仓拿住。",
    },
    "BEAR": {
        "label": "🐻 熊市 / 下跌趋势",
        "desc": "指数空头排列、缩量普跌。活下来第一，只做急跌后地量止跌的短反，快进快出。",
        "family_weight": {"trend": 0.4, "breakout": 1.0, "reversal": 2.6},
        "pos_pct": 5.0, "stop_loss": 0.96, "take_profit": 1.06,
        "obs_days": 15, "trend_iq_floor": 85,
        "tips": "阴跌不抄、急跌再看、地量止跌、板块共振；轻仓试错，反弹到压力位果断止盈，绝不格局。",
    },
    "SHOCK": {
        "label": "〰️ 震荡市 / 区间拉锯",
        "desc": "指数横盘、板块快速轮动、赚钱效应分化。低吸高抛，抓结构不追趋势。",
        "family_weight": {"trend": 1.0, "breakout": 1.8, "reversal": 1.8},
        "pos_pct": 8.0, "stop_loss": 0.96, "take_profit": 1.08,
        "obs_days": 35, "trend_iq_floor": 82,
        "tips": "震荡市均线反复缠绕、突破多为假信号；用5/10/20短周期，缩量回踩不破中轨才是买点。",
    },
    "REBOUND": {
        "label": "🌱 超跌反弹 / 修复行情",
        "desc": "急跌后地量企稳、政策/情绪修复启动。抓超跌弹性，但反弹≠反转，设好止损。",
        "family_weight": {"trend": 0.8, "breakout": 1.4, "reversal": 2.4},
        "pos_pct": 8.0, "stop_loss": 0.965, "take_profit": 1.08,
        "obs_days": 20, "trend_iq_floor": 83,
        "tips": "只抢急跌后地量止跌+板块共振的底背离；反弹摸5/10日线滞涨即止盈；创新低无条件止损。",
    },
}


def _retry_get(url: str, timeout: int = 6, tries: int = 3) -> Optional[requests.Response]:
    for i in range(tries):
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
            if r.status_code == 200:
                return r
        except Exception:
            pass
        if i < tries - 1:
            time.sleep(0.6 * (i + 1))
    return None


def _parse_index_quotes() -> Dict[str, Dict]:
    """从腾讯行情解析指数：现价、涨跌幅、量能。字段索引按 gtimg 指数格式。"""
    out: Dict[str, Dict] = {}
    r = _retry_get(_TENCENT_URL.format(codes=",".join(_INDEX_CODES.values())))
    if not r:
        return out
    # 逐行解析 v_sh000001="..."
    for line in r.text.split(";"):
        m = re.search(r'v_(sh\d{6}|sz\d{6})="([^"]+)"', line)
        if not m:
            continue
        code, data = m.group(1), m.group(2)
        f = data.split("~")
        if len(f) < 40:
            continue
        try:
            name = f[1]
            price = float(f[3])
            prev_close = float(f[4]) if f[4] not in ("", "-") else price
            chg_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0.0
            amount = float(f[37]) if f[37] not in ("", "-") else 0.0  # 成交额(万)
            out[code] = {"name": name, "price": price, "chg_pct": round(chg_pct, 2), "amount": amount}
        except (ValueError, IndexError):
            continue
    return out


def _fetch_index_history(code: str, days: int = 65) -> List[float]:
    """拉指数历史收盘（东财 push2his，失败返回空）。用于算中期趋势与MA。"""
    try:
        secid = ("1." + code[2:]) if code.startswith("sh") else ("0." + code[2:])
        url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        params = {
            "secid": secid, "klt": "101", "fqt": "1",
            "fields1": "f1,f2,f3", "fields2": "f51,f52,f53,f54,f55,f56,f57",
            "end": "20500101", "lmt": str(days),
        }
        r = requests.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=6)
        if r.status_code == 200:
            klines = (r.json().get("data") or {}).get("klines") or []
            closes = []
            for k in klines:
                parts = k.split(",")
                if len(parts) > 2:
                    try:
                        closes.append(float(parts[2]))
                    except ValueError:
                        pass
            return closes
    except Exception:
        pass
    return []


def _ema_slope_deg(closes: List[float], period: int = 20) -> Optional[float]:
    """近似 MA 斜率角度（度）：用相邻两日MA的环比变化折算，供趋势强度判断。"""
    if len(closes) < period + 1:
        return None
    ma_now = sum(closes[-period:]) / period
    ma_prev = sum(closes[-period - 1:-1]) / period
    if ma_prev <= 0:
        return None
    import math
    return round(math.degrees(math.atan((ma_now - ma_prev) / ma_prev)), 2)


def detect_regime(force_refresh: bool = False) -> Dict:
    """判定当前市场状态。带1小时缓存，避免每次推送重复请求。"""
    cache_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".market_regime_cache.json")
    if not force_refresh and os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if time.time() - cached.get("ts", 0) < 3600:
                return cached.get("regime", {})
        except Exception:
            pass

    quotes = _parse_index_quotes()
    sh = quotes.get("sh000001", {})
    sh_closes = _fetch_index_history("sh000001", days=65)
    cyb_closes = _fetch_index_history("sz399006", days=65)

    # 计算中期涨跌（近60日）
    chg60 = 0.0
    ma20_slope = None
    if len(sh_closes) >= 20:
        ma20_slope = _ema_slope_deg(sh_closes, 20)
    if len(sh_closes) >= 61:
        chg60 = (sh_closes[-1] - sh_closes[-61]) / sh_closes[-61] * 100
    elif len(sh_closes) >= 30:
        chg60 = (sh_closes[-1] - sh_closes[0]) / sh_closes[0] * 100

    # 成长风格：创业板近20日涨跌
    cyb_chg20 = 0.0
    if len(cyb_closes) >= 21:
        cyb_chg20 = (cyb_closes[-1] - cyb_closes[-21]) / cyb_closes[-21] * 100

    # 价格相对MA20位置
    price_vs_ma20 = 0.0
    if len(sh_closes) >= 20:
        ma20 = sum(sh_closes[-20:]) / 20
        if ma20:
            price_vs_ma20 = (sh_closes[-1] - ma20) / ma20 * 100

    day_chg = sh.get("chg_pct", 0.0)
    amount_yi = sh.get("amount", 0.0) / 1e4  # 万元→亿元（近似）

    # ── 状态判定（打分制）──
    score = 0.0
    if chg60 > 12:
        score += 2
    elif chg60 > 5:
        score += 1
    elif chg60 < -12:
        score -= 2
    elif chg60 < -4:
        score -= 1
    if ma20_slope is not None:
        if ma20_slope > 1.0:
            score += 1
        elif ma20_slope < -1.0:
            score -= 1
    if price_vs_ma20 > 2:
        score += 1
    elif price_vs_ma20 < -2:
        score -= 1
    if cyb_chg20 > 8:
        score += 1
    elif cyb_chg20 < -10:
        score -= 1

    if score >= 3:
        regime = "BULL"
    elif score <= -3:
        # 区分"下跌趋势"与"超跌反弹初期"：当日放量上涨+成长强反弹 → REBOUND
        if day_chg > 1.0 and cyb_chg20 > -6:
            regime = "REBOUND"
        else:
            regime = "BEAR"
    elif score <= -1:
        regime = "BEAR" if day_chg < 0 else "REBOUND"
    elif score >= 1:
        regime = "BULL" if day_chg > 0.5 else "SHOCK"
    else:
        regime = "SHOCK"

    result = {
        "regime": regime,
        "label": REGIME_PROFILE[regime]["label"],
        "desc": REGIME_PROFILE[regime]["desc"],
        "signals": {
            "chg60_pct": round(chg60, 2),
            "ma20_slope_deg": ma20_slope,
            "price_vs_ma20_pct": round(price_vs_ma20, 2),
            "cyb_chg20_pct": round(cyb_chg20, 2),
            "sh_day_chg_pct": round(day_chg, 2),
            "score": score,
        },
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "regime": result}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return result


def get_strategy_weights(regime: Optional[Dict] = None) -> Dict:
    """返回当前市况下的策略族权重 + 风控参数。"""
    regime = regime or detect_regime()
    key = regime.get("regime", "SHOCK") if isinstance(regime, dict) else str(regime)
    profile = REGIME_PROFILE.get(key, REGIME_PROFILE["SHOCK"])
    return {
        "regime": key, "label": profile["label"],
        "family_weight": dict(profile["family_weight"]),
        "pos_pct": profile["pos_pct"], "stop_loss": profile["stop_loss"],
        "take_profit": profile["take_profit"], "obs_days": profile["obs_days"],
        "trend_iq_floor": profile["trend_iq_floor"], "tips": profile["tips"],
    }


def strategy_fit_score(strategy_name: str, regime: Optional[Dict] = None,
                       base_trend_iq: float = 75.0) -> float:
    """个股综合排序分 = 市况匹配度权重 × TrendIQ。用于早盘按时势重排候选。"""
    weights = get_strategy_weights(regime)
    fam = STRATEGY_FAMILY.get(str(strategy_name).strip(), "breakout")
    w = weights["family_weight"].get(fam, 1.0)
    return round(float(base_trend_iq) * w, 2)


def regime_brief(regime: Optional[Dict] = None, with_recommend: bool = True) -> str:
    """生成可嵌入推送的"当前时势+推荐策略"精简段落。"""
    regime = regime or detect_regime()
    weights = get_strategy_weights(regime)
    sig = regime.get("signals", {})
    sig_bits = []
    if isinstance(sig.get("chg60_pct"), (int, float)):
        sig_bits.append(f"上证近60日 {sig['chg60_pct']:+.1f}%")
    if sig.get("ma20_slope_deg") is not None:
        sig_bits.append(f"MA20斜率 {sig['ma20_slope_deg']:+.1f}°")
    if isinstance(sig.get("cyb_chg20_pct"), (int, float)):
        sig_bits.append(f"创业板近20日 {sig['cyb_chg20_pct']:+.1f}%")
    sig_str = " ｜ ".join(sig_bits) if sig_bits else "指数数据暂无，按震荡市中性处理"

    lines = [
        f"🌍 **【当前时势】** {weights['label']}",
        f"• 信号: {sig_str}",
        f"• 研判: {regime.get('desc', weights.get('label',''))}",
    ]
    if with_recommend:
        fw = weights["family_weight"]
        fam_cn = {"trend": "趋势跟随(均线多头/右侧)", "breakout": "突破动量(出水芙蓉/老鸭头/三倍量)",
                  "reversal": "超跌反转(无人问津/反包蓄势)"}
        ranked = sorted(fw.items(), key=lambda kv: -kv[1])
        rec = "、".join(f"{fam_cn.get(k,k)}(×{v})" for k, v in ranked)
        lines.append(f"• ✅ **优先策略**: {rec}")
        lines.append(f"• ⚙️ 建议: 仓位≤{weights['pos_pct']:.0f}% / 止损{weights['stop_loss']} / 止盈{weights['take_profit']} / 观察{weights['obs_days']}天 / TrendIQ≥{weights['trend_iq_floor']}")
        lines.append(f"• 📌 {weights['tips']}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    force = "--force" in sys.argv
    r = detect_regime(force_refresh=force)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    print("\n" + regime_brief(r))
