"""
个股多维分析模块（财报 / 盘面 / 资金面 / 消息面 / 风险提示）
=====================================================================
供 morning_picker（早盘研报）与 evening_review（晚间复盘）共用。

数据来源：
  - hedge_fund/data/cn_stock_data.py 的 CNStockDataFetcher（东财财报 / 股息率 / 主力净流入 / 舆情）
文字分析：
  - 优先调用 LLM（hedge_fund/llm/client.make_llm）生成多维度点评；
  - LLM 不可用 / 超时 / 无 key 时，自动降级为「财报数据拼装」模板，保证主流程不中断。

设计原则：分析尽量精简（默认 WeChat 完整版 ≤ 400 字，飞书摘要 ≤ 300 字），
因为企业微信单条字数过多会被风控封锁，需配合分段推送。
"""

import os
import re
import sys
import time


def _ensure_repo_root_on_path():
    """
    把仓库根（含 hedge_fund 包）加入 sys.path。

    无论脚本以何种方式启动（cron / python3 xxx.py / python3 -m / 其它入口），
    都保证 `hedge_fund.data.cn_stock_data.CNStockDataFetcher` 一定可被导入，
    避免因路径未就绪导致 CNStockDataFetcher 变成 None、多维分析整段变空。
    """
    here = os.path.dirname(os.path.abspath(__file__))          # .../hedge_fund/strategies
    for c in (here, os.path.dirname(here), os.path.dirname(os.path.dirname(here))):
        if c and c not in sys.path:
            sys.path.insert(0, c)


_ensure_repo_root_on_path()

# 🔧 数据源（多源兜底导入，确保 CNStockDataFetcher 一定可用）
try:
    from hedge_fund.data.cn_stock_data import CNStockDataFetcher, get_market_snapshot
except ImportError:
    try:
        from data.cn_stock_data import CNStockDataFetcher, get_market_snapshot
    except ImportError:
        CNStockDataFetcher = None
        try:
            from hedge_fund.data.cn_stock_data import get_market_snapshot
        except ImportError:
            try:
                from data.cn_stock_data import get_market_snapshot
            except ImportError:
                get_market_snapshot = None

# 🔧 LLM（可选，缺失/无 key 时降级为模板）
try:
    from llm.client import make_llm
except ImportError:
    try:
        from hedge_fund.llm.client import make_llm
    except ImportError:
        make_llm = None

_LLM_TIMEOUT = float(os.environ.get("ANALYSIS_LLM_TIMEOUT", "45"))


def _get_fetcher() -> "CNStockDataFetcher":
    if CNStockDataFetcher is None:
        raise RuntimeError("CNStockDataFetcher 不可用（data 模块缺失）")
    return CNStockDataFetcher()


# ──────────────────────────────────────────────────────────────
# 1. 数据富集：财报 + 股息 + 资金面 + 舆情
# ──────────────────────────────────────────────────────────────
# 🔧 财报字段数值合理性阈值：东财 push2 的 f182/f183（营收/净利同比）偶发返回
# 天文数字原始值（如 133283652508.95），直接格式化会污染多空打分。这里在
# 下游所有消费点之前统一兜底过滤：超出合理区间一律归「暂无」（诚实优于假数据）。
_FIN_SANE_LIMITS = {
    "营收同比": 10000.0,        # |%| 超过 10000% 视为脏值
    "净利润同比": 10000.0,      # |%| 同上
    "市盈率(TTM)": 1000.0,      # 正常 PE 多在 -1000~1000（负值为亏损股，放行）
    "市净率(PB)": 1000.0,       # PB 正常 <= 1000
    "ROE(净资产收益率)": 1000.0,  # |%| 正常 <= 1000%
    "股息率(TTM)": 100.0,       # |%| 正常 <= 100
}


def _sanitize_financials(fin: dict) -> dict:
    """就地过滤财报字段的脏值：超出合理区间的百分比/倍数归「暂无」。"""
    if not isinstance(fin, dict):
        return fin
    for key, limit in _FIN_SANE_LIMITS.items():
        val = fin.get(key, "暂无")
        if val in ("暂无", "", None):
            continue
        try:
            num = float(str(val).replace("%", "").replace(",", "").strip())
        except (ValueError, TypeError):
            fin[key] = "暂无"
            continue
        if abs(num) > limit:
            fin[key] = "暂无"
    return fin


