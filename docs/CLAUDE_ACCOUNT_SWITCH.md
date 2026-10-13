# Claude のアカウントをつなぐ・付け替える

エージェントがどの Claude アカウント（どの Max 契約）で動くかは、次の **2つだけ** で決まる。コードには書かれていない。

1. `.env` の `CLAUDE_CODE_OAUTH_TOKEN`（`claude setup-token` で発行する長期トークン。こちらが優先）
2. 無い時は、動かしている Mac ユーザーの Claude Code ログイン（`CLAUDE_CONFIG_DIR`、既定 `~/.claude` とキーチェーン）

なので、付け替え = 「新しいアカウントでトークンを発行し、`.env` を差し替えて再起動」。

> 注意: 個人のサブスクを複数人で使う社内ボットに使う場合は、プランの利用規約を確認すること。
> 会社で使うなら会社名義で契約し、規模が大きくなったら Team / Enterprise プランか API（従量）への切り替えを検討する。

## 初回

エージェント用の Mac ユーザーで行う。

```bash
cd ~/slack-claude-agent
# 同梱の claude CLI の場所
CLAUDE=$(.venv/bin/python -c 'import claude_agent_sdk,os;print(os.path.join(os.path.dirname(claude_agent_sdk.__file__),"_bundled","claude"))')
"$CLAUDE" setup-token
```

1. ブラウザが開くので、**エージェントに使う Claude アカウント**でログインして許可する
   （別のアカウントでログイン済みのブラウザなら、先にログアウトする）
2. 画面に出たトークンを `.env` の `CLAUDE_CODE_OAUTH_TOKEN=` に入れる（チャットや Slack に貼らない）
3. 起動して、Slack でメンションして返事が来ることを確認

トークンの有効期限は 1 年。期限前に同じ手順で発行し直す（カレンダーに入れておく）。

## 付け替え（例: 立ち上げ時に借りていた契約 → 会社名義の契約）

1. 新しいアカウントで「初回」の 1〜2 を行い、新しいトークンを用意する
2. 作業中の依頼が無いことを確かめて再起動する（`memory_data/active_runs.json` が `{}`）

   ```bash
   python3 -c 'import json,sys; d=json.load(open("memory_data/active_runs.json")); sys.exit(1 if d else 0)' \
     && launchctl kickstart -k gui/$(id -u)/com.<AGENT_ID>.agent \
     || echo "作業中の依頼があるので、終わってから再起動してください"
   ```
3. 確認
   - `logs/agent.log` に認証エラー（401 / invalid token / login）が出ていない
   - Slack でメンションして返事が来る
   - 新しいアカウントの claude.ai 「設定 → 使用量」で使用量が増えている
4. 古いアカウント側で、古いトークンを失効させる（claude.ai の設定 → Claude Code の接続を解除）

## 同じ Mac で別のアカウントを分けたい時

`.env` の `CLAUDE_CONFIG_DIR` に専用フォルダ（例: `~/.claude-my-agent`）を指定すると、
Claude Code の設定・ログインがそのフォルダに分かれる。基本は「専用の Mac ユーザー」で分ける方が確実。

## 戻し方

`.env` の `CLAUDE_CODE_OAUTH_TOKEN` を前の値に戻して、上と同じ手順で再起動する。
