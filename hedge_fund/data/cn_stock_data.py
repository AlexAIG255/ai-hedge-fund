"""
hedge_fund/data/cn_stock_data.py
专用于选中标的之【财务基础面】与【舆情新闻】定向补充数据源

数据来源（均为公开接口，无需 API Key，多源兜底）：
  - qt.gtimg.cn（腾讯基础行情）      → PE(TTM)/PB/流通市值/总市值【用户机器稳定可达，作为估值兜底主源】
  - datacenter-web.eastmoney.com     → 估值分析(PE_TTM) / 财务指标(ROE/营收/净利)【已实测可用】
  - push2.eastmoney.com (股票实时行情) → PB / 股息率TTM / 营收净利同比 / 主力净流入 / 流通市值【用户机器可达】
  - push2.eastmoney.com (个股资金流 daykline) → 主力净流入(按日)

健壮性：
  - 所有请求带重试 + 退避，避免盘中突发请求被限流时整批变「暂无」。
  - 每个数据点多源兜底：datacenter 优先（已验证），push2 补充（PB/股息/成长/资金）。
  - 单只失败不影响整体，返回「暂无」并打日志，便于用 __main__ 自检定位。
"""

import datetime
import os
import re
import time
from typing import Dict, List, Optional, Tuple
import requests


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
}


def _retry_get(url: str, params: Optional[dict] = None, timeout: int = 6, tries: int = 3) -> Optional[requests.Response]:
    """带退避的重试 GET，限流/超时自动重试；全失败返回 None。"""
    last_err = None
    for attempt in range(tries):
        try:
            r = requests.get(url, params=params, headers=_HEADERS, timeout=timeout)
            if r.status_code == 200 and len(r.content) > 2:
                return r
            last_err = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
        if attempt < tries - 1:
            time.sleep(0.4 * (attempt + 1))  # 0.4s / 0.8s 退避
    return None


def _secid(code: str) -> str:
    code = code.replace("sh", "").replace("sz", "").replace("bj", "").strip()
    return f"1.{code}" if code.startswith("60") else f"0.{code}"


def _fallback_chain(nodes, validate, label: str = "数据", default=None, verbose: bool = True):
    """通用故障转移链：按节点顺序逐个尝试，首个通过 validate 的结果立即返回。

    设计目标与三个受保护价格节点的容灾策略一致：主节点失败 → 自动切备用节点 →
    再失败继续切 → 直至成功；全部失败才返回 default。每个节点独立 try，单一节点
    异常/空结果不影响其余节点，并打印细粒度日志便于定位哪个节点在生效。

    参数:
      nodes: [(节点名, 无参可调用), ...]，按优先级排列
      validate: callable(result) -> bool，判定结果是否有效
      label: 日志前缀（维度名）
      default: 全部失败时的返回值
    返回:
      (结果, 命中节点名)；全部失败返回 (default, None)
    """
    for name, fn in nodes:
        try:
            res = fn()
            if validate(res):
                if verbose:
                    print(f"✅ [{label}] 节点『{name}』命中")
                return res, name
            if verbose:
                print(f"⚠️ [{label}] 节点『{name}』无有效数据，切换下一节点")
        except Exception as e:  # noqa: BLE001
            if verbose:
                print(f"⚠️ [{label}] 节点『{name}』异常({str(e)[:50]})，切换下一节点")
    if verbose:
        print(f"❌ [{label}] 全部节点失败，返回默认值")
    return default, None


# 大盘涨跌（只读复用现有腾讯 gtimg 节点的公开数据，不修改该节点任何逻辑）
# 带进程级缓存，5 分钟内复用，避免每个标的都重复请求大盘。
_MARKET_CACHE: Dict[str, object] = {"ts": 0.0, "pct": 0.0, "summary": ""}


def get_market_snapshot() -> dict:
    """返回大盘涨跌幅（上证为主）与摘要，供新闻-股价归因判断『大市波动』。
    仅读取现有行情节点的公开指数数据，不改动三个受保护价格节点。"""
    now = time.time()
    if _MARKET_CACHE.get("summary") and now - float(_MARKET_CACHE.get("ts", 0)) < 300:
        return {"pct": _MARKET_CACHE["pct"], "summary": _MARKET_CACHE["summary"]}
    pct, summary = 0.0, ""
    try:
        r = _retry_get("http://qt.gtimg.cn/q=sh000001", timeout=5, tries=1)
        if r and '="' in r.text:
            f = r.text.split('="')[1].split("~")
            if len(f) > 33:
                pct = float(f[32].replace("%", "") or 0)
                summary = f"{f[1]} {pct:+.2f}%"
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ 大盘指数读取失败，按中性处理: {str(e)[:50]}")
    if not summary:
        summary = "大盘数据暂无（按中性处理）"
    _MARKET_CACHE.update({"ts": now, "pct": pct, "summary": summary})
    return {"pct": pct, "summary": summary}


