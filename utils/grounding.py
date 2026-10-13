"""接地レジャー（2026-07-16 ハーネス改善・軸3=ハルシネーション防止のコード側担保）

「応答が数値の事実主張を含むか」×「その根拠となるデータ系ツール呼び出しが
実際にあったか」をコードで突合する。プロンプト（persona「事実の接地」節）の
遵守を裏から監視する層3。

Stage 0（現在）: ログのみ（誤検知率を実測してから enforce を判断）
Stage 1（環境変数 AGENT_GROUNDING_ENFORCE=1）: 数値主張あり×データ未参照のとき
  応答末尾に注意書きを1行コードが付ける（モデルの自己申告に頼らない）
"""

import logging
import os
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# 数値＋単位の事実主張（日本語ビジネス文脈）
NUMERIC_CLAIM = re.compile(
    r"\d[\d,，.]*\s*(件|円|%|％|駅|本|人|回|カ所|か所|箇所|台|億|万|社|店舗|スポット)"
)

# 「実データに当たった」とみなすツール名（部分一致）
DATA_TOOLS = (
    "gsk_web_search", "gsk_deep_research", "gsk_crawl",
    "gdrive", "gsheet", "sheets", "bigquery", "notion",
    "fetch_slack", "slack_search", "Read", "WebFetch", "web_search",
)

_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_DATE_TIME_RE = re.compile(r"\d{1,4}[年月日時分秒/:]")


@dataclass(frozen=True)
class GroundingVerdict:
    has_claims: bool
    grounded: bool

    @property
    def suspicious(self) -> bool:
        return self.has_claims and not self.grounded


def check_grounding(text: str, tool_ledger: set) -> GroundingVerdict:
    """応答テキストとツール台帳を突合する"""
    if not text:
        return GroundingVerdict(False, False)
    # コードブロックは除外（SQL例・コード内の数字は事実主張ではない）
    stripped = _CODE_BLOCK_RE.sub("", text)
    has_claims = bool(NUMERIC_CLAIM.search(stripped))
    grounded = any(
        d.lower() in t.lower() for t in (tool_ledger or set()) for d in DATA_TOOLS
    )
    return GroundingVerdict(has_claims, grounded)


UNGROUNDED_NOTE = (
    "\n\n_※上の数字はエージェントの記憶ベースで、今回は実データを参照していません。"
    "正確な値が必要なら、元のデータで確認しますね_"
)


def apply_grounding_check(text: str, tool_ledger: set) -> str:
    """接地チェックを適用。Stage 0=ログのみ、AGENT_GROUNDING_ENFORCE=1で注記付与"""
    verdict = check_grounding(text, tool_ledger)
    if verdict.has_claims:
        logger.info(
            f"[Grounding] claims=True grounded={verdict.grounded} "
            f"tools={sorted(tool_ledger or set())[:8]}"
        )
    if verdict.suspicious and os.getenv("AGENT_GROUNDING_ENFORCE", "0") == "1":
        # 既に推定と明記している応答には付けない
        if not re.search(r"推定|概算|未確認|参考値", text):
            return text + UNGROUNDED_NOTE
    return text
