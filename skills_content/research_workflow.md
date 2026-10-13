---
name: research-workflow
description: Web検索やDeep Researchでリサーチを実施する。「調べて」「リサーチして」等の依頼時に使用。
capabilities: 調査深度確認, Deep Research実行, 出典整理, 成果物形式調整, 調査範囲整理
limitations: 根拠なし断定, Slack長文べた貼り, 現地調査
---

## リサーチ依頼時のヒアリングフロー

ユーザーが「調べて」「リサーチして」と言った場合、まず以下を確認:

a) *調査の深さ*: クイック検索（数秒）か、Deep Research（数分）か？
b) *アウトプット形式*: Slackに投稿？ CSV/データファイル？ PDF？ スライド資料？
c) *調査の範囲・件数*: 何件くらい？ どこまで深く？

Deep Researchモード時:
- `gsk_deep_research` ツールを使用（Perplexity sonar-deep-research）
- 数分かかるため進捗を伝える
- 結果は必ず出典URL付きで整理する
- アウトプットはCSV/データファイル、PDF、Word・PowerPoint・Excel ファイルのいずれかで仕上げる（Slackべた貼りはNG）
