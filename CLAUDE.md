# slack-claude-agent

Slack の Socket Mode で動く社内AIアシスタント。名前・口調は `AGENT_NAME` と `prompts/persona.txt` で決める。公開リポジトリなので、社名・人名・社内IDを入れない。

## 絶対に守ること

- **ANTHROPIC_API_KEY は使わない・置かない**。Claude は Claude Max（サブスク）の枠で Claude Code を通して使う
- 秘密情報は `.env` にだけ。値を出力・ログ・チャット・コミットに出さない
- 特定の会社・人・社内IDの情報をコードやドキュメントに入れない（`tests/forbidden_words.local.txt` で検出）
- MCP サーバー（`mcp_agent_tools/server.py`）の中で LLM を呼ばない。AI の処理は会話側でやってからツールに渡す
- 外部への送信・書き込みは承認ボタンを通す（`tools/executor.py` の `WRITE_TOOLS`）
- 再起動は `memory_data/active_runs.json` が空のときだけ（作業中の依頼が切れるため）

## 構成と流れ

```
Slack → app.py（Bolt AsyncApp, Socket Mode）
      → handlers/message_handler.py（受信・まとめ・ボタン）
      → utils/claude_pty_engine.py（既定: Claude Code の対話画面を裏で操作）/ utils/claude_engine.py（SDK）
          ├ prompts/persona.txt ＋ 選ばれたスキル（skills_content/）を system prompt に
          ├ MCP: mcp_agent_tools/server.py（メモ・リマインダー・スキル管理・PDF 編集…）
          └ 実行前フック: scripts/hooks/guard_tools.py（config/claude_guard_settings.json）
      → 返信・成果物の添付
```

- 設定: `config/settings.py`（.env から）、モデル: `config/models.py`
- 記憶・状態: `memory_data/`（gitignore）。会話の正本は Slack のスレッドで、毎回読み直す

## 開発

```bash
uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python -r requirements.txt
git config core.hooksPath scripts/git-hooks
.venv/bin/python -m pytest -q tests      # 変更したら必ず
python3 scripts/check_secrets.py
```

- 版は `requirements.txt` で固定。上げるときはテストを回す（mcp 2 系は作りが変わるので注意）
- 業務の連携を足すときは `SECURITY.md` の「連携を足すとき」に従う。`.env.example` に名前だけ足す
- 日本語のコメント・docstring、async/await で統一、`logging` を使う

## ドキュメント

- `docs/SETUP_MAC.md` 動かす Mac の準備と常駐
- `docs/CLAUDE_ACCOUNT_SWITCH.md` Claude Max の付け替え
- `CHANGELOG.md` 変更履歴

## 変更ログ

`CHANGELOG.md` に書く。