def enrich_stock(code: str, name: str, price: float = 0.0, news_limit: int = 3) -> dict:
    """拉取单只标的的多维数据快照（带容错，单只失败不影响整体）。"""
    try:
        snap = _get_fetcher().fetch_full_snapshot(code, name, news_limit=news_limit)
        snap["price"] = price
        _sanitize_financials(snap.get("financials", {}))  # 🔧 过滤脏值，下游统一干净
        # 📰 技术指标 + 新闻股价归因（布林线 + 甄别消息 + 相对大盘超额），失败不影响主流程
        try:
            tech = compute_technicals(snap.get("kline") or [], price)
            snap["technicals"] = tech
            mkt = None
            if get_market_snapshot is not None:
                try:
                    mkt = get_market_snapshot()  # {'pct':..., 'summary':...}
                except Exception:
                    mkt = None
            snap["attribution"] = attribute_news_impact(
                snap.get("news_digest") or {}, tech, mkt, price)
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ 技术/归因计算 {name}({code}) 跳过: {str(e)[:50]}")
            snap.setdefault("technicals", {})
            snap.setdefault("attribution", "")
        return snap
    except Exception as e:
        print(f"⚠️ 富集 {name}({code}) 数据失败: {e}")
        return {
            "code": str(code).replace("sh", "").replace("sz", "").replace("bj", "").strip(),
            "name": name, "price": price,
            "financials": {"市盈率(TTM)": "暂无", "市净率(PB)": "暂无",
                           "ROE(净资产收益率)": "暂无", "营收同比": "暂无",
                           "净利润同比": "暂无", "股息率(TTM)": "暂无"},
            "dividend_yield": "暂无", "capital_flow": "暂无",
            "news": [], "news_digest": {}, "industry_news": [],
            "kline": [], "technicals": {}, "attribution": "",
        }


# ──────────────────────────────────────────────────────────────
# 1.5 技术指标（布林线 BOLL 20,2 / MA20 / 位置），纯标准库计算
# ──────────────────────────────────────────────────────────────
def compute_technicals(kline: list, price: float = 0.0) -> dict:
    """由日K算布林线与位置。返回空 dict 表示数据不足（调用方降级跳过）。
    kline: [{'close':..., 'volume':...}, ...] 按日期升序。
    """
    if not kline or len(kline) < 10:
        return {}
    try:
        closes = [float(b.get("close", 0)) for b in kline if float(b.get("close", 0)) > 0]
    except (ValueError, TypeError, AttributeError):
        return {}
    if len(closes) < 10:
        return {}
    n = min(20, len(closes))
    win = closes[-n:]
    ma = sum(win) / n
    var = sum((x - ma) ** 2 for x in win) / n
    sd = var ** 0.5
    upper, lower = ma + 2 * sd, ma - 2 * sd
    cur = float(price) if price and price > 0 else closes[-1]
    bandwidth = (upper - lower) / ma * 100 if ma else 0.0
    if upper != lower:
        pct_b = (cur - lower) / (upper - lower)  # 0=下轨, 1=上轨, 可越界
    else:
        pct_b = 0.5
    if cur > upper:
        pos = "突破上轨"
    elif cur < lower:
        pos = "跌破下轨"
    elif pct_b >= 0.8:
        pos = "接近上轨"
    elif pct_b <= 0.2:
        pos = "接近下轨"
    elif pct_b >= 0.55:
        pos = "中上轨"
    else:
        pos = "中下轨"
    # 近5日/当日涨跌
    day_chg = 0.0
    if len(closes) >= 2 and closes[-2]:
        day_chg = (closes[-1] - closes[-2]) / closes[-2] * 100
    week_chg = 0.0
    if len(closes) >= 6 and closes[-6]:
        week_chg = (closes[-1] - closes[-6]) / closes[-6] * 100
    # 量能：最新量 / 前5日均量
    vol_ratio = 1.0
    try:
        vols = [float(b.get("volume", 0)) for b in kline[-6:]]
        if len(vols) >= 6 and sum(vols[:5]) > 0:
            vol_ratio = vols[-1] / (sum(vols[:5]) / 5)
    except (ValueError, TypeError, AttributeError, ZeroDivisionError):
        pass
    return {
        "ma20": round(ma, 3), "upper": round(upper, 3), "lower": round(lower, 3),
        "pct_b": round(pct_b, 3), "bandwidth": round(bandwidth, 2), "position": pos,
        "day_chg": round(day_chg, 2), "week_chg": round(week_chg, 2),
        "vol_ratio": round(vol_ratio, 2),
    }


