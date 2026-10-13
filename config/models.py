"""モデルIDの中央管理（2026-07-16 ハーネス改善で導入）

方針:
- 全コードはここの定数を参照する。モデルIDの直書き禁止。
- env で個別上書き可能（ロールバック手段を常に残す）。
- 世代更新はこのファイル1箇所の変更で完結させる。

tier の使い分け:
  LIGHT   : 相槌・軽い判定・サイドチャネルの要約など（最安・最速）
  MEDIUM  : メンション応答のデフォルト（Sonnet 5.5 = ほぼOpus級・安quota・1Mコンテキスト）
  HEAVY   : データ分析・長尺成果物・CMO個人投稿など明示トリガーのみ
  PREFLIGHT: 前処理プランナー（スキル選定+意図エコー+tierヒント+曖昧度）
  UTILITY : スケジューラ等サイドチャネルの軽量タスク（= LIGHT と同値、意味を分けて管理）
"""

import os

LIGHT = os.getenv("CLAUDE_MODEL_LIGHT", "claude-haiku-4-5-20251001")
# 2026-09-29: Sonnet 5.5 へ更新（Sonnet 5 と同価格 $2/$10・30%超高速・1M context）。
# 要件: Claude Code 2.1.284 以降（2.1.281 は未知モデル扱いで文脈を200Kに制限する）。
MEDIUM = os.getenv("CLAUDE_MODEL_MEDIUM", "claude-sonnet-5-5")
# 2026-09-24: Opus 5.5 へ更新（$4/$20 per MTok = Opus 5 の $5/$25 より安価、1M context は同じ）。
# 要件: Claude Code 2.1.280 以降。古い CLI では API 400 で弾かれるので CLAUDE_BIN と対で運用する。
HEAVY = os.getenv("CLAUDE_MODEL_HEAVY", "claude-opus-5-5")

# 前処理プランナー: スキル誤選定のコストが高いので MEDIUM（Sonnet 5.5）
PREFLIGHT = os.getenv("CLAUDE_MODEL_PREFLIGHT", MEDIUM)

# サイドチャネル（スケジューラ・ツール内の軽量LLM呼び出し）
UTILITY = os.getenv("CLAUDE_MODEL_UTILITY", LIGHT)

# 自発介入の価値判定（既存 intervention tier の後継）
INTERVENTION = os.getenv("CLAUDE_MODEL_INTERVENTION", MEDIUM)
