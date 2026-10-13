#!/bin/bash
# 開発・お試し用: ./start.sh で起動したエージェントを止める
# （処理中の依頼は、次に起動した時に自動で続きから再開される）

cd "$(dirname "$0")"

if [ ! -f logs/agent.pid ]; then
    echo "❌ logs/agent.pid がありません（起動していない可能性があります）"
    exit 1
fi

PID=$(cat logs/agent.pid)
if kill -0 "$PID" 2>/dev/null; then
    echo "停止しています… (PID: $PID)"
    kill "$PID"          # SIGTERM: 記憶を保存してから止まる
    sleep 3
    if kill -0 "$PID" 2>/dev/null; then
        kill -9 "$PID"
    fi
    echo "✅ 停止しました"
else
    echo "⚠️  プロセス $PID はすでに停止しています"
fi
rm -f logs/agent.pid
