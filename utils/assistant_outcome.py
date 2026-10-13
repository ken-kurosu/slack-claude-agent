"""応答アウトカムの構造化パーサ（2026-07-16 ハーネス改善）

1) <agent_clarify> JSON envelope のパース
   モデルが「解釈の分岐が高価」と判断した時だけ応答末尾に付ける確認ブロック。
   生テキストマーカー（[[CLARIFY]]等）はパース失敗時にユーザーへ漏れるため、
   終端センチネル + JSON + fail-open（壊れていたら黙って除去）方式を採用。

2) ClarificationStore
   確認ボタンの保留状態を memory_data/clarify_pending.json に永続化。
   ボタンvalueには選択肢本文を入れず opaque ID のみ（Slack 2000字制限＋改ざん対策）。

3) forbidden claims（既知の未検証数値のコード側ネット）
   personaで禁止していてもモデルが出してしまった場合の最後の安全網。
"""

import json
import logging
import os
import re
import time
import uuid
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# ── <agent_clarify> envelope ──────────────────────────

_CLARIFY_RE = re.compile(
    r"<agent_clarify>\s*(\{.*?\})\s*</agent_clarify>", re.DOTALL
)


def parse_clarify(text: str) -> Tuple[str, Optional[dict]]:
    """応答テキストから <agent_clarify> ブロックを抽出する。

    戻り値: (ブロックを除去した本文, clarifyデータ or None)
    - JSONが壊れている場合はブロックだけ黙って除去し None を返す（fail-open。
      生マーカーを絶対にユーザーに見せない）
    - options が2件未満/5件以上の場合も不正として除去
    """
    if not text or "<agent_clarify>" not in text:
        return text, None
    m = _CLARIFY_RE.search(text)
    if not m:
        # 開始タグはあるが構造が壊れている → タグ以降を除去
        cleaned = text.split("<agent_clarify>")[0].rstrip()
        logger.warning("[Clarify] malformed envelope (no close/JSON) — stripped")
        return cleaned, None
    cleaned = (text[:m.start()] + text[m.end():]).strip()
    try:
        data = json.loads(m.group(1))
        question = str(data.get("question") or "").strip()
        raw_options = data.get("options") or []
        options = []
        for o in raw_options[:4]:
            if isinstance(o, dict):
                oid = str(o.get("id") or f"opt{len(options)}")[:32]
                label = str(o.get("label") or "")[:70]
                full = str(o.get("text") or o.get("label") or "")[:500]
            else:
                oid = f"opt{len(options)}"
                label = str(o)[:70]
                full = str(o)[:500]
            if label:
                options.append({"id": oid, "label": label, "text": full})
        if question and len(options) >= 2:
            return cleaned, {"question": question, "options": options}
        logger.warning("[Clarify] invalid clarify payload (question/options) — stripped")
        return cleaned, None
    except Exception as e:
        logger.warning(f"[Clarify] JSON parse failed — stripped: {e}")
        return cleaned, None


# ── 保留Clarificationの永続ストア ──────────────────────

_TTL_SECONDS = 24 * 3600


class ClarificationStore:
    def __init__(self, persist_path: Optional[str] = None):
        memory_dir = os.getenv(
            "MEMORY_DIR",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "memory_data"),
        )
        self.path = persist_path or os.path.join(memory_dir, "clarify_pending.json")
        self.data = {}
        self._load()

    def _load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
        except Exception:
            logger.exception("[Clarify] store load failed — starting empty")
            self.data = {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception:
            logger.exception("[Clarify] store save failed")

    def create(self, *, channel: str, thread_ts: str, requester: str,
               original_request: str, question: str, options: list) -> str:
        cid = f"cl_{uuid.uuid4().hex[:12]}"
        # 期限切れエントリの掃除
        now = time.time()
        self.data = {k: v for k, v in self.data.items()
                     if now - v.get("created", 0) < _TTL_SECONDS}
        self.data[cid] = {
            "channel": channel,
            "thread_ts": thread_ts,
            "requester": requester,
            "original_request": original_request[:2000],
            "question": question,
            "options": {o["id"]: o for o in options},
            "created": now,
            "consumed": False,
        }
        self._save()
        return cid

    def consume(self, cid: str, option_id: str, clicked_by: str) -> Optional[dict]:
        """検証つき一回消費。成功時は {entry, option} を返し、失敗時 None"""
        entry = self.data.get(cid)
        if not entry:
            return None
        if entry.get("consumed"):
            return None  # 二重クリックの冪等化
        if time.time() - entry.get("created", 0) > _TTL_SECONDS:
            return None
        if clicked_by != entry.get("requester"):
            logger.info(f"[Clarify] click by non-requester {clicked_by} — ignored")
            return None
        option = (entry.get("options") or {}).get(option_id)
        if not option:
            return None
        entry["consumed"] = True
        self._save()
        return {"entry": entry, "option": option}


# ── forbidden claims（未検証数値の最終ネット） ──────────────

# (pattern, 置換文字列, 理由)
_FORBIDDEN_CLAIMS: list = [
    # (re.compile(r"出典のない数値のパターン"), "置き換える文字列", "理由")
    # 業務で「使ってはいけない古い数字・誤った主張」が分かったらここに足す（返答の最終安全網）
]


def apply_forbidden_claims(text: str) -> str:
    """既知の未検証数値主張をコード側で置換する（personaルールの最終安全網）"""
    if not text:
        return text
    for pattern, replacement, reason in _FORBIDDEN_CLAIMS:
        if pattern.search(text):
            logger.warning(f"[ForbiddenClaim] blocked: {reason}")
            text = pattern.sub(replacement, text)
    return text
