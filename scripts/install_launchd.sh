#!/bin/bash
# 本番用: エージェントを launchd（ログイン中のユーザーの常駐）に登録して起動する
#
#   ./scripts/install_launchd.sh            — 登録して起動（既に登録済みなら入れ直す）
#   ./scripts/install_launchd.sh uninstall  — 止めて登録を外す
#
# ・登録名は .env の AGENT_ID から決める（com.<AGENT_ID>.agent）。同じ Mac の別エージェントと衝突しない
# ・Claude Code はキーチェーンのログインを使うので、画面にログインしたユーザーのセッション（gui/<uid>）で動かす
# ・plist に秘密情報は書かない（.env はアプリが自分で読む）
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

AGENT_ID="$(grep -E '^AGENT_ID=' .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'"'"' ')"
AGENT_ID="${AGENT_ID:-slack-agent}"
if ! [[ "$AGENT_ID" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
    echo "❌ AGENT_ID は英小文字・数字・ハイフンだけにしてください: $AGENT_ID"; exit 1
fi
LABEL="com.${AGENT_ID}.agent"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
DOMAIN="gui/$(id -u)"

if [ "${1:-}" = "uninstall" ]; then
    launchctl bootout "$DOMAIN" "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "✅ 登録を外しました: $LABEL"
    exit 0
fi

[ -f .env ] || { echo "❌ .env がありません"; exit 1; }
[ -x .venv/bin/python ] || { echo "❌ .venv がありません"; exit 1; }
mkdir -p logs "$HOME/Library/LaunchAgents"

sed -e "s#__LABEL__#${LABEL}#g" -e "s#__ROOT__#${ROOT}#g" scripts/launchd/agent.plist.template > "$PLIST"
plutil -lint "$PLIST" >/dev/null

launchctl bootout "$DOMAIN" "$PLIST" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "✅ 登録して起動しました: $LABEL"
echo "   ログ: tail -f $ROOT/logs/agent.log"
echo "   再起動: launchctl kickstart -k $DOMAIN/$LABEL （処理中の依頼が無い時に）"
