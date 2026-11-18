"""
推送策略 Skill 引擎（胜率 / 归因 / 自动升级迭代）
=====================================================================
目标：把"每次推送的胜率 + 盈亏归因 + 经验教训"沉淀成一个可量化、
      可版本化、可自动升级迭代的 Skill，供早盘/晚间/持仓三个 Agent 共用。

核心能力：
  1. compute_win_rate()   —— 统一终态口径，用 total_return 判定真实胜负
                             （修复旧逻辑只数 WIN 状态导致胜率虚低到 7.5% 的 bug）
  2. attribute_outcomes() —— 基于 reason/close_reason/news_attrib 做盈亏归因分类统计
  3. evolve_skill()       —— 对比上一版指标，自动调参（TrendIQ 门槛/止损/仓位/风口加权），
                             生成版本化 Skill 文档 push_skill.md + 结构化 push_skill_state.json
  4. render_push_summary()—— 生成可嵌入推送的"胜率+归因+Skill版本"精简段落

用法：
  from push_skill_engine import render_push_summary, evolve_skill
  evolve_skill(tracker_data)              # 晚间复盘末尾调用，自动升级
  render_push_summary(tracker_data)       # 推送里展示真实胜率与归因

设计原则：零外部依赖、纯标准库、异常不中断主流程。
"""
from __future__ import annotations

import json
import os
import time
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple

# 终态集合（与 evening_review.TERMINAL_STATUSES 保持一致）
TERMINAL_STATUSES = {"WIN", "LOSS", "WIN_CLOSE", "LOSS_CLOSE", "CLOSED"}
STOPLOSS_STATUSES = {"LOSS", "LOSS_CLOSE"}

_HERE = os.path.dirname(os.path.abspath(__file__))
# 仓库根目录（strategies 的上一级再上一级）
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir))
SKILL_MD = os.path.join(_REPO_ROOT, "push_skill.md")
SKILL_STATE = os.path.join(_REPO_ROOT, "push_skill_state.json")
TRACKER_FILE = os.path.join(_REPO_ROOT, "portfolio_tracker.json")

# 可调参数初始值（自动升级会重写这些）
_DEFAULT_PARAMS = {
    "trend_iq_floor": 80,        # 入选 TrendIQ 硬门槛
    "stop_loss_ratio": 0.95,     # 止损线（相对成本）
    "suggest_pos_pct": 10.0,     # 单只建议仓位
    "require_capital_inflow": False,  # 是否要求主力净流入
    "favor_news_bull": True,     # 甄别利多新闻是否加权
}


def _is_terminal(status) -> bool:
    return str(status or "").strip().upper() in TERMINAL_STATUSES


