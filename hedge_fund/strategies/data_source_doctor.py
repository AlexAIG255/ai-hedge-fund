#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据源自检诊断 (data_source_doctor)

用途：当推送里财务/新闻/资金面显示「暂无」时，在【运行机器】上执行本脚本，
      逐节点探测可用性，定位到底是哪个数据源在你的网络环境不可达。

用法：
    python3 data_source_doctor.py                 # 默认诊断 600409 三友化工
    python3 data_source_doctor.py 603121 华培动力  # 指定代码与名称

说明：本脚本只做只读探测，不修改行情节点，不影响线上推送。
"""
import sys
import time
import json
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"))


def _probe(label: str, fn, judge):
    """执行一次探测：返回 (ok, detail, seconds)。judge(result) -> bool 判定是否有效数据。"""
    t0 = time.time()
    try:
        res = fn()
        ok = bool(judge(res))
        detail = str(res)[:90].replace("\n", " ") if res is not None else "None"
    except Exception as e:  # noqa: BLE001
        ok, detail = False, f"异常: {str(e)[:70]}"
    return ok, detail, time.time() - t0


def main():
    code = sys.argv[1] if len(sys.argv) > 1 else "600409"
    name = sys.argv[2] if len(sys.argv) > 2 else "三友化工"
    code = code.replace("sh", "").replace("sz", "").replace("bj", "").strip()

    try:
        from cn_stock_data import CNStockDataFetcher, _retry_get
        f = CNStockDataFetcher()
    except Exception as e:  # noqa: BLE001
        print(f"❌ 无法导入数据模块: {e}")
        return 2

    results = []

    # 1) 腾讯行情（价格节点，只读）
    def _tx():
        r = _retry_get(f"http://qt.gtimg.cn/q=sh{code}", timeout=6, tries=1)
        if not r:
            return None
        parts = r.text.split('="')[-1].split("~")
        return parts[3] if len(parts) > 3 else None
    results.append(("腾讯行情 qt.gtimg.cn (价格)",
                    *_probe("tx", _tx, lambda v: v and str(v).replace('.', '').isdigit())))

    # 2) 东财 push2 实时行情（价格节点，只读）
    def _push2():
        q = f._push2_quote(code, fields="f43,f57,f58,f162,f167")
        return q if q and q.get("f43") not in (None, "-", 0) else None
    results.append(("东财 push2 行情 (价格)",
                    *_probe("push2", _push2, lambda v: bool(v))))

    # 3) 新浪行情（价格节点，只读）
    def _sina_q():
        r = _retry_get(f"https://hq.sinajs.cn/list=sh{code}", timeout=6, tries=1)
        return r.text[20:80] if r and "=" in r.text else None
    results.append(("新浪行情 hq.sinajs.cn (价格)",
                    *_probe("sina_q", _sina_q, lambda v: bool(v))))

    # 4) 财务：东财 push2 估值字段
    def _fin_push2():
        q = f._push2_quote(code, fields="f162,f167,f116,f117")
        return q
    results.append(("财务 push2 (PE/PB/市值)",
                    *_probe("fin_push2", _fin_push2,
                            lambda v: bool(v) and any(v.get(k) not in (None, "-", 0) for k in ("f162", "f167")))))

    # 5) 财务：datacenter 主要指标（营收/净利同比）
    def _fin_dc():
        return f._datacenter_value("RPT_LICO_FN_CPD", code, "SJLTZ")
    results.append(("财务 datacenter (净利同比)",
                    *_probe("fin_dc", _fin_dc, lambda v: isinstance(v, (int, float)))))

    # 6) 资金流：东财 f62
    def _flow_em():
        q = f._push2_quote(code, fields="f62")
        return q.get("f62") if q else None
    results.append(("资金流 东财 f62",
                    *_probe("flow_em", _flow_em, lambda v: isinstance(v, (int, float)))))

    # 7) 资金流：新浪 MoneyFlow（异构兜底）
    def _flow_sina():
        r = _retry_get(
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            f"MoneyFlow.ssi_ssfx_flzjtj?daima=sh{code}", timeout=6, tries=1)
        if not r:
            return None
        r.encoding = "gbk"
        d = json.loads(r.text)
        return d.get("r0") or d.get("netamount")
    results.append(("资金流 新浪 MoneyFlow",
                    *_probe("flow_sina", _flow_sina,
                            lambda v: v is not None and str(v).replace('.', '').replace('-', '').isdigit())))

    # 8) 新闻：东财研报
    def _news_report():
        r = _retry_get(
            "https://reportapi.eastmoney.com/report/list?cb=cb&pageSize=3"
            "&beginTime=2026-01-01&endTime=2026-12-31&pageNo=1&qType=0"
            f"&code={code}&p=1&pageNum=1", timeout=7, tries=1)
        if not r:
            return None
        s, e = r.text.find("cb("), r.text.rfind(")")
        d = json.loads(r.text[s + 3:e]) if s >= 0 and e > s else {}
        data = d.get("data") or []
        return data[0].get("title") if data else None
    results.append(("新闻 东财研报",
                    *_probe("news_report", _news_report, lambda v: bool(v))))

    # 9) 新闻：东财公告
    def _news_ann():
        r = _retry_get(
            "https://np-anotice-stock.eastmoney.com/api/security/ann",
            params={"sr": -1, "page_size": 3, "page_index": 1, "ann_type": "A",
                    "client_source": "web", "stock_list": code}, timeout=7, tries=1)
        if not r:
            return None
        lst = (r.json().get("data") or {}).get("list") or []
        return lst[0].get("title") if lst else None
    results.append(("新闻 东财公告",
                    *_probe("news_ann", _news_ann, lambda v: bool(v))))

    # 10) 新闻：新浪个股资讯
    def _news_sina():
        got = f._sina_stock_news(code, name, 3)
        return got[0].get("title") if got else None
    results.append(("新闻 新浪个股资讯",
                    *_probe("news_sina", _news_sina, lambda v: bool(v))))

    # ---- 输出 ----
    print("=" * 68)
    print(f"🩺 数据源自检诊断 | 标的: {name}({code}) | {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 68)
    ok_cnt = 0
    for label, ok, detail, sec in results:
        flag = "✅" if ok else "❌"
        if ok:
            ok_cnt += 1
        print(f"{flag} {label:<30} {sec:5.1f}s | {detail}")
    print("-" * 68)
    print(f"可用节点: {ok_cnt}/{len(results)}")

    # 结论与建议
    fin_ok = all(r[1] for r in results if "财务" in r[0])
    news_ok = any(r[1] for r in results if "新闻" in r[0])
    print()
    if fin_ok and news_ok:
        print("✅ 结论：财务与新闻数据源均可用。若推送仍显示『暂无』，请确认：")
        print("   1) 运行脚本的工作目录与缓存是否一致（ENABLE_KLINE_TECHNICALS 是否超时）；")
        print("   2) 是否运行的是最新代码（git pull 后再跑）。")
    else:
        if not fin_ok:
            print("⚠️ 财务数据源部分不可用：请检查本机到东财接口的网络/代理设置。")
        if not news_ok:
            print("⚠️ 新闻数据源全部不可用：本机无法访问新浪/东财，建议检查 DNS/代理。")
    return 0 if (fin_ok and news_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
