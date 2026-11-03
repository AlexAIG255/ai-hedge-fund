"""
trading_calendar.py — A 股交易日历（节假日自动跳过）
=====================================================================
- 优先使用 `chinese_calendar` 包（最准确，内置周末 + 法定节假日 + 调休补班），
  安装： pip install chinese_calendar
- 未安装时回退到：周末 + 内置 A 股休市表（2025/2026，数据来自沪深交易所官方公告）
- 供 scheduler/run_job.sh 与各定时任务调用：今日休市则优雅跳过，不刷屏、不浪费 API。

用法:
    from trading_calendar import is_trading_day, today_is_trading_day, next_trading_day

命令行（供 run_job.sh 守卫）:
    python trading_calendar.py   # 是交易日 -> 退出码 0；休市 -> 退出码 1
"""

from __future__ import annotations

import datetime
import sys
from typing import Optional

# ───────────────────────── 内置 A 股休市区间（兜底用） ─────────────────────────
# 数据来自沪深北交易所官方公告；调休补班日以 chinese_calendar 为准。
# 仅作为『未安装 chinese_calendar 包』时的兜底，建议生产环境安装 chinese_calendar。
_HOLIDAY_RANGES = [
    # 2025
    ("2025-01-01", "2025-01-01"),   # 元旦
    ("2025-01-28", "2025-02-04"),   # 春节
    ("2025-04-04", "2025-04-06"),   # 清明
    ("2025-05-01", "2025-05-05"),   # 劳动节
    ("2025-05-31", "2025-06-02"),   # 端午
    ("2025-10-01", "2025-10-08"),   # 国庆 + 中秋
    # 2026
    ("2026-01-01", "2026-01-03"),   # 元旦
    ("2026-02-15", "2026-02-23"),   # 春节
    ("2026-04-04", "2026-04-06"),   # 清明
    ("2026-05-01", "2026-05-05"),   # 劳动节
    ("2026-06-19", "2026-06-21"),   # 端午
    ("2026-09-25", "2026-09-27"),   # 中秋
    ("2026-10-01", "2026-10-07"),   # 国庆
]

_HOLIDAY_SET = set()
for _s, _e in _HOLIDAY_RANGES:
    _d0 = datetime.date.fromisoformat(_s)
    _d1 = datetime.date.fromisoformat(_e)
    _cur = _d0
    while _cur <= _d1:
        _HOLIDAY_SET.add(_cur)
        _cur += datetime.timedelta(days=1)

# 内置休市表覆盖的年份（以沪深交易所官方公告为准，权威）。
# 注意：chinese_calendar 的个别年份数据存在错误（如 2026-05-09 被误判为补班），
# 故覆盖年份内一律以本表为准；表未覆盖的年份（如 2027+）才回退 chinese_calendar。
_COVERED_YEARS = {int(s.split("-")[0]) for s, _ in _HOLIDAY_RANGES}


def _has_chinese_calendar() -> bool:
    try:
        import chinese_calendar  # noqa: F401
        return True
    except Exception:
        return False


def is_trading_day(dt: Optional[datetime.date] = None) -> bool:
    """判断某天是否为 A 股交易日（开市）。

    优先级：内置官方休市表（覆盖年份）> chinese_calendar（未覆盖年份）> 周末+休市表兜底。
    """
    dt = dt or datetime.date.today()

    # 周末：覆盖年份内直接判休市；未覆盖年份才信任 chinese_calendar 的调休补班
    if dt.weekday() >= 5:          # 5=周六, 6=周日
        if dt.year not in _COVERED_YEARS and _has_chinese_calendar():
            import chinese_calendar
            return chinese_calendar.is_workday(dt)
        return False

    # 工作日
    if dt.year in _COVERED_YEARS:
        return dt not in _HOLIDAY_SET
    if _has_chinese_calendar():
        import chinese_calendar
        return chinese_calendar.is_workday(dt)
    return dt not in _HOLIDAY_SET


def today_is_trading_day() -> bool:
    return is_trading_day(datetime.date.today())


def next_trading_day(dt: Optional[datetime.date] = None) -> datetime.date:
    dt = dt or datetime.date.today()
    while True:
        dt += datetime.timedelta(days=1)
        if is_trading_day(dt):
            return dt


if __name__ == "__main__":
    # 给 run_job.sh 用：是交易日返回 0，否则返回 1（脚本据此跳过）
    sys.exit(0 if today_is_trading_day() else 1)
