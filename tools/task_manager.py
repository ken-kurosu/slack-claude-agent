"""
長期タスク管理ツール: 並行処理の制御と中断・再開機能
- タスクの並行処理トラッキング
- キャパシティ閾値による受付制御
- タスクごとの中断コマンド（@エージェント stop-{task_id}）
- 中断時のサマリ報告
- 再開可能な状態保存
"""
import asyncio
import json
import logging
import time
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class TaskPriority(str, Enum):
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


class LongRunningTask:
    """長期タスクの状態を管理するクラス"""

    def __init__(
        self,
        task_type: str,
        description: str,
        user: str,
        channel: str,
        thread_ts: str,
        params: Optional[dict] = None,
        priority: TaskPriority = TaskPriority.NORMAL,
    ):
        self.task_id = f"task-{uuid.uuid4().hex[:6]}"
        self.task_type = task_type
        self.description = description
        self.user = user
        self.channel = channel
        self.thread_ts = thread_ts
        self.params = params or {}
        self.priority = priority

        self.status = TaskStatus.QUEUED
        self.created_at = datetime.now()
        self.started_at: Optional[datetime] = None
        self.completed_at: Optional[datetime] = None
        self.paused_at: Optional[datetime] = None

        # 進捗管理
        self.progress_percent = 0
        self.progress_message = ""
        self.steps_completed: List[str] = []
        self.steps_remaining: List[str] = []
        self.current_step = ""

        # 中断・再開
        self.interrupt_requested = False
        self.resume_data: Optional[dict] = None  # 再開に必要なデータ

        # 結果
        self.result: Optional[dict] = None
        self.error: Optional[str] = None

        # asyncio タスク参照
        self._async_task: Optional[asyncio.Task] = None

    @property
    def stop_command(self) -> str:
        """このタスクの中断コマンド"""
        return f"stop-{self.task_id}"

    @property
    def resume_command(self) -> str:
        """このタスクの再開コマンド"""
        return f"resume-{self.task_id}"

    @property
    def status_command(self) -> str:
        """このタスクの状況確認コマンド"""
        return f"status-{self.task_id}"

    @property
    def elapsed_seconds(self) -> float:
        if not self.started_at:
            return 0
        end = self.completed_at or self.paused_at or datetime.now()
        return (end - self.started_at).total_seconds()

    def request_interrupt(self):
        """中断をリクエスト（タスク処理側でチェックされる）"""
        self.interrupt_requested = True

    def check_interrupt(self) -> bool:
        """中断がリクエストされているかチェック"""
        return self.interrupt_requested

    def mark_running(self):
        self.status = TaskStatus.RUNNING
        self.started_at = datetime.now()
        self.interrupt_requested = False

    def mark_paused(self, resume_data: Optional[dict] = None):
        self.status = TaskStatus.PAUSED
        self.paused_at = datetime.now()
        self.resume_data = resume_data

    def mark_interrupted(self, resume_data: Optional[dict] = None):
        self.status = TaskStatus.INTERRUPTED
        self.paused_at = datetime.now()
        self.resume_data = resume_data

    def mark_completed(self, result: Optional[dict] = None):
        self.status = TaskStatus.COMPLETED
        self.completed_at = datetime.now()
        self.progress_percent = 100
        self.result = result

    def mark_failed(self, error: str):
        self.status = TaskStatus.FAILED
        self.completed_at = datetime.now()
        self.error = error

    def update_progress(self, percent: int, message: str = "", step: str = ""):
        self.progress_percent = min(percent, 100)
        if message:
            self.progress_message = message
        if step:
            self.current_step = step
            if step not in self.steps_completed:
                self.steps_completed.append(step)

    def get_summary(self) -> dict:
        """タスクの現在状態をサマリとして返す"""
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "description": self.description,
            "status": self.status.value,
            "progress_percent": self.progress_percent,
            "progress_message": self.progress_message,
            "current_step": self.current_step,
            "steps_completed": self.steps_completed,
            "steps_remaining": self.steps_remaining,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "created_at": self.created_at.isoformat(),
            "stop_command": self.stop_command,
            "resume_command": self.resume_command if self.status in (TaskStatus.PAUSED, TaskStatus.INTERRUPTED) else None,
            "can_resume": self.resume_data is not None,
            "user": self.user,
            "channel": self.channel,
        }

    def get_interrupt_report(self) -> str:
        """中断時のサマリレポートを生成"""
        elapsed = self.elapsed_seconds
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)

        lines = [
            f"⏸️ *タスク中断レポート*",
            f"",
            f"📋 タスク: {self.description}",
            f"🆔 ID: `{self.task_id}`",
            f"⏱️ 経過時間: {mins}分{secs}秒",
            f"📊 進捗: {self.progress_percent}%",
        ]

        if self.steps_completed:
            lines.append(f"")
            lines.append(f"✅ *完了したステップ:*")
            for step in self.steps_completed:
                lines.append(f"  • {step}")

        if self.steps_remaining:
            lines.append(f"")
            lines.append(f"⏳ *残りのステップ:*")
            for step in self.steps_remaining:
                lines.append(f"  • {step}")

        if self.resume_data:
            lines.append(f"")
            lines.append(f"▶️ *再開するには:*")
            lines.append(f"  `@エージェント {self.resume_command}`")
        else:
            lines.append(f"")
            lines.append(f"⚠️ このタスクは再開できません。最初からやり直す必要があります。")

        return "\n".join(lines)


