"""
Agent 1: 大盘早晚选股 Agent (Morning Stock Picker Agent)
职责：接收候选股票池（来自通达信/AkShare/大盘扫描），结合技术分析与 LLM，选出候选标的与目标买点。
"""

import json
from typing import Any, Dict, List


class MorningStockPickerAgent:

    def __init__(self, llm_client=None):
        self.llm_client = llm_client

    def pick_stocks(
        self, stock_candidates: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """输入候选股票列表，输出评估后的精选标的与买点建议"""
        selected_targets = []

        for stock in stock_candidates:
            symbol = stock.get("symbol")
            name = stock.get("name", "未知")
            current_price = stock.get("price", 0.0)
            atr = stock.get("atr", 0.0)

            # 选股与买点算法逻辑（可在此扩展 LLM Prompt 评估）
            # 策略示例：以当前价回调 1% 作为目标买点，以 2 倍 ATR 或 3% 作为止损距
            target_buy_price = round(current_price * 0.99, 2)

            if atr > 0:
                stop_loss_price = round(target_buy_price - (1.5 * atr), 2)
            else:
                stop_loss_price = round(target_buy_price * 0.97, 2)

            selected_targets.append({
                "symbol": symbol,
                "name": name,
                "current_price": current_price,
                "target_buy_price": target_buy_price,
                "stop_loss_price": stop_loss_price,
                "atr": atr,
                "pick_reason": stock.get(
                    "reason", "放量突破形态，等待回调买点"
                ),
            })

        self._log_results(selected_targets)
        return selected_targets

    def _log_results(self, targets: List[Dict[str, Any]]):
        """打印选股日志"""
        print("\n================ [Agent 1 早盘选股完成] ================")
        print(f"共筛选出 {len(targets)} 只精选候选标的：")
        for item in targets:
            print(
                f"📌 代码: {item['symbol']} | 名称: {item['name']}\n"
                f"   现价: {item['current_price']} 元 | 目标买点: {item['target_buy_price']} 元\n"
                f"   建议止损位: {item['stop_loss_price']} 元 | 理由: {item['pick_reason']}\n"
                f"------------------------------------------------"
            )


# ==================== 本地独立测试 ====================
if __name__ == "__main__":
    # 模拟通达信或行情接口推送过来的早盘数据
    mock_candidates = [
        {
            "symbol": "600519",
            "name": "贵州茅台",
            "price": 1600.0,
            "atr": 25.0,
            "reason": "站稳 20 日均线",
        },
        {
            "symbol": "002594",
            "name": "比亚迪",
            "price": 250.0,
            "atr": 5.5,
            "reason": "主力资金净流入，突破平台",
        },
    ]

    agent = MorningStockPickerAgent()
    results = agent.pick_stocks(mock_candidates)
