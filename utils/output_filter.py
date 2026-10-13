"""
エージェント 出力フィルタ — Slack 露出物の一元管理。

役割:
1. 推論ログ（thinking/メタ独白）を Slack に出すかの判定
2. 最終応答の先頭にある英語メタ独白を除去するフェイルセーフ
3. Slack 添付ファイルの拡張子ホワイトリスト

設計方針（2026-05-29 hotfix）:
- 本番デフォルトは「ユーザー向け最終応答 + 成果物バイナリのみ」
- 推論可視化は AGENT_SHOW_REASONING=true 環境変数で明示的にON
- テキスト系拡張子（.md/.txt/.json/.html/.py/.log/.yaml）はデフォルト不可
"""
import os
import re
import logging

logger = logging.getLogger(__name__)


# ── 推論可視化スイッチ ──
def show_reasoning() -> bool:
    """thinking / メタ独白を Slack に出すかどうか（本番デフォルト false）

    デバッグ時は環境変数 AGENT_SHOW_REASONING=true で有効化。
    """
    return os.getenv("AGENT_SHOW_REASONING", "false").strip().lower() in ("true", "1", "yes", "on")


# ── 英語メタ独白の除去（最終応答のフェイルセーフ） ──
# Claude が日本語応答の前に "Let me format..." "The user is..." 等の独白を残すケースを除去
_META_PARA_RE = re.compile(
    r"^(?:Let me\b|Let's\b|Now I\b|Now,? I\b|I'll\b|I should\b|I need to\b|"
    r"I can\b|I'm\b|I am\b|First,?\s+let\b|First,?\s+I\b|"
    r"No response requested\b|No reply (?:needed|required)\b|"
    r"The user\b|The issue\b|The response\b|"
    r"Since I'm\b|Since the\b|Since we\b|"
    r"Looking at\b|Let me check\b|Let me look\b)",
    re.IGNORECASE,
)


def _japanese_char_count(text: str) -> int:
    """ひらがな・カタカナ・漢字の文字数"""
    return sum(
        1 for ch in text
        if ("぀" <= ch <= "ゟ")  # ひらがな
        or ("゠" <= ch <= "ヿ")  # カタカナ
        or ("一" <= ch <= "鿿")  # 漢字
    )


# 行単位で除去する既知のメタ独白（回答本文に紛れても消す。2026-07-22）
# 例: モデルが先頭に "No response requested." を出す事故が観測された
_META_LINE_RE = re.compile(
    r"^\s*(no response requested\.?|"
    r"let me\b.*|i'?ll\b.*|i will\b.*|the user\b.*|okay,?\s*let.*|"
    r"i need to\b.*|i should\b.*)\s*$",
    re.IGNORECASE,
)


def strip_meta_lines(text: str) -> str:
    """英語メタ独白の「行」を除去する（段落でなく行単位）。

    strip_leading_english_meta は段落(空行区切り)単位のため、単一改行で
    日本語と同段落になったメタ行を取りこぼす。これはその補完。
    AGENT_SHOW_REASONING=true の時は何もしない。

    全行がメタだった場合は空文字を返す（原文フォールバックはしない）。
    以前は安全側のつもりで原文を返していたが、"No response requested." だけの
    応答がそのまま Slack に出る事故になった（2026-08-25/27）。
    """
    if not text or show_reasoning():
        return text
    lines = text.split("\n")
    kept = [ln for ln in lines if not _META_LINE_RE.match(ln)]
    return "\n".join(kept).strip()


def strip_leading_english_meta(text: str) -> str:
    """応答の先頭にある英語メタ独白を段落単位で除去。

    - 1段落ずつ見て、日本語が十分含まれていればそこで停止
    - 段落が英語メタパターンで始まればその段落を削除
    - 最大5段落まで削除（暴走防止）
    - 全消えする場合は元のテキストを返す（安全側）

    AGENT_SHOW_REASONING=true の時は何もしない。
    """
    if not text or show_reasoning():
        return text

    out = text
    for _ in range(5):
        stripped = out.lstrip()
        if not stripped:
            break
        # 次の段落区切り（空行）まで
        idx = stripped.find("\n\n")
        para = stripped[:idx] if idx >= 0 else stripped

        # 日本語が一定量含まれていれば「これは最終応答だ」と判定して止める
        if _japanese_char_count(para) >= 5:
            break

        # 英語メタパターンに該当すれば削除
        if _META_PARA_RE.match(para.lstrip()):
            out = stripped[idx + 2:] if idx >= 0 else ""
            logger.info(f"[OutputFilter] Stripped leading meta paragraph: {para[:80]!r}")
            continue

        break

    return out if out.strip() else text


# ── Slack 添付ホワイトリスト ──
# ユーザー要件: pptx/pdf/png/jpg/jpeg/xlsx/csv/mp4 を許可、それ以外（特に .md/.txt/.json）は不可
DELIVERABLE_EXTS = frozenset({
    ".pptx", ".pdf", ".xlsx", ".docx",
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".mp3", ".mp4", ".wav", ".m4a", ".mov",
    ".zip", ".csv",
})

# 拒否を明示するための一覧（ログ判定用、実装的には DELIVERABLE_EXTS の補集合で十分）
EXPLICITLY_BLOCKED_EXTS = frozenset({
    ".md", ".txt", ".json", ".yaml", ".yml", ".html", ".xml",
    ".py", ".js", ".ts", ".sh", ".log", ".env",
})


def is_attachment_allowed(filepath: str, allow_explicit: bool = False) -> bool:
    """Slack に添付してよいファイルかどうか。

    Args:
        filepath: 判定対象のファイルパス
        allow_explicit: True ならホワイトリストをバイパス（ユーザーが明示的に
            「.md で出して」等と指示した場合のみ True）

    Returns:
        True ならアップロード可
    """
    if not filepath or not isinstance(filepath, str):
        return False

    base = os.path.basename(filepath)
    # SDK が吐く tool-result spillover ファイル
    if base.startswith("mcp-agent-tools-") or base.startswith("mcp__"):
        return False
    # 読むためだけに落としたスレッドの添付画像（入力）。成果物ではないので貼らない
    # （2026-10-08: スレッド元の写真が回答の後ろに3枚再投稿された）
    if "/memory_data/thread_images/" in filepath:
        return False

    if allow_explicit:
        return True

    ext = os.path.splitext(filepath)[1].lower()
    # 動くスライド（motion_slides）の完成HTMLだけは成果物として添付する（原稿 source.v*.html は除く）
    if ext == ".html" and "/memory_data/motion_slides/" in filepath and not base.startswith("source.v"):
        return True
    return ext in DELIVERABLE_EXTS