def attribute_news_impact(news_digest: dict, tech: dict, market: dict = None,
                          price: float = 0.0) -> str:
    """新闻-股价归因：判断当前涨跌主要是【消息驱动 / 大市波动 / 技术性波动 / 无明显催化】。
    证据链 = 新闻情绪(已甄别) + 布林线位置 + 个股相对大盘的超额收益。
    LLM 不可用时这套规则也能给出中肯归因，三个 agent 共用。
    """
    market = market or {}
    if not tech:
        digest = news_digest or {}
        st = digest.get("stats", {}) or {}
        nb = len(digest.get("bull_points", []) or [])
        nr = len(digest.get("bear_points", []) or [])
        if nb or nr:
            tone = f"甄别后有 {nb} 条利多 / {nr} 条利空消息"
            return f"📰 归因：{tone}，但缺历史K线无法校验价格位置，暂判【消息面偏{'多' if nb > nr else '空'}，待技术确认】。"
        return "📰 归因：暂无有效消息与技术数据，维持中性观察。"

    digest = news_digest or {}
    nb = len(digest.get("bull_points", []) or [])
    nr = len(digest.get("bear_points", []) or [])
    day_chg = tech.get("day_chg", 0.0)
    # 大盘当日涨跌：优先用结构化 pct 字段，其次从 summary 文本解析
    mkt_chg = 0.0
    if isinstance(market.get("pct"), (int, float)):
        mkt_chg = float(market["pct"])
    else:
        m = re.search(r"([+-]?\d+\.?\d*)%", str(market.get("summary", "")))
        if m:
            try:
                mkt_chg = float(m.group(1))
            except ValueError:
                mkt_chg = 0.0
    excess = day_chg - mkt_chg  # 相对大盘超额
    pos = tech.get("position", "")
    pb = tech.get("pct_b", 0.5)
    vr = tech.get("vol_ratio", 1.0)
    pos_word = f"股价处布林{pos}（%B={pb:.2f}）"

    # 判定主因
    reasons = []
    verdict = ""
    up_move = day_chg > 0
    news_bull = nb > nr
    news_bear = nr > nb
    # 只要有至少一条明确方向的甄别消息即纳入消息驱动判定（超额达标才成立）
    strong_news = (nb + nr) >= 1

    if strong_news and news_bull and excess >= 1.0:
        verdict = "消息驱动（利多）"
        lead = (digest.get("bull_points") or [""])[0]
        reasons.append(f"个股涨 {day_chg:+.1f}% 跑赢大盘 {mkt_chg:+.1f}%（超额 {excess:+.1f}%），与甄别利多同向")
        reasons.append(f"催化：{str(lead)[:34]}")
    elif strong_news and news_bear and excess <= -1.0:
        verdict = "消息驱动（利空）"
        lead = (digest.get("bear_points") or [""])[0]
        reasons.append(f"个股跌 {day_chg:+.1f}% 弱于大盘 {mkt_chg:+.1f}%（超额 {excess:+.1f}%），与甄别利空同向")
        reasons.append(f"压制：{str(lead)[:34]}")
    elif abs(excess) < 1.0 and abs(mkt_chg) >= 0.5:
        verdict = "大市波动主导"
        reasons.append(f"个股 {day_chg:+.1f}% 基本跟随大盘 {mkt_chg:+.1f}%（超额仅 {excess:+.1f}%），非个股独立行情")
    elif pos in ("突破上轨", "跌破下轨", "接近上轨", "接近下轨") and not strong_news:
        verdict = "技术性波动"
        reasons.append(f"无明显个股消息，但{pos_word}、量比 {vr:.1f}，属技术位波动")
    else:
        verdict = "多因素交织" if (nb or nr) else "无明显催化"
        if nb or nr:
            reasons.append(f"甄别消息 {nb} 多/{nr} 空，涨跌 {day_chg:+.1f}% 与消息方向{'部分吻合' if (news_bull == up_move) else '不一致'}")
        else:
            reasons.append(f"无有效个股消息，{pos_word}，当日 {day_chg:+.1f}%")

    reasons.append(f"技术面：{pos_word}，带宽 {tech.get('bandwidth', 0):.1f}%，近5日 {tech.get('week_chg', 0):+.1f}%")
    return f"📰 归因【{verdict}】：" + "；".join(reasons) + "。"