def load_tracker(path: str = TRACKER_FILE) -> List[Dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else (data.get("records", []) if isinstance(data, dict) else [])
    except Exception:
        return []


def _outcome(item: Dict) -> Optional[str]:
    """统一判定单条结案标的的最终胜负：win / loss / flat / None(未结案)。"""
    if not _is_terminal(item.get("status")):
        return None
    tr = item.get("total_return")
    if isinstance(tr, (int, float)):
        if tr > 0.3:      # 千分位噪声，>0.3% 才算盈利
            return "win"
        if tr < -0.3:
            return "loss"
        return "flat"
    # 无收益字段时退回状态判定
    st = str(item.get("status", "")).upper()
    if st in {"WIN", "WIN_CLOSE"}:
        return "win"
    if st in STOPLOSS_STATUSES:
        return "loss"
    return "flat"


def compute_win_rate(tracker_data: List[Dict]) -> Dict:
    """真实胜率统计（口径统一、含 CLOSED 的 total_return）。"""
    settled = [x for x in tracker_data if _is_terminal(x.get("status"))]
    outcomes = [_outcome(x) for x in settled]
    outcomes = [o for o in outcomes if o]
    total = len(outcomes)
    win = outcomes.count("win")
    loss = outcomes.count("loss")
    flat = outcomes.count("flat")
    decided = win + loss  # 胜率分母用"非平局"
    win_rate = (win / decided * 100) if decided else 0.0
    # 活跃持仓平均收益
    active = [x for x in tracker_data if str(x.get("status", "")).upper() == "TRACKING"]
    act_rets = [x["total_return"] for x in active if isinstance(x.get("total_return"), (int, float))]
    avg_active = (sum(act_rets) / len(act_rets)) if act_rets else 0.0
    # 所有结案平均收益
    all_rets = [x["total_return"] for x in settled if isinstance(x.get("total_return"), (int, float))]
    avg_all = (sum(all_rets) / len(all_rets)) if all_rets else 0.0
    return {
        "settled": total, "win": win, "loss": loss, "flat": flat,
        "win_rate": round(win_rate, 2),
        "avg_active": round(avg_active, 2),
        "avg_all": round(avg_all, 2),
        "active": len(active),
    }


def _classify_reason(item: Dict) -> str:
    text = f"{item.get('reason') or ''} {item.get('close_reason') or ''} {item.get('news_attrib') or ''}"
    if any(k in text for k in ("止盈", "达标", "突破目标", "获利")):
        return "达标止盈"
    if any(k in text for k in ("止损", "风控", "防守离场", "触损")):
        return "触及止损"
    if any(k in text for k in ("到期", "观察期", "期满")):
        return "到期清算"
    if any(k in text for k in ("大盘", "板块", "系统性", "回调")):
        return "大盘/板块拖累"
    return "策略波动"


def attribute_outcomes(tracker_data: List[Dict]) -> Dict:
    """盈亏归因分类统计 + 提炼主导因素。"""
    reason_win: Counter = Counter()
    reason_loss: Counter = Counter()
    strat_stat: Dict[str, Dict[str, int]] = defaultdict(lambda: {"win": 0, "loss": 0, "flat": 0})
    for x in tracker_data:
        o = _outcome(x)
        if not o:
            continue
        cat = _classify_reason(x)
        if o == "win":
            reason_win[cat] += 1
        elif o == "loss":
            reason_loss[cat] += 1
        strat = str(x.get("strategy") or "综合选股")
        strat_stat[strat][o] += 1

    win_top = reason_win.most_common(3)
    loss_top = reason_loss.most_common(3)
    # 策略胜率排名（样本>=3）
    strat_rank = []
    for s, d in strat_stat.items():
        decided = d["win"] + d["loss"]
        if decided >= 3:
            strat_rank.append((s, round(d["win"] / decided * 100, 1), decided))
    strat_rank.sort(key=lambda t: t[1], reverse=True)

    if win_top:
        win_cause = f"主要来自「{win_top[0][0]}」({win_top[0][1]}次)"
    else:
        win_cause = "样本不足，暂无稳定盈利归因"
    if loss_top:
        loss_cause = f"主要来自「{loss_top[0][0]}」({loss_top[0][1]}次)"
    else:
        loss_cause = "样本不足，暂无稳定亏损归因"

    return {
        "reason_win": dict(reason_win),
        "reason_loss": dict(reason_loss),
        "win_cause": win_cause,
        "loss_cause": loss_cause,
        "strategy_rank": strat_rank,
    }


def _load_state() -> Dict:
    try:
        with open(SKILL_STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: Dict):
    try:
        with open(SKILL_STATE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 保存 Skill 状态失败: {e}")


def _auto_tune(params: Dict, wr: Dict, attr: Dict) -> Tuple[Dict, List[str]]:
    """根据胜率与归因自动微调参数，返回(新参数, 变更说明列表)。"""
    new = dict(params)
    changes: List[str] = []
    rate = wr["win_rate"]
    n = wr["settled"]

    # 样本充足才调参，避免小样本过拟合
    if n >= 20:
        if rate < 40:
            new["trend_iq_floor"] = min(92, new["trend_iq_floor"] + 2)
            new["stop_loss_ratio"] = max(0.93, round(new["stop_loss_ratio"] - 0.005, 3))
            new["suggest_pos_pct"] = max(5.0, round(new["suggest_pos_pct"] - 1.0, 1))
            changes.append(f"胜率{rate:.1f}%偏低(<40%)：TrendIQ门槛↑{new['trend_iq_floor']}、止损收紧至{new['stop_loss_ratio']}、仓位降至{new['suggest_pos_pct']}%")
        elif rate < 50:
            new["trend_iq_floor"] = min(92, new["trend_iq_floor"] + 1)
            changes.append(f"胜率{rate:.1f}%偏弱：TrendIQ门槛微调↑{new['trend_iq_floor']}")
        elif rate >= 60:
            new["trend_iq_floor"] = max(70, new["trend_iq_floor"] - 1)
            new["suggest_pos_pct"] = min(15.0, round(new["suggest_pos_pct"] + 0.5, 1))
            changes.append(f"胜率{rate:.1f}%良好：适度放宽门槛至{new['trend_iq_floor']}、仓位升至{new['suggest_pos_pct']}%")

    # 亏损主因是止损 → 强化资金面过滤
    loss_reasons = attr.get("reason_loss", {})
    if loss_reasons.get("触及止损", 0) >= 5 and not new.get("require_capital_inflow"):
        new["require_capital_inflow"] = True
        changes.append("触及止损频次偏高：启用『主力净流入』硬性过滤")

    # 盈利主因与消息/突破相关 → 保留利多新闻加权
    if attr.get("win_cause", "") and "止盈" in attr["win_cause"]:
        new["favor_news_bull"] = True

    if not changes:
        changes.append("指标稳定，参数维持不变")
    return new, changes


def evolve_skill(tracker_data: Optional[List[Dict]] = None, auto_evolve: Optional[bool] = None) -> Dict:
    """自动升级 Skill：计算真实胜率→归因→调参→写版本化文档+状态。"""
    if auto_evolve is None:
        auto_evolve = os.environ.get("SKILL_AUTO_EVOLVE", "true").lower() in ("true", "1", "yes")
    tracker_data = tracker_data if tracker_data is not None else load_tracker()
    wr = compute_win_rate(tracker_data)
    attr = attribute_outcomes(tracker_data)

    state = _load_state()
    prev_params = state.get("params", _DEFAULT_PARAMS)
    prev_rate = state.get("last_win_rate")
    version = int(state.get("version", 0)) + 1

    if auto_evolve:
        params, changes = _auto_tune(prev_params, wr, attr)
    else:
        params, changes = dict(prev_params), ["自动升级已关闭(SKILL_AUTO_EVOLVE=false)"]

    delta = ""
    if isinstance(prev_rate, (int, float)):
        d = wr["win_rate"] - prev_rate
        delta = f"{d:+.2f}pp（上版 {prev_rate:.2f}%）"

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    strat_lines = "\n".join(
        f"  - {s}: 胜率 {r}% (样本 {n})" for s, r, n in attr["strategy_rank"][:8]
    ) or "  - 暂无足够样本"

    md = f"""# 📈 推送策略 Skill（自动升级版）

> 本文件由 `push_skill_engine.py` 自动生成，每次晚间复盘自动迭代。请勿手动编辑。

## 版本信息
- **当前版本**: v{version}
- **更新时间**: {now}
- **真实胜率**: **{wr['win_rate']:.2f}%** {delta}
  - 结案样本 {wr['settled']} 只（胜 {wr['win']} / 负 {wr['loss']} / 平 {wr['flat']}）
  - 结案平均收益 {wr['avg_all']:+.2f}% ｜ 活跃持仓 {wr['active']} 只，平均 {wr['avg_active']:+.2f}%
- **口径说明**: 胜率 = 胜/(胜+负)，以 `total_return>0` 判定，统一纳入 WIN/LOSS/CLOSED/WIN_CLOSE/LOSS_CLOSE 全终态
  （修复旧逻辑仅统计 WIN 状态导致胜率虚低至 7.5% 的问题）

## 🎯 盈亏归因
- **盈利主因**: {attr['win_cause']}
- **亏损主因**: {attr['loss_cause']}
- **盈利原因分布**: {attr['reason_win'] or '暂无'}
- **亏损原因分布**: {attr['reason_loss'] or '暂无'}

## 🏆 策略胜率排名（样本≥3）
{strat_lines}

## ⚙️ 当前生效参数（自动调参）
| 参数 | 值 | 说明 |
|------|-----|------|
| TrendIQ 入选门槛 | {params['trend_iq_floor']} | 低于此分不入选 |
| 止损线 | {params['stop_loss_ratio']} | 相对成本价 |
| 单只建议仓位 | {params['suggest_pos_pct']}% | 风控上限 |
| 主力净流入硬过滤 | {'开' if params.get('require_capital_inflow') else '关'} | 净流出标的剔除 |
| 利多新闻加权 | {'开' if params.get('favor_news_bull') else '关'} | 甄别后利多消息加分 |

## 🔄 本次迭代动作
{chr(10).join('- ' + c for c in changes)}

## 📌 迭代机制
每次晚间复盘自动执行：计算真实胜率 → 盈亏归因 → 对比上版 → 参数微调 → 版本+1。
样本 <20 时不调参（防过拟合）；胜率 <40% 收紧风控，≥60% 适度放宽。
"""
    try:
        with open(SKILL_MD, "w", encoding="utf-8") as f:
            f.write(md)
    except Exception as e:
        print(f"⚠️ 写入 push_skill.md 失败: {e}")

    state = {
        "version": version,
        "updated_at": now,
        "params": params,
        "last_win_rate": wr["win_rate"],
        "last_settled": wr["settled"],
        "changes": changes,
        "win_rate_history": (state.get("win_rate_history", [])[-19:] + [wr["win_rate"]]),
    }
    _save_state(state)
    print(f"🧠 Skill 已升级至 v{version}: 真实胜率 {wr['win_rate']:.2f}% | " + "；".join(changes[:2]))
    return {"version": version, "win_rate": wr, "attribution": attr, "params": params, "changes": changes}


def render_push_summary(tracker_data: Optional[List[Dict]] = None, with_attribution: bool = True) -> str:
    """生成可嵌入推送的胜率+归因精简段落（晚间复盘用）。"""
    tracker_data = tracker_data if tracker_data is not None else load_tracker()
    wr = compute_win_rate(tracker_data)
    state = _load_state()
    ver = state.get("version", 1)
    lines = [
        f"📊 **【策略 Skill v{ver}·真实胜率】** {wr['win_rate']:.1f}% "
        f"(结案 {wr['settled']} 只：胜 {wr['win']}/负 {wr['loss']})",
    ]
    if with_attribution:
        attr = attribute_outcomes(tracker_data)
        lines.append(f"🏆 盈利主因: {attr['win_cause']}")
        lines.append(f"📉 亏损主因: {attr['loss_cause']}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    data = load_tracker()
    result = evolve_skill(data, auto_evolve=False) if "--dry" in sys.argv else evolve_skill(data)
    print(json.dumps({"win_rate": result["win_rate"], "changes": result["changes"]}, ensure_ascii=False, indent=2))
