"""
持仓分析 Agent（截图识别 + 多维度分析 + 每日总结 + 自动进化 Skill）
=====================================================================
需求（用户）：
  1. 输入持仓截图 → 识别 股票代码 / 持仓数量 / 成本价 / 最新价
  2. 搜寻相关新闻、资金面等多维度资料
  3. 每日详细总结：提示 买入 / 卖出 / 做T 的价格机会，含盈亏原因归因
  4. GitHub 上的 Skill 能【自动进化】，降低误判机率

设计：
  - OCR 用多模态大模型（视觉接口，OpenAI 兼容），无需本地 OCR 引擎。
  - 多维资料复用 stock_analyzer.enrich_stock（财报/股息/资金面/舆情）。
  - 个股分析优先用 LLM 生成结构化建议（买卖/T 价位 + 盈亏归因），失败降级模板。
  - 经验记忆 skills_memory.json：沉淀历史教训/案例，分析时检索相关经验注入提示词，
    并由晚间复盘结果自动进化（SKILL_AUTO_EVOLVE），逐步降低误判。

入口：
  python holding_analyzer.py --image 持仓截图.jpg     # 识别+分析+推送
  python holding_analyzer.py --daily                 # 用已存持仓做每日总结
  python holding_analyzer.py --holdings-file x.json  # 直接给持仓JSON（免OCR，便于测试）
  python holding_analyzer.py --feedback "经验文字"     # 手动沉淀一条经验
"""

import argparse
import base64
import json
import os
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 📝 自动加载 .env（cron/手动运行都需；否则 WECHAT_WEBHOOK 等为空导致静默不推送）
try:
    from _env import load_env
except ImportError:
    from hedge_fund.strategies._env import load_env
load_env()

# ── 依赖（均带优雅降级）────────────────────────────────────────────────────
try:
    from strategy_config import to_market_code
except ImportError:
    def to_market_code(c):
        c = str(c).zfill(6)
        if c.startswith(("60", "68", "688")):
            return f"sh{c}"
        if c.startswith(("00", "30")):
            return f"sz{c}"
        return f"sh{c}"

try:
    from stock_analyzer import enrich_stock, build_risk_hint, split_wechat_message
except ImportError:
    enrich_stock = build_risk_hint = split_wechat_message = None

try:
    from data.cn_stock_data import CNStockDataFetcher
except ImportError:
    try:
        from hedge_fund.data.cn_stock_data import CNStockDataFetcher
    except ImportError:
        CNStockDataFetcher = None

try:
    from llm.client import make_llm, extract_json
except ImportError:
    try:
        from hedge_fund.llm.client import make_llm, extract_json
    except ImportError:
        make_llm = None
        extract_json = None


# ── 配置 ─────────────────────────────────────────────────────────────────
HOLDINGS_FILE = os.environ.get("HOLDINGS_FILE", "my_holdings.json")
EXPERIENCE_FILE = os.environ.get("EXPERIENCE_FILE", "skills_memory.json")
WECHAT_WEBHOOK = os.environ.get("WECHAT_WEBHOOK", "").strip()
_LLM_TIMEOUT = float(os.environ.get("ANALYSIS_LLM_TIMEOUT", "45"))

# 🪶 Token / 字数 控制（免费额度或本地模型都更省）
VISION_MAX_IMAGE_SIDE = int(os.environ.get("VISION_MAX_IMAGE_SIDE", "1280"))  # 截图最长边压缩到此像素
VISION_MAX_TOKENS = int(os.environ.get("VISION_MAX_TOKENS", "900"))          # 视觉OCR输出上限
ANALYSIS_LLM_MAX_TOKENS = int(os.environ.get("ANALYSIS_LLM_MAX_TOKENS", "600"))  # 文本分析输出上限
SUMMARY_MAX_CHARS = int(os.environ.get("SUMMARY_MAX_CHARS", "100000"))        # 单只持仓总结字数软上限

