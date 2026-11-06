"""
多 Agent 议会 · 中肯决策引擎（agent_council）
=====================================================================
需求（用户）：
  不要只听单方面买入/卖出指令，要【多个 Agent 交汇】得出中肯报告：
    - 看多操盘手   ：只负责把「做多理由」讲到极致
    - 看空操盘手   ：只负责把「做空/风险理由」讲到极致
    - 大市观察员   ：给大盘/板块/系统性背景（不站队，只看势）
    - 风险风控官   ：终裁者，拥有【一票否决 + 仓位上限】的最终决定权
  流程：Round1 各自独立开仓观点 → Round2 多空互相驳论（交锋）→
        Round3 风控官综合所有人观点 + 硬性风控规则，给出最终动作与仓位上限。

设计原则：
  1. 风控终裁是【规则化、确定性】的（见 _risk_final_decision），LLM 只负责写
     「论据/叙事」，不负责拍板——以此保证「风控负责最后」且结论始终稳健。
  2. 复用现有地基：stock_analyzer.enrich_stock（财报/股息/资金/舆情）、
     stock_analyzer.build_risk_hint（规则风险提示）、holding_analyzer.llm_complete
     （Dify→免费后端→make_llm 三级降级）。三个行情数据源节点【一律不碰】。
  3. 大市观察员用 qt.gtimg.cn 只读拉取上证/深证/创业板指数（同源家族、只读，
     非那三个被保护的个股行情节点）。
  4. 任何 LLM 调用失败都不影响主流程：自动降级为模板论据，议会照常产出报告。
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _ensure_repo_root_on_path():
    here = os.path.dirname(os.path.abspath(__file__))
    for c in (here, os.path.dirname(here), os.path.dirname(os.path.dirname(here))):
        if c and c not in sys.path:
            sys.path.insert(0, c)


_ensure_repo_root_on_path()

# ── 依赖（均带优雅降级）────────────────────────────────────────────────────
try:
    from _env import load_env
except ImportError:
    try:
        from hedge_fund.strategies._env import load_env
    except ImportError:
        def load_env():
            return False
load_env()

try:
    from stock_analyzer import enrich_stock, build_risk_hint
except ImportError:
    try:
        from hedge_fund.strategies.stock_analyzer import enrich_stock, build_risk_hint
    except ImportError:
        enrich_stock = None
        build_risk_hint = None

try:
    from holding_analyzer import llm_complete
except ImportError:
    try:
        from hedge_fund.strategies.holding_analyzer import llm_complete
    except ImportError:
        llm_complete = None

try:
    from data.cn_stock_data import CNStockDataFetcher
except ImportError:
    try:
        from hedge_fund.data.cn_stock_data import CNStockDataFetcher
    except ImportError:
        CNStockDataFetcher = None

import requests

# 🪶 议会 LLM 字数控制（每只 Agent 短评，省 token）
_COUNCIL_MAX_TOKENS = int(os.environ.get("COUNCIL_MAX_TOKENS", "320"))
_COUNCIL_TIMEOUT = float(os.environ.get("COUNCIL_LLM_TIMEOUT", "40"))
COUNCIL_ENABLED = os.environ.get("COUNCIL_ENABLED", "true").lower() in ("true", "1", "yes")


# ==========================================================
# 0. 大市观察员 · 只读拉取指数（qt.gtimg.cn，同源只读）
# ==========================================================
def fetch_market_index() -> dict:
    """只读拉取上证/深证/创业板指数涨跌幅，供大市观察员做背景。"""
    out = {"summary": "大盘数据获取失败（网络/源异常），按中性处理",
           "sh": None, "sz": None, "cyb": None, "stance": "中性"}
    try:
        url = "http://qt.gtimg.cn/q=sh000001,sz399001,sz399006"
        res = requests.get(url, timeout=5,
                          headers={"User-Agent": "Mozilla/5.0"})
        if res.status_code == 200 and "=" in res.text:
            parts = [p for p in res.text.split(";") if "=" in p]
            vals = {}
            for p in parts:
                # gtimg 格式： v_sh000001="1~上证指数~..."，代码在 v_ 与 = 之间
                if not p.startswith("v_"):
                    continue
                code = p[2:].split("=")[0]
                f = p.split('="')[1].split("~") if '="' in p else []
                if len(f) > 33:
                    try:
                        vals[code] = {
                            "name": f[1], "price": float(f[3]),
                            "pct": float(f[32].replace("%", "")),
                        }
                    except (ValueError, IndexError):
                        continue
            sh = vals.get("sh000001")
            sz = vals.get("sz399001")
            cyb = vals.get("sz399006")
            out["sh"], out["sz"], out["cyb"] = sh, sz, cyb
            pcts = [v["pct"] for v in vals.values() if v and isinstance(v["pct"], (int, float))]
            avg = sum(pcts) / len(pcts) if pcts else 0.0
            out["stance"] = "偏多" if avg > 0.5 else ("偏空" if avg < -0.5 else "中性")
            if sh or sz or cyb:
                items = []
                for v in (sh, sz, cyb):
                    if v:
                        items.append(f"{v['name']} {v['pct']:+.2f}%")
                out["summary"] = "、".join(items)
    except Exception as e:
        print(f"⚠️ 大市指数获取失败: {e}")
    return out


# ==========================================================
# 1. 论据生成（LLM 优先 + 模板降级）
# ==========================================================
_ROLE_SYSTEM = {
    "bull": "你是A股『看多操盘手』，只负责把【做多/买入】的逻辑讲到极致：从基本面改善、"
            "估值修复、资金流入、催化剂、技术面右侧等角度全力论证为何该买/持有。克制在120字内，不要寒暄。",
    "bear": "你是A股『看空操盘手』，只负责把【做空/规避/卖出】的逻辑讲到极致：从盈利恶化、估值过高、"
            "主力出货、利好兑现、技术破位等角度全力论证为何该卖/观望/减仓。克制在120字内，不要寒暄。",
    "observer": "你是A股『大市观察员』，不站队，只客观描述大盘/板块/系统性背景与风险偏好，"
               "给操盘手提供战场环境。克制在100字内，不要给出买卖指令。",
    "rebuttal_bull": "你是看多操盘手。你刚听到了看空操盘手与大盘观察员的反对意见，请【正面回应】"
                     "他们的核心质疑（估值/盈利/大盘），说明为何仍看多或做出何种让步。克制在100字内。",
    "rebuttal_bear": "你是看空操盘手。你刚听到了看多操盘手与大盘观察员的反驳，请【正面回应】"
                     "他们的核心论据（改善/资金/催化），说明为何仍看空或做出何种让步。克制在100字内。",
    "risk": "你是A股『风险风控官』，是最终决策者。你已看到看多、看空、大市观察员三方观点及硬性风控数据。"
           "请输出最终风控裁决的【裁决理由】（为何放行/否决/减仓），强调纪律与仓位约束。克制在130字内。",
}


def _llm_view(role: str, user: str) -> str:
    if llm_complete is None:
        raise RuntimeError("llm_complete 不可用")
    return llm_complete(_ROLE_SYSTEM[role], user, max_tokens=_COUNCIL_MAX_TOKENS,
                        timeout=_COUNCIL_TIMEOUT).strip().replace("```", "").strip()


def _data_block(snap: dict, holding: dict, market: dict, strategy: str) -> str:
    fin = (snap.get("financials", {}) or {}) if snap else {}
    fin_str = "、".join(f"{k}:{v}" for k, v in fin.items()) or "暂无"
    news = (snap.get("news", []) or []) if snap else []
    news_str = "；".join(n.get("title", "") for n in news[:3]) or "暂无重大舆情"
    cost = float(holding.get("cost_price", 0)) if holding else 0.0
    pnl = float(holding.get("pnl_pct", 0)) if holding else None
    hline = f"【持仓】成本{cost}元 盈亏{pnl:+.2f}%" if (holding and cost) else "【候选】拟新开仓"
    return (
        f"标的：{snap.get('name')}({snap.get('code')}) 现价约{snap.get('price')}元 策略:{strategy}\n"
        f"{hline}\n"
        f"财报估值：{fin_str}\n"
        f"股息率：{snap.get('dividend_yield','暂无')} 主力净流入：{snap.get('capital_flow','暂无')}\n"
        f"当日舆情：{news_str}\n"
        f"大盘背景：{market.get('summary','-')}（{market.get('stance','-')}）"
    )


# ── 模板论据（无 LLM 时）─────────────────────────────────────────────────────
def _template_bull(snap, market, strategy) -> str:
    fin = (snap.get("financials", {}) or {}) if snap else {}
    net = fin.get("净利润同比", "-")
    cap = snap.get("capital_flow", "暂无") if snap else "暂无"
    up = ("净利润同比 " + str(net) + " 显示盈利向好") if (net not in ("-", "暂无") and not str(net).startswith("-")) else "盈利端暂无明确改善信号"
    inflow = ("且主力资金呈净流入态势" if ("净流入" in str(cap) and "-" not in str(cap)) else "资金面暂无显著流入")
    mkt = ("，叠加" + market.get("stance", "中性") + "的大盘环境") if market.get("stance") == "偏多" else ""
    return f"看多论据：{up}{inflow}{mkt}；按「{strategy}」信号入选，存在右侧延续/估值修复空间，可逢低关注。"


def _template_bear(snap, market, strategy) -> str:
    fin = (snap.get("financials", {}) or {}) if snap else {}
    net = fin.get("净利润同比", "-")
    pe = fin.get("市盈率(TTM)", "-")
    cap = snap.get("capital_flow", "暂无") if snap else "暂无"
    warn = []
    if net not in ("-", "暂无") and str(net).startswith("-"):
        warn.append(f"净利润同比下滑({net})")
    try:
        if float(str(pe).replace("%", "")) > 60:
            warn.append(f"估值偏高(PE {pe})")
    except (ValueError, TypeError):
        pass
    if "净流出" in str(cap) or str(cap).startswith("-"):
        warn.append("主力资金净流出")
    if market.get("stance") == "偏空":
        warn.append("大盘偏空压制风险偏好")
    w = "、".join(warn) if warn else "暂无明显利空但需防追高与回撤"
    return f"看空论据：{w}；动能策略波动大，若不及预期易触发止损，宜控仓或等回调确认。"


def _template_observer(market) -> str:
    return f"大市观察：{market.get('summary','-')}，整体格局【{market.get('stance','中性')}】。" \
           f"系统性风险{'偏高' if market.get('stance')=='偏空' else ('偏低' if market.get('stance')=='偏多' else '中性')}，操盘手需据此调节仓位节奏。"


def _template_rebuttal(role, bull, bear, observer) -> str:
    if role == "bull":
        return f"看多回应：承认{bear[:40]}…等风险确实存在，但认为当前核心矛盾在改善侧，愿以严格止损（破位即撤）控制下行。"
    return f"看空回应：承认{bull[:40]}…等亮点，但认为已部分price-in/或持续性存疑，宁可错过也不追高，优先保本。"


# ==========================================================
# 2. 多空信心量化（规则，确定性）
# ==========================================================
def _operator_conviction(snap: dict, direction: str) -> int:
    """0-100 的多/空信心分（规则，从数据推导）。bear 取反。"""
    fin = (snap.get("financials", {}) or {}) if snap else {}
    net = fin.get("净利润同比", "-")
    pe = fin.get("市盈率(TTM)", "-")
    cap = snap.get("capital_flow", "暂无") if snap else "暂无"
    score = 50
    try:
        if net not in ("-", "暂无") and not str(net).startswith("-"):
            score += 15
        else:
            score -= 20
    except Exception:
        pass
    try:
        pev = float(str(pe).replace("%", ""))
        if pev <= 0:
            score -= 20
        elif pev > 60:
            score -= 10
        elif pev < 30:
            score += 10
    except (ValueError, TypeError):
        pass
    if "净流入" in str(cap) and "-" not in str(cap):
        score += 10
    elif "净流出" in str(cap) or str(cap).startswith("-"):
        score -= 10
    score = max(0, min(100, score))
    return 100 - score if direction == "bear" else score


# ==========================================================
# 3. 风险风控官 · 终裁（规则化、确定性、一票否决）
# ==========================================================
def _risk_final_decision(snap: dict, market: dict, holding: dict, strategy: str,
                         bull_score: int, bear_score: int, risk_hint: str) -> dict:
    """风控终裁：综合硬性数据 + 大盘 + 多空强度，给出最终动作与仓位上限。
    返回 dict：final_action / confidence / max_position_pct / stop_loss / overrides / reasoning。"""
    fin = (snap.get("financials", {}) or {}) if snap else {}
    net = str(fin.get("净利润同比", "-"))
    pe = str(fin.get("市盈率(TTM)", "-"))
    cap = str(snap.get("capital_flow", "暂无") if snap else "暂无")
    mkt_stance = market.get("stance", "中性")
    is_holding = bool(holding and float(holding.get("cost_price", 0) or 0) > 0)
    pnl = float(holding.get("pnl_pct", 0)) if holding else 0.0
    price = float(snap.get("price") or 0) if snap else 0.0

    score = 0  # 风控天平：>0 偏放行，<0 偏收紧
    notes = []

    # —— 基本面 ——
    if net not in ("-", "暂无") and net.startswith("-"):
        score -= 2
        notes.append("盈利同比为负")
    elif net not in ("-", "暂无"):
        score += 1
        notes.append("盈利同比为正")
    try:
        pev = float(pe.replace("%", ""))
        if pev <= 0:
            score -= 2
            notes.append("当前亏损(PE为负)")
        elif pev > 60:
            score -= 1
            notes.append(f"估值偏高(PE {pe})")
        elif pev < 30:
            score += 1
            notes.append(f"估值偏低(PE {pe})")
    except (ValueError, TypeError):
        pass

    # —— 资金面 ——
    if "净流出" in cap or cap.startswith("-"):
        score -= 1
        notes.append("主力净流出")
    elif "净流入" in cap:
        score += 1

    # —— 大盘 ——
    if mkt_stance == "偏空":
        score -= 1
        notes.append("大盘偏空")
    elif mkt_stance == "偏多":
        score += 1

    # —— 持仓盈亏（若是存量持仓）——
    if is_holding:
        if pnl <= -8:
            score -= 2
            notes.append(f"已深度浮亏({pnl:+.1f}%)")
        elif pnl >= 15:
            notes.append(f"已有厚利({pnl:+.1f}%)注意回撤保护")

    # —— 多空强度（仅作参考，不单独决定）——
    if bull_score - bear_score >= 30:
        notes.append("看多明显占优")
    elif bear_score - bull_score >= 30:
        notes.append("看空明显占优")

    # —— 映射为最终动作 + 仓位上限 ——
    if score <= -3:
        action = "减仓/观望" if not is_holding else "止损/减仓"
        max_pos = 0.0 if not is_holding else 0.0
    elif score == -2:
        action = "观望"
        max_pos = 0.0 if not is_holding else 3.0
    elif score == -1:
        action = "观望（谨慎，不加仓）"
        max_pos = 5.0
    elif score == 0:
        action = "持有"
        max_pos = 8.0
    elif score == 1:
        action = "持有/小幅加仓"
        max_pos = 10.0
    else:
        action = "买入/加仓" if not is_holding else "加仓"
        max_pos = min(15.0, 12.0 + (score - 2) * 1.5)

    # 新建仓硬约束：亏损股(TTM为负)一律禁止开新仓
    if not is_holding and pe not in ("-", "暂无") and pe.replace("%", "").replace(".", "").isdigit():
        try:
            if float(pe) <= 0:
                action = "观望（禁止开亏损股新仓）"
                max_pos = 0.0
        except ValueError:
            pass

    # 持仓深度浮亏硬约束：风控一票否决任何「加仓/买入」动作，强制防守（禁止抄底摊平）
    if is_holding and pnl <= -8 and ("加仓" in action or "买入" in action):
        action = "减仓/观望"
        max_pos = 0.0
        notes.append(f"深度浮亏({pnl:+.1f}%)，风控否决加仓/买入、禁止摊平")

    confidence = int(max(20, min(95, 50 + score * 9)))

    # 止损线：持仓按成本*0.95；新仓按现价*0.94（仅当非纯观望/卖出时给）
    stop = None
    if action not in ("观望", "观望（谨慎，不加仓）", "减仓/观望", "止损/减仓", "观望（禁止开亏损股新仓）"):
        base = float(holding.get("cost_price", 0)) if is_holding else price
        if base > 0:
            stop = round(base * (0.95 if is_holding else 0.94), 2)

    # —— 否决说明（体现「风控否决操盘手」）——
    overrides = ""
    if "买入" in action or "加仓" in action:
        if bear_score >= bull_score:
            overrides = "尽管看空占优，风控基于硬性数据仍给予有限放行，但仓位已压至上限并设严格止损。"
        else:
            overrides = "风控放行看多，但仓位受上限约束，破位即撤。"
    elif "观望" in action or "减仓" in action or "止损" in action:
        if bull_score > bear_score + 10:
            overrides = f"风控【否决看多操盘手】的买入诉求：{('、'.join(notes) or '硬性风控未达标')}，终裁以风控为准。"
        else:
            overrides = "风控与看空/大盘方向一致，维持防守姿态。"

    reasoning = ("风控终裁依据：" + ("、".join(notes) if notes else "无显著风险信号") +
                 f"；大盘{mkt_stance}；最终动作【{action}】，建议仓位上限 {max_pos:.0f}%。")

    return {
        "final_action": action,
        "confidence": confidence,
        "max_position_pct": round(max_pos, 1),
        "stop_loss": stop,
        "overrides": overrides,
        "reasoning": reasoning,
    }


# ==========================================================
# 4. 议会主流程
# ==========================================================
def run_council(code: str, name: str, price: float = 0.0, strategy: str = "",
                holding: dict = None, snap: dict = None) -> dict:
    """运行四角色议会，返回结构化报告 dict（含 report_md 中肯报告）。

    参数：
      code/name/price : 标的
      strategy        : 匹配策略名
      holding         : 持仓字典（含 cost_price/pnl_pct），None 表示候选新开仓
      snap            : 已拉取的多维快照，None 则内部 enrich_stock
    """
    code = str(code).replace("sh", "").replace("sz", "").replace("bj", "").strip().zfill(6)
    is_holding = bool(holding and float(holding.get("cost_price", 0) or 0) > 0)
    # 持仓模式：若未提供真实浮亏比例，则由成本/现价自行推算（避免漏判深度浮亏）
    if is_holding:
        _cost = float(holding.get("cost_price", 0) or 0)
        if _cost > 0 and not holding.get("pnl_pct"):
            holding["pnl_pct"] = round((float(price or 0) - _cost) / _cost * 100, 2)

    # 数据
    if snap is None:
        if enrich_stock is not None:
            try:
                snap = enrich_stock(code, name, price)
            except Exception as e:
                print(f"⚠️ 议会 enrich {name}({code}) 失败: {e}")
                snap = {}
        else:
            snap = {}
    if not snap:
        snap = {"code": code, "name": name, "price": price,
                "financials": {}, "dividend_yield": "暂无",
                "capital_flow": "暂无", "news": []}
    snap["price"] = price or snap.get("price", 0)
    risk_hint = build_risk_hint(snap, strategy) if build_risk_hint else ""

    market = fetch_market_index()
    data_block = _data_block(snap, holding, market, strategy)

    # —— Round1：独立开仓观点 ——
    bull_view, bear_view, observer_view = "", "", ""
    use_llm = COUNCIL_ENABLED and llm_complete is not None
    try:
        if use_llm:
            bull_view = _llm_view("bull", data_block)
            bear_view = _llm_view("bear", data_block)
            observer_view = _llm_view("observer", data_block)
    except Exception as e:
        print(f"⚠️ 议会 Round1 LLM 失败，降级模板: {e}")
        use_llm = False
    if not bull_view:
        bull_view = _template_bull(snap, market, strategy)
    if not bear_view:
        bear_view = _template_bear(snap, market, strategy)
    if not observer_view:
        observer_view = _template_observer(market)

    bull_score = _operator_conviction(snap, "bull")
    bear_score = _operator_conviction(snap, "bear")

    # —— Round2：多空驳论（交锋），仅 LLM 时生成；模板给交叉回应 ——
    bull_rebuttal, bear_rebuttal = "", ""
    if use_llm:
        try:
            ctx = (f"【看空观点】{bear_view}\n【大市观点】{observer_view}\n【你的原看多观点】{bull_view}\n"
                   f"请正面回应看空与大市的质疑。")
            bull_rebuttal = _llm_view("rebuttal_bull", ctx)
            ctx2 = (f"【看多观点】{bull_view}\n【大市观点】{observer_view}\n【你的原看空观点】{bear_view}\n"
                    f"请正面回应看多与大市的反驳。")
            bear_rebuttal = _llm_view("rebuttal_bear", ctx2)
        except Exception as e:
            print(f"⚠️ 议会 Round2 驳论失败，降级交叉回应: {e}")
            bull_rebuttal = _template_rebuttal("bull", bull_view, bear_view, observer_view)
            bear_rebuttal = _template_rebuttal("bear", bull_view, bear_view, observer_view)
    else:
        bull_rebuttal = _template_rebuttal("bull", bull_view, bear_view, observer_view)
        bear_rebuttal = _template_rebuttal("bear", bull_view, bear_view, observer_view)

    # —— Round3：风控终裁（规则化，最终拍板）——
    risk = _risk_final_decision(snap, market, holding, strategy,
                                bull_score, bear_score, risk_hint)
    risk_reasoning = risk["reasoning"]
    if use_llm:
        try:
            rctx = (f"{data_block}\n【看多】{bull_view}\n【看空】{bear_view}\n"
                    f"【大市】{observer_view}\n【风控硬规则结论】动作:{risk['final_action']} 仓位上限:{risk['max_position_pct']}%\n"
                    f"请据此输出风控裁决理由（强调纪律）。")
            risk_reasoning = _llm_view("risk", rctx) or risk_reasoning
        except Exception as e:
            print(f"⚠️ 议会风控叙事 LLM 失败，用规则理由: {e}")

    # —— 共识标签 ——
    if "买入" in risk["final_action"] or "加仓" in risk["final_action"]:
        if bull_score > bear_score + 10:
            consensus = "看多占优 · 风控放行"
        else:
            consensus = "多空拉锯 · 风控有限放行"
    elif "止损" in risk["final_action"] or "减仓" in risk["final_action"]:
        consensus = "风控强收 · 防守优先"
    elif "观望" in risk["final_action"]:
        if bull_score > bear_score + 10:
            consensus = "多空激辩 · 风控否决看多"
        else:
            consensus = "偏谨慎 · 观望等待"
    else:
        consensus = "中性 · 持有待变"

    report = {
        "code": code, "name": name, "price": snap.get("price", price),
        "strategy": strategy, "is_holding": is_holding,
        "market": {"stance": market.get("stance"), "summary": market.get("summary")},
        "bull": {"stance": "看多", "conviction": bull_score, "view": bull_view, "rebuttal": bull_rebuttal},
        "bear": {"stance": "看空", "conviction": bear_score, "view": bear_view, "rebuttal": bear_rebuttal},
        "observer": {"stance": market.get("stance", "中性"), "view": observer_view},
        "risk_final": risk,
        "consensus_label": consensus,
        "risk_hint": risk_hint,
    }
    report["report_md"] = _to_markdown(report)
    return report


# ==========================================================
# 5. 中肯报告 markdown
# ==========================================================
def council_compact(r: dict) -> str:
    """紧凑版议会结论（供企业微信逐只摘要，控制长度）。"""
    rf = r["risk_final"]
    stop = f" 止损`{rf['stop_loss']}`" if rf["stop_loss"] else ""
    verdict = rf["overrides"] or rf["reasoning"]
    return (
        f"🏛️ **议会中肯结论**【{r['consensus_label']}】\n"
        f"　🐂看多({r['bull']['conviction']}) vs 🐻看空({r['bear']['conviction']})；"
        f"🌐大市{r['market'].get('stance','-')}\n"
        f"　🛡️ **风控终裁：{rf['final_action']}** 仓位≤{rf['max_position_pct']}%{stop}\n"
        f"　↳ {verdict}"
    )


def council_brief(r: dict) -> str:
    """紧凑版议会结论（**含多空双方辩论 + 风控终裁**），供企业微信推送，
    保证「多空双方」清晰可见且单只标的不会因过长被拆段而丢失。"""
    rf = r["risk_final"]
    b, be = r["bull"], r["bear"]
    obs = r["observer"]["view"]
    stop = f" 止损`{rf['stop_loss']}`" if rf["stop_loss"] else ""
    verdict = rf["overrides"] or rf["reasoning"]
    return (
        f"🏛️ **【多空议会 · {r['consensus_label']}】**\n"
        f"🌐 大市({r['market'].get('stance', '-')}): {obs}\n"
        f"🐂 **看多操盘手(信心{b['conviction']})**: {b['view']}\n"
        f"　↳ 交锋: {b['rebuttal']}\n"
        f"🐻 **看空操盘手(信心{be['conviction']})**: {be['view']}\n"
        f"　↳ 交锋: {be['rebuttal']}\n"
        f"🛡️ **风险风控官·终裁: {rf['final_action']}** 仓位≤{rf['max_position_pct']}%{stop}\n"
        f"　↳ {verdict}"
    )


def _to_markdown(r: dict) -> str:
    rf = r["risk_final"]
    icon = "🟢" if r["is_holding"] else "🔍"
    lines = [
        f"{icon} **【议会中肯报告】{r['name']}** (`{r['code']}`) 现价≈{r['price']}元",
        f"策略：`{r['strategy'] or '综合'}` | 性质：{'持仓' if r['is_holding'] else '候选'} | 共识：**{r['consensus_label']}**",
        "────────────────────",
        f"🌐 **大市观察员**（{r['market'].get('stance','-')}）：{r['observer']['view']}",
        "────────────────────",
        f"🐂 **看多操盘手**（信心 {r['bull']['conviction']}）：{r['bull']['view']}",
        f"　↳ 交锋回应：{r['bull']['rebuttal']}",
        f"🐻 **看空操盘手**（信心 {r['bear']['conviction']}）：{r['bear']['view']}",
        f"　↳ 交锋回应：{r['bear']['rebuttal']}",
        "────────────────────",
        f"🛡️ **风险风控官 · 终裁**：**{rf['final_action']}**　置信 {rf['confidence']}%　仓位上限 **{rf['max_position_pct']}%**",
        f"　• 止损参考：`{rf['stop_loss'] if rf['stop_loss'] else '不适用'}`",
        f"　• 否决/裁定：{rf['overrides']}",
        f"　• 依据：{rf['reasoning']}",
    ]
    if r.get("risk_hint") and r["risk_hint"] != "暂无显著基本面风险信号，但仍需关注大盘与板块系统性风险":
        lines.append(f"　• 规则风险提示：{r['risk_hint']}")
    lines.append("💡 以上为多方论据交汇后的中肯结论，非单边买卖指令；最终决策请结合实时盘口与个人风险偏好。")
    return "\n".join(lines)


# ==========================================================
# 6. CLI（独立查看议会结果）
# ==========================================================
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="多 Agent 议会 · 中肯决策（看多/看空/大市/风控）")
    ap.add_argument("--code", required=True, help="6 位股票代码")
    ap.add_argument("--name", default="", help="股票名称")
    ap.add_argument("--price", type=float, default=0.0, help="现价")
    ap.add_argument("--strategy", default="综合选股", help="匹配策略")
    ap.add_argument("--cost", type=float, default=0.0, help="持仓成本（填了即按持仓模式）")
    args = ap.parse_args()

    holding = None
    if args.cost > 0:
        holding = {"cost_price": args.cost, "pnl_pct": 0.0}
    rep = run_council(args.code, args.name or args.code, args.price, args.strategy, holding)
    print("\n" + rep["report_md"])
