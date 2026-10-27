#!/usr/bin/env bash
# 运行一次复盘。示例：./run.sh --demo    ./run.sh --date 2026-10-27
cd "$(dirname "$0")"
mkdir -p output
echo "==== $(date '+%F %T') $* ====" >> output/run.log
.venv/bin/python -m reviewbot.main "$@" 2>&1 | tee -a output/run.log
exit "${PIPESTATUS[0]}"