class TaskManager:
    """長期タスクの管理を行うマネージャー"""

    # デフォルト並行処理上限
    DEFAULT_MAX_CONCURRENT = 3

    def __init__(self, max_concurrent: int = DEFAULT_MAX_CONCURRENT):
        self.max_concurrent = max_concurrent
        # 全タスク {task_id: LongRunningTask}
        self._tasks: Dict[str, LongRunningTask] = {}
        # タスク完了時のコールバック
        self._completion_callbacks: Dict[str, Callable] = {}
        # ユーザーごとのアクティブタスク数
        self._user_task_counts: Dict[str, int] = {}

    @property
    def active_task_count(self) -> int:
        """現在実行中のタスク数"""
        return sum(
            1 for t in self._tasks.values()
            if t.status in (TaskStatus.RUNNING, TaskStatus.QUEUED)
        )

    @property
    def is_at_capacity(self) -> bool:
        """処理能力の上限に達しているか"""
        return self.active_task_count >= self.max_concurrent

    def can_accept_task(self, user: str = "") -> tuple:
        """新しいタスクを受け付けられるかチェック

        Returns:
            (can_accept: bool, message: str)
        """
        if self.is_at_capacity:
            running_tasks = [
                t for t in self._tasks.values()
                if t.status == TaskStatus.RUNNING
            ]
            task_list = "\n".join(
                f"  • {t.description} ({t.progress_percent}% 完了) - 中断: `@エージェント {t.stop_command}`"
                for t in running_tasks
            )
            return False, (
                f"⏳ 現在 {len(running_tasks)} 件のタスクを処理中です。\n"
                f"もう少しお待ちいただくか、タスクを中断してから再度お試しください。\n\n"
                f"*実行中のタスク:*\n{task_list}"
            )
        return True, ""

    def create_task(
        self,
        task_type: str,
        description: str,
        user: str,
        channel: str,
        thread_ts: str,
        params: Optional[dict] = None,
        priority: TaskPriority = TaskPriority.NORMAL,
    ) -> LongRunningTask:
        """新しいタスクを作成して登録"""
        task = LongRunningTask(
            task_type=task_type,
            description=description,
            user=user,
            channel=channel,
            thread_ts=thread_ts,
            params=params,
            priority=priority,
        )
        self._tasks[task.task_id] = task

        # ユーザーのアクティブタスク数を更新
        self._user_task_counts[user] = self._user_task_counts.get(user, 0) + 1

        logger.info(
            f"Task created: {task.task_id} ({task_type}) for user {user}"
        )
        return task

    def get_task(self, task_id: str) -> Optional[LongRunningTask]:
        """タスクIDからタスクを取得"""
        return self._tasks.get(task_id)

    def get_task_by_command(self, command: str) -> Optional[LongRunningTask]:
        """コマンド文字列からタスクを検索

        Examples:
            "stop-task-abc123" -> task with id "task-abc123"
            "resume-task-abc123" -> task with id "task-abc123"
            "status-task-abc123" -> task with id "task-abc123"
        """
        for prefix in ("stop-", "resume-", "status-"):
            if command.startswith(prefix):
                task_id = command[len(prefix):]
                task = self._tasks.get(task_id)
                if task:
                    return task
        return None

    def get_user_tasks(self, user: str, active_only: bool = True) -> List[LongRunningTask]:
        """ユーザーのタスク一覧を取得"""
        tasks = [t for t in self._tasks.values() if t.user == user]
        if active_only:
            tasks = [
                t for t in tasks
                if t.status in (TaskStatus.RUNNING, TaskStatus.QUEUED, TaskStatus.PAUSED, TaskStatus.INTERRUPTED)
            ]
        return sorted(tasks, key=lambda t: t.created_at, reverse=True)

    def interrupt_task(self, task_id: str) -> Optional[str]:
        """タスクに中断をリクエスト

        Returns:
            中断レポート（まだ中断処理中の場合はNone）
        """
        task = self._tasks.get(task_id)
        if not task:
            return None

        if task.status == TaskStatus.RUNNING:
            task.request_interrupt()
            logger.info(f"Interrupt requested for task {task_id}")
            return None  # 実際の中断はタスク処理側で行われる

        elif task.status in (TaskStatus.QUEUED,):
            task.mark_interrupted()
            return task.get_interrupt_report()

        return None

    def resume_task(self, task_id: str) -> Optional[LongRunningTask]:
        """中断されたタスクの再開情報を取得"""
        task = self._tasks.get(task_id)
        if not task:
            return None

        if task.status not in (TaskStatus.PAUSED, TaskStatus.INTERRUPTED):
            return None

        if not task.resume_data:
            return None

        # ステータスをQUEUEDに戻す
        task.status = TaskStatus.QUEUED
        task.interrupt_requested = False
        return task

    def complete_task(self, task_id: str, result: Optional[dict] = None):
        """タスクを完了にする"""
        task = self._tasks.get(task_id)
        if task:
            task.mark_completed(result)
            user = task.user
            if user in self._user_task_counts:
                self._user_task_counts[user] = max(0, self._user_task_counts[user] - 1)
            logger.info(f"Task completed: {task_id}")

    def fail_task(self, task_id: str, error: str):
        """タスクを失敗にする"""
        task = self._tasks.get(task_id)
        if task:
            task.mark_failed(error)
            user = task.user
            if user in self._user_task_counts:
                self._user_task_counts[user] = max(0, self._user_task_counts[user] - 1)
            logger.info(f"Task failed: {task_id} - {error}")

    def get_all_active_tasks(self) -> List[LongRunningTask]:
        """全アクティブタスク一覧"""
        return [
            t for t in self._tasks.values()
            if t.status in (TaskStatus.RUNNING, TaskStatus.QUEUED)
        ]

    def cleanup_old_tasks(self, max_age_hours: int = 24):
        """古い完了済みタスクを削除"""
        now = datetime.now()
        to_remove = []
        for task_id, task in self._tasks.items():
            if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                if task.completed_at:
                    age = (now - task.completed_at).total_seconds() / 3600
                    if age > max_age_hours:
                        to_remove.append(task_id)
        for task_id in to_remove:
            del self._tasks[task_id]
        if to_remove:
            logger.info(f"Cleaned up {len(to_remove)} old tasks")

    def format_task_list(self, tasks: List[LongRunningTask]) -> str:
        """タスクリストをSlack形式でフォーマット"""
        if not tasks:
            return "現在実行中のタスクはありません。"

        lines = ["📋 *タスク一覧:*\n"]
        for t in tasks:
            status_emoji = {
                TaskStatus.RUNNING: "🔄",
                TaskStatus.QUEUED: "⏳",
                TaskStatus.PAUSED: "⏸️",
                TaskStatus.INTERRUPTED: "⏹️",
                TaskStatus.COMPLETED: "✅",
                TaskStatus.FAILED: "❌",
            }.get(t.status, "❓")

            lines.append(
                f"{status_emoji} *{t.description}*\n"
                f"   ID: `{t.task_id}` | 進捗: {t.progress_percent}% | "
                f"状態: {t.status.value}"
            )

            if t.status == TaskStatus.RUNNING:
                lines.append(f"   中断: `@エージェント {t.stop_command}`")
            elif t.status in (TaskStatus.PAUSED, TaskStatus.INTERRUPTED) and t.resume_data:
                lines.append(f"   再開: `@エージェント {t.resume_command}`")

        return "\n".join(lines)

    def get_task_start_message(self, task: LongRunningTask) -> str:
        """タスク開始時にユーザーに伝えるメッセージを生成"""
        return (
            f"📊 タスクを開始しました！\n\n"
            f"🆔 タスクID: `{task.task_id}`\n"
            f"📋 内容: {task.description}\n\n"
            f"*コマンド:*\n"
            f"  ⏹️ 中断: `@エージェント {task.stop_command}`\n"
            f"  📊 状況確認: `@エージェント {task.status_command}`\n\n"
            f"処理が完了したらお知らせしますね✨"
        )


