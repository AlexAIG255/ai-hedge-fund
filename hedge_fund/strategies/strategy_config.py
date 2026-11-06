"""
strategy_config.py — 全局策略与风控配置中心
=====================================================================
集中管理你最关心的三件事：
  1. 持续观察周期（到达后强制清算平仓）
  2. 止损线 / 止盈线 比例（相对建仓价）
  3. 追踪止损（保本线 + 移动止盈保护）
并统一策略名称映射与行情代码前缀，供 morning_picker / evening_review 共用。

你日常调参只改这个文件即可，不用动两个 Agent 的逻辑。
"""

import os
from typing import Dict, List, Tuple

# ───────────────────────── 1. 策略名称映射（key ↔ 中文名） ─────────────────────────
# morning_picker 内部用 key（如 TRIPLE_VOLUME），evening_review / 飞书用中文名。
STRATEGY_NAME_BY_KEY = {
    "BOTTOM_REVERSAL": "底部放量反转",
    "TREND_FOLLOWING": "多头向上的圆月线",
    "RIGHT_SIDE_LAUNCH": "右侧启动",
    "LOTUS_BREAKOUT": "出水芙蓉",
    "OVERSOLD_ENGULFING": "超跌反包强势",
    "OVERSOLD_BOUNCE": "超跌反弹",
    "DESERTED_LOW_BUY": "买在无人问津处",
    "TRIPLE_VOLUME": "三倍量战法",
}


def strategy_name(key_or_name) -> str:
    """兼容策略 key 与中文名，统一返回中文名（飞书 / 复盘统一口径）。"""
    if key_or_name in STRATEGY_NAME_BY_KEY:
        return STRATEGY_NAME_BY_KEY[key_or_name]
    return str(key_or_name) if key_or_name else "综合选股"


def is_triple_volume(strategy) -> bool:
    """判断是否『三倍量战法』——它用更激进的止盈/更紧的止损。"""
    return "三倍量" in (str(strategy) or "")


# ───────────────────────── 2. 止损 / 止盈 比例（相对建仓价） ─────────────────────────
# 例如 0.95 表示：止损价 = 建仓价 × 0.95（即 -5% 离场）。
STOP_LOSS_RATIO = {
    "default": 0.96,      # 稳健型：默认 -4% 止损（少利润、先保本金）
    "三倍量战法": 0.97,    # 三倍量：更紧， -3% 止损
}
TAKE_PROFIT_RATIO = {
    "default": 1.08,      # 综合选股：默认 +8% 止盈
    "三倍量战法": 1.10,    # 三倍量：更激进， +10% 止盈
}


# ───────────────────────── 3. 持续观察周期（自然日） ─────────────────────────
# 这是你要求的『推荐股票需要持续观察一定时间』的核心参数。
# 到达观察天数仍未触碰止损/止盈，则到期强制清算（到期平仓）。
DEFAULT_OBSERVATION_DAYS = 45
OBSERVATION_DAYS_BY_STRATEGY = {
    "三倍量战法": 20,          # 动量短线，周期缩短，避免拖太久
    "买在无人问津处": 60,      # 慢牛缩量，周期放长，给足蓄势时间
    "default": DEFAULT_OBSERVATION_DAYS,
}


# ───────────────────────── 4. 追踪止损（移动止盈保护） ─────────────────────────
# 解决『涨上去又跌回来』的利润回吐问题：
#   - 浮盈 ≥ BREAKEVEN_AFTER_PCT 时，止损线抬升到成本（保本，绝不亏本出）。
#   - 浮盈 ≥ TRAILING_TRIGGER_PCT 时，启动追踪：止损线跟随阶段峰值回撤 TRAILING_STOP_RATIO。
TRAILING_TRIGGER_PCT = 5.0      # 浮盈 ≥ 5% 才启动追踪止损
TRAILING_STOP_RATIO = 0.95      # 从阶段峰值回撤 5% 即触发保护抛售
BREAKEVEN_AFTER_PCT = 8.0       # 浮盈 ≥ 8% 后止损线抬升至成本（保本）


# ───────────────────────── 5. 取参工具函数 ─────────────────────────
def get_stop_loss_ratio(strategy: str) -> float:
    key = "三倍量战法" if is_triple_volume(strategy) else "default"
    return STOP_LOSS_RATIO.get(key, STOP_LOSS_RATIO["default"])


