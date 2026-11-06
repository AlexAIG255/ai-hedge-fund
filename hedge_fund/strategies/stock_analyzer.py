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
    from hedge_fund.data.cn_stock_data import CNStockDataFetcher
except ImportError:
    try:
        from data.cn_stock_data import CNStockDataFetcher
    except ImportError:
        CNStockDataFetcher = None

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
        return snap
    except Exception as e:
        print(f"⚠️ 富集 {name}({code}) 数据失败: {e}")
        return {
            "code": str(code).replace("sh", "").replace("sz", "").replace("bj", "").strip(),
            "name": name, "price": price,
            "financials": {}, "dividend_yield": "暂无",
            "capital_flow": "暂无", "news": [],
        }


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
    fin_str = "、".join(f"{k}:{v}" for k, v in fin.items()) or "暂无"
    news = snap.get("news", []) or []
    news_str = "；".join(n.get("title", "") for n in news[:3]) or "暂无重大舆情"
    return (
        f"标的：{snap.get('name')}({snap.get('code')}) 现价约{price}元 策略:{strategy}\n"
        f"财报估值：{fin_str}\n"
        f"股息率：{snap.get('dividend_yield')} 主力净流入：{snap.get('capital_flow')}\n"
        f"当日舆情：{news_str}"
    )


def _template_analysis(snap: dict, strategy: str, price: float) -> str:
    fin = snap.get("financials", {}) or {}
    pe = fin.get("市盈率(TTM)", "-")
    roe = fin.get("ROE(净资产收益率)", "-")
    rev_yoy = fin.get("营收同比", "-")
    net_yoy = fin.get("净利润同比", "-")
    div = snap.get("dividend_yield", "暂无")
    cap = snap.get("capital_flow", "暂无")
    news = snap.get("news", []) or []
    top_news = news[0].get("title", "暂无重大舆情") if news else "暂无重大舆情"

    return (
        f"【财报面】PE {pe}，ROE {roe}，营收同比 {rev_yoy}，净利同比 {net_yoy}，"
        f"整体{'盈利向好' if ('-' not in str(net_yoy) and not str(net_yoy).startswith('-')) else '需警惕'}。\n"
        f"【估值/红利】估值水平 {('偏高' if str(pe).replace('%','').isdigit() and float(str(pe).replace('%',''))>60 else '中性')}，"
        f"股息率 {div}（{'有红利防守' if div not in ('暂无','') else '无'}）。\n"
        f"【资金面】当日主力净流入 {cap}，资金{'呈流入态势' if '亿' in str(cap) or '万' in str(cap) else '数据暂无'}。\n"
        f"【消息面】{top_news}。\n"
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
def split_wechat_message(text: str, max_len: int = 1800) -> list:
    """
    把超长消息按『自然边界』拆分为多条，避免企业微信单条过长被风控封锁。
    优先按换行/分隔符切，其次硬切；返回若干条字符串。
    """
    if not text:
        return []
    chunks = []
    cur = ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 <= max_len:
            cur = (cur + "\n" + line).strip() if cur else line
        else:
            if cur:
                chunks.append(cur)
            # 单行超长则硬切
            while len(line) > max_len:
                chunks.append(line[:max_len])
                line = line[max_len:]
            cur = line
    if cur:
        chunks.append(cur)
    return chunks


if __name__ == "__main__":
    # 简单自测（不联网）：验证降级模板与分段逻辑
    demo = enrich_stock("600000", "浦发银行", 10.0)
    print("快照:", demo)
    print("风险:", build_risk_hint(demo, "右侧启动"))
    print("模板分析:", generate_analysis_text("600000", "浦发银行", 10.0, demo, "右侧启动"))
    long = "段落\n" * 200
    print("分段数:", len(split_wechat_message(long, 1800)))