# ──────────────────────────────────────────────────────────────
# 2. 风险提示（规则，不依赖 LLM）
# ──────────────────────────────────────────────────────────────
def build_risk_hint(snap: dict, strategy: str = "") -> str:
    """根据财报/估值/策略生成风险提示要点。"""
    hints = []
    fin = snap.get("financials", {}) or {}
    pe = fin.get("市盈率(TTM)", "-")
    roe = fin.get("ROE(净资产收益率)", "-")
    net_yoy = fin.get("净利润同比", "-")
    div = snap.get("dividend_yield", "暂无")

    try:
        pe_v = float(str(pe).replace("%", ""))
        if pe_v > 60:
            hints.append(f"估值偏高（PE {pe}），注意追高风险")
        elif pe_v <= 0:
            hints.append("当前亏损（PE 为负），基本面承压")
    except (ValueError, TypeError):
        pass

    if "负值" in str(net_yoy) or str(net_yoy).startswith("-"):
        hints.append(f"净利润同比下滑（{net_yoy}），盈利质量需警惕")

    if "三倍量" in strategy:
        hints.append("动量短线策略，波动大、止损纪律要严格执行")

    if div not in ("暂无", "", None) and div != "暂无":
        hints.append(f"股息率 {div}，具备一定红利防守属性")

    if not hints:
        hints.append("暂无显著基本面风险信号，但仍需关注大盘与板块系统性风险")
    return "；".join(hints)


# ──────────────────────────────────────────────────────────────
# 3. 多维度文字分析（LLM 优先，模板降级）
# ──────────────────────────────────────────────────────────────
def _snapshot_to_text(snap: dict, strategy: str, price: float) -> str:
    fin = snap.get("financials", {}) or {}
    fin_str = "、".join(f"{k}:{v}" for k, v in fin.items() if v not in ("暂无", "", None)) or "财务数据暂无"
    news = snap.get("news", []) or []
    news_str = "；".join(n.get("title", "") for n in news[:4]) or "暂无重大舆情"
    # 📰 甄别后的结构化要点（利多/利空/行业），优先喂给 LLM，确保"关键新闻"被重点解读
    digest = snap.get("news_digest", {}) or {}
    key_points = []
    if digest.get("bull_points"):
        key_points.append("【利多】" + "；".join(digest["bull_points"][:3]))
    if digest.get("bear_points"):
        key_points.append("【利空】" + "；".join(digest["bear_points"][:3]))
    if digest.get("industry_points"):
        key_points.append("【行业】" + "；".join(digest["industry_points"][:2]))
    industry_news = snap.get("industry_news", []) or []
    ind_titles = "；".join(n.get("title", "") for n in industry_news[:2])
    digest_str = ("\n".join(key_points) + (f"\n【板块消息】{ind_titles}" if ind_titles else "")) if key_points else ""
    tech = snap.get("technicals", {}) or {}
    tech_str = ""
    if tech:
        tech_str = (f"\n技术面：布林位置{tech.get('position', '-')} "
                    f"日涨跌{tech.get('day_chg', '-')}% 量比{tech.get('vol_ratio', '-')}")
    return (
        f"标的：{snap.get('name')}({snap.get('code')}) 现价约{price}元 策略:{strategy}\n"
        f"财报估值：{fin_str}\n"
        f"股息率：{snap.get('dividend_yield')} 主力净流入：{snap.get('capital_flow')}\n"
        f"{digest_str or ('当日舆情：' + news_str)}\n"
        f"原始舆情：{news_str}{tech_str}"
    )


def _template_analysis(snap: dict, strategy: str, price: float) -> str:
    fin = snap.get("financials", {}) or {}
    pe = fin.get("市盈率(TTM)", "暂无")
    roe = fin.get("ROE(净资产收益率)", "暂无")
    rev_yoy = fin.get("营收同比", "暂无")
    net_yoy = fin.get("净利润同比", "暂无")
    div = snap.get("dividend_yield", "暂无")
    cap = snap.get("capital_flow", "暂无")
    news = snap.get("news", []) or []
    digest = snap.get("news_digest", {}) or {}

    # 📰 消息面优先用甄别后的关键要点（利多/利空/行业），确保"重要新闻"真正出现在推送里
    msg_bits = []
    bull = digest.get("bull_points") or []
    bear = digest.get("bear_points") or []
    ind = digest.get("industry_points") or []
    if bull:
        msg_bits.append("利多:" + "；".join(bull[:2]))
    if bear:
        msg_bits.append("利空:" + "；".join(bear[:2]))
    if ind:
        msg_bits.append("行业:" + "；".join(ind[:1]))
    if msg_bits:
        msg = "｜".join(msg_bits)
    elif news:
        msg = "；".join(n.get("title", "") for n in news[:2])
    else:
        msg = "暂无重大舆情"

    net_ok = str(net_yoy) not in ("暂无", "-", "", "None") and not str(net_yoy).startswith("-")
    pe_num = None
    try:
        pe_num = float(str(pe).replace("%", "").replace(",", ""))
    except (ValueError, TypeError):
        pass
    val_level = "偏高" if (pe_num is not None and pe_num > 60) else ("偏低" if (pe_num is not None and 0 < pe_num < 15) else "中性")

    # 💰 资金流向判断：解析数值符号，避免"净流入-1794万"被误判为流入
    cap_s = str(cap)
    cap_flow_desc = "数据暂无"
    if cap_s not in ("暂无", "-", "", "None"):
        try:
            cap_val = float(cap_s.replace(",", "").replace("亿", "").replace("万", "").strip())
            if "亿" in cap_s:
                cap_val *= 100000000
            elif "万" in cap_s:
                cap_val *= 10000
            if cap_val > 0:
                cap_flow_desc = "呈净流入态势"
            elif cap_val < 0:
                cap_flow_desc = "呈净流出态势（资金撤离，谨慎）"
            else:
                cap_flow_desc = "基本持平"
        except (ValueError, TypeError):
            cap_flow_desc = "数据暂无"

    return (
        f"【财报面】PE {pe}，ROE {roe}，营收同比 {rev_yoy}，净利同比 {net_yoy}，"
        f"整体{'盈利向好' if net_ok else '需警惕'}。\n"
        f"【估值/红利】估值水平 {val_level}，"
        f"股息率 {div}（{'有红利防守' if div not in ('暂无','') else '无'}）。\n"
        f"【资金面】当日主力净流入 {cap}，资金{cap_flow_desc}。\n"
        f"【消息面】{msg}。\n"
        f"【盘面/策略】按「{strategy}」信号入选，现价约 {price}元，注意止损纪律。"
    )


