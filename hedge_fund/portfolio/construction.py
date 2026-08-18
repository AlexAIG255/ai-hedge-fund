import json
import os
from datetime import datetime
from typing import Any
from pydantic import BaseModel, ConfigDict
from hedge_fund.risk.limits import RiskLimits

class BlendResult(BaseModel):
    model_config = ConfigDict(extra="allow")
    status: str = "SUCCESS"

def blend_signals(*args, **kwargs) -> Any:
    return []

class PortfolioManager:
    def __init__(self, portfolio_path="data/portfolio.json"):
        self.portfolio_path = portfolio_path
        self.limits = RiskLimits.load_from_config()

    def _get_default_portfolio((self) -> dict:
        """生成默认持仓结构"""
        return {
            "initial_capital": 100000.0,
            "available_cash": 100000.0,
            "total_equity": 100000.0,
            "peak_equity": 100000.0,
            "current_drawdown": 0.0,
            "trade_status": "NORMAL",
            "positions": [],
            "trade_history": []
        }

    def load_portfolio(self) -> dict:
        """加载持仓，不存在则自动初始化创建"""
        os.makedirs(os.path.dirname(self.portfolio_path), exist_ok=True)
        if os.path.exists(self.portfolio_path):
            with open(self.portfolio_path, "r", encoding="utf-8") as f:
                return json.load(f)
        
        # 不存在则新建默认文件
        default_data = self._get_default_portfolio()
        self.update_portfolio(default_data)
        return default_data

    def update_portfolio(self, data: dict):
        """写入/更新持仓数据库"""
        os.makedirs(os.path.dirname(self.portfolio_path), exist_ok=True)
        with open(self.portfolio_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def run_evening_review(self, closing_prices: dict = None) -> dict:
        """晚间复盘调仓引擎"""
        closing_prices = closing_prices or {}
        portfolio = self.load_portfolio()

        market_value = 0.0
        rebalance_orders = []

        for pos in portfolio.get("positions", []):
            symbol = pos["symbol"]
            curr_price = closing_prices.get(symbol, pos.get("cost_price", 0))
            market_value += pos.get("shares", 0) * curr_price

            if curr_price <= pos.get("stop_loss_price", 0):
                rebalance_orders.append({
                    "symbol": symbol,
                    "action": "SELL_ALL",
                    "reason": f"STOP_LOSS_TRIGGERED (Current: {curr_price} <= Stop: {pos['stop_loss_price']})"
                })

        available_cash = portfolio.get("available_cash", 100000.0)
        total_equity = round(available_cash + market_value, 2)
        peak_equity = max(portfolio.get("peak_equity", total_equity), total_equity)
        drawdown = round((peak_equity - total_equity) / peak_equity, 4) if peak_equity > 0 else 0.0

        trade_status = "NORMAL"
        if drawdown >= self.limits.max_portfolio_drawdown:
            trade_status = "FREEZE_BUY_AND_HALVE_POSITIONS"

        portfolio["total_equity"] = total_equity
        portfolio["peak_equity"] = peak_equity
        portfolio["current_drawdown"] = drawdown
        portfolio["trade_status"] = trade_status
        portfolio["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        self.update_portfolio(portfolio)

        return {
            "total_equity": total_equity,
            "drawdown_pct": f"{drawdown * 100:.2f}%",
            "trade_status": trade_status,
            "rebalance_orders": rebalance_orders
        }
