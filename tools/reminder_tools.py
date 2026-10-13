"""
リマインダーツール: 文脈理解型リマインダー（他者へのリマインド対応）
- 会話の文脈からリマインド内容を自動抽出
- 自分宛てだけでなく、他のユーザー宛てのリマインダーも設定可能
- 指定時刻にDMまたはスレッドでリマインド
"""
import asyncio
import json
import logging
import os
import time
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class ReminderManager:
    """文脈理解型リマインダー管理"""

    def __init__(self, persist_path: str = ""):
        self.persist_path = persist_path or "/tmp/agent_reminders.json"
        self.reminders: List[dict] = []
        self._load()

    def _load(self):
        """永続化ファイルから読み込み"""
        try:
            if os.path.exists(self.persist_path):
                with open(self.persist_path, "r") as f:
                    self.reminders = json.load(f)
                logger.info(f"Loaded {len(self.reminders)} reminders")
        except Exception as e:
            logger.error(f"Failed to load reminders: {e}")
            self.reminders = []

    def _save(self):
        """永続化ファイルに保存"""
        try:
            os.makedirs(os.path.dirname(self.persist_path) or ".", exist_ok=True)
            with open(self.persist_path, "w") as f:
                json.dump(self.reminders, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to save reminders: {e}")

    def add_reminder(
        self,
        target_user: str,
        message: str,
        remind_at: str,
        context: str = "",
        requester: str = "",
        channel: str = "",
        thread_ts: str = "",
        workspace: str = "main",
    ) -> dict:
        """リマインダー追加

        Args:
            target_user: リマインド対象のユーザーID
            message: リマインドメッセージ
            remind_at: リマインド日時 (ISO 8601 or "YYYY-MM-DD HH:MM")
            context: 元の会話の文脈（自動抽出用）
            requester: リマインダーを設定したユーザーID
            channel: 元のチャンネルID（スレッドリンク用）
            thread_ts: 元のスレッドts（スレッドリンク用）
        """
        reminder_id = f"rem_{int(time.time())}_{target_user[:6]}"

        # 日時パース
        parsed_time = self._parse_datetime(remind_at)
        if not parsed_time:
            return {
                "success": False,
                "error": f"日時を解釈できませんでした: {remind_at}",
            }

        reminder = {
            "id": reminder_id,
            "target_user": target_user,
            "message": message,
            "context": context[:500],
            "remind_at": parsed_time.isoformat(),
            "remind_at_ts": parsed_time.timestamp(),
            "requester": requester,
            "channel": channel,
            "thread_ts": thread_ts,
            "workspace": workspace,  # "main" or "maint"
            "status": "pending",  # pending / sent / cancelled
            "created_at": time.time(),
        }
        self.reminders.append(reminder)
        self._save()

        # フォーマット表示用
        display_time = parsed_time.strftime("%Y年%m月%d日 %H:%M")
        is_self = target_user == requester

        if is_self:
            msg = f"リマインダーを設定しました！\n⏰ {display_time} にお知らせします"
        else:
            msg = (
                f"リマインダーを設定しました！\n"
                f"⏰ {display_time} に <@{target_user}> さんへDMでお知らせします\n"
                f"（依頼者: <@{requester}>）"
            )

        return {
            "success": True,
            "reminder_id": reminder_id,
            "target_user": target_user,
            "remind_at": display_time,
            "message": msg,
        }

    def cancel_reminder(self, reminder_id: str, user: str) -> dict:
        """リマインダーをキャンセル"""
        for rem in self.reminders:
            if rem["id"] == reminder_id and rem["status"] == "pending":
                if rem["requester"] != user and rem["target_user"] != user:
                    return {"success": False, "error": "このリマインダーをキャンセルする権限がありません"}
                rem["status"] = "cancelled"
                self._save()
                return {"success": True, "message": f"リマインダー {reminder_id} をキャンセルしました"}
        return {"success": False, "error": f"リマインダー {reminder_id} が見つかりません"}

    def list_reminders(self, user: str = "") -> List[dict]:
        """リマインダー一覧（ユーザー指定でフィルタ可能）"""
        active = [r for r in self.reminders if r["status"] == "pending"]
        if user:
            active = [
                r for r in active
                if r["target_user"] == user or r["requester"] == user
            ]
        return active

    async def check_and_send(self, slack_client) -> int:
        """期限が来たリマインダーを送信。送信件数を返す。

        Args:
            slack_client: Slackクライアント
        """
        now = time.time()
        sent_count = 0

        for rem in self.reminders:
            if rem["status"] != "pending":
                continue
            if rem["remind_at_ts"] > now:
                continue

            try:
                target = rem["target_user"]
                requester = rem["requester"]
                message = rem["message"]
                context = rem.get("context", "")
                channel = rem.get("channel", "")
                thread_ts = rem.get("thread_ts", "")

                # DM送信
                conv = await slack_client.conversations_open(users=[target])
                dm_channel = conv["channel"]["id"]

                # メッセージ構築
                blocks = [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"⏰ *リマインダー* from <@{requester}>\n\n{message}",
                        },
                    },
                ]

                # 文脈情報があれば追加
                if context:
                    blocks.append({
                        "type": "context",
                        "elements": [
                            {"type": "mrkdwn", "text": f"💬 _元の会話: {context[:200]}_"},
                        ],
                    })

                # 元スレッドへのリンク
                if channel and thread_ts:
                    # Slack deep link
                    blocks.append({
                        "type": "context",
                        "elements": [
                            {"type": "mrkdwn", "text": f"🔗 <https://slack.com/archives/{channel}/p{thread_ts.replace('.', '')}|元のスレッドを見る>"},
                        ],
                    })

                await slack_client.chat_postMessage(
                    channel=dm_channel,
                    text=f"⏰ リマインダー from <@{requester}>: {message}",
                    blocks=blocks,
                )

                rem["status"] = "sent"
                sent_count += 1
                logger.info(f"Reminder sent: {rem['id']} -> {target}")

            except Exception as e:
                logger.error(f"Failed to send reminder {rem['id']}: {e}")
                # 1時間以上経っても送れない場合は諦める
                if now - rem["remind_at_ts"] > 3600:
                    rem["status"] = "failed"

        if sent_count > 0:
            self._save()
            # 送信済みの古いリマインダーをクリーンアップ
            self._cleanup()

        return sent_count

    def _cleanup(self):
        """送信済み/キャンセル済みの7日以上前のリマインダーを削除"""
        cutoff = time.time() - 7 * 86400
        self.reminders = [
            r for r in self.reminders
            if r["status"] == "pending" or r.get("created_at", 0) > cutoff
        ]
        self._save()

    @staticmethod
    def _parse_datetime(dt_str: str) -> Optional[datetime]:
        """日時文字列をパース（様々なフォーマットに対応）"""
        formats = [
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y/%m/%d %H:%M",
            "%m/%d %H:%M",
            "%m月%d日 %H:%M",
            "%Y年%m月%d日 %H:%M",
        ]
        for fmt in formats:
            try:
                dt = datetime.strptime(dt_str, fmt)
                # 年がない場合は今年を補完
                if dt.year == 1900:
                    dt = dt.replace(year=datetime.now().year)
                return dt
            except ValueError:
                continue

        # 相対指定のパース
        now = datetime.now()
        relative_patterns = [
            (r"(\d+)分後", lambda m: now + timedelta(minutes=int(m.group(1)))),
            (r"(\d+)時間後", lambda m: now + timedelta(hours=int(m.group(1)))),
            (r"(\d+)日後", lambda m: now + timedelta(days=int(m.group(1)))),
            (r"明日\s*(\d{1,2}):?(\d{2})?", lambda m: (now + timedelta(days=1)).replace(
                hour=int(m.group(1)), minute=int(m.group(2) or 0), second=0, microsecond=0)),
            (r"来週の?(月|火|水|木|金|土|日)曜?\s*(\d{1,2})?:?(\d{2})?", None),  # 複雑なので別処理
        ]
        for pattern, handler in relative_patterns:
            match = re.match(pattern, dt_str)
            if match and handler:
                return handler(match)

        return None