def generate_analysis_text(code: str, name: str, price: float, snap: dict, strategy: str,
                           max_chars: int = 400) -> str:
    """生成个股多维度分析文字。LLM 可用则优先，否则模板降级。"""
    if make_llm is not None:
        try:
            llm = make_llm(timeout=_LLM_TIMEOUT, max_tokens=600)
            system = (
                "你是A股资深分析师。基于给定数据，输出简洁的多维度个股分析，"
                "必须涵盖：财报面（盈利/估值）、资金面（主力流向）、消息面（舆情）、"
                "盘面与策略，并给出一句风险提示。用中文分点，克制在350字以内，不要寒暄。"
            )
            user = _snapshot_to_text(snap, strategy, price)
            out = llm.complete(system, user).strip()
            if out:
                # 清理可能的代码围栏
                out = out.replace("```", "").strip()
                return out[:max_chars]
        except Exception as e:
            print(f"⚠️ LLM 分析 {name}({code}) 失败，降级模板: {e}")

    return _template_analysis(snap, strategy, price)[:max_chars]


def summarize_for_feishu(analysis_text: str, limit: int = 300) -> str:
    """飞书单元格对长度敏感，截断为摘要。"""
    if not analysis_text:
        return ""
    return analysis_text[:limit] + ("…" if len(analysis_text) > limit else "")


# ──────────────────────────────────────────────────────────────
# 4. 企业微信分段推送工具（防封锁）
# ──────────────────────────────────────────────────────────────
def split_wechat_message(text: str, max_len: int = 1800, header: str = "") -> list:
    """
    把超长消息按『自然边界』拆分为多条，避免企业微信单条过长被风控封锁。
    优先按换行/分隔符切，其次硬切；返回若干条字符串。

    header：可选抬头（如『【3/5】贵州茅台(600519)』）。提供时拆出的【每一段】都会
            前置该抬头并标注段号，避免多只个股拆段后消息混淆、看不出属于哪只票。
    """
    if not text:
        return []
    # 若传了抬头，正文可用长度要扣掉抬头与段号占位，确保拼接后仍不超长
    body_max = max_len
    if header:
        body_max = max(200, max_len - len(header) - 16)

    chunks = []
    cur = ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 <= body_max:
            cur = (cur + "\n" + line).strip() if cur else line
        else:
            if cur:
                chunks.append(cur)
            # 单行超长则硬切
            while len(line) > body_max:
                chunks.append(line[:body_max])
                line = line[body_max:]
            cur = line
    if cur:
        chunks.append(cur)

    if header and len(chunks) > 1:
        total = len(chunks)
        return [f"{header}（{i}/{total}）\n{p}" for i, p in enumerate(chunks, 1)]
    if header:
        return [f"{header}\n{chunks[0]}"]
    return chunks


if __name__ == "__main__":
    # 简单自测（不联网）：验证降级模板与分段逻辑
    demo = enrich_stock("600000", "浦发银行", 10.0)
    print("快照:", demo)
    print("风险:", build_risk_hint(demo, "右侧启动"))
    print("模板分析:", generate_analysis_text("600000", "浦发银行", 10.0, demo, "右侧启动"))
    long = "段落\n" * 200
    print("分段数:", len(split_wechat_message(long, 1800)))
