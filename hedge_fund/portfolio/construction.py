import json
import os
from datetime import datetime
from typing import Any
from pydantic import BaseModel, ConfigDict
from hedge_fund.risk.limits import RiskLimits

# ==========================================
# 兼容旧接口声明（防止 portfolio/__init__.py 导入报错）
# ==========================================
class BlendResult(BaseModel):
    model_config = ConfigDict(extra="allow")
    status: str = "SUCCESS"

def blend_signals(*args, **kwargs) -> Any:
    return []

# ==========================================
# 核心持仓与晚间复盘管理器
# ==========================================
class PortfolioManager:
    def __init__(self, portfolio_path="data/portfolio.json"):
        self.portfolio_path = portfolio_path
        self.limits = RiskLimits.load_from_config()

    def load_portfolio(self) -> dict:
        """加载持仓与账户数据库"""
        if os.path.exists(self.portfolio_path):
            with open(self.portfolio_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def update_portfolio(self, data: dict):
        """写入/更新持仓数据库"""
        with open(self.portfolio_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def run_evening_review(self, closing_prices: dict = None) -> dict:
        """
        晚间复盘调仓引擎：更新账户总净值、刷新最高峰值、计算当前回撤，并判定止损与熔断
        """
        closing_prices = closing_prices or {}
        portfolio = self.load_portfolio()
        if not portfolio:
            return {"status": "ERROR", "message": "data/portfolio.json 文件不存在"}

        market_value = 0.0
        rebalance_orders = []

        # 1. 结算当前持仓市值并判断止损位
        for pos in portfolio.get("positions", []):
            symbol = pos["symbol"]
            curr_price = closing_prices.get(symbol, pos.get("cost_price", 0))
            market_value += pos.get("shares", 0) * curr_price

            # 触及止损价，生成平仓指令
            if curr_price <= pos.get("stop_loss_price", 0):
                rebalance_orders.append({
                    "symbol": symbol,
                    "action": "SELL_ALL",
                    "reason": f"STOP_LOSS_TRIGGERED (Current: {curr_price} <= Stop: {pos['stop_loss_price']})"
                })

        # 2. 结算账户净值与历史最高点
        available_cash = portfolio.get("available_cash", 0.0)
        total_equity = round(available_cash + market_value, 2)
        peak_equity = max(portfolio.get("peak_equity", total_equity), total_equity)
        drawdown = round((peak_equity - total_equity) / peak_equity, 4) if peak_equity > 0 else 0.0

        # 3. 账户回撤熔断判定（达到 8% 触发硬防护）
        trade_status = "NORMAL"
        if drawdown >= self.limits.max_portfolio_drawdown:
            trade_status = "FREEZE_BUY_AND_HALVE_POSITIONS"

        # 4. 自动更新并回写 data/portfolio.json
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