# 🔗 可选：Dify 后端（把分析/识图走 Dify 的免费模型）。默认关闭，配置 DIFY_API_KEY 并设
#    DIFY_ENABLED=true 才启用。这样你给 Dify 控制权后，可由 Dify 侧选免费 API、限流、控字数。
DIFY_API_URL = os.environ.get("DIFY_API_URL", "https://api.dify.ai/v1/chat-messages")
DIFY_API_KEY = os.environ.get("DIFY_API_KEY", "").strip()
DIFY_ENABLED = bool(DIFY_API_KEY) and os.environ.get("DIFY_ENABLED", "false").lower() in ("true", "1", "yes")
DIFY_USER = os.environ.get("DIFY_USER", "hedge-fund-bot")


# ==========================================================
# 1. 视觉 OCR：从持仓截图识别 代码 / 数量 / 价格
# ==========================================================
# 🔌 视觉/分析 后端（全部 OpenAI 兼容 /chat/completions，可一键切换免费额度）
#   openai      → 付费/token，质量最高（默认 gpt-4o）
#   siliconflow → 硅基流动，国内友好，注册送免费额度（默认 GLM-4V-9B，智谱·非千问）
#   openrouter  → 全球，有真·免费视觉模型（默认 google/gemini-2.0-flash-001）
#   github      → GitHub Models，免费限流（默认 gpt-4o）
#   ollama      → 本地 Mac mini 部署后免费（默认 minicpm-v:8b，不耗 token）
VISION_BACKEND = os.environ.get("VISION_BACKEND", "openai").lower()
_BACKEND_PRESETS = {
    "openai":      ("https://api.openai.com/v1", "gpt-4o"),
    "siliconflow": ("https://api.siliconflow.cn/v1", "THUDM/glm-4v-9b"),
    "openrouter":  ("https://openrouter.ai/api/v1", "google/gemini-2.0-flash-001"),
    "github":      ("https://models.inference.ai.azure.com", "gpt-4o"),
    "ollama":      ("http://127.0.0.1:11434/v1", "minicpm-v:8b"),
}
# 各后端对应的 API Key 环境变量（ollama 本地免 key）
_BACKEND_KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "siliconflow": "SILICONFLOW_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "github": "GITHUB_TOKEN",
    "ollama": None,
}


def _resolve_backend():
    """按 VISION_BACKEND 解析 (api_key, base_url, model)。
    允许 VISION_API_KEY / VISION_API_BASE / VISION_MODEL 显式覆盖（兼容旧手动配置）。
    注意：实时读 os.environ，以支持运行时切换后端（不依赖导入时的缓存值）。"""
    backend = os.environ.get("VISION_BACKEND", "openai").lower()
    preset_base, preset_model = _BACKEND_PRESETS.get(backend, _BACKEND_PRESETS["openai"])
    base = os.environ.get("VISION_API_BASE") or preset_base
    model = os.environ.get("VISION_MODEL") or preset_model
    key_env = _BACKEND_KEY_ENV.get(backend)
    key = (os.environ.get("VISION_API_KEY")
           or (os.environ.get(key_env) if key_env else "")
           or os.environ.get("OPENAI_API_KEY", ""))
    if backend == "ollama" and not key:
        key = "sk-no-key-needed"  # 本地 Ollama 不校验 key
    return key, base, model


