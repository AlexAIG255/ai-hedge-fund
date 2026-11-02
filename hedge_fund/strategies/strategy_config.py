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

from typing import Dict

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
    "default": 0.95,      # 综合选股：默认 -5% 止损
    "三倍量战法": 0.96,    # 三倍量：更紧， -4% 止损
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
