"""能力マップのローダ（2026-07-16 ハーネス改善）

config/capabilities.yaml を読み、
- PreFlight用の圧縮カタログ（compact_prompt）
- id引き（get） … LLMの capability_id 自己申告を「実在するidのみ有効」で検証する
を提供する。層3（コード検証）の一部: LLMが根拠なく「できない」と言い出すのを
id実在チェックで決定的に抑止する。fail-open（読めなければ空マップ）。
"""

import logging
import os
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_YAML_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "config", "capabilities.yaml"
)


@dataclass(frozen=True)
class Capability:
    id: str
    can: object  # False | "partial"
    claim: str
    alternative: str = ""
    condition: str = ""


class CapabilityMap:
    def __init__(self, path: Optional[str] = None):
        self.path = path or _YAML_PATH
        self._by_id: Dict[str, Capability] = {}
        self._load()

    def _load(self):
        try:
            import yaml  # python-frontmatter 依存で PyYAML は既に入っている
            with open(self.path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or []
            for item in raw:
                cap = Capability(
                    id=str(item.get("id", "")),
                    can=item.get("can", False),
                    claim=str(item.get("claim", "")),
                    alternative=str(item.get("alternative", "")),
                    condition=str(item.get("condition", "")),
                )
                if cap.id and cap.claim:
                    self._by_id[cap.id] = cap
            logger.info(f"[CapabilityMap] loaded {len(self._by_id)} entries")
        except Exception:
            logger.exception("[CapabilityMap] load failed — 空マップで継続 (fail-open)")
            self._by_id = {}

    def get(self, cap_id: str) -> Optional[Capability]:
        return self._by_id.get(cap_id or "")

    def compact_prompt(self) -> str:
        """PreFlightプロンプト用の圧縮カタログ（~600字）"""
        if not self._by_id:
            return ""
        lines = []
        for cap in self._by_id.values():
            mark = "✕" if cap.can is False else "△"
            lines.append(f"- {cap.id} [{mark}]: {cap.claim}")
        return "\n".join(lines)

    def injection_note(self, cap_id: str) -> str:
        """blocked/partial時にsystem promptへ注入する確定情報"""
        cap = self.get(cap_id)
        if not cap:
            return ""
        if cap.can is False:
            return (
                f"\n\n## 能力マップ判定（コード側の確定情報 — 変更・推測禁止）\n"
                f"この依頼は「{cap.claim}」を含むが、このエージェントにはこれが *できない*。\n"
                f"ごまかしたり黙って失敗したりせず、「Xはできない。理由と代わりにYができる」の順で"
                f"正直に伝え、必ず次の代替案を提示すること: {cap.alternative}\n"
                f"代替案が依頼者の目的を満たすなら、そのまま代替案を実行してよい。"
            )
        return (
            f"\n\n## 能力マップ判定（コード側の確定情報 — 変更・推測禁止）\n"
            f"この依頼の「{cap.claim}」は *条件付きでのみ* 可能: {cap.condition}\n"
            f"この条件を依頼者に明示した上で、可能な範囲を実行すること。"
        )
