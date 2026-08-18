import json
import os

class PortfolioManager:
    def __init__(self, portfolio_path="data/portfolio.json"):
        self.portfolio_path = portfolio_path

    def load_portfolio(self):
        if os.path.exists(self.portfolio_path):
            with open(self.portfolio_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def update_portfolio(self, data):
        with open(self.portfolio_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def evaluate_exits(self, current_prices):
        """每天盘后/盘中检查持仓是否触及止损价"""
        portfolio = self.load_portfolio()
        positions = portfolio.get("positions", [])
        sell_signals = []

        for pos in positions:
            symbol = pos["symbol"]
            if symbol in current_prices:
                price = current_prices[symbol]
                # 触发止损位，发出平仓信号
                if price <= pos["stop_loss_price"]:
                    sell_signals.append({
                        "symbol": symbol,
                        "action": "SELL_ALL",
                        "reason": f"STOP_LOSS_TRIGGERED (Current: {price}, Stop: {pos['stop_loss_price']})"
                    })
        return sell_signals