def parse_task_command(text: str) -> Optional[dict]:
    """メンションテキストからタスクコマンドをパース

    Returns:
        {"action": "stop"|"resume"|"status"|"list", "task_id": "task-xxx"} or None
    """
    import re
    text = text.strip().lower()

    # タスク一覧
    if text in ("タスク一覧", "tasks", "task-list", "タスクリスト"):
        return {"action": "list", "task_id": ""}

    # stop-task-xxx
    match = re.match(r"stop-(task-[a-f0-9]+)", text)
    if match:
        return {"action": "stop", "task_id": match.group(1)}

    # resume-task-xxx
    match = re.match(r"resume-(task-[a-f0-9]+)", text)
    if match:
        return {"action": "resume", "task_id": match.group(1)}

    # status-task-xxx
    match = re.match(r"status-(task-[a-f0-9]+)", text)
    if match:
        return {"action": "status", "task_id": match.group(1)}

    # 日本語コマンド
    match = re.match(r"(中断|停止|止めて)\s*(task-[a-f0-9]+)", text)
    if match:
        return {"action": "stop", "task_id": match.group(2)}

    match = re.match(r"(再開|続き)\s*(task-[a-f0-9]+)", text)
    if match:
        return {"action": "resume", "task_id": match.group(2)}

    return None