async def set_reminder(
    target_user: str,
    message: str,
    remind_at: str,
    context: str = "",
    requester: str = "",
    channel: str = "",
    thread_ts: str = "",
    workspace: str = "main",
    reminder_manager: "ReminderManager" = None,
) -> dict:
    """リマインダーを設定（ツールインターフェース）"""
    if not reminder_manager:
        return {"success": False, "error": "ReminderManagerが初期化されていません"}
    return reminder_manager.add_reminder(
        target_user=target_user,
        message=message,
        remind_at=remind_at,
        context=context,
        requester=requester,
        channel=channel,
        thread_ts=thread_ts,
        workspace=workspace,
    )


async def list_reminders(
    user: str = "",
    reminder_manager: "ReminderManager" = None,
) -> dict:
    """リマインダー一覧を取得"""
    if not reminder_manager:
        return {"success": False, "error": "ReminderManagerが初期化されていません"}
    reminders = reminder_manager.list_reminders(user)
    return {
        "success": True,
        "reminders": reminders,
        "count": len(reminders),
    }


async def cancel_reminder(
    reminder_id: str,
    user: str,
    reminder_manager: "ReminderManager" = None,
) -> dict:
    """リマインダーをキャンセル"""
    if not reminder_manager:
        return {"success": False, "error": "ReminderManagerが初期化されていません"}
    return reminder_manager.cancel_reminder(reminder_id, user)