def _openai_chat(system: str, user: str, max_tokens: int, timeout: int = 60) -> str:
    """调用 OpenAI 兼容的文本对话接口（持仓文本分析用，与视觉 OCR 共用后端）。"""
    key, base, model = _resolve_backend()
    if not key:
        raise RuntimeError(
            f"未配置 {VISION_BACKEND} 后端 API Key（请设置对应 Key，或 VISION_API_KEY / OPENAI_API_KEY）。"
        )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    r = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload, timeout=timeout,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _resize_image(image_bytes: bytes, max_side: int = VISION_MAX_IMAGE_SIDE) -> bytes:
    """把截图压缩到最长边 max_side 以内再转 base64，显著减少视觉 token 消耗。
    PIL 不可用时原样返回（仍可工作，只是 token 偏多）。"""
    if not max_side or max_side <= 0:
        return image_bytes
    try:
        from io import BytesIO
        from PIL import Image
    except Exception:
        return image_bytes
    try:
        img = Image.open(BytesIO(image_bytes))
        img = img.convert("RGB")
        if max(img.size) > max_side:
            ratio = max_side / max(img.size)
            new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
            img = img.resize(new_size, Image.LANCZOS)
        out = BytesIO()
        img.save(out, format="JPEG", quality=82)
        return out.getvalue()
    except Exception as e:
        print(f"⚠️ 截图压缩失败，使用原图: {e}")
        return image_bytes


def _dify_chat(system: str, user: str, max_tokens: int) -> str:
    """走 Dify Chatflow/对话型应用（可用其绑定的免费模型）。返回模型文本。"""
    if not DIFY_API_KEY:
        raise RuntimeError("未配置 DIFY_API_KEY")
    payload = {
        "inputs": {},
        "query": f"{system}\n\n{user}",
        "response_mode": "blocking",
        "user": DIFY_USER,
    }
    r = requests.post(
        DIFY_API_URL,
        headers={"Authorization": f"Bearer {DIFY_API_KEY}", "Content-Type": "application/json"},
        json=payload, timeout=_LLM_TIMEOUT + 30,
    )
    r.raise_for_status()
    return r.json().get("answer", "")


def llm_complete(system: str, user: str, max_tokens: int = ANALYSIS_LLM_MAX_TOKENS,
                 timeout: int = None) -> str:
    """统一 LLM 文本调用入口（供 agent_council 等复用）。

    降级顺序：Dify（若启用）→ OpenAI 兼容免费后端（VISION_BACKEND 切换） → make_llm。
    全部失败则抛 RuntimeError，由调用方自行降级到模板，保证主流程不中断。
    """
    timeout = _LLM_TIMEOUT if timeout is None else timeout
    if DIFY_ENABLED:
        try:
            return _dify_chat(system, user, max_tokens)
        except Exception as e:
            print(f"⚠️ Dify 调用失败，降级免费后端: {e}")
    try:
        return _openai_chat(system, user, max_tokens, timeout)
    except Exception as e1:
        if make_llm is not None:
            try:
                llm = make_llm(timeout=timeout, max_tokens=max_tokens)
                return llm.complete(system, user)
            except Exception as e2:
                raise RuntimeError(f"LLM 全部后端不可用（免费后端:{e1}; make_llm:{e2}）")
        raise RuntimeError(f"LLM 后端不可用（免费后端:{e1}）")


def vision_ocr_image(image_bytes: bytes, mime: str = "image/jpeg", timeout: int = 60) -> List[Dict]:
    """调用多模态大模型，从持仓截图识别每只持仓股。返回持仓字典列表。"""
    key, base, model = _resolve_backend()
    if not key:
        raise RuntimeError(
            f"未配置 {VISION_BACKEND} 后端 API Key（请设置对应 Key，或 VISION_API_KEY / OPENAI_API_KEY），无法做截图 OCR。"
        )

    # 🪶 压缩截图尺寸 → 省视觉 token
    image_bytes = _resize_image(image_bytes, VISION_MAX_IMAGE_SIDE)
    b64 = base64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:image/jpeg;base64,{b64}"
    system = (
        "你是A股持仓识别助手。从券商App『持仓』截图提取每只股票："
        "code(6位代码)、name、quantity(股)、cost_price(成本)、current_price(现价,看不清留空)。"
        "只输出JSON数组，不要多余文字。"
    )
    user = (
        "识别此持仓截图，返回JSON数组（看不清填0或空）：\n"
        '[ {"code":"600000","name":"浦发银行","quantity":1000,"cost_price":10.5,"current_price":10.8} ]'
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": [
                {"type": "text", "text": user},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]},
        ],
        "max_tokens": VISION_MAX_TOKENS,
        "temperature": 0,
    }
    r = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload, timeout=timeout,
    )
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    return _parse_holdings_json(content)