def get_take_profit_ratio(strategy: str) -> float:
    key = "三倍量战法" if is_triple_volume(strategy) else "default"
    return TAKE_PROFIT_RATIO.get(key, TAKE_PROFIT_RATIO["default"])


def get_observation_days(strategy: str) -> int:
    name = strategy_name(strategy)
    return OBSERVATION_DAYS_BY_STRATEGY.get(name, OBSERVATION_DAYS_BY_STRATEGY["default"])


def compute_stop_loss(entry_price: float, strategy: str) -> float:
    if entry_price <= 0:
        return 0.0
    return round(entry_price * get_stop_loss_ratio(strategy), 2)


def compute_take_profit(entry_price: float, strategy: str) -> float:
    if entry_price <= 0:
        return 0.0
    return round(entry_price * get_take_profit_ratio(strategy), 2)


def compute_effective_stop(entry_price: float, peak_price: float, base_stop: float,
                           ret_pct: float) -> float:
    """
    计算『当前有效止损价』：在基础止损价之上，叠加保本线与追踪止损。
      - ret_pct ≥ BREAKEVEN_AFTER_PCT → 抬升至成本（保本）
      - ret_pct ≥ TRAILING_TRIGGER_PCT → 跟随 peak 回撤 TRAILING_STOP_RATIO
    返回三者取大，确保越涨止损越高、利润越锁越牢。
    """
    effective = float(base_stop)
    if ret_pct >= BREAKEVEN_AFTER_PCT:
        effective = max(effective, float(entry_price))          # 保本线
    if ret_pct >= TRAILING_TRIGGER_PCT:
        trailing = round(float(peak_price) * TRAILING_STOP_RATIO, 2)
        effective = max(effective, trailing)                    # 追踪止损线
    return round(effective, 2)


def to_market_code(code: str) -> str:
    """A 股代码 → 行情接口前缀（sh / sz / bj）。"""
    code = str(code).zfill(6)
    if code.startswith(("60", "68", "688")):
        return f"sh{code}"
    if code.startswith(("00", "30")):
        return f"sz{code}"
    if code.startswith(("8", "4", "9", "920", "83", "87", "43")):
        return f"bj{code}"
    return f"sh{code}"


# ───────────────────────── 6. 盘中异动监控阈值 ─────────────────────────
# 盘中监控（intraday_monitor.py）只用『异动提醒』，不再做止盈止损平仓。
# 以下为异动判定阈值，统一在此调参；支持用环境变量覆盖（见 get_anomaly_thresholds）。
# 环境变量键名：ANOMALY_STRONG_PCT / ANOMALY_WEAK_PCT / ANOMALY_VOL_RATIO /
#              ANOMALY_TURNOVER / ANOMALY_AMPLITUDE
ANOMALY_THRESHOLDS = {
    "strong_pct": 5.0,       # 涨幅 ≥ 该值(%) → 强势异动（放量上攻）
    "weak_pct": -5.0,        # 跌幅 ≤ 该值(%) → 弱势异动（风险提醒）
    "vol_ratio": 3.0,        # 量比 ≥ 该值    → 放量异动
    "turnover": 8.0,         # 换手率 ≥ 该值(%) → 放量异动
    "amplitude": 6.0,        # 振幅 ≥ 该值(%)  → 振幅异动（日内波动剧烈）
}


def _env_float(key: str, default: float) -> float:
    try:
        v = os.environ.get(key)
        return float(v) if v not in (None, "") else default
    except (ValueError, TypeError):
        return default


def get_anomaly_thresholds() -> dict:
    """返回盘中异动阈值（环境变量覆盖默认值），供监控模块调用。"""
    t = dict(ANOMALY_THRESHOLDS)
    t["strong_pct"] = _env_float("ANOMALY_STRONG_PCT", t["strong_pct"])
    t["weak_pct"] = _env_float("ANOMALY_WEAK_PCT", t["weak_pct"])
    t["vol_ratio"] = _env_float("ANOMALY_VOL_RATIO", t["vol_ratio"])
    t["turnover"] = _env_float("ANOMALY_TURNOVER", t["turnover"])
    t["amplitude"] = _env_float("ANOMALY_AMPLITUDE", t["amplitude"])
    return t


