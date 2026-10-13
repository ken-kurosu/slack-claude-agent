"""
エージェント session store — bot restart 越しに会話セッションを保持する JSON checkpoint。

設計（2026-05-29 P2-2）:
- claude_engine の `_thread_sessions` (in-memory dict) を外部 JSON に同期
- bot 起動時に load() で復元、session_id ベースの会話継続を可能に
- 各書き込みごとに dump（少ない write 頻度なので flush 同期で OK）
- 永続化先は memory_data/sessions.json（durable）。/tmp は macOS の定期クリーンアップで
  消えるため使わない（2026-04-12 の memory.json /tmp 消失と同じ罠を回避）
- 会話の正本は Slack スレッド本体（claude_engine の _fetch_thread_messages が毎ターン
  履歴を再注入する）。SDK セッションの resume はその上の高速化レイヤなので、TTL は長め
  （既定14日, env AGENT_SESSION_TTL_HOURS で調整可）に取り、期限切れ・再起動・セッション
  衝突時は Slack 履歴注入で文脈を復元する

保存形式:
    {
        "thread_C0AHPS4RQ5A_1736666666.000": {
            "session_id": "uuid-xxx",
            "tier": "medium",
            "last_used": "2026-05-29T15:30:00",
        },
        ...
    }
"""
import json
import logging
import os
import threading
from datetime import datetime, timedelta
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# 永続化先は memory_data/（durable）。/tmp は macOS の定期クリーンアップで消えるため使わない。
# memory.json と同じディレクトリ（MEMORY_DIR or リポジトリ直下 memory_data/）に置く。
_DEFAULT_DIR = os.environ.get("MEMORY_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "memory_data"
)
DEFAULT_PATH = os.environ.get("AGENT_SESSION_STORE", os.path.join(_DEFAULT_DIR, "sessions.json"))
# Slack スレッドが会話の正本（毎ターン履歴を再注入）。SDK セッション resume は高速化レイヤなので
# TTL は長め（既定14日）に取り、失効時は Slack 履歴注入で文脈を復元する。env で調整可。
TTL_HOURS = int(os.environ.get("AGENT_SESSION_TTL_HOURS", "336"))


class SessionStore:
    """Thread-safe な session の永続化ストア。"""

    def __init__(self, path: str = DEFAULT_PATH):
        self.path = path
        self._lock = threading.RLock()
        self._data: Dict[str, dict] = {}
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        """起動時に既存 JSON を読み込んで stale を捨てる。"""
        if not os.path.exists(self.path):
            logger.info(f"[SessionStore] no existing store at {self.path}")
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                logger.warning(f"[SessionStore] invalid format, starting fresh")
                return
            cutoff = datetime.now() - timedelta(hours=TTL_HOURS)
            kept = {}
            dropped = 0
            for key, entry in raw.items():
                if not isinstance(entry, dict):
                    continue
                ts_str = entry.get("last_used", "")
                try:
                    last_used = datetime.fromisoformat(ts_str)
                    if last_used >= cutoff:
                        kept[key] = entry
                    else:
                        dropped += 1
                except (ValueError, TypeError):
                    # last_used を解釈できないものは捨てる
                    dropped += 1
            self._data = kept
            logger.info(
                f"[SessionStore] loaded {len(kept)} active session(s) "
                f"(dropped {dropped} stale entries from {self.path})"
            )
        except Exception as e:
            logger.error(f"[SessionStore] load failed: {e}", exc_info=True)
            self._data = {}

    def _flush_to_disk(self) -> None:
        """現在の dict を atomic に JSON へ書き出し。"""
        try:
            tmp_path = self.path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.path)
        except Exception as e:
            logger.error(f"[SessionStore] flush failed: {e}", exc_info=True)

    # ── 公開 API（_thread_sessions の dict 互換に近い操作） ──

    def get(self, key: str) -> Optional[dict]:
        with self._lock:
            return self._data.get(key)

    def set(self, key: str, value: dict) -> None:
        """value に last_used を自動付与してから保存。"""
        with self._lock:
            stamped = {**value, "last_used": datetime.now().isoformat(timespec="seconds")}
            self._data[key] = stamped
            self._flush_to_disk()

    def update_partial(self, key: str, patch: dict) -> None:
        """既存 entry の一部キーだけ更新（tier だけ変えるとか）。"""
        with self._lock:
            current = self._data.get(key, {})
            current.update(patch)
            current["last_used"] = datetime.now().isoformat(timespec="seconds")
            self._data[key] = current
            self._flush_to_disk()

    def delete(self, key: str) -> None:
        with self._lock:
            if key in self._data:
                del self._data[key]
                self._flush_to_disk()

    def __contains__(self, key: str) -> bool:
        with self._lock:
            return key in self._data

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def snapshot(self) -> Dict[str, dict]:
        """テスト/デバッグ用に dict の copy を返す。"""
        with self._lock:
            return dict(self._data)
