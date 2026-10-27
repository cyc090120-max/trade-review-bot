#!/usr/bin/env bash
# 一键安装（Ubuntu / Debian）。在项目目录里运行：./install.sh
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"
USER_NAME="$(whoami)"

echo "▶ 1/5 安装系统依赖（Python、中文字体）"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip fonts-noto-cjk git

echo "▶ 2/5 创建 Python 环境并安装依赖"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

echo "▶ 3/5 安装无头浏览器"
.venv/bin/python -m playwright install --with-deps chromium

echo "▶ 4/5 准备配置文件"
[ -f config.yaml ] || cp config.example.yaml config.yaml
[ -f .env ] || { cp .env.example .env; chmod 600 .env; }
chmod +x run.sh

echo "▶ 5/5 设置定时任务（周一到周五 美东 16:15）"
sed -e "s#__DIR__#${DIR}#g" -e "s#__USER__#${USER_NAME}#g" deploy/review-bot.service | sudo tee /etc/systemd/system/review-bot.service >/dev/null
sudo cp deploy/review-bot.timer /etc/systemd/system/review-bot.timer
sudo systemctl daemon-reload
sudo systemctl enable --now review-bot.timer

echo
echo "✅ 安装完成。下一步："
echo "   1. 编辑 .env 填入 GEMINI_API_KEY 和 DISCORD_WEBHOOK_URL：  nano .env"
echo "   2. 测试：  ./run.sh --demo"
echo "   3. 查看下次运行时间：  systemctl list-timers review-bot.timer"
