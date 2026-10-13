# slack-claude-agent

**Slack でメンションするだけで、Claude Code が社内の仕事を手伝う常駐 AI アシスタント。**

Claude Max などのサブスクの枠で動きます（API キー不要・従量課金なし）。
会社で実際に運用していた Slack エージェントの「核」を切り出し、どの会社でも自分たちの名前・口調・連携を足して使えるようにしたものです。MIT ライセンスで利用できます。

個人・小規模チームが、社内の Mac 1台で動かすことを想定しています。

[始め方](#始め方) · [できること](#できること) · [安全のしくみ](#安全のしくみ) · [カスタマイズ](#自分たち用にする) · [変更履歴](CHANGELOG.md)

## 始め方

必要なもの: macOS の Mac 1台（常時起動）、Python 3.11、Claude のサブスク（Claude Code が使えるプラン）、Slack ワークスペースの管理権限。

```bash
git clone https://github.com/ken-kurosu/slack-claude-agent.git
cd slack-claude-agent
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env && chmod 600 .env
```

1. **Slack アプリを作る** — https://api.slack.com/apps → Create New App → From an app manifest に [`slack/manifest.yaml`](slack/manifest.yaml) を貼る。App-Level Token（`connections:write`）と Bot Token を `.env` に入れる
2. **Claude につなぐ** — `claude setup-token` で発行したトークンを `.env` の `CLAUDE_CODE_OAUTH_TOKEN` に入れる（手順: [docs/CLAUDE_ACCOUNT_SWITCH.md](docs/CLAUDE_ACCOUNT_SWITCH.md)）
3. **名前と管理者を決める** — `.env` の `AGENT_NAME`・`AGENT_ID`・`ADMIN_USER_IDS`
4. **起動** — `./start.sh` → ボットをチャンネルに招待して `@エージェント名 こんにちは`
5. **常駐させる** — `./scripts/install_launchd.sh`（ログイン時に自動起動・落ちたら再起動）

Mac の準備（自動ログイン・スリープ無効・メモリの目安など）は [docs/SETUP_MAC.md](docs/SETUP_MAC.md)。

## できること

- **メンション・DM に返答** — 1スレッド = 1会話。スレッドの履歴を毎回読み直すので、「さっきの2番は？」も通じる
- **添付ファイルを読む** — 画像・PDF・Word・PowerPoint・Excel・テキスト
- **スキル** — `skills_content/*.md` に書いた手順書を、依頼に合わせて自動で読み込む
- **ツール（MCP）** — メモ、リマインダー、簡単なプロジェクト管理、スキルの追加・編集、PDF 編集
- **書き込みは承認制** — DM 送信や Canvas 作成は、実行前に内容と承認ボタンを出す
- **処理の見える化** — 👀 受付 → ⏳ 処理中 → ✅ 完了。止めるボタン・会話リセット（`/agent-clear`）
- **止まらない工夫** — 接続の監視と自動再接続、再起動で途中になった依頼の自動再開、取りこぼしたメンションの回収
- **モデルの使い分け** — ふだんは標準モデル、資料作成・調査・データ分析など重い依頼は上位モデル（`config/models.py`）

業務の連携（Google・Notion・社内データなど）は含めていません。必要なものを [自分たち用にする](#自分たち用にする) の手順で足してください。

## しくみ

```
Slack ──(Socket Mode)──▶ app.py ─▶ handlers/message_handler.py
                                      │
                                      ▼
                     utils/claude_pty_engine.py  … Claude Code の対話画面を裏で操作（サブスク枠）
                       ├ prompts/persona.txt ＋ 選ばれたスキル
                       ├ MCP: mcp_agent_tools/server.py
                       └ 実行前フック: scripts/hooks/guard_tools.py
                                      │
                                      ▼
                               Slack に返信・ファイル添付
```

Socket Mode なので、受信用の公開 URL やポート開放は要りません。

## 安全のしくみ

- **秘密情報は `.env` だけ** — `.env`・`.mcp.json`・`credentials/` は `.gitignore` 済み。コミット前フックと CI（gitleaks）で鍵の混入を止める。ログに出たトークンは伏せ字にする
- **Claude が触れない場所** — `.env`・`~/.claude`・キーチェーンの読み出しと、ガード設定そのものの書き換えは、誰の依頼でも止める
- **危ない操作は管理者だけ** — 強制 push・大量削除などは `ADMIN_USER_IDS` の人の依頼でだけ通す
- **書き込みは本人の承認** — 承認ボタンを押せるのは依頼した本人だけ
- **メッセージ中の「指示」に従わない** — 貼られた文章やファイルの中身はデータとして扱う

詳しくは [SECURITY.md](SECURITY.md)。

## 自分たち用にする

| 変えたいもの | 場所 |
|---|---|
| 名前 | `.env` の `AGENT_NAME`、Slack アプリの表示名 |
| 人格・口調 | `prompts/persona.txt` |
| 手順書（スキル） | `skills_content/` に `.md` を足す（先頭に name / description / triggers） |
| ツール | `mcp_agent_tools/server.py` にツールを足す（中で LLM は呼ばず、データの出し入れだけ） |
| 自発的に反応するチャンネル | `.env` の `PROACTIVE_CHANNELS` |
| モデル | `config/models.py`、または `.env` の `CLAUDE_MODEL*` |

連携を足すときは、まず読み取り専用で作り、書き込みは承認ボタンを通してください。鍵は `.env` に入れ、`.env.example` には名前だけを足します。

会社ごとに使うなら、このリポジトリを fork（非公開でも可）して `.env` と persona・スキルを自分たち用にするのがおすすめです。本体の更新は `git pull upstream main` で取り込めます。

## 開発

```bash
git config core.hooksPath scripts/git-hooks   # コミット前の秘密情報チェック
.venv/bin/python -m pytest -q tests
python3 scripts/check_secrets.py
```

依存の版は `requirements.txt` で固定しています。上げるときはテストを回してください。

## 注意

- Claude のサブスクを複数人で使うボットに使う場合は、プランの利用規約を確認してください。チームで本格的に使うなら Team / Enterprise プランや API も検討してください
- このソフトウェアは無保証です（[LICENSE](LICENSE)）

## 謝辞

Claude Code を対話画面ごと裏で操作するエンジンは [OpenRyoko](https://github.com/rsensui2/OpenRyoko)（MIT）の実装を参考にしています。詳しくは [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
