#!/usr/bin/env bash
# =====================================================================
# AI Hedge Fund 定时任务统一入口
#   用法: ./run_job.sh <morning|evening|intraday>
#   - morning  早盘选股 (建议 09:50)
#   - evening  晚间复盘 (建议 16:00)
#   - intraday 盘中止损止盈抛售监控 (建议 09:35/10:30/13:30/14:30)
# 所有日志写入仓库 logs/ 目录。
# =====================================================================
set -e

JOB="${1:-morning}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../../.." && pwd)"   # scheduler → strategies → hedge_fund → 仓库根
cd "$REPO"

# 如使用虚拟环境，自动激活（无则忽略）
if [ -f "$REPO/.venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$REPO/.venv/bin/activate"
fi

PY="${PYTHON:-python3}"
mkdir -p logs

case "$JOB" in
    morning)
        echo "[$(date '+%F %T')] 启动 早盘选股..." >> logs/morning.log
        "$PY" hedge_fund/strategies/morning_picker.py >> logs/morning.log 2>&1
        ;;
    evening)
        echo "[$(date '+%F %T')] 启动 晚间复盘..." >> logs/evening.log
        "$PY" hedge_fund/strategies/evening_review.py >> logs/evening.log 2>&1
        ;;
    intraday)
        echo "[$(date '+%F %T')] 启动 盘中监控..." >> logs/intraday.log
        "$PY" hedge_fund/strategies/evening_review.py --mode intraday >> logs/intraday.log 2>&1
        ;;
    *)
        echo "未知任务: $JOB (可选 morning|evening|intraday)"
        exit 1
        ;;
esac
echo "[$(date '+%F %T')] 任务 $JOB 完成。" >> logs/"$JOB".log
