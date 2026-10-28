#!/usr/bin/env bash
# 在 Mac 上运行：手动登录 Tradovate / Discord，保存登录状态，上传到服务器并检查。
#   ./login.sh              两个都登录
#   ./login.sh discord      只登录 Discord
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "▶ 第一次运行：准备 Python 环境（1–2 分钟）"
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi
[ -d "/Applications/Google Chrome.app" ] || .venv/bin/python -m playwright install chromium
mkdir -p secrets && chmod 700 secrets

.venv/bin/python -m reviewbot.login "$@"

set -a; [ -f .env ] && . ./.env; set +a
if [ -z "${SERVER_SSH:-}" ]; then
  echo "（.env 里没有设置 SERVER_SSH，只保存在本机，没有上传）"
  exit 0
fi
PORT="${SERVER_SSH_PORT:-22}"
DIR="${SERVER_DIR:-trade-review-bot}"
SITES="${*:-tradovate discord}"
FILES=()
for s in $SITES; do FILES+=("secrets/${s}_state.json" "secrets/${s}_session.json" "secrets/${s}_info.json"); done

echo
echo "▶ 上传到服务器"
ssh -o ServerAliveInterval=15 -p "$PORT" "$SERVER_SSH" "mkdir -p '$DIR/secrets' && chmod 700 '$DIR/secrets'"
scp -q -o ServerAliveInterval=15 -P "$PORT" "${FILES[@]}" "$SERVER_SSH:$DIR/secrets/"
echo "▶ 在服务器上检查登录是否有效（每个网站约 15 秒）"
ssh -o ServerAliveInterval=15 -p "$PORT" "$SERVER_SSH" "cd '$DIR' && .venv/bin/python -m reviewbot.session check $SITES"
