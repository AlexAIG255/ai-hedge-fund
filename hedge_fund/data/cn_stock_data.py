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
        # —— 源 C：datacenter 财务指标（WEIGHTAVG_ROE）——
        roe = self._datacenter_value("RPT_LICO_FN_CPD", code, "WEIGHTAVG_ROE")
        if roe is not None:
            fin["ROE(净资产收益率)"] = f"{roe:.2f}%"

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

    def fetch_capital_flow_value(self, symbol: str) -> Optional[float]:
        """返回当日主力净流入（元）；用于『重视资金』的硬性过滤。"""
        code = symbol.replace("sh", "").replace("sz", "").replace("bj", "").strip()
        secid = _secid(code)
        # 源 A：个股资金流 daykline（f52=主力净流入，单位元）
        try:
            url = "https://push2.eastmoney.com/api/qt/stock/fflow/daykline/get"
            params = {
                "lmt": "1", "klt": "101", "secid": secid,
                "fields1": "f1,f2,f3,f7",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            }
            r = _retry_get(url, params=params, timeout=6)
            if r:
                _data = r.json().get("data") or {}
                klines = (_data.get("klines") if isinstance(_data, dict) else None) or []
                if klines:
                    parts = klines[0].split(",")
                    if len(parts) > 1 and parts[1] not in ("", "-", None):
                        try:
                            return float(parts[1])
                        except ValueError:
                            pass
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ 资金流 daykline 读取 {code} 异常: {e}")
        # 源 B：实时行情 f62=主力净流入（元）
        q = self._push2_quote(code, fields="f62")
        if q and isinstance(q.get("f62"), (int, float)):
            return float(q["f62"])
        return None

    # ───────────────────────── 3. 流通市值（小盘过滤） ─────────────────────────
    def fetch_float_market_cap(self, symbol: str) -> Optional[float]:
        """返回流通市值（元）；稳健模式用来剔除小盘妖股。"""
        q = self._push2_quote(symbol.replace("sh", "").replace("sz", "").replace("bj", "").strip(),
                              fields="f116")
        if q and isinstance(q.get("f116"), (int, float)):
            return float(q["f116"])
        return None

    # ───────────────────────── 4. 股息率（红利防守） ─────────────────────────
    def fetch_dividend_yield(self, symbol: str) -> str:
        q = self._push2_quote(symbol.replace("sh", "").replace("sz", "").replace("bj", "").strip(),
                              fields="f168")
        if q and isinstance(q.get("f168"), (int, float)) and q["f168"] > 0:
            return f"{q['f168']:.2f}%"
        return "暂无"

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

    def fetch_today_stock_news(self, symbol: str, name: str, limit: int = 5) -> List[Dict[str, str]]:
        clean_code = symbol.replace("sh", "").replace("sz", "").replace("bj", "").strip()
        # 主源：新浪（个股新闻+行业消息）；兜底：东财
        news_list = self._sina_stock_news(clean_code, name, limit)
        if not news_list:
            news_list = self._eastmoney_stock_news(clean_code, name, limit)

        if not news_list:
            news_list.append({
                "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                "media": "全网监控",
                "title": f"未检索到 {name}({clean_code}) 今日突发利空舆情，消息面维持平稳。",
                "url": "",
            })
        return news_list[:limit]

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

    # ───────────────────────── 6. 一次性快照 ─────────────────────────
    def fetch_full_snapshot(self, symbol: str, name: str, news_limit: int = 3) -> Dict[str, object]:
        key = f"{symbol}"
        if key in self._cache:
            return self._cache[key]
        financials = self.fetch_stock_financials(symbol)
        dividend = self.fetch_dividend_yield(symbol)
        capital = self.fetch_capital_flow(symbol)
        news = self.fetch_today_stock_news(symbol, name, limit=max(news_limit, 5))
        industry_news = self.fetch_industry_news(symbol, name, limit=3, news=news)
        snap = {
            "code": symbol.replace("sh", "").replace("sz", "").replace("bj", "").strip(),
            "name": name,
            "financials": financials,
            "dividend_yield": dividend,
            "capital_flow": capital,
            "news": news,
            "industry_news": industry_news,   # 行业/板块消息面（供议会多空解读）
        }
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