def _parse_holdings_json(text: str) -> List[Dict]:
    raw = None
    try:
        if extract_json is not None:
            raw = extract_json(text)
        else:
            import re as _re
            m = _re.search(r"\[.*\]", text, _re.DOTALL)
            raw = json.loads(m.group(0)) if m else json.loads(text)
    except Exception:
        # 退一步：尝试直接 loads
        try:
            raw = json.loads(text.strip())
        except Exception:
            raise RuntimeError(f"无法从视觉模型返回中解析持仓JSON: {text[:200]}")

    items = raw if isinstance(raw, list) else raw.get("holdings", [])
    holdings = []
    for it in items:
        code = str(it.get("code") or "").strip()
        # 容错：去掉 sh/sz 前缀、补齐 6 位
        code = code.replace("sh", "").replace("sz", "").replace("bj", "").strip()
        if not (code.isdigit() and len(code) == 6):
            continue
        try:
            qty = float(str(it.get("quantity") or 0))
            cost = float(str(it.get("cost_price") or 0))
            cur = float(str(it.get("current_price") or 0))
        except (ValueError, TypeError):
            continue
        holdings.append({
            "code": code,
            "name": str(it.get("name") or code),
            "quantity": qty,
            "cost_price": cost,
            "current_price": cur,
        })
    return holdings


