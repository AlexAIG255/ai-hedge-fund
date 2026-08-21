"""
hedge_fund/data/cn_stock_data.py
专用于选中标的之【财务基础面】与【舆情新闻】定向补充数据源
"""

import datetime
import time
from typing import Dict, List
import requests


class CNStockDataFetcher:

    def __init__(self):
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        }

    def fetch_stock_financials(self, symbol: str) -> Dict[str, str]:
        """抓取单只股票核心财务/估值指标 (以新浪财经/东方财富 API 兜底)"""
        clean_code = symbol.replace("sh", "").replace("sz", "").strip()
        sec_id = f"1.{clean_code}" if clean_code.startswith("60") else f"0.{clean_code}"

        url = f"https://push2.eastmoney.com/api/qt/stock/get?secid={sec_id}&fields=f57,f58,f162,f167,f173,f183,f184,f185"

        try:
            res = requests.get(url, headers=self.headers, timeout=5)
            if res.status_code == 200:
                data = res.json().get("data", {})
                if data:
                    return {
                        "市盈率(TTM)": f"{data.get('f162', '-'):.2f}" if isinstance(data.get('f162'), (int, float)) else "-",
                        "市净率(PB)": f"{data.get('f167', '-'):.2f}" if isinstance(data.get('f167'), (int, float)) else "-",
                        "ROE(净资产收益率)": f"{data.get('f173', '-'):.2f}%" if isinstance(data.get('f173'), (int, float)) else "-",
                        "营收同比": f"{data.get('f183', '-'):.2f}%" if isinstance(data.get('f183'), (int, float)) else "-",
                        "净利润同比": f"{data.get('f184', '-'):.2f}%" if isinstance(data.get('f184'), (int, float)) else "-",
                    }
        except Exception as e:
            print(f"⚠️ 读取个股 {clean_code} 财务数据异常: {e}")

        return {"市盈率(TTM)": "暂无", "ROE": "暂无", "净利润同比": "暂无"}

    def fetch_today_stock_news(self, symbol: str, name: str, limit: int = 5) -> List[Dict[str, str]]:
        """抓取特定标的当天/最新舆情新闻与公告"""
        clean_code = symbol.replace("sh", "").replace("sz", "").strip()
        news_list = []

        # 东方财富个股舆情 news api
        url = f"https://search-api-web.eastmoney.com/search/jsonp?cb=cb&param=%7B%20%22uid%22%3A%22%22%2C%22keyword%22%3A%22{clean_code}%20{name}%22%2C%22type%22%3A%5B%22cmsArticleWebOld%22%5D%2C%22pageIndex%22%3A1%2C%22pageSize%22%3A{limit}%7D"

        try:
            res = requests.get(url, headers=self.headers, timeout=5)
            if res.status_code == 200:
                raw_text = res.text
                if "cb(" in raw_text:
                    json_str = raw_text[raw_text.find("cb(") + 3 : raw_text.rfind(")")]
                    import json
                    data = json.loads(json_str)
                    items = data.get("result", {}).get("cmsArticleWebOld", [])
                    for item in items:
                        title = item.get("title", "").replace("<em>", "").replace("</em>", "")
                        show_time = item.get("showTime", "")
                        media_name = item.get("mediaName", "财经快讯")
                        url_link = item.get("url", "")

                        if title:
                            news_list.append({
                                "time": show_time,
                                "media": media_name,
                                "title": title,
                                "url": url_link
                            })
        except Exception as e:
            print(f"⚠️ 拉取个股 {clean_code} 舆情新闻失败: {e}")

        # 如果新闻接口未获取到，给一条默认安全提示
        if not news_list:
            news_list.append({
                "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                "media": "全网监控",
                "title": f"未检索到 {name}({clean_code}) 今日突发利空舆情，消息面维持平稳。",
                "url": ""
            })

        return news_list[:limit]
