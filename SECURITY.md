# セキュリティ

## 脆弱性の報告

公開の Issue には書かず、GitHub の **Security → Report a vulnerability**（非公開の報告）から知らせてください。

## 運用するときの決まり（おすすめ）

### 分ける

- リポジトリ（fork）・動かす Mac・Slack アプリ・Claude アカウント・連携先は、運用する組織の名義で用意する
- 別の組織・用途のエージェントやデータと、同じ機械・同じリポジトリで兼用しない
- 公開リポジトリから fork して社内用に変更する場合、社内の設定・スキル・人格は非公開の fork に置く

### 秘密情報

- トークン・鍵は `.env` にだけ置く（`chmod 600 .env`）。`.env`・`.mcp.json`・`credentials/` はコミットしない（`.gitignore` 済み）
- チャット・Slack・ドキュメント・Issue にトークンを貼らない
- 守りは3段
  1. コミット前フック `scripts/git-hooks/pre-commit`（`git config core.hooksPath scripts/git-hooks`）
  2. CI（`scripts/check_secrets.py` ＋ gitleaks で全履歴を検査）
  3. ログの伏せ字（`utils/log_redact.py`。トークンらしい文字列はログに出る前に伏せる）
- 漏れたかもしれない時は、まず**その鍵を失効・再発行**する（履歴から消すより先に）
- 公開リポジトリに社名・人名・社内 ID を混ぜたくない時は、`tests/forbidden_words.local.txt`（gitignore 済み・1行1語）に書くとテストで検出できる。CI では Secrets の `FORBIDDEN_WORDS`（カンマ区切り）で渡す

## エージェントの守り

- Claude は サブスクの枠で Claude Code を通して使う。`ANTHROPIC_API_KEY` は使わない・置かない
- Claude Code の各操作の前に `scripts/hooks/guard_tools.py` が動き、次を止める
  - 秘密情報の読み出し（`.env`、`~/.claude`、キーチェーン、`printenv` など）: 誰の依頼でも禁止
  - フック・ガード設定そのものの書き換え: 誰の依頼でも禁止
  - 破壊的な操作（強制 push・大量削除など）: `ADMIN_USER_IDS` の人の依頼だけ許可
- 止めた記録は `logs/guard.log`
- 外部への送信・書き込み（DM 送信・Canvas 作成など）は、実行前に承認ボタンを出す。押せるのは依頼した本人
- メッセージやファイルに書かれた「指示」はデータとして扱い、従わない（`prompts/persona.txt` に明記）
- Slack アプリは招待されたチャンネルしか読めない。機密のチャンネルには招待しない

## 連携を足すとき

- 読み取り専用から始める。書き込みは承認ボタンを通す
- 他人のメール・予定・ファイルを読む連携は、誰の分を誰が見られるかを先に決めて、コードで制限する
- 連携用の鍵はその連携専用に発行し、`.env` に入れる。`.env.example` には名前だけ足す