class CNStockDataFetcher:

    # 跨实例共享缓存，避免同一进程内重复拉取同一标的
    _CACHE: Dict[str, dict] = {}

    def __init__(self):
        self._cache = CNStockDataFetcher._CACHE

    # ───────────────────────── 1. 财务/估值（多源兜底） ─────────────────────────
    def fetch_stock_financials(self, symbol: str) -> Dict[str, str]:
        """抓取单只股票核心财务/估值指标。腾讯 gtimg 优先（用户机器可达），
        datacenter 补充 ROE/成长，push2 补充 PB/股息率/成长；多源兜底避免整段变空。"""
        code = symbol.replace("sh", "").replace("sz", "").replace("bj", "").strip()
        fin: Dict[str, str] = {}

        # —— 源 A（优先）：腾讯 gtimg 基础行情 —— PE(TTM)[39] / PB[46] / 流通市值[44] / 总市值[45]
        tq = self._tencent_quote(code)
        if tq:
            for idx, key in ((39, "市盈率(TTM)"), (46, "市净率(PB)"),
                             (44, "流通市值(亿)"), (45, "总市值(亿)")):
                v = tq.get(str(idx)) or tq.get(idx)
                if v not in (None, "", "-", "--"):
                    try:
                        fin.setdefault(key, f"{float(v):.2f}")
                    except ValueError:
                        pass

        # —— 源 B：datacenter 估值分析（PE_TTM 兜底）——
        if "市盈率(TTM)" not in fin:
            pe_ttm = self._datacenter_value("RPT_VALUEANALYSIS_DET", code, "PE_TTM")
            if pe_ttm is not None:
                fin["市盈率(TTM)"] = f"{pe_ttm:.2f}"
        # —— 源 C：datacenter 财务指标（权威同比：YSTZ 营收同比 / SJLTZ 净利同比 / WEIGHTAVG_ROE）——
        # push2 的 f182/f183 常返回天文脏值，改用 datacenter 正式财报字段，避免"净利润同比暂无"。
        roe = self._datacenter_value("RPT_LICO_FN_CPD", code, "WEIGHTAVG_ROE")
        if roe is not None and abs(roe) <= 10000:
            fin["ROE(净资产收益率)"] = f"{roe:.2f}%"
        ystz = self._datacenter_value("RPT_LICO_FN_CPD", code, "YSTZ")
        if ystz is not None and abs(ystz) <= 10000:
            fin["营收同比"] = f"{ystz:.2f}%"
        sjltz = self._datacenter_value("RPT_LICO_FN_CPD", code, "SJLTZ")
        if sjltz is not None and abs(sjltz) <= 10000:
            fin["净利润同比"] = f"{sjltz:.2f}%"

        # —— 源 D：push2 实时行情补充 PB / 股息率TTM / 营收同比 / 净利同比 ——
        q = self._push2_quote(code, fields="f57,f58,f167,f168,f182,f183,f173")
        if q:
            pb = q.get("f167")
            div = q.get("f168")
            rev_yoy = q.get("f182")
            net_yoy = q.get("f183")
            if "市净率(PB)" not in fin and isinstance(pb, (int, float)):
                fin.setdefault("市净率(PB)", f"{pb:.2f}")
            if isinstance(div, (int, float)) and div > 0:
                fin.setdefault("股息率(TTM)", f"{div:.2f}%")
            # 🔧 合理性过滤：东财 f182/f183 偶发返回天文数字原始值（如 1.3e11），
            # 直接格式化会污染分析。|%| 超 10000 视为脏值，跳过 → 归「暂无」。
            if isinstance(rev_yoy, (int, float)) and abs(rev_yoy) <= 10000:
                fin.setdefault("营收同比", f"{rev_yoy:.2f}%")
            if isinstance(net_yoy, (int, float)) and abs(net_yoy) <= 10000:
                fin.setdefault("净利润同比", f"{net_yoy:.2f}%")

        # —— 归一化：缺失字段统一为「暂无」，避免模板里出现裸「-」 ——
        for k in ("市盈率(TTM)", "市净率(PB)", "ROE(净资产收益率)",
                  "营收同比", "净利润同比", "股息率(TTM)"):
            fin.setdefault(k, "暂无")

        if not any(v != "暂无" for v in fin.values()):
            print(f"⚠️ 读取个股 {code} 财务数据全部失败（接口限流或网络），返回暂无。")
        return fin

    def _datacenter_value(self, report: str, code: str, column: str) -> Optional[float]:
        try:
            url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
            params = {
                "reportName": report,
                "columns": f"SECURITY_CODE,{column}",
                # 用原始字符串，交给 requests 统一编码（不要手动预编码，否则会双重编码）
                "filter": f'(SECURITY_CODE="{code}")',
                "source": "WEB", "client": "WEB", "pageSize": "1",
            }
            r = _retry_get(url, params=params, timeout=6)
            if r:
                data = r.json().get("result", {}).get("data") or []
                if data and data[0].get(column) is not None:
                    return float(data[0][column])
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ datacenter({report}.{column}) 读取 {code} 异常: {e}")
        return None

    def _push2_quote(self, code: str, fields: str) -> Optional[dict]:
        try:
            secid = _secid(code)
            url = "https://push2.eastmoney.com/api/qt/stock/get"
            params = {"secid": secid, "fields": fields, "invt": "2", "fltt": "2"}
            r = _retry_get(url, params=params, timeout=6)
            if r:
                return r.json().get("data") or {}
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ push2 行情读取 {code} 异常: {e}")
        return None

    # ───────────────────────── 1.5 腾讯 gtimg 基础行情（兜底） ─────────────────────────
    def _tencent_quote(self, code: str) -> Optional[dict]:
        """腾讯 gtimg 基础行情，返回按 `~` 拆分的字段表。

        用户机器（大陆）对 qt.gtimg.cn 稳定可达（早盘价格即取自该节点），
        故作为东财估值/市值数据的兜底源，避免东财偶发限流时估值维度整段变空。
        字段索引（v_sh600000="..." 按 ~ 拆分）：
          [39]=市盈率(TTM)  [46]=市净率(PB)  [44]=流通市值(亿)  [45]=总市值(亿)
        """
        tc = ("sh" if code.startswith("60") else "sz") + code
        try:
            r = _retry_get(f"http://qt.gtimg.cn/q={tc}", timeout=6)
            if r and '="' in r.text:
                fields = r.text.split('="')[1].split("~")
                return {i: v for i, v in enumerate(fields)}
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ 腾讯 gtimg 读取 {code} 异常: {e}")
        return None

    # ───────────────────────── 2. 主力净流入（资金面） ─────────────────────────
    def fetch_capital_flow(self, symbol: str) -> str:
        val = self.fetch_capital_flow_value(symbol)
        if val is None:
            return "暂无"
        return self._fmt_money(val)

    def _sina_capital_flow_value(self, code: str) -> Optional[float]:
        """新浪异构资金流节点：MoneyFlow.ssi_ssfx_flzjtj 返回 netamount(净流入元)。
        与东财 push2 完全异构，东财限流/改版时的关键兜底。实测可用。"""
        try:
            prefix = self._market_prefix(code)
            url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                   f"MoneyFlow.ssi_ssfx_flzjtj?daima={prefix}{code}")
            r = _retry_get(url, timeout=6, tries=2)
            if r:
                import json as _json
                r.encoding = "gbk"
                d = _json.loads(r.text)
                # netamount=净流入总额；r0=超大单净额，作为次选
                for k in ("netamount", "r0"):
                    v = d.get(k)
                    if v not in (None, "", "-"):
                        val = float(v)
                        if abs(val) > 1:  # 过滤 0/脏值
                            return val
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ 新浪资金流读取 {code} 异常: {str(e)[:50]}")
        return None

    def fetch_capital_flow_value(self, symbol: str) -> Optional[float]:
        """返回当日主力净流入（元）；三级故障转移链，直到成功为止。

        节点顺序（异构优先，避免同源同挂）：
          1) 东财 push2 实时 f62  2) 东财 daykline f52  3) 新浪 MoneyFlow(异构)
        """
        code = symbol.replace("sh", "").replace("sz", "").replace("bj", "").strip()
        secid = _secid(code)

        def node_f62():
            q = self._push2_quote(code, fields="f62")
            v = q.get("f62") if q else None
            return float(v) if isinstance(v, (int, float)) else None

        def node_daykline():
            url = "https://push2.eastmoney.com/api/qt/stock/fflow/daykline/get"
            params = {
                "lmt": "1", "klt": "101", "secid": secid,
                "fields1": "f1,f2,f3,f7",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            }
            r = _retry_get(url, params=params, timeout=6, tries=2)
            if r:
                _data = r.json().get("data") or {}
                klines = (_data.get("klines") if isinstance(_data, dict) else None) or []
                if klines:
                    parts = klines[0].split(",")
                    if len(parts) > 1 and parts[1] not in ("", "-", None):
                        return float(parts[1])
            return None

        def node_sina():
            return self._sina_capital_flow_value(code)

        val, _hit = _fallback_chain(
            [("东财push2-f62", node_f62),
             ("东财daykline", node_daykline),
             ("新浪资金流", node_sina)],
            validate=lambda v: isinstance(v, (int, float)) and abs(v) > 1,
            label=f"资金流 {code}", default=None,
        )
        return val

    # ───────────────────────── 2.5 历史K线（布林线/技术指标用，独立节点） ─────────────────────────
    def fetch_kline(self, symbol: str, days: int = 30) -> List[Dict[str, float]]:
        """取近 N 日日K（前复权），供布林线/MA/量能计算。
        这是【独立的行情补充节点】，与三个受保护的价格节点无关，不修改它们。
        主源：东财 push2his；兜底：新浪 CN_MarketDataService。全失败返回 []。"""
        clean = symbol.replace("sh", "").replace("sz", "").replace("bj", "").strip()
        prefix = self._market_prefix(clean)
        secid = f"1.{clean}" if prefix == "sh" else (f"0.{clean}" if prefix == "sz" else f"0.{clean}")
        bars: List[Dict[str, float]] = []

        # 主源：东财 push2his（fields2: 日期,开,收,高,低,成交量,成交额,振幅...）
        try:
            u = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
            p = {
                "secid": secid,
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
                "klt": "101", "fqt": "1", "end": "20500101", "lmt": str(max(days, 25)),
            }
            r = _retry_get(u, params=p, timeout=7, tries=2)
            if r:
                klines = ((r.json().get("data") or {}).get("klines") or [])
                for line in klines:
                    f = line.split(",")
                    if len(f) < 6:
                        continue
                    try:
                        bars.append({
                            "date": f[0], "open": float(f[1]), "close": float(f[2]),
                            "high": float(f[3]), "low": float(f[4]), "volume": float(f[5]),
                        })
                    except (ValueError, IndexError):
                        continue
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ 东财K线 {clean} 失败: {str(e)[:50]}")

        # 兜底：新浪日K
        if not bars:
            try:
                u2 = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
                r2 = _retry_get(u2, params={"symbol": f"{prefix}{clean}", "scale": 240,
                                            "ma": "no", "datalen": max(days, 25)}, timeout=7, tries=2)
                if r2:
                    for it in (r2.json() or []):
                        try:
                            bars.append({
                                "date": str(it.get("day", ""))[:10],
                                "open": float(it["open"]), "close": float(it["close"]),
                                "high": float(it["high"]), "low": float(it["low"]),
                                "volume": float(it.get("volume", 0) or 0),
                            })
                        except (ValueError, KeyError, TypeError):
                            continue
            except Exception as e:  # noqa: BLE001
                print(f"⚠️ 新浪K线兜底 {clean} 失败: {str(e)[:50]}")

        bars.sort(key=lambda x: x["date"])
        return bars[-days:]

    # ───────────────────────── 3. 流通市值（小盘过滤） ─────────────────────────
    def fetch_float_market_cap(self, symbol: str) -> Optional[float]:
        """返回流通市值（元）；稳健模式用来剔除小盘妖股。"""
        q = self._push2_quote(symbol.replace("sh", "").replace("sz", "").replace("bj", "").strip(),
                              fields="f116")
        if q and isinstance(q.get("f116"), (int, float)):
            return float(q["f116"])
        return None

    # ───────────────────────── 4. 股息率（红利防守，多级故障转移） ─────────────────────────
    def _sina_trade_price(self, code: str) -> Optional[float]:
        """复用新浪资金流接口返回的 trade（现价），用于股息率估算；失败返回 None。"""
        try:
            prefix = self._market_prefix(code)
            r = _retry_get(
                "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                f"MoneyFlow.ssi_ssfx_flzjtj?daima={prefix}{code}", timeout=6, tries=1)
            if r:
                import json as _json
                r.encoding = "gbk"
                d = _json.loads(r.text)
                p = float(d.get("trade", 0) or 0)
                return p if p > 0 else None
        except Exception:
            pass
        return None

    def _datacenter_dividend(self, code: str) -> Optional[float]:
        """东财 datacenter 分红送配报表：优先 DIVIDENT_RATIO，其次用每10股派息/现价估算。"""
        try:
            r = _retry_get(
                "https://datacenter-web.eastmoney.com/api/data/v1/get",
                params={"reportName": "RPT_SHAREBONUS_DET", "columns": "ALL",
                        "filter": f'(SECURITY_CODE="{code}")', "source": "WEB", "client": "WEB",
                        "pageSize": "8", "sortColumns": "REPORTDATE", "sortTypes": "-1"},
                timeout=7, tries=1)
            if r:
                data = (r.json().get("result") or {}).get("data") or []
                for it in data[:5]:
                    dr = it.get("DIVIDENT_RATIO")
                    if isinstance(dr, (int, float)) and 0 < dr < 50:
                        return float(dr)
                # 兜底：最近一期每10股税前派息 / 现价
                for it in data[:3]:
                    pretax = it.get("PRETAX_BONUS_RMB")
                    if isinstance(pretax, (int, float)) and pretax > 0:
                        price = self._sina_trade_price(code)
                        if price:
                            return pretax / 10.0 / price * 100
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ datacenter 股息率 {code} 异常: {str(e)[:50]}")
        return None

    def fetch_dividend_yield(self, symbol: str) -> str:
        """股息率：故障转移链 —— 东财 push2 f168 → datacenter 分红报表（异构）。"""
        code = symbol.replace("sh", "").replace("sz", "").replace("bj", "").strip()

        def node_push2():
            q = self._push2_quote(code, fields="f168")
            v = q.get("f168") if q else None
            return float(v) if isinstance(v, (int, float)) and 0 < v < 50 else None

        val, _hit = _fallback_chain(
            [("东财push2-f168", node_push2),
             ("东财datacenter分红", lambda: self._datacenter_dividend(code))],
            validate=lambda v: isinstance(v, (int, float)) and 0 < v < 50,
            label=f"股息率 {code}", default=None,
        )
        return f"{val:.2f}%" if val is not None else "暂无"

    # ───────────────────────── 5. 舆情新闻（新浪主源 + 东财兜底） ─────────────────────────
    @staticmethod
    def _market_prefix(clean_code: str) -> str:
        if clean_code.startswith(("60", "68", "9")):
            return "sh"
        if clean_code.startswith(("00", "30", "2")):
            return "sz"
        if clean_code.startswith(("8", "4")):
            return "bj"
        return "sh"

    def _sina_stock_news(self, clean_code: str, name: str, limit: int) -> List[Dict[str, str]]:
        """新浪财经个股资讯页（gb18030，.datelist 下 <a> 即真实新闻标题）。
        该页同时含【个股新闻】与【行业/板块消息】，实测大陆环境稳定可达。"""
        prefix = self._market_prefix(clean_code)
        news_list: List[Dict[str, str]] = []
        url = (f"https://vip.stock.finance.sina.com.cn/corp/go.php/"
               f"vCB_AllNewsStock/symbol/{prefix}{clean_code}.phtml")
        try:
            r = _retry_get(url, timeout=8, tries=2)
            if r is None:
                return []
            r.encoding = "gb18030"
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(r.text, "html.parser")
                dl = soup.select_one(".datelist")
                anchors = dl.select("a") if dl else []
            except ImportError:
                # 无 bs4 时用正则从 datelist 片段里抠 <a>
                seg = r.text[r.text.find('datelist'):]
                anchors = re.findall(r"<a[^>]*href=['\"]([^'\"]+)['\"][^>]*>(.*?)</a>", seg[:20000], re.S)
                news_list = []
                for href, title in anchors:
                    t = re.sub(r"<[^>]+>", "", title).strip()
                    if len(t) >= 8:
                        news_list.append({"media": "新浪财经", "title": t, "url": href})
                return news_list[:limit]
            for a in anchors:
                title = a.get_text(" ", strip=True)
                if len(title) < 8:
                    continue
                news_list.append({
                    "media": "新浪财经",
                    "title": title,
                    "url": a.get("href", ""),
                })
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ 新浪个股资讯 {clean_code} 失败: {e}")
        return news_list[:limit]

    def _eastmoney_stock_news(self, clean_code: str, name: str, limit: int) -> List[Dict[str, str]]:
        """东财兜底源（原 cmsArticleWebOld 接口已失效，改用资讯搜索 jsonp）。"""
        news_list: List[Dict[str, str]] = []
        import json as _json
        from urllib.parse import quote
        param = {
            "uid": "", "keyword": name, "type": ["cmsArticleWebOld"],
            "client": "web", "clientType": "web", "clientVersion": "curr",
            "param": {"cmsArticleWebOld": {
                "searchScope": "default", "sort": "time", "pageIndex": 1, "pageSize": limit,
            }},
        }
        url = "https://search-api-web.eastmoney.com/search/jsonp?cb=cb&param=" + quote(_json.dumps(param))
        try:
            r = _retry_get(url, timeout=6, tries=2)
            if r and "cb(" in r.text:
                data = _json.loads(r.text[r.text.find("cb(") + 3: r.text.rfind(")")])
                for item in (data.get("result", {}) or {}).get("cmsArticleWebOld", []):
                    title = item.get("title", "").replace("<em>", "").replace("</em>", "")
                    if title:
                        news_list.append({
                            "media": item.get("mediaName", "财经快讯"),
                            "title": title,
                            "url": item.get("url", ""),
                        })
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ 东财资讯兜底 {clean_code} 失败: {e}")
        return news_list[:limit]

    def _xueqiu_stock_news(self, clean_code: str, name: str, limit: int) -> List[Dict[str, str]]:
        """雪球个股资讯：先访问首页取 cookie（雪球反爬需会话），再调资讯搜索 API。
        雪球在部分网络环境被 WAF 拦截，失败返回空由聚合层跳过，不影响其他源。"""
        news_list: List[Dict[str, str]] = []
        prefix = self._market_prefix(clean_code).upper()  # SH / SZ / BJ
        symbol = f"{prefix}{clean_code}"
        import json as _json
        try:
            sess = requests.Session()
            sess.headers.update(_HEADERS)
            sess.get("https://xueqiu.com/", timeout=6)  # 取 cookie（xq_a_token 等）
            url = "https://xueqiu.com/statuses/search.json"
            params = {
                "count": max(limit * 2, 10), "comment": 0, "symbol": symbol,
                "hl": 0, "source": "all", "sort": "time", "page": 1, "q": name,
            }
            r = sess.get(url, params=params, timeout=7)
            if r.status_code == 200 and r.text.strip().startswith("{"):
                data = r.json()
                for item in (data.get("list") or []):
                    title = (item.get("title") or item.get("description") or "").strip()
                    if not title or len(title) < 8:
                        continue
                    # 去 HTML 标签
                    title = re.sub(r"<[^>]+>", "", title)
                    created = item.get("created_at", 0)
                    tm = ""
                    if isinstance(created, (int, float)) and created > 0:
                        try:
                            tm = datetime.datetime.fromtimestamp(created / 1000).strftime("%Y-%m-%d %H:%M")
                        except (ValueError, OSError):
                            tm = ""
                    news_list.append({
                        "time": tm, "media": "雪球",
                        "title": title[:200],
                        "url": f"https://xueqiu.com/S/{symbol}",
                    })
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ 雪球资讯 {clean_code} 失败（已跳过）: {str(e)[:60]}")
        return news_list[:limit]

    def _ths_stock_news(self, clean_code: str, name: str, limit: int) -> List[Dict[str, str]]:
        """同花顺个股资讯：F10 资讯页 JS 动态加载，改为抓其资讯列表 API。
        失败返回空由聚合层跳过。"""
        news_list: List[Dict[str, str]] = []
        import json as _json
        try:
            # 同花顺 F10 资讯列表（部分环境可达）
            u = f"https://basic.10jqka.com.cn/api/stock/news/{clean_code}/?page=1&size={max(limit * 2, 10)}"
            r = _retry_get(u, timeout=6, tries=1)
            if r and r.text.strip().startswith(("{", "[")):
                data = _json.loads(r.text)
                items = data if isinstance(data, list) else (
                    data.get("data", {}).get("list", []) if isinstance(data.get("data"), dict)
                    else data.get("data", []) if isinstance(data.get("data"), list) else [])
                for item in items[:limit * 2]:
                    title = (item.get("title") or "").strip()
                    if title and len(title) >= 8:
                        news_list.append({
                            "time": item.get("time", item.get("date", "")),
                            "media": "同花顺", "title": title[:200],
                            "url": item.get("url", ""),
                        })
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ 同花顺资讯 {clean_code} 失败（已跳过）: {str(e)[:60]}")
        return news_list[:limit]

    def _eastmoney_report_news(self, clean_code: str, name: str, limit: int) -> List[Dict[str, str]]:
        """东财研报（异构权威源）：reportapi.eastmoney.com，返回券商研报标题+机构+日期。
        实测可达，作为新浪个股资讯之外的关键兜底；失败返回空由链跳过。"""
        news_list: List[Dict[str, str]] = []
        import json as _json
        try:
            now = datetime.datetime.now()
            url = "https://reportapi.eastmoney.com/report/list"
            params = {
                "cb": "cb", "pageSize": max(limit * 2, 10),
                "beginTime": f"{now.year - 1}-01-01", "endTime": f"{now.year}-12-31",
                "pageNo": "1", "qType": "0", "code": clean_code, "p": "1", "pageNum": "1",
            }
            r = _retry_get(url, params=params, timeout=7, tries=2)
            if r and "cb(" in r.text:
                txt = r.text
                data = _json.loads(txt[txt.find("cb(") + 3: txt.rfind(")")])
                for it in (data.get("data") or [])[:max(limit * 2, 10)]:
                    title = (it.get("title") or "").strip()
                    if len(title) < 8:
                        continue
                    org = it.get("orgSName") or it.get("orgName") or "券商研报"
                    pub = str(it.get("publishDate") or "")[:10]
                    news_list.append({
                        "time": pub, "media": f"研报·{org}", "title": title,
                        "url": it.get("infoCode", ""),
                    })
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ 东财研报 {clean_code} 失败（已跳过）: {str(e)[:60]}")
        return news_list[:limit]

    def _eastmoney_ann_news(self, clean_code: str, name: str, limit: int) -> List[Dict[str, str]]:
        """东财公告（异构权威源）：np-anotice-stock.eastmoney.com，返回公司公告标题+日期。
        公告往往是最硬的个股事件信息（定增/中标/减持/业绩），实测可达。"""
        news_list: List[Dict[str, str]] = []
        try:
            url = "https://np-anotice-stock.eastmoney.com/api/security/ann"
            params = {
                "sr": "-1", "page_size": str(max(limit * 2, 10)), "page_index": "1",
                "ann_type": "A", "client_source": "web", "stock_list": clean_code,
            }
            r = _retry_get(url, params=params, timeout=7, tries=2)
            if r:
                lst = ((r.json().get("data") or {}).get("list") or [])
                for it in lst[:max(limit * 2, 10)]:
                    title = (it.get("title") or "").strip()
                    if len(title) < 8:
                        continue
                    # 去掉 "皖通高速:" 前缀，保留公告主体
                    title = re.sub(r"^[^:：]{2,12}[:：]", "", title) or title
                    news_list.append({
                        "time": str(it.get("notice_date") or "")[:16],
                        "media": "公司公告", "title": title,
                        "url": f"https://data.eastmoney.com/notices/detail/{clean_code}/{it.get('art_code','')}.html",
                    })
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ 东财公告 {clean_code} 失败（已跳过）: {str(e)[:60]}")
        return news_list[:limit]

    def _sina_flash_news(self, clean_code: str, name: str, limit: int) -> List[Dict[str, str]]:
        """新浪 7×24 全局快讯（异构兜底）：按个股名/代码过滤快讯流。
        个股命中率有限，但作为最后补充节点，在主源/研报/公告都失败时仍可能捞到盘中异动。"""
        news_list: List[Dict[str, str]] = []
        try:
            url = ("https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size=100"
                   "&zhibo_id=152&tag_id=0&dire=f&dpc=1")
            r = _retry_get(url, timeout=7, tries=1)
            if r:
                feed = ((r.json().get("result") or {}).get("data") or {}).get("feed") or {}
                for it in (feed.get("list") or []):
                    text = re.sub(r"<[^>]+>", "", str(it.get("rich_text") or "")).strip()
                    if len(text) >= 8 and (name in text or clean_code in text):
                        news_list.append({
                            "time": str(it.get("create_time") or "")[:16],
                            "media": "新浪快讯", "title": text[:160],
                            "url": "",
                        })
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ 新浪快讯 {clean_code} 失败（已跳过）: {str(e)[:60]}")
        return news_list[:limit]

    def fetch_today_stock_news(self, symbol: str, name: str, limit: int = 5) -> List[Dict[str, str]]:
        """个股资讯故障转移链：主源失败自动切备用，逐个尝试【直至成功】。

        与价格节点容灾策略一致——不再"所有源都并发请求再合并"，而是严格按优先级
        顺序：某节点一旦拿到足够有效新闻即停止（省请求）；失败/无数据则切下一节点；
        只有全部节点都失败才返回占位提示。节点刻意选用异构后端，避免同源同挂。

        节点顺序：
          1) 新浪个股资讯页（主源，实测稳定）
          2) 东财研报（异构权威，券商观点）
          3) 东财公告（异构权威，硬事件）
          4) 东财资讯搜索（异构补充）
          5) 雪球（可能 WAF）6) 同花顺（可能 JS 动态）7) 新浪7×24快讯（最后补充）

        返回【原始候选】，供后续 _distill_news 甄别筛选。
        """
        clean_code = symbol.replace("sh", "").replace("sz", "").replace("bj", "").strip()
        nodes = [
            ("新浪个股资讯", lambda: self._sina_stock_news(clean_code, name, limit)),
            ("东财研报", lambda: self._eastmoney_report_news(clean_code, name, limit)),
            ("东财公告", lambda: self._eastmoney_ann_news(clean_code, name, limit)),
            ("东财资讯", lambda: self._eastmoney_stock_news(clean_code, name, limit)),
            ("雪球", lambda: self._xueqiu_stock_news(clean_code, name, limit)),
            ("同花顺", lambda: self._ths_stock_news(clean_code, name, limit)),
            ("新浪快讯", lambda: self._sina_flash_news(clean_code, name, limit)),
        ]
        # 目标条数：拿到 >= 该值即视为"成功"，停止后续节点
        target = max(limit, 3)
        news_list: List[Dict[str, str]] = []
        hit_nodes: List[str] = []
        for media, fetch in nodes:
            try:
                got = fetch() or []
                for n in got:
                    n.setdefault("media", media)
                if got:
                    news_list.extend(got)
                    hit_nodes.append(media)
                    print(f"✅ [新闻 {name}] 节点『{media}』+{len(got)} 条"
                          f"（累计 {len(news_list)}）")
            except Exception as e:  # noqa: BLE001
                print(f"⚠️ [新闻 {name}] 节点『{media}』异常，切换下一节点: {str(e)[:50]}")
            if len(news_list) >= target:
                print(f"🏁 [新闻 {name}] 已凑够 {target} 条，命中节点 {hit_nodes}，停止")
                break

        if not news_list:
            news_list.append({
                "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                "media": "全网监控",
                "title": f"未检索到 {name}({clean_code}) 今日公开资讯，消息面维持平稳。",
                "url": "",
            })
        return news_list[:max(limit * 3, 12)]

    def fetch_industry_news(self, symbol: str, name: str, limit: int = 3,
                            news: List[Dict[str, str]] = None) -> List[Dict[str, str]]:
        """行业/板块消息面：从个股资讯页里挑出含行业/板块关键词的资讯，作为行业消息代理。
        没有独立行情源时能真实反映所处赛道动态（如『险资入市』『科技与红利』『板块轮动』）。
        传入 news 可复用已抓取的资讯列表，避免重复请求。"""
        news = news or self._sina_stock_news(
            symbol.replace("sh", "").replace("sz", "").replace("bj", "").strip(), name, limit=12)
        kw = ("行业", "板块", "赛道", "概念", "指数", "ETF", "资金", "轮动", "红利", "科技",
              "AI", "芯片", "医药", "白酒", "新能源", "券商", "银行", "地产", "有色", "军工",
              "半导体", "光伏", "储能", "消费", "险资", "机构", "长钱")
        picked = []
        for n in news:
            t = n.get("title", "")
            if any(k in t for k in kw) and t not in [p["title"] for p in picked]:
                picked.append(n)
            if len(picked) >= limit:
                break
        if not picked and news:
            # 没命中行业关键词时，退而取与个股相关度较低的前若干条宏观/市场资讯
            picked = news[:limit]
        return picked

    # ───────────────────────── 5.5 新闻甄别总结层 ─────────────────────────
    # 设计目标：多源抓来的原始资讯 → 清洗/去重/相关性过滤/情绪甄别 → 结构化摘要，
    #   再喂给多空 agent 交锋，避免"原始标题堆砌"和"营销软文/无关噪音"污染判断。
    # 全部为本地规则计算，不依赖任何 LLM/API Key，沙箱与用户机器行为一致。

    # 广告/营销/无信息量噪音（命中即剔除）
    _NEWS_SPAM_KW = (
        "广告", "推广", "扫码", "加群", "荐股", "客服", "免费领", "点击领", "内部消息",
        "带你", "加微信", "联系电话", "网址", "http", "www.", "二维码", "开户链接",
        "直播预告", "课程", "培训", "大涨必备", "牛股", "明日爆发", "节前必看",
    )
    # 生活/娱乐等与财经完全无关的噪音（即便来自定向源也剔除）
    _NEWS_IRRELEVANT_KW = (
        "天气", "出游", "旅游攻略", "美食", "菜谱", "明星", "娱乐圈", "八卦", "综艺",
        "电视剧", "电影票", "演唱会", "球赛", "球迷", "招聘", "租房", "二手房买卖",
    )
    # 个股强相关词（命中则提高相关性权重）
    _REL_HIGH_KW = ("业绩", "营收", "净利", "利润", "公告", "回购", "增持", "减持", "中标",
                    "订单", "股价", "涨停", "跌停", "目标价", "评级", "融资", "分红", "股东")
    # 行业/赛道关键词（用于识别行业消息）
    _INDUSTRY_KW = (
        "行业", "板块", "赛道", "概念", "指数", "ETF", "资金", "轮动", "红利", "科技",
        "AI", "芯片", "半导体", "医药", "白酒", "新能源", "券商", "银行", "地产", "有色",
        "军工", "光伏", "储能", "消费", "险资", "机构", "长钱", "政策", "监管", "工信部",
        "发改委", "央行", "降息", "降准", "国产替代", "算力", "机器人", "汽车", "锂电",
    )
    # 严格行业词：仅这些命中才归入"行业赛道要点"（排除"资金/机构/轮动"等个股面泛词）
    _STRICT_INDUSTRY_KW = (
        "行业", "板块", "赛道", "概念", "ETF", "红利", "科技", "AI", "芯片", "半导体",
        "医药", "白酒", "新能源", "券商", "银行", "地产", "有色", "军工", "光伏", "储能",
        "消费", "险资", "政策", "监管", "工信部", "发改委", "央行", "降息", "降准",
        "国产替代", "算力", "机器人", "汽车", "锂电", "景气度", "产业",
    )
    # 来源可信度权重（甄别排序用）
    _SOURCE_WEIGHT = {
        "新浪财经": 1.0, "雪球": 0.9, "同花顺": 1.0, "东方财富": 1.0,
        "财经快讯": 0.8, "全网监控": 0.3,
    }

    @classmethod
    def _news_tone(cls, title: str) -> str:
        """甄别式情绪判定：强/弱词加权，返回 偏多/偏空/中性。"""
        t = str(title)
        bear = ("跌停", "大跌", "重挫", "暴跌", "下挫", "流出", "抛售", "减持", "亏损", "下滑",
                "下降", "风险", "监管", "处罚", "问询", "退市", "爆雷", "承压", "新低", "弱势",
                "套现", "立案", "调查", "预亏", "商誉减值", "破位", "腰斩")
        bull = ("涨停", "大涨", "新高", "突破", "增长", "超预期", "中标", "获批", "签约", "增持",
                "回购", "净流入", "买入", "利好", "扶持", "景气", "回暖", "修复", "反转", "预增",
                "分红", "提振", "催化", "重磅")
        b = sum(1 for k in bear if k in t)
        g = sum(1 for k in bull if k in t)
        # "不跌/未减"等否定语境做粗校正
        if b > g:
            return "偏空"
        if g > b:
            return "偏多"
        return "中性"

    @classmethod
    def _distill_news(cls, raw_news: List[Dict[str, str]], name: str, code: str,
                      limit: int = 5) -> Dict[str, object]:
        """新闻甄别总结管道：
        1) 清洗：去 HTML、去噪音标题、长度过滤
        2) 去重：跨源标题相似度（前 16 字指纹 + 关键词集合近似）
        3) 相关性过滤：个股名/代码/业绩词/行业词 命中才保留，否则剔除
        4) 打分：来源权重 + 相关性 + 情绪强度 + 时效（有明确时间的加分）
        5) 分类：利好 / 利空 / 中性 / 行业，各自按分排序
        6) 摘要：生成结构化 news_digest（供议会交锋 + 推送展示）
        """
        stats = {"raw": len(raw_news or []), "spam": 0, "dup": 0, "irrelevant": 0, "kept": 0,
                 "sources": []}
        cleaned: List[Dict[str, object]] = []
        seen_fp = set()
        seen_kw = []

        for n in (raw_news or []):
            title = re.sub(r"<[^>]+>", "", str(n.get("title", ""))).strip()
            if len(title) < 8 or len(title) > 160:
                stats["spam"] += 1
                continue
            # 广告/营销噪音
            if any(sp in title for sp in cls._NEWS_SPAM_KW):
                stats["spam"] += 1
                continue
            # 生活/娱乐等财经无关噪音（即便来自定向源也剔除）
            if any(sp in title for sp in cls._NEWS_IRRELEVANT_KW):
                stats["irrelevant"] += 1
                continue
            # 去重：前 16 字指纹（去标点/空白）
            fp = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", title)[:16]
            if not fp or fp in seen_fp:
                stats["dup"] += 1
                continue
            # 关键词集合近似去重（与已保留标题 7 字词重合度 > 0.6 视为重复）
            kws = {title[i:i + 7] for i in range(0, max(1, len(title) - 6), 3)}
            dup_kw = False
            for prev in seen_kw:
                if not kws or not prev:
                    continue
                inter = len(kws & prev)
                if inter / max(1, min(len(kws), len(prev))) > 0.6:
                    dup_kw = True
                    break
            if dup_kw:
                stats["dup"] += 1
                continue
            # 相关性过滤：个股名/代码/业绩词/行业词 至少命中一个
            rel = 0
            if name and name in title:
                rel += 3
            if code and code in title:
                rel += 3
            if any(k in title for k in cls._REL_HIGH_KW):
                rel += 2
            is_industry = any(k in title for k in cls._STRICT_INDUSTRY_KW)
            if is_industry:
                rel += 1
            # 兜底：若该源就是"个股资讯页"(新浪/雪球/同花顺个股页)，本身即定向抓取，给予基础相关分
            media = str(n.get("media", ""))
            if media in ("新浪财经", "雪球", "同花顺", "东方财富") and rel == 0:
                rel += 1
            if rel == 0:
                stats["irrelevant"] += 1
                continue

            tone = cls._news_tone(title)
            # 打分：来源权重(×3) + 相关性(×2) + 情绪强度 + 时效
            score = cls._SOURCE_WEIGHT.get(media, 0.7) * 3 + rel * 2
            if tone != "中性":
                score += 1.5
            if n.get("time"):
                score += 0.5
            seen_fp.add(fp)
            seen_kw.append(kws)
            cleaned.append({
                "title": title, "media": media, "time": n.get("time", ""),
                "url": n.get("url", ""), "tone": tone, "rel": rel,
                "is_industry": is_industry, "score": round(score, 2),
            })

        cleaned.sort(key=lambda x: x["score"], reverse=True)
        stats["kept"] = len(cleaned)
        stats["sources"] = sorted({c["media"] for c in cleaned})

        bull_points = [c["title"] for c in cleaned if c["tone"] == "偏多"][:limit]
        bear_points = [c["title"] for c in cleaned if c["tone"] == "偏空"][:limit]
        neutral_points = [c["title"] for c in cleaned if c["tone"] == "中性"][:2]
        industry_points = [c["title"] for c in cleaned if c["is_industry"]][:limit]

        # 结构化摘要：优先呈现甄别后最有信息量的要点
        top = [c["title"] for c in cleaned[:3]]
        if top:
            summary = f"经甄别保留 {len(cleaned)} 条有效资讯（原始 {stats['raw']} 条）：" + "；".join(top)
        else:
            summary = f"{name} 今日无可靠个股/行业资讯（原始 {stats['raw']} 条经甄别均过滤），消息面维持平稳。"

        digest = {
            "bull_points": bull_points,
            "bear_points": bear_points,
            "neutral_points": neutral_points,
            "industry_points": industry_points,
            "summary": summary,
            "stats": stats,
        }
        # 保留甄别后的干净原始列表（含 tone），供推送"多维分析-消息面"展示
        kept_news = [{
            "title": c["title"], "media": c["media"], "time": c["time"],
            "url": c["url"], "tone": c["tone"],
        } for c in cleaned[:max(limit * 2, 6)]]
        return digest, kept_news

    # ───────────────────────── 6. 一次性快照 ─────────────────────────
    def fetch_full_snapshot(self, symbol: str, name: str, news_limit: int = 3) -> Dict[str, object]:
        """一次性快照：每个子源【独立容错】，任一源失败只降级该维度，不再让整段变空。

        关键修复（财务/新闻全"暂无"根因）：
          - 旧版任一子请求抛异常 → 上层 enrich_stock 的 except 吞掉 → 返回全空快照；
          - 且空快照被永久缓存，一次失败污染全天所有推送。
          - 现在：逐维度 try/except + 脏值兜底 + 仅"有效快照"才入缓存。
        """
        key = f"{symbol}"
        if key in self._cache:
            return self._cache[key]

        clean_code = symbol.replace("sh", "").replace("sz", "").replace("bj", "").strip()

        def _safe(label: str, fn, default):
            try:
                return fn()
            except Exception as e:  # noqa: BLE001
                print(f"⚠️ 快照子源[{label}] {name}({clean_code}) 失败，降级: {str(e)[:60]}")
                return default

        financials = _safe("财务", lambda: self.fetch_stock_financials(symbol), {})
        if not isinstance(financials, dict) or not financials:
            financials = {}
        for k in ("市盈率(TTM)", "市净率(PB)", "ROE(净资产收益率)",
                  "营收同比", "净利润同比", "股息率(TTM)"):
            financials.setdefault(k, "暂无")
        dividend = _safe("股息率", lambda: self.fetch_dividend_yield(symbol), "暂无")
        capital = _safe("资金流", lambda: self.fetch_capital_flow(symbol), "暂无")
        raw_news = _safe("新闻", lambda: self.fetch_today_stock_news(
            symbol, name, limit=max(news_limit, 5)), [])
        raw_news = raw_news or []
        news_digest, news = _safe("甄别", lambda: self._distill_news(
            raw_news, name, clean_code, limit=max(news_limit, 3)), ({}, []))
        if not isinstance(news_digest, dict):
            news_digest, news = {}, []
        industry_news = _safe("行业消息", lambda: self.fetch_industry_news(
            symbol, name, limit=3, news=news), [])
        industry_news = industry_news or []
        for n in industry_news:
            try:
                n["tone"] = self._news_tone(n.get("title", ""))
            except Exception:
                n["tone"] = "中性"
        # 历史K线（布林线/技术指标用，独立节点；可用开关关闭省请求）
        kline: List[Dict[str, float]] = []
        if os.environ.get("ENABLE_KLINE_TECHNICALS", "true").lower() in ("true", "1", "yes"):
            kline = _safe("K线", lambda: self.fetch_kline(symbol, days=30), [])

        snap = {
            "code": clean_code,
            "name": name,
            "financials": financials,
            "dividend_yield": dividend if dividend else "暂无",
            "capital_flow": capital if capital else "暂无",
            "news": news or [],
            "news_digest": news_digest,        # 甄别后结构化摘要（供议会多空交锋）
            "industry_news": industry_news,   # 行业/板块消息面（供议会多空解读）
            "kline": kline or [],             # 历史日K（布林线/技术指标用）
        }
        # 仅当至少一个维度有真实数据才缓存，避免一次网络抖动把空快照钉死一整天
        has_real = (
            any(v not in ("暂无", "", None) for v in financials.values())
            or bool(news) or bool(kline)
        )
        if has_real:
            self._cache[key] = snap
        return snap

    @staticmethod
    def _fmt_money(v: float) -> str:
        if abs(v) >= 1e8:
            return f"{v / 1e8:.2f}亿"
        if abs(v) >= 1e4:
            return f"{v / 1e4:.1f}万"
        return f"{v:.0f}元"


# ───────────────────────── 自检：python cn_stock_data.py 600000 ─────────────────────────
if __name__ == "__main__":
    import sys
    test_code = sys.argv[1] if len(sys.argv) > 1 else "600000"
    f = CNStockDataFetcher()
    print(f"=== 自检标的 {test_code} ===")
    fin = f.fetch_stock_financials(test_code)
    print("财务/估值:", fin)
    print("主力净流入:", f.fetch_capital_flow(test_code))
    print("流通市值:", f._fmt_money(f.fetch_float_market_cap(test_code) or 0))
    print("股息率:", f.fetch_dividend_yield(test_code))
    print("舆情新闻条数:", len(f.fetch_today_stock_news(test_code, "测试", 3)))
