"""
ユーザー単位のレートリミッター
高コスト操作（AI呼び出し・コード実行・画像生成など）の濫用を防止
"""
import time
import logging
from collections import defaultdict
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class RateLimiter:
    """スライディングウィンドウ方式のレートリミッター"""

    def __init__(self):
        # {category -> {user_id -> [timestamp, ...]}}
        self._buckets: Dict[str, Dict[str, list]] = defaultdict(lambda: defaultdict(list))

        # カテゴリ別のレート制限設定: (最大回数, ウィンドウ秒数)
        self._limits: Dict[str, Tuple[int, int]] = {
            # AI応答: 1分に10回まで
            "ai_response": (10, 60),
            # コード実行: 5分に5回まで
            "execute_code": (15, 300),
            # 画像生成: 10分に5回まで
            "image_generate": (5, 600),
            # 書き込み操作: 1分に5回まで
            "write_operation": (5, 60),
            # Web検索: 1分に10回まで
            "web_search": (10, 60),
        }

    def check(self, user_id: str, category: str) -> Optional[str]:
        """レートリミットをチェック。超過時はエラーメッセージを返す。OK なら None。"""
        if category not in self._limits:
            return None

        max_count, window_sec = self._limits[category]
        now = time.time()
        cutoff = now - window_sec

        # 期限切れのタイムスタンプを除去
        bucket = self._buckets[category][user_id]
        self._buckets[category][user_id] = [ts for ts in bucket if ts > cutoff]
        bucket = self._buckets[category][user_id]

        if len(bucket) >= max_count:
            wait_sec = int(bucket[0] + window_sec - now) + 1
            logger.warning(
                f"Rate limit exceeded: user={user_id}, category={category}, "
                f"count={len(bucket)}/{max_count} in {window_sec}s"
            )
            return (
                f"⏳ レート制限に達しました（{category}: {window_sec}秒間に{max_count}回まで）。"
                f"約{wait_sec}秒後に再度お試しください。"
            )

        return None

    def record(self, user_id: str, category: str):
        """操作を記録"""
        self._buckets[category][user_id].append(time.time())

    def cleanup(self):
        """全バケットの期限切れエントリをクリーンアップ"""
        now = time.time()
        for category, users in self._buckets.items():
            window_sec = self._limits.get(category, (0, 3600))[1]
            cutoff = now - window_sec
            for user_id in list(users.keys()):
                users[user_id] = [ts for ts in users[user_id] if ts > cutoff]
                if not users[user_id]:
                    del users[user_id]