async def handle_task_command(
    command: dict,
    task_manager: "TaskManager",
    user: str,
) -> Optional[str]:
    """タスクコマンドを処理してレスポンスメッセージを返す"""
    action = command.get("action", "")
    task_id = command.get("task_id", "")

    if action == "list":
        tasks = task_manager.get_user_tasks(user, active_only=False)
        return task_manager.format_task_list(tasks[:10])

    if not task_id:
        return "タスクIDを指定してください。`@エージェント タスク一覧` で確認できます。"

    task = task_manager.get_task(task_id)
    if not task:
        return f"タスク `{task_id}` が見つかりません。"

    # ユーザー検証
    if task.user != user:
        return f"タスク `{task_id}` は別のユーザーのタスクです。"

    if action == "stop":
        if task.status not in (TaskStatus.RUNNING, TaskStatus.QUEUED):
            return f"タスク `{task_id}` は現在 {task.status.value} 状態のため中断できません。"

        report = task_manager.interrupt_task(task_id)
        if report:
            return report
        else:
            return (
                f"⏸️ タスク `{task_id}` に中断リクエストを送りました。\n"
                f"現在のステップが完了次第、安全に停止します。しばらくお待ちください。"
            )

    elif action == "resume":
        if task.status not in (TaskStatus.PAUSED, TaskStatus.INTERRUPTED):
            return f"タスク `{task_id}` は {task.status.value} 状態のため再開できません。"

        if not task.resume_data:
            return f"タスク `{task_id}` には再開用のデータがありません。最初からやり直す必要があります。"

        resumed = task_manager.resume_task(task_id)
        if resumed:
            return (
                f"▶️ タスク `{task_id}` を再開キューに入れました。\n"
                f"前回の進捗 ({task.progress_percent}%) から再開します。"
            )
        return f"タスク `{task_id}` の再開に失敗しました。"

    elif action == "status":
        summary = task.get_summary()
        elapsed = summary["elapsed_seconds"]
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)

        status_emoji = {
            "running": "🔄",
            "queued": "⏳",
            "paused": "⏸️",
            "interrupted": "⏹️",
            "completed": "✅",
            "failed": "❌",
        }.get(summary["status"], "❓")

        lines = [
            f"{status_emoji} *タスク状況: {task.description}*",
            f"",
            f"🆔 ID: `{task.task_id}`",
            f"📊 進捗: {summary['progress_percent']}%",
            f"⏱️ 経過: {mins}分{secs}秒",
            f"🏷️ 状態: {summary['status']}",
        ]

        if summary.get("current_step"):
            lines.append(f"📍 現在: {summary['current_step']}")

        if summary.get("steps_completed"):
            lines.append(f"✅ 完了: {len(summary['steps_completed'])}ステップ")

        if task.status == TaskStatus.RUNNING:
            lines.append(f"\n中断: `@エージェント {task.stop_command}`")

        return "\n".join(lines)

    return None
