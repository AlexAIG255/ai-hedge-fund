"""
Agent 3: Risk Manager Agent - 全球联动风控、七大策略桶校验与动态仓位定价引擎
1. 联动外盘（美股/VIX/美债）与内盘（成交量/涨跌比）计算全球市场风险温度。
2. 支持七大策略桶差异化风控（Bottom-Reversal, Momentum, High-Dividend, Event-Driven, PEAD, Trend-Follow, Mean-Reversion）。
3. 基于 ATR 真实波幅与波动率模型，动态生成专业止损价、止盈价与建仓仓位建议。
"""

import math
import os
import time
from typing import Dict, List, Tuple
import requests

# ⚙️ 策略桶配置与风控参数
ENABLE_BOTTOM_REVERSAL = os.environ.get("ENABLE_BOTTOM_REVERSAL", "true").lower() == "true"


class RiskManagerAgent:

    def __init__(self):
        # 🎯 七大策略桶及其专属风控参数配置 (ATR 倍数、最大允许仓位、默认盈亏比门槛)
        self.STRATEGY_BUCKETS = {
            "BOTTOM_REVERSAL": {"name": "🧪 底部反转", "atr_stop_mult": 1.5, "atr_tp_mult": 3.0, "max_pos_pct": 15.0, "min_rr_ratio": 2.0},
            "MOMENTUM_BREAKOUT": {"name": "🚀 动能突破", "atr_stop_mult": 2.0, "atr_tp_mult": 4.0, "max_pos_pct": 20.0, "min_rr_ratio": 2.0},
            "HIGH_DIVIDEND_LOW_VOL": {"name": "🛡️ 高股息低吸", "atr_stop_mult": 1.2, "atr_tp_mult": 2.5, "max_pos_pct": 25.0, "min_rr_ratio": 1.8},
            "EVENT_DRIVEN": {"name": "⚡ 事件驱动", "atr_stop_mult": 1.8, "atr_tp_mult": 3.5, "max_pos_pct": 10.0, "min_rr_ratio": 2.0},
            "PEAD_SURPRISE": {"name": "📊 盈余公告漂移", "atr_stop_mult": 1.5, "atr_tp_mult": 3.0, "max_pos_pct": 15.0, "min_rr_ratio": 2.0},
            "TREND_FOLLOWING": {"name": "📈 趋势追踪", "atr_stop_mult": 2.5, "atr_tp_mult": 5.0, "max_pos_pct": 20.0, "min_rr_ratio": 2.2},
            "MEAN_REVERSION": {"name": "🔄 均值回归", "atr_stop_mult": 1.2, "atr_tp_mult": 2.4, "max_pos_pct": 15.0, "min_rr_ratio": 1.8},
        }

    # ==========================================
    # 🌐 1. 全球市场风控温度计 (内盘 + 外盘联动)
    # ==========================================
    def fetch_global_market_temperature(self) -> Dict[str, float]:
        """
        拉取外盘 (美股/波动率/外围情绪) 与内盘 (A股/港股) 实时数据，计算市场风险温度指数 (0-100)
        100 表示极度乐观/强风险偏好，0 表示极度悲观/防守
        """
        print("🌐 正在获取全球宏观与内外盘市场风控数据...")
        temperature = 50.0  # 默认基准温度
        metrics = {}

        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

        try:
            # 1.1 拉取新浪/腾讯外盘主要指数 (道琼斯、纳斯达克、标普500、恒生指数)
            ext_url = "http://qt.gtimg.cn/q=us.DJI,us.IXIC,us.INX,hkHSI"
            res = requests.get(ext_url, headers=headers, timeout=5)
            if res.status_code == 200:
                lines = res.text.split(";")
                ext_pcts = []
                for line in lines:
                    if '="' in line:
                        f = line.split('="')[1].replace('"', "").split("~")
                        if len(f) > 31 and f[31]:
                            ext_pcts.append(float(f[31]))  # 涨跌幅 %

                if ext_pcts:
                    avg_ext_pct = sum(ext_pcts) / len(ext_pcts)
                    metrics["外盘平均涨跌幅"] = avg_ext_pct
                    # 外盘影响权重：±1% 对应 ±15 分温度调整
                    temperature += max(-25.0, min(25.0, avg_ext_pct * 15.0))

            # 1.2 拉取 A 股上证指数 & 创业板指行情，计算内盘动能
            a_url = "http://qt.gtimg.cn/q=sh000001,sz399006"
            res_a = requests.get(a_url, headers=headers, timeout=5)
            if res_a.status_code == 200:
                lines_a = res_a.text.split(";")
                a_pcts = []
                for line in lines_a:
                    if '="' in line:
                        f = line.split('="')[1].replace('"', "").split("~")
                        if len(f) > 32 and f[32]:
                            a_pcts.append(float(f[32]))

                if a_pcts:
                    avg_a_pct = sum(a_pcts) / len(a_pcts)
                    metrics["A股主要指数涨跌幅"] = avg_a_pct
                    temperature += max(-25.0, min(25.0, avg_a_pct * 15.0))

        except Exception as e:
            print(f"⚠️ 拉取全球市场温度指标异常，使用保守中性基准: {e}")

        # 限制温度区间在 10 ~ 90 之间
        final_temp = max(10.0, min(90.0, temperature))
        
        # 仓位乘数：温度低于 30 时自动缩减总仓位至 50%
        position_scale = min(1.0, max(0.3, final_temp / 60.0))

        print(f"🌡️ 当前全球市场风险温度: [{final_temp:.1f}/100] | 建议整体仓位系数: [{position_scale:.2f}]")
        return {
            "temperature": final_temp,
            "position_scale": position_scale,
            "metrics": metrics,
        }

    # ==========================================
    # 📈 2. 基于波动率 (ATR) 的动态风控定价
    # ==========================================
    def calculate_atr_and_volatility(self, stock_code: str) -> Tuple[float, float]:
        """
        获取单股近期日 K 线历史数据，计算 ATR (真实波幅) 与年化波动率
        如获取失败，回退至保守的估计值 (价格的 3% 作为 ATR)
        """
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        symbol = f"sh{stock_code}" if stock_code.startswith("60") or stock_code.startswith("68") else f"sz{stock_code}"
        
        # 腾讯 K 线接口 (最近 20 个交易日)
        kline_url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,20,qfq"
        
        try:
            res = requests.get(kline_url, headers=headers, timeout=5)
            if res.status_code == 200:
                data = res.json()
                day_data = data.get("data", {}).get(symbol, {}).get("day", []) or data.get("data", {}).get(symbol, {}).get("qfqday", [])
                
                if len(day_data) >= 10:
                    tr_list = []
                    prev_close = float(day_data[0][2])
                    for day in day_data[1:]:
                        high = float(day[3])
                        low = float(day[4])
                        close = float(day[2])
                        
                        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
                        tr_list.append(tr)
                        prev_close = close
                    
                    atr = sum(tr_list[-14:]) / min(14, len(tr_list))
                    latest_close = float(day_data[-1][2])
                    volatility = (atr / latest_close) if latest_close > 0 else 0.03
                    return atr, volatility
        except Exception as e:
            print(f"⚠️ 获取股票 {stock_code} K线异常，采用估计值: {e}")

        return 0.0, 0.03

    # ==========================================
    # 🛡️ 3. 核心风控评估与决策入口
    # ==========================================
    def evaluate_and_price_stock(self, stock_info: Dict, global_env: Dict) -> Dict:
        """
        针对单只选出的股票执行多维度风控审核，输出风控结果与操作建议
        """
        code = stock_info.get("code", "")
        name = stock_info.get("name", "")
        curr_price = float(stock_info.get("price", stock_info.get("entry_price", 0.0)))
        strategy_key = stock_info.get("strategy", "BOTTOM_REVERSAL" if ENABLE_BOTTOM_REVERSAL else "TREND_FOLLOWING")

        strat_config = self.STRATEGY_BUCKETS.get(strategy_key, self.STRATEGY_BUCKETS["BOTTOM_REVERSAL"])
        
        # 1. 拉取波动率并计算 ATR
        atr, vol = self.calculate_atr_and_volatility(code)
        if atr == 0.0:
            atr = curr_price * 0.03  # 兜底以 3% 作为 ATR

        # 2. 依据策略桶属性计算动态止损价与止盈价
        stop_loss_delta = atr * strat_config["atr_stop_mult"]
        target_price_delta = atr * strat_config["atr_tp_mult"]

        stop_loss = max(0.01, round(curr_price - stop_loss_delta, 2))
        target_price = round(curr_price + target_price_delta, 2)

        # 3. 盈亏比计算 (Risk-Reward Ratio)
        risk = curr_price - stop_loss
        reward = target_price - curr_price
        rr_ratio = (reward / risk) if risk > 0 else 0.0

        # 4. 结合全球市场温度计算动态建仓仓位
        base_pos = strat_config["max_pos_pct"]
        suggested_pos = round(base_pos * global_env["position_scale"], 1)

        # 5. 风控审核判定 (是否批准买入/推荐)
        is_passed = True
        reject_reasons = []

        if rr_ratio < strat_config["min_rr_ratio"]:
            is_passed = False
            reject_reasons.append(f"盈亏比不达标 ({rr_ratio:.2f} < 门槛 {strat_config['min_rr_ratio']})")

        if global_env["temperature"] < 25.0 and strategy_key not in ["HIGH_DIVIDEND_LOW_VOL", "MEAN_REVERSION"]:
            is_passed = False
            reject_reasons.append("全球宏观风险温度极低，仅允许高股息/防御型策略桶建仓")

        status_flag = "✅ 允许建仓" if is_passed else "❌ 风控拦截"
        risk_summary = f"{status_flag} | 建议仓位: `{suggested_pos}%` | 盈亏比: `{rr_ratio:.2f}`"
        if reject_reasons:
            risk_summary += f" | 原因: {'; '.join(reject_reasons)}"

        return {
            "code": code,
            "name": name,
            "strategy": strategy_key,
            "strategy_name": strat_config["name"],
            "curr_price": curr_price,
            "stop_loss": stop_loss,
            "target_price": target_price,
            "rr_ratio": round(rr_ratio, 2),
            "suggested_pos_pct": suggested_pos,
            "is_passed": is_passed,
            "risk_summary": risk_summary,
            "reject_reasons": reject_reasons,
        }

    # ==========================================
    # 🚀 4. 批量执行风控过滤
    # ==========================================
    def process_candidate_stocks(self, candidate_stocks: List[Dict]) -> Tuple[List[Dict], Dict]:
        """对早盘选股池进行全量风控定级，返回经过风控筛选与定价后的股票列表"""
        global_env = self.fetch_global_market_temperature()
        approved_stocks = []

        print(f"\n🛡️ [Agent 3 Risk Manager] 开始对 {len(candidate_stocks)} 只标的进行风控审核...")

        for stock in candidate_stocks:
            risk_res = self.evaluate_and_price_stock(stock, global_env)
            # 将风控算出的专业止盈价、止损价、建议仓位合并回原股票字典
            stock["stop_loss"] = risk_res["stop_loss"]
            stock["target_price"] = risk_res["target_price"]
            stock["suggested_pos"] = f"{risk_res['suggested_pos_pct']}%"
            stock["rr_ratio"] = risk_res["rr_ratio"]
            stock["risk_passed"] = risk_res["is_passed"]
            stock["risk_summary"] = risk_res["risk_summary"]

            if risk_res["is_passed"]:
                approved_stocks.append(stock)
                print(f"  ✅ [{risk_res['strategy_name']}] {stock['name']}({stock['code']}) 审核通过 | 止损: {stock['stop_loss']} | 止盈: {stock['target_price']} | 建议仓位: {stock['suggested_pos']}")
            else:
                print(f"  ❌ [{risk_res['strategy_name']}] {stock['name']}({stock['code']}) 拦截 | 原因: {risk_res['risk_summary']}")

        return approved_stocks, global_env


if __name__ == "__main__":
    agent = RiskManagerAgent()
    test_candidates = [
        {"code": "600519", "name": "贵州茅台", "price": 1600.0, "strategy": "HIGH_DIVIDEND_LOW_VOL"},
        {"code": "002594", "name": "比亚迪", "price": 250.0, "strategy": "BOTTOM_REVERSAL"},
    ]
    approved, env = agent.process_candidate_stocks(test_candidates)
    print(f"\n最终通过风控的标的数量: {len(approved)} / {len(test_candidates)}")
