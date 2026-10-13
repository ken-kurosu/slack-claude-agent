"""
設定管理モジュール
環境変数（.env）から Slack・モデル・エージェントの設定を読み込む。秘密情報はコードに書かない。
"""
import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv
from config import models

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _csv(name: str) -> List[str]:
    return [v.strip() for v in os.getenv(name, "").split(",") if v.strip()]


@dataclass
class Settings:
    # エージェントの名前（Slack の表示・ペルソナで使う。未定なので仮の名前）
    agent_name: str = "Agent"
    # 同じ機械の他のエージェントと取り違えないための識別子。ロック・作業フォルダ・常駐の登録名に使う
    agent_id: str = "slack-agent"

    # Slack設定
    slack_bot_token: str = ""
    slack_app_token: str = ""  # Socket Mode用
    slack_signing_secret: str = ""

    # モデル（config/models.py の既定を環境変数で上書きできる）
    claude_model: str = models.LIGHT  # Light（挨拶・簡単なQ&A）
    claude_model_medium: str = models.MEDIUM  # Medium（ツール使用・通常業務）
    claude_model_heavy: str = models.HEAVY  # Heavy（分析・リサーチ・資料作成）
    intervention_model: str = models.INTERVENTION  # 介入判断専用モデル
    # claude_pty: Claude Code の対話画面を裏で操作（Claude Max のサブスク枠で動く）
    # claude_code: Agent SDK / CLI 経由
    ai_engine_type: str = "claude_pty"

    # 管理者（危険な操作・設定変更を許す Slack ユーザーID）。コードに書かず環境変数で渡す
    admin_user_ids: List[str] = field(default_factory=list)

    # Bot動作設定
    bot_user_id: str = ""  # 起動時に自動取得
    proactive_channels: List[str] = field(default_factory=list)  # 自発的に見るチャンネル（空なら見ない）
    proactive_keywords: List[str] = field(default_factory=lambda: [
        "画像", "リサイズ", "翻訳", "要約", "調べて", "教えて",
        "作って", "変換", "修正", "レビュー", "分析",
        "image", "resize", "translate", "summarize", "help",
    ])
    response_language: str = "ja"  # デフォルト応答言語

    # ファイル保存先（作業用）と永続データ（記憶・セッション）
    workspace_dir: str = "/tmp/slack-agent-workspace"
    data_dir: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        """環境変数から設定を読み込む"""
        load_dotenv(os.path.join(PROJECT_ROOT, ".env"), override=True)
        agent_id = os.getenv("AGENT_ID", "slack-agent")
        return cls(
            agent_name=os.getenv("AGENT_NAME", "Agent"),
            agent_id=agent_id,
            slack_bot_token=os.environ["SLACK_BOT_TOKEN"],
            slack_app_token=os.environ["SLACK_APP_TOKEN"],
            slack_signing_secret=os.getenv("SLACK_SIGNING_SECRET", ""),
            claude_model=os.getenv("CLAUDE_MODEL", "") or models.LIGHT,
            claude_model_medium=os.getenv("CLAUDE_MODEL_MEDIUM", "") or models.MEDIUM,
            claude_model_heavy=os.getenv("CLAUDE_MODEL_HEAVY", "") or models.HEAVY,
            intervention_model=os.getenv("INTERVENTION_MODEL", "") or models.INTERVENTION,
            ai_engine_type=os.getenv("AI_ENGINE_TYPE", "claude_pty"),
            admin_user_ids=_csv("ADMIN_USER_IDS"),
            proactive_channels=_csv("PROACTIVE_CHANNELS"),
            workspace_dir=os.getenv("WORKSPACE_DIR", f"/tmp/{agent_id}-workspace"),
            data_dir=os.getenv("MEMORY_DIR", os.path.join(PROJECT_ROOT, "memory_data")),
        )