# ==========================================================
# 2. 持仓文件读写
# ==========================================================
def save_holdings(holdings: List[Dict]):
    payload = {"updated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "holdings": holdings}
    with open(HOLDINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"💾 持仓已写入 {HOLDINGS_FILE}（共 {len(holdings)} 只）")


def load_holdings() -> List[Dict]:
    if os.path.exists(HOLDINGS_FILE):
        try:
            with open(HOLDINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("holdings", []) if isinstance(data, dict) else data
        except Exception:
            return []
    return []


# ==========================================================
# 3. 实时价格
# ==========================================================
def fetch_realtime_price(code: str) -> float:
    tc = to_market_code(code)
    try:
        res = requests.get(f"http://qt.gtimg.cn/q={tc}", timeout=4)
        if res.status_code == 200 and '="' in res.text:
            fields = res.text.split('="')[1].split("~")
            p = float(fields[3] or 0)
            if p > 0:
                return p
    except Exception:
        pass
    return 0.0


# ==========================================================
# 4. 个股分析（LLM 优先 + 模板降级）
# ==========================================================
def _template_decision(code: str, name: str, cost: float, cur: float, snap: dict) -> Dict:
    """无 LLM 时的降级结构化建议。"""
    if cost <= 0:
        cost = cur or 1.0
    stop = round(cost * 0.95, 2)
    tp = round(cost * 1.08, 2)
    t_buy = round(min(cur, cost) * 0.99, 2)
    t_sell = round(max(cur, cost) * 1.01, 2)
    pnl_pct = ((cur - cost) / cost * 100) if cost > 0 else 0.0
    if pnl_pct >= 8:
        action = "持有/可做T"
    elif pnl_pct <= -5:
        action = "观望/控仓"
    else:
        action = "持有"
    fin = snap.get("financials", {}) or {}
    net = fin.get("净利润同比", "-")
    cap = snap.get("capital_flow", "暂无")
    attribution = (
        f"当前{'盈利' if pnl_pct >= 0 else '亏损'} {pnl_pct:+.2f}%，"
        f"净利润同比 {net}，主力净流入 {cap}；"
        f"归因：{'基本面与资金面共振' if ('-' not in str(net) and not str(net).startswith('-')) else '盈利或资金承压'}。"
    )
    return {
        "action": action, "t_buy": t_buy, "t_sell": t_sell,
        "stop_loss": stop, "take_profit": tp,
        "attribution": _trim(attribution, 60),
        "comment": _trim(f"按成本 {cost}元、现价 {cur}元 测算，注意止损纪律。", 40),
    }


def _trim(text: str, n: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= n else text[:n] + "…"


def _assemble_decision(parsed: Dict, cost: float, cur: float) -> Dict:
    """把 LLM/Dify 返回的 JSON 组装成决策字典，并控制字数、补默认值。"""
    def num(v, default):
        try:
            return float(v)
        except (ValueError, TypeError):
            return default
    return {
        "action": _trim(str(parsed.get("action", "持有")), 8) or "持有",
        "t_buy": num(parsed.get("t_buy"), round(min(cur, cost) * 0.99, 2)),
        "t_sell": num(parsed.get("t_sell"), round(max(cur, cost) * 1.01, 2)),
        "stop_loss": num(parsed.get("stop_loss"), round(cost * 0.95, 2)),
        "take_profit": num(parsed.get("take_profit"), round(cost * 1.08, 2)),
        # 🪶 字数控制：盈亏归因 ≤60 字、点评 ≤40 字，避免长文本刷屏/耗 token
        "attribution": _trim(parsed.get("attribution", ""), 60),
        "comment": _trim(parsed.get("comment", ""), 40),
    }


def analyze_holding(h: Dict, lessons_ctx: str = "") -> Dict:
    code = str(h["code"]).zfill(6)
    name = h.get("name", code)
    cost = float(h.get("cost_price") or 0)
    cur = float(h.get("current_price") or 0) or fetch_realtime_price(code)
    qty = float(h.get("quantity") or 0)

    snap = {}
    if enrich_stock is not None:
        try:
            snap = enrich_stock(code, name, cur)
        except Exception as e:
            print(f"⚠️ 富集 {name}({code}) 失败: {e}")
            snap = {}
    if not snap:
        snap = {"financials": {}, "dividend_yield": "暂无", "capital_flow": "暂无", "news": []}

    pnl = (cur - cost) * qty
    pnl_pct = ((cur - cost) / cost * 100) if cost > 0 else 0.0

    decision = None
    # 优先 Dify（若启用），否则用本地/直连 LLM；都失败则模板降级
    if DIFY_ENABLED:
        try:
            system = (
                "你是A股资深投顾。基于持仓与多维资料，输出严格JSON（不要多余文字），字段："
                "action(持有/加仓/减仓/做T/观望/止损)、t_buy(做T低吸价,元)、"
                "t_sell(做T高抛价,元)、stop_loss(止损价,元)、take_profit(止盈价,元)、"
                "attribution(盈亏归因1-2句,结合财报/资金/消息/大盘)、comment(一句话点评)。"
            )
            news = snap.get("news", []) or []
            news_str = "；".join(n.get("title", "") for n in news[:3]) or "暂无重大舆情"
            fin = snap.get("financials", {}) or {}
            user = (
                f"持仓：{name}({code}) 成本{cost}元 现价{cur}元 数量{qty}股 盈亏{pnl_pct:+.2f}%\n"
                f"财报：{fin} 股息率：{snap.get('dividend_yield')} 主力净流入：{snap.get('capital_flow')}\n"
                f"舆情：{news_str}\n{lessons_ctx}"
            )
            out = _dify_chat(system, user, ANALYSIS_LLM_MAX_TOKENS).strip().replace("```", "").strip()
            parsed = extract_json(out) if extract_json else json.loads(out)
            decision = _assemble_decision(parsed, cost, cur)
        except Exception as e:
            print(f"⚠️ Dify 分析 {name}({code}) 失败，降级模板: {e}")
    else:
        # 优先 OpenAI 兼容后端（免费额度/SiliconFlow/OpenRouter/GitHub/Ollama 均可），
        # 无 key 时优雅降级到 make_llm（Anthropic/DeepSeek 等），再不行降级模板。
        system = (
            "你是A股资深投顾。基于持仓与多维资料，输出严格JSON（不要多余文字），字段：\n"
            "action(持有/加仓/减仓/做T/观望/止损)、t_buy(做T低吸买点,元)、"
            "t_sell(做T高抛卖点,元)、stop_loss(止损价,元)、take_profit(止盈价,元)、"
            "attribution(盈亏原因归因,1-2句，结合财报/资金/消息/大盘)、comment(一句话操作点评)。"
            "做T区间应围绕成本价与现价给出可操作的低吸高抛价位。"
        )
        news = snap.get("news", []) or []
        news_str = "；".join(n.get("title", "") for n in news[:3]) or "暂无重大舆情"
        fin = snap.get("financials", {}) or {}
        user = (
            f"持仓：{name}({code}) 成本价{cost}元 现价{cur}元 数量{qty}股 盈亏{pnl_pct:+.2f}%\n"
            f"财报：{fin} 股息率：{snap.get('dividend_yield')} 主力净流入：{snap.get('capital_flow')}\n"
            f"舆情：{news_str}\n"
            f"{lessons_ctx}"
        )
        try:
            out = _openai_chat(system, user, ANALYSIS_LLM_MAX_TOKENS).strip().replace("```", "").strip()
            parsed = extract_json(out) if extract_json else json.loads(out)
            decision = _assemble_decision(parsed, cost, cur)
        except Exception as e1:
            print(f"⚠️ OpenAI兼容后端分析 {name}({code}) 失败: {e1}")
            if make_llm is not None:
                try:
                    llm = make_llm(timeout=_LLM_TIMEOUT, max_tokens=ANALYSIS_LLM_MAX_TOKENS)
                    out = llm.complete(system, user).strip().replace("```", "").strip()
                    parsed = extract_json(out) if extract_json else json.loads(out)
                    decision = _assemble_decision(parsed, cost, cur)
                except Exception as e2:
                    print(f"⚠️ make_llm 分析 {name}({code}) 也失败，降级模板: {e2}")

    if decision is None:
        decision = _template_decision(code, name, cost, cur, snap)

    return {
        "code": code, "name": name, "quantity": qty,
        "cost_price": cost, "current_price": cur,
        "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2),
        "dividend_yield": snap.get("dividend_yield", "暂无"),
        "capital_flow": snap.get("capital_flow", "暂无"),
        "risk_hint": build_risk_hint(snap, "") if build_risk_hint else "",
        "news": [n.get("title", "") for n in (snap.get("news") or [])][:3],
        **decision,
    }


def analyze_all(holdings: List[Dict], lessons_ctx: str = "") -> List[Dict]:
    results = []
    for h in holdings:
        results.append(analyze_holding(h, lessons_ctx))
    return results


# ==========================================================
# 5. 每日详细总结
# ==========================================================
def build_daily_summary(results: List[Dict]) -> str:
    today = time.strftime("%Y-%m-%d")
    total_pnl = sum(r["pnl"] for r in results)
    total_cost = sum(r["cost_price"] * r["quantity"] for r in results)
    total_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0.0

    lines = [
        f"📊 **【我的持仓 · 每日详细总结】** ({today})",
        f"-----------------------------------",
        f"持仓标的：{len(results)} 只 | 总浮动盈亏：`{total_pnl:+.2f}元` ({total_pct:+.2f}%)",
        f"-----------------------------------",
    ]
    for r in results:
        icon = "🟢" if r["pnl"] >= 0 else "🔴"
        lines.append(
            f"{icon} **{r['name']}** (`{r['code']}`) × {int(r['quantity'])}股\n"
            f"• 成本 `{r['cost_price']:.2f}` / 现价 `{r['current_price']:.2f}` → 盈亏 `{r['pnl']:+.2f}元` ({r['pnl_pct']:+.2f}%)\n"
            f"• 操作建议：**{r['action']}**\n"
            f"• 做T区间：低吸 `{r['t_buy']:.2f}` ~ 高抛 `{r['t_sell']:.2f}`\n"
            f"• 止损 `{r['stop_loss']:.2f}` / 止盈 `{r['take_profit']:.2f}`\n"
            f"• 资金面：主力净流入 {r['capital_flow']} | 股息率 {r['dividend_yield']}\n"
            f"• 盈亏归因：{r['attribution']}\n"
            f"• 点评：{r['comment']}\n"
            f"• 舆情：{('；'.join(r['news']) if r['news'] else '暂无重大舆情')}"
        )
        lines.append("-----------------------------------")

    lines.append("💡 提示：以上为量化+基本面辅助参考，非投资建议；做T需结合实时盘口与手续费成本。")
    return "\n".join(lines)


# ==========================================================
# 6. 推送（企业微信分段）
# ==========================================================
def push_wechat(text: str) -> bool:
    if not WECHAT_WEBHOOK:
        print("⚠️ 未配置 WECHAT_WEBHOOK，跳过企业微信推送（仅打印）。")
        print(text)
        return False
    parts = split_wechat_message(text, 1800) if split_wechat_message else [text]
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


# ==========================================================
# 7. 经验记忆（自动进化 Skill）
# ==========================================================
_SEED_LESSONS = [
    "A股做T需覆盖手续费与滑点，单笔价差一般需 >0.3% 才划算，震荡市优于单边市。",
    "放量滞涨（量比高但涨幅小）往往是出货信号，谨慎追高。",
    "缩量回调不破关键均线（如20日线）多为洗盘，可逢低关注而非恐慌止损。",
    "财报披露窗口期（业绩预告/季报）前后波动加大，应提前收紧止损。",
    "弱势市场中亏损标的应优先控仓，避免摊平亏损（向下加仓）。",
    "主力连续净流入但股价不涨，需警惕对倒或利好兑现，不宜盲目乐观。",
]


class ExperienceMemory:
    """持仓分析 Skill 的经验记忆：沉淀教训/案例，分析时检索注入，逐步降低误判。"""

    def __init__(self, path: str = EXPERIENCE_FILE):
        self.path = path
        self.data = {"lessons": [], "cases": []}
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
                if not isinstance(self.data.get("lessons"), list):
                    self.data["lessons"] = []
                if not isinstance(self.data.get("cases"), list):
                    self.data["cases"] = []
            except Exception:
                self.data = {"lessons": [], "cases": []}
        # 首次运行注入基线经验
        if not self.data["lessons"]:
            for t in _SEED_LESSONS:
                self.data["lessons"].append({"date": "seed", "tag": "通用", "text": t, "weight": 1.0})
            self._save()

    def _save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def add_lesson(self, text: str, tag: str = "用户反馈"):
        self.data["lessons"].append({
            "date": time.strftime("%Y-%m-%d"), "tag": tag, "text": text, "weight": 1.0,
        })
        self._save()
        print(f"🧠 已沉淀经验：{text[:40]}…")

    def add_case(self, code: str, decision: str, outcome: str, note: str = ""):
        self.data["cases"].append({
            "date": time.strftime("%Y-%m-%d"), "code": code,
            "decision": decision, "outcome": outcome, "note": note,
        })
        self._save()

    def retrieve_relevant(self, query: str, k: int = 5) -> str:
        """按关键词重叠检索最相关经验，返回提示词上下文。"""
        q_tokens = set(query)
        scored = []
        for les in self.data["lessons"]:
            text = les.get("text", "")
            overlap = len(set(text) & q_tokens)
            score = overlap * les.get("weight", 1.0)
            scored.append((score, text))
        scored.sort(key=lambda x: x[0], reverse=True)
        top = [t for s, t in scored[:k] if s > 0]
        if not top:
            top = [t for _, t in scored[:3]]
        if not top:
            return ""
        return "【历史经验/避坑指南，请结合参考以降低误判】\n" + "\n".join(f"- {t}" for t in top)

    def auto_evolve(self, win_rate_by_strategy: Dict[str, Dict]):
        """由复盘胜率自动沉淀经验（SKILL_AUTO_EVOLVE 开启时调用）。"""
        if os.environ.get("SKILL_AUTO_EVOLVE", "true").lower() not in ("true", "1", "yes"):
            return
        for strat, info in win_rate_by_strategy.items():
            total = info.get("total", 0)
            win = info.get("win", 0)
            if total >= 5 and win / total < 0.45:
                text = (
                    f"策略『{strat}』在历史 {total} 个样本中胜率仅 {win/total*100:.0f}%，"
                    f"偏低，建议对该类标的收紧止损(如-4%)或降低单次仓位，并提高放量滞涨的警觉。"
                )
                # 避免重复沉淀：同类策略教训已有则跳过
                if not any(strat in l.get("text", "") for l in self.data["lessons"]):
                    self.add_lesson(text, tag="自动进化")


# ==========================================================
# 8. 流水线编排
# ==========================================================
def run_from_image(image_path: str) -> str:
    with open(image_path, "rb") as f:
        img_bytes = f.read()
    mime = "image/png" if image_path.lower().endswith(".png") else "image/jpeg"
    return run_from_image_bytes(img_bytes, mime)


def run_from_image_bytes(img_bytes: bytes, mime: str = "image/jpeg") -> str:
    print("🔍 正在用视觉模型识别持仓截图…")
    holdings = vision_ocr_image(img_bytes, mime)
    if not holdings:
        return "⚠️ 未能从截图中识别到持仓，请确认截图清晰且为券商持仓页面。"
    save_holdings(holdings)
    return _analyze_and_summarize(holdings)


def run_from_holdings_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    holdings = data.get("holdings", data) if isinstance(data, dict) else data
    save_holdings(holdings)
    return _analyze_and_summarize(holdings)


def run_daily() -> str:
    holdings = load_holdings()
    if not holdings:
        return "⚠️ 暂无持仓数据。请先发送持仓截图，或执行 --image / --holdings-file 录入。"
    return _analyze_and_summarize(holdings)


def _analyze_and_summarize(holdings: List[Dict]) -> str:
    mem = ExperienceMemory()
    # 汇总查询用于经验检索
    query = " ".join(f"{h.get('name','')}{h.get('code','')}" for h in holdings)
    lessons_ctx = mem.retrieve_relevant(query)
    results = analyze_all(holdings, lessons_ctx)
    summary = build_daily_summary(results)
    push_wechat(summary)
    print("🎉 持仓每日总结已生成并推送！")
    return summary


# ==========================================================
# 9. CLI
# ==========================================================
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="持仓截图分析 + 每日总结 + 自动进化 Skill")
    ap.add_argument("--image", help="持仓截图路径（jpg/png），识别后分析推送")
    ap.add_argument("--holdings-file", help="直接传入持仓JSON文件（免OCR，便于测试）")
    ap.add_argument("--daily", action="store_true", help="用已存持仓做每日详细总结")
    ap.add_argument("--feedback", help="手动沉淀一条经验到 Skill 记忆")
    args = ap.parse_args()

    if args.feedback:
        ExperienceMemory().add_lesson(args.feedback, tag="用户反馈")
    elif args.image:
        print(run_from_image(args.image))
    elif args.holdings_file:
        print(run_from_holdings_file(args.holdings_file))
    elif args.daily:
        print(run_daily())
    else:
        ap.print_help()
