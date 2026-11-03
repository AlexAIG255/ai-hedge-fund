# AI Hedge Fund

This is a proof of concept for an AI-powered hedge fund. The goal of this project is to explore the use of AI to make trading decisions. This project is for **educational** purposes only and is not intended for real trading or investment.

> **🚧 The project is evolving.** We're rebuilding it into a persistent, always-on AI hedge fund — a *fund* as a first-class entity you can backtest, paper-trade, and (opt-in) run live, with the investor agents reimagined as pluggable, backtestable "alpha models." Read the **[Vision →](VISION.md)** and the **[Roadmap →](ROADMAP.md)**.

Note: the system does not actually make any trades.

[![Twitter Follow](https://img.shields.io/twitter/follow/virattt?style=social)](https://twitter.com/virattt)

## Disclaimer

This project is for **educational and research purposes only**.

- Not intended for real trading or investment
- No investment advice or guarantees provided
- Creator assumes no liability for financial losses
- Consult a financial advisor for investment decisions
- Past performance does not indicate future results

By using this software, you agree to use it solely for learning purposes.

## How to Install

```bash
pipx install aihf
```

(or `uv tool install aihf`, or `pip install aihf` into an environment of your choice)

Then run it from anywhere:

```bash
aihf
```

### API keys

The app asks for keys the first time it needs them and saves them to `~/.hedge-fund/.env` — nothing to configure up front. It needs:

- A [Financial Datasets](https://financialdatasets.ai) API key, for prices, fundamentals, and earnings.
- One LLM API key for the LLM-powered alpha models. Supported providers: Anthropic, OpenAI, DeepSeek, Google, xAI, Kimi.

Keys exported in your shell always win over the saved file.

## How to Run

### Interactive app

```bash
aihf
```

With no arguments, this launches the interactive terminal app. Build a fund — pick stocks, strategies, rebalance cadence — or backtest a saved fund and watch its equity curve draw against its benchmark. Funds you build are saved as mandate files in `~/.hedge-fund/mandates/`.

### Non-interactive

Run one fund cycle from a mandate file. The full cycle record prints to stdout as JSON; a short human summary goes to stderr:

```bash
aihf ~/.hedge-fund/mandates/example.yaml --tickers AAPL,MSFT
```

Backtest the mandate over history at its rebalance cadence:

```bash
aihf ~/.hedge-fund/mandates/example.yaml --tickers AAPL,MSFT --backtest
```

A mandate is the desk — strategies, staff, risk, capital, cadence — and never names tickers; `--tickers` says what to point it at for this run.

## Development

```bash
git clone https://github.com/virattt/ai-hedge-fund.git
cd ai-hedge-fund
poetry install
poetry run aihf
poetry run pytest hedge_fund
```

## How to Contribute

1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Push to the branch
5. Create a Pull Request

**Important**: Please keep your pull requests small and focused. This will make it easier to review and merge.

## Feature Requests

If you have a feature request, please open an [issue](https://github.com/virattt/ai-hedge-fund/issues) and make sure it is tagged with `enhancement`.

## License

This project is licensed under the MIT License - see the LICENSE file for details.

---

## 中文 A 股选股 / 复盘 自动化（`hedge_fund/strategies`）

仓库内置一套面向 A 股的「早盘选股 + 盘中监控 + 晚间复盘」自动化工具，数据与推送均对接你的飞书多维表格与企业微信。

### 能力
- **早盘选股**（`morning_picker.py`）：多策略筛选 → 三源行情校验 → 风控测算，并输出每只标的的
  **多维分析**（财报面 / 估值红利 / 资金面 / 消息面 / 风险提示），LLM 生成，失败自动降级为模板。
- **盘中监控**（`evening_review.py --mode intraday`）：触碰止损/止盈线即推送 🚨 抛售提醒。
- **晚间复盘**（`evening_review.py`）：收盘判定止盈/止损/到期平仓，回写飞书，并推送复盘总览与抛售清单。
- **飞书同步**：`feishu_sync.py`（按真实列名+类型自动对齐）、`feishu_migrate.py`（一键补列）。
- **分段推送**：企业微信单条超 1800 字自动拆分，避免被风控封锁。

### 定时任务
见 `hedge_fund/strategies/scheduler/`：早盘 09:50、盘中多时点、晚间 16:00，详见该目录 README。

### 配置（`.env`）
```
FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_APP_TOKEN / FEISHU_TABLE_ID   # 飞书多维表格
WECHAT_WEBHOOK                                                            # 企业微信机器人
HEDGE_FUND_LLM_MODEL + 对应 API_KEY                                        # 多维分析（可选）
EVENING_ANALYSIS=true                                                      # 晚间也补分析（可选）
ANALYSIS_MAX_STOCKS=8                                                      # 分析股数上限
```

> ⚠️ 本工具用于学习研究，不构成任何投资建议。

