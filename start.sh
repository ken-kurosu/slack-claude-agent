#!/bin/bash
# 開発・お試し用: エージェントをバックグラウンドで起動する（本番の常駐は scripts/install_launchd.sh）
#
#   ./start.sh            — 起動
#   ./stop.sh             — 停止
#   tail -f logs/agent.log — ログを見る

cd "$(dirname "$0")"
mkdir -p logs

if [ ! -f .env ]; then
    echo "❌ .env がありません。cp .env.example .env して値を入れてください"
    exit 1
fi
if [ ! -x .venv/bin/python ]; then
    echo "❌ .venv がありません。python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi

if [ -f logs/agent.pid ]; then
    OLD_PID=$(cat logs/agent.pid)
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "⚠️  すでに起動中です (PID: $OLD_PID)。停止するには ./stop.sh"
        exit 1
    fi
    rm -f logs/agent.pid
fi

echo "起動しています…"
nohup .venv/bin/python app.py > logs/agent.log 2>&1 &
echo $! > logs/agent.pid
echo "✅ 起動しました (PID: $(cat logs/agent.pid))  ログ: tail -f logs/agent.log"
