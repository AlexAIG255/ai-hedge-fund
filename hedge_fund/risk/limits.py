import json
import os
from pydantic import BaseModel, ConfigDict, Field

class RiskLimits(BaseModel):
    """账户硬性风控规则模型"""
    model_config = ConfigDict(extra="forbid")

    max_position_pct: float = Field(default=0.20, description="单只股票最大仓位比例")
    max_portfolio_drawdown: float = Field(default=0.08, description="账户最大允许总回撤")
    max_daily_loss: float = Field(default=0.02, description="单日最大允许亏损比例")
    default_stop_loss_pct: float = Field(default=0.05, description="默认单笔止损比例")

    @classmethod
    def load_from_config(cls, config_path="config/risk_rules.json") -> "RiskLimits":
        """自动读取 config/risk_rules.json 的风控参数"""
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return cls(
                max_position_pct=data.get("max_single_stock_ratio", 0.20),
                max_portfolio_drawdown=data.get("max_portfolio_drawdown_limit", 0.08),
                max_daily_loss=data.get("max_daily_loss_limit", 0.02),
                default_stop_loss_pct=data.get("default_stop_loss_ratio", 0.05)
            )
        return cls()

def clamp_position_size(total_capital: float, entry_price: float, stop_loss_price: float, limits: RiskLimits) -> int:
    """风控计算器：基于单笔最大风险与风控上限，计算最大可买股数"""
    if entry_price <= 0 or stop_loss_price >= entry_price:
        return 0

    # 单笔交易允许的最大亏损金额
    max_risk_amount = total_capital * limits.max_daily_loss
    risk_per_share = entry_price - stop_loss_price
    
    # 按照风险推算的股数
    shares_by_risk = int(max_risk_amount / risk_per_share)
    
    # 按照单票资金上限推算的股数
    max_capital_allowed = total_capital * limits.max_position_pct
    shares_by_cap = int(max_capital_allowed / entry_price)

    # 取两者的最小值（硬风控截断）
    return max(0, min(shares_by_risk, shares_by_cap))
