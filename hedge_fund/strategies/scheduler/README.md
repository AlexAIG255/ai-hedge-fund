# 定时任务部署（早盘选股 / 盘中监控 / 晚间复盘）

本目录提供把"早盘选股 09:50、盘中监控、晚间复盘 16:00"做成系统定时任务的脚本。

## 一、前置条件

1. 仓库已配置好运行环境（Python 3.11 + 依赖；推荐用虚拟环境 `.venv`）。
2. 已在仓库根目录的 `.env` 配好：
   - 飞书：`FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_APP_TOKEN` / `FEISHU_TABLE_ID`
   - 企业微信推送：`WECHAT_WEBHOOK`
   - （可选）LLM 多维分析：`HEDGE_FUND_LLM_MODEL` + 对应 `API_KEY`

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
bash hedge_fund/strategies/scheduler/run_job.sh intraday   # 盘中监控
bash hedge_fund/strategies/scheduler/run_job.sh evening    # 晚间复盘
```

日志在仓库根 `logs/` 目录。

## 四、时间说明

| 任务 | 建议时间 | 脚本参数 |
|------|----------|----------|
| 早盘选股 | 09:50（开盘后，A 股 09:30 开盘） | `morning` |
| 盘中监控 | 09:35 / 10:30 / 13:30 / 14:30 | `intraday` |
| 晚间复盘 | 16:00（15:00 收盘后） | `evening` |

> 仅在周一~周五运行。节假日休市不会自动跳过，如需跳过可在 `run_job.sh` 内加节假日判断。

## 五、可选增强

- **多维分析**：早盘研报默认对每只入选股做财报/股息/资金面/舆情分析（LLM 优先，失败降级模板）。
  晚间复盘默认不补分析（省资源）；如需开启，设置环境变量 `EVENING_ANALYSIS=true`。
- **分析股数上限**：`ANALYSIS_MAX_STOCKS`（默认 8，控制 LLM 调用量）。
