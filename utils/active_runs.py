"""走行中ラン台帳（2026-07-17 導入）

問題: エージェントの再起動（デプロイ・daily-restart 04:00・watchdog・クラッシュ）が
走行中の長時間タスクを殺すと、進捗メッセージが残ったまま無言で止まり、
ユーザーには「フリーズした」ようにしか見えない（長いデータ移行で実害があった）。

対策: 走行開始時にこの台帳へ記録し、正常終了時に削除する。
起動時に台帳へ残っているエントリ＝再起動で殺された孤児ランなので、
該当スレッドへ「中断しました。『続きやって』で再開できます」を自動投稿する。
"""

import json
import logging
import os
import time
from typing import List

logger = logging.getLogger(__name__)


def _memory_dir() -> str:
    return os.getenv(
        "MEMORY_DIR",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "memory_data"),
    )


def _path() -> str:
    return os.path.join(_memory_dir(), "active_runs.json")


def _resume_path() -> str:
    """再開試行カウンタ（自動再開の無限ループ防止用。ライブ台帳とは分離）"""
    return os.path.join(_memory_dir(), "resume_attempts.json")


def _load_file(path: str) -> dict:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        logger.exception(f"[ActiveRuns] load failed: {path}")
    return {}


def _save_file(path: str, data: dict):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception:
        logger.exception(f"[ActiveRuns] save failed: {path}")


def _load() -> dict:
    return _load_file(_path())


def _save(data: dict):
    _save_file(_path(), data)


def register_run(thread_key: str, channel: str, thread_ts=None, user=None, text=None):
    """走行開始を記録（プロセスが死んでも残る＝孤児検出の根拠）。

    user/text も保存しておくと、起動時に孤児を **自動再開** する際に
    発話者トーンと元依頼の要約を復元できる。
    """
    data = _load()
    data[thread_key] = {
        "channel": channel,
        "thread_ts": str(thread_ts) if thread_ts else "",
        "started": time.time(),
        "user": str(user) if user else "",
        "text": (str(text)[:1500] if text else ""),
    }
    _save(data)


def unregister_run(thread_key: str):
    """正常終了（応答返却まで到達）したら台帳から消す。

    タスクが最後まで完了した＝再開ループも解消なので、再開試行カウンタもクリアする。
    """
    data = _load()
    if thread_key in data:
        del data[thread_key]
        _save(data)
    clear_resume_attempts(thread_key)


def pop_orphans(min_age_seconds: int = 20) -> List[dict]:
    """起動時に呼ぶ: 台帳に残っている孤児ランを全て取り出してクリアする。

    min_age_seconds より新しいエントリは（起動直後の競合を避けるため）残す。
    """
    data = _load()
    now = time.time()
    orphans = []
    keep = {}
    for key, run in data.items():
        if now - run.get("started", 0) < min_age_seconds:
            keep[key] = run
        else:
            orphans.append({"thread_key": key, **run})
    _save(keep)
    return orphans


# ── 自動再開の無限ループ防止カウンタ ──
# 再起動が走行中タスクを殺す → 起動時に自動再開、を繰り返すと、
# 万一そのタスク自体がプロセスを落とす類なら再起動ループになる。
# スレッド単位で試行回数を数え、上限を超えたら自動再開をやめて手動案内にフォールバックする。

def note_resume_attempt(thread_key: str, decay_seconds: int = 3600) -> int:
    """自動再開を1回試みるたびに呼ぶ。累積試行回数を返す。

    最後の試行から decay_seconds 以上経っていれば（別機会の中断とみなして）カウントをリセットする。
    """
    data = _load_file(_resume_path())
    now = time.time()
    entry = data.get(thread_key)
    if entry and (now - entry.get("last", 0)) < decay_seconds:
        count = int(entry.get("count", 0)) + 1
    else:
        count = 1
    data[thread_key] = {"count": count, "last": now}
    _save_file(_resume_path(), data)
    return count


def clear_resume_attempts(thread_key: str):
    """タスクが完走した／手動案内にフォールバックした時に呼ぶ。カウンタを消す。"""
    data = _load_file(_resume_path())
    if thread_key in data:
        del data[thread_key]
        _save_file(_resume_path(), data)