# ───────────────────────── 7. 稳健型模式（右侧交易 + 重视资金 + 控风险） ─────────────────────────
# 用户明确偏好：稳健型，以右侧交易为主、重视资金面，可接受少利润但避免高风险。
# 该模式在「保留原 8 大策略信号」基础上叠加硬性过滤与再排序，提高胜率、降低回撤。
STEADY_MODE = os.environ.get("STEADY_MODE", "true").lower() in ["true", "1", "yes"]

STEADY_PARAMS = {
    # ① 重视资金：要求当日主力净流入 > 0（元），资金不流入一票否决
    "require_main_inflow": True,
    "min_main_inflow_yi": 0.0,          # 主力净流入下限（亿元），0 表示只要为正即可
    # ② 右侧交易：要求现价 > MA20 且 MA20 向上（不在下降趋势里抄底）
    "require_right_side": True,
    # ③ 控风险：剔除小盘妖股，流通市值 ≥ 该值（元）。默认 50 亿
    "min_float_mcap": float(os.environ.get("STEADY_MIN_FLOAT_MCAP", "5e9")),
    # ④ 不追高：60 日涨幅上限（%），避免追入已大幅拉升的标的
    "max_pct_60d": float(os.environ.get("STEADY_MAX_PCT_60D", "40")),
    # ⑤ 估值护栏：PE(TTM) 上限（亏损股 PE 为负，单独放行但会在风险提示里标注）
    "max_pe": float(os.environ.get("STEADY_MAX_PE", "80")),
    # ⑥ 稳健型更紧的止损（相对建仓价）：少利润、先保本金
    "steady_stop_loss_ratio": 0.96,     # -4% 止损（原默认 -5%）
}

# 稳健型更偏好的策略（排序时优先）：右侧/趋势/无人问津 优于 三倍量/超跌反弹
STEADY_STRATEGY_PRIORITY = {
    "多头向上的圆月线": 0,
    "右侧启动": 1,
    "买在无人问津处": 2,
    "出水芙蓉": 3,
    "底部放量反转": 4,
    "超跌反包强势": 5,
    "超跌反弹": 6,
    "三倍量战法": 7,
}


def get_steady_params() -> dict:
    return dict(STEADY_PARAMS)


def apply_steady_filter(item: Dict, capital_net: float = None, float_mcap: float = None,
                        ma20: float = 0.0, ma20_prev: float = 0.0, price: float = 0.0,
                        pe: float = None) -> Tuple[bool, List[str]]:
    """
    稳健型硬性过滤。返回 (是否通过, 未通过原因列表)。
    任一硬性条件不满足即淘汰；用于「保留原策略信号 + 叠加稳健护栏」。
    """
    p = STEADY_PARAMS
    blocked = []

    # ① 重视资金：主力净流入必须为正
    if p["require_main_inflow"]:
        if capital_net is None or capital_net <= p["min_main_inflow_yi"] * 1e8:
            blocked.append("主力资金未净流入")

    # ② 右侧交易：现价 > MA20 且 MA20 向上
    if p["require_right_side"]:
        if price > 0 and ma20 > 0 and price < ma20:
            blocked.append("价格跌破MA20(非右侧)")
        if ma20 > 0 and ma20_prev > 0 and ma20 <= ma20_prev:
            blocked.append("MA20未向上(趋势未确认)")

    # ③ 剔除小盘妖股
    if float_mcap is not None and float_mcap < p["min_float_mcap"]:
        blocked.append(f"流通市值过小(<{p['min_float_mcap']/1e8:.0f}亿)")

    # ④ 不追高
    pct_60d = float(item.get("pct_60d", 0) or 0)
    if pct_60d > p["max_pct_60d"]:
        blocked.append(f"60日涨幅过大({pct_60d:+.0f}%)")

    # ⑤ 估值护栏（PE 为正且超上限才淘汰；负值亏损股放行并标注）
    if pe is not None and pe > p["max_pe"]:
        blocked.append(f"估值偏高(PE {pe:.0f})")

    return (len(blocked) == 0), blocked


def steady_priority(strategy: str) -> int:
    """稳健型排序优先级（越小越优先）。"""
    name = strategy_name(strategy)
    return STEADY_STRATEGY_PRIORITY.get(name, 9)
