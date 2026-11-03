# 定时任务部署（早盘选股 / 盘中异动 / 晚间复盘 / 持仓总结）

本目录提供把"早盘选股 09:50、盘中异动监控、晚间复盘 16:00、持仓每日总结 20:30"做成系统定时任务的脚本。

## 一、前置条件

1. 仓库已配置好运行环境（Python 3.11 + 依赖；推荐用虚拟环境 `.venv`）。
   **新增依赖**：`pycryptodome`（企微机器人 AES 解密）、`chinese_calendar`（精准交易日历，可选但推荐）。
2. 已在仓库根目录的 `.env` 配好：
   - 飞书：`FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_APP_TOKEN` / `FEISHU_TABLE_ID`
   - 企业微信推送：`WECHAT_WEBHOOK`
   - （可选）LLM 多维分析：`HEDGE_FUND_LLM_MODEL` + 对应 `API_KEY`
   - （可选）持仓截图 OCR：`VISION_API_KEY` / `VISION_API_BASE` / `VISION_MODEL`
   - （可选）企微自建应用机器人：`WX_CORP_ID` / `WX_AGENT_ID` / `WX_CORP_SECRET` / `WX_TOKEN` / `WX_AES_KEY`

## 二、安装 cron（推荐）

编辑 `crontab.example`，把第一行的 `SCHEDULER` 改成你机器上的绝对路径，例如：

```bash
SCHEDULER=/home/ubuntu/ai-hedge-fund/hedge_fund/strategies/scheduler
```

然后导入：

```bash
crontab /home/ubuntu/ai-hedge-fund/hedge_fund/strategies/scheduler/crontab.example
```

查看已安装的任务：`crontab -l`

## 三、手动运行（调试用）

```bash
bash hedge_fund/strategies/scheduler/run_job.sh morning    # 早盘选股
bash hedge_fund/strategies/scheduler/run_job.sh intraday   # 盘中异动监控
bash hedge_fund/strategies/scheduler/run_job.sh evening    # 晚间复盘
bash hedge_fund/strategies/scheduler/run_job.sh holdings   # 持仓每日总结
```

日志在仓库根 `logs/` 目录。

## 四、时间说明

| 任务 | 建议时间 | 脚本参数 | 说明 |
|------|----------|----------|------|
| 早盘选股 | 09:50（A 股 09:30 开盘后） | `morning` | 多策略筛选 + 三源校验 + 多维分析 |
| 盘中异动监控 | 09:35 / 10:30 / 13:30 / 14:30 | `intraday` | **仅异动提醒**（涨幅/跌幅/放量/振幅），不做止盈止损平仓 |
| 晚间复盘 | 16:00（15:00 收盘后） | `evening` | 止盈/止损/到期平仓 + 回写飞书 + 复盘总览 |
| 持仓每日总结 | 20:30 | `holdings` | 用已录入持仓做买卖/做T/盈亏归因详细总结 |

> **节假日自动跳过**：`run_job.sh` 内置交易日守卫（`trading_calendar.py`），周末与法定节假日（含 2025/2026 休市表，调休以 `chinese_calendar` 为准）会自动跳过，不刷屏、不浪费 API。仅在周一~周五 + 交易日运行。

## 五、可选增强

- **多维分析**：早盘研报默认对每只入选股做财报/股息/资金面/舆情分析（LLM 优先，失败降级模板）。
  晚间复盘默认不补分析（省资源）；如需开启，设置环境变量 `EVENING_ANALYSIS=true`。
- **分析股数上限**：`ANALYSIS_MAX_STOCKS`（默认 8，控制 LLM 调用量）。
- **持仓分析机器人**：发送持仓截图给企微自建应用即可自动识别+分析。详见仓库 `hedge_fund/strategies/wx_bot_server.py` 顶部说明，启动：`python hedge_fund/strategies/wx_bot_server.py`。
- **自动进化 Skill**：`SKILL_AUTO_EVOLVE=true` 时，持仓分析会从复盘胜率中自动沉淀经验，降低误判；也可用 `--feedback "经验文字"` 手动沉淀。

