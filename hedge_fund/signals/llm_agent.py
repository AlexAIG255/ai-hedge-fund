import json
from hedge_fund.risk.limits import RiskLimits, clamp_position_size

def generate_morning_signals(candidates: list, total_capital: float) -> list:
    """
    早盘选股 Agent：对输入的候选股票进行风控测算，输出建议买点、止损位与最大可买股数
    """
    limits = RiskLimits.load_from_config()
    signals = []

    for stock in candidates:
        price = stock["price"]
        atr = stock.get("atr", price * 0.02)
        stop_price = round(price - (2 * atr), 2)  # 基于 2 倍 ATR 挂止损线

        max_shares = clamp_position_size(total_capital, price, stop_price, limits)
        if max_shares > 0:
            signals.append({
                "symbol": stock["symbol"],
                "action": "BUY",
                "entry_price": price,
                "stop_loss_price": stop_price,
                "shares": max_shares,
                "reason": stock.get("reason", "LLM_QUANT_SELECTED")
            })

    return signals
