# 動かす Mac の準備

エージェントは「画面にログインした Mac ユーザー」の常駐プロセスとして動きます。
Claude Code のログイン情報がそのユーザーのキーチェーンに入るためです。

## 0. 機械の条件

| 項目 | 目安 |
|---|---|
| メモリ | 16GB 以上を推奨（8GB だと同時処理 1 件が限界。Claude Code 1本あたり 0.5〜1.5GB） |
| OS | macOS 14 以降 |
| 電源 | 常時通電。スリープしない設定 |
| ネット | 外向きの HTTPS だけで動く（Socket Mode なので受信用の公開 URL・ポート開放は不要） |

**別の会社・別の用途のエージェントやデータと、同じ機械で兼用しないことをおすすめします。**
同じ機械に置く場合は、`AGENT_ID`・Mac ユーザー・`CLAUDE_CONFIG_DIR` をすべて分けてください。

## 1. Mac の設定

1. エージェント専用の Mac ユーザーを作る（例: `my-agent`）。管理者権限は付けない
2. **自動ログイン**をそのユーザーにする（システム設定 → ユーザとグループ）。
   停電・アップデートの再起動後にログインされないと、エージェントは止まったままになる
   - FileVault が有効だと自動ログインできない。その場合は再起動のたびに人がログインする運用にする
3. スリープさせない: システム設定 → ディスプレイ/バッテリー → 「ディスプレイがオフのときに自動でスリープさせない」
4. 停電後に自動で起動: `sudo pmset -a autorestart 1`
5. 遠隔で保守するなら Tailscale などで自分たちのネットワークに参加させる。SSH は鍵認証のみ

## 2. 必要なソフト

```bash
# Homebrew の後に
brew install uv git
uv python install 3.11
# claude CLI は claude-agent-sdk に同梱のものを使う（別に入れたい時は .env の CLAUDE_BIN）
```

## 3. コードと Python 環境

```bash
git clone https://github.com/ken-kurosu/slack-claude-agent.git ~/slack-claude-agent
cd ~/slack-claude-agent
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt
git config core.hooksPath scripts/git-hooks
.venv/bin/python -m pytest -q tests     # 全部通ることを確認
```

## 4. Slack アプリ

1. https://api.slack.com/apps → Create New App → From an app manifest → 使うワークスペース
2. `slack/manifest.yaml` を貼る（表示名は自分たちのエージェント名に変える）
3. Basic Information → App-Level Tokens → `connections:write` で作成 → `xapp-…` が **SLACK_APP_TOKEN**
4. Install to Workspace → OAuth & Permissions の `xoxb-…` が **SLACK_BOT_TOKEN**
5. 使うチャンネルにアプリを招待する（`/invite @エージェント名`）

トークンはチャット・Slack・ドキュメントに貼らず、`.env` にだけ入れてください。

## 5. Claude

[CLAUDE_ACCOUNT_SWITCH.md](CLAUDE_ACCOUNT_SWITCH.md) の「初回」の手順でログインし、
`CLAUDE_CODE_OAUTH_TOKEN` を `.env` に入れます。

## 6. .env

```bash
cp .env.example .env
chmod 600 .env
open -e .env
```

`AGENT_ID`（英小文字・数字・ハイフン）と `ADMIN_USER_IDS`（管理者の Slack ユーザーID）は必ず入れてください。
危ない操作の許可は管理者の依頼だけに限られます。

## 7. 動作確認 → 常駐

```bash
./start.sh                  # 手動で起動。logs/agent.log を見ながら Slack でメンションして返事を確認
./stop.sh
./scripts/install_launchd.sh            # 常駐に登録（ログイン時に自動起動・落ちたら再起動）
tail -f logs/agent.log
./scripts/install_launchd.sh uninstall  # 常駐をやめる
```

## 再起動するとき

作業中の依頼があるときに止めると、その依頼は途中で切れます（起動時に自動再開はしますが、なるべく避けてください）。
`memory_data/active_runs.json` が空（`{}`）のときに再起動します。`<AGENT_ID>` は `.env` の値です。

```bash
python3 -c 'import json,sys; d=json.load(open("memory_data/active_runs.json")); sys.exit(1 if d else 0)' \
  && launchctl kickstart -k gui/$(id -u)/com.<AGENT_ID>.agent \
  || echo "作業中の依頼があるので、終わってから再起動してください"
```
