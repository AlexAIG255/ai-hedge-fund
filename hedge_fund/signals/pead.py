def filter_pead_momentum_stocks(market_data: list) -> list:
    """
    早盘动量/事件驱动筛选：排除高波动爆雷股，锁定低回撤趋势标的
    """
    qualified = []
    for data in market_data:
        # 简单过滤条件：均线多头且 ATR 波动率小于 5%
        if data["close"] > data["ma20"] and (data["atr"] / data["close"]) < 0.05:
            qualified.append({
                "symbol": data["symbol"],
                "price": data["close"],
                "atr": data["atr"],
                "reason": "MA20_UPTREND_LOW_VOLATILITY"
            })
    return qualified
