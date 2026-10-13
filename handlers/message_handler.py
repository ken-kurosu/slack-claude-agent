"""
Slackメッセージハンドラー:
- メンション時の応答
- 人間味のある自発的タスク検出（Claude判断ベース）
- ファイルアップロード時の処理
- 外部ツール書き込み時の確認フロー
"""
import asyncio
import base64
import json
import logging
import os
import re
import time
from collections import OrderedDict
from typing import Dict, List, Optional

import aiohttp
import anthropic
from slack_bolt.async_app import AsyncApp

from config.settings import Settings
from utils.memory import Memory, MemoryExtractor
from tools.executor import ToolExecutor
from tools.task_manager import parse_task_command, handle_task_command

logger = logging.getLogger(__name__)

# ── リアクション管理: 処理状態をSlackリアクションで可視化 ──
class ReactionManager:
    """処理状態をSlackリアクションで可視化するヘルパー。
    :eyes: → :hourglass_flowing_sand: → 完了時に全除去 + :white_check_mark: / エラー時 :x:
    """

    # 状態遷移で使うリアクション一覧（除去対象）
    _MANAGED_REACTIONS = ("eyes", "hourglass_flowing_sand", "hourglass")

    def __init__(self, client, channel: str, message_ts: str):
        self.client = client
        self.channel = channel
        self.ts = message_ts
        self._added: list[str] = []

    async def start(self):
        """処理開始: :eyes: を付与"""
        await self._add("eyes")

    async def processing(self):
        """SDK処理中: :hourglass_flowing_sand: に切替"""
        await self._remove("eyes")
        await self._add("hourglass_flowing_sand")

    async def complete(self):
        """完了: 管理リアクション全除去 + :white_check_mark:"""
        await self._remove_all()
        await self._add("white_check_mark")

    async def error(self):
        """エラー: 管理リアクション全除去 + :x:"""
        await self._remove_all()
        await self._add("x")

    async def cancel(self):
        """キャンセル（トリアージskip等）: 管理リアクション全除去"""
        await self._remove_all()

    async def _add(self, emoji: str):
        try:
            await self.client.reactions_add(channel=self.channel, name=emoji, timestamp=self.ts)
            self._added.append(emoji)
        except Exception:
            pass

    async def _remove(self, emoji: str):
        try:
            await self.client.reactions_remove(channel=self.channel, name=emoji, timestamp=self.ts)
            if emoji in self._added:
                self._added.remove(emoji)
        except Exception:
            pass

    async def _remove_all(self):
        for emoji in list(self._added):
            await self._remove(emoji)
        # 安全のため管理対象も全除去（他の経路で付いた場合）
        for emoji in self._MANAGED_REACTIONS:
            if emoji not in self._added:
                try:
                    await self.client.reactions_remove(channel=self.channel, name=emoji, timestamp=self.ts)
                except Exception:
                    pass


# ── ActiveThreadTracker: ボットが返信したスレッドを一定時間トリアージスキップ ──
class ActiveThreadTracker:
    """ボットが返信したスレッドを追跡。TTL内はトリアージ（LLM判定）をスキップして即応答扱いにする。"""

    def __init__(self, ttl_seconds: int = 600):
        self._ttl = ttl_seconds
        # {thread_key: last_reply_time}
        self._threads: Dict[str, float] = {}

    def mark_replied(self, channel: str, thread_ts: str):
        """ボットがスレッドに返信した時刻を記録"""
        key = f"{channel}:{thread_ts}"
        self._threads[key] = time.time()
        # 古いエントリを掃除（100件超えたら）
        if len(self._threads) > 100:
            self._cleanup()

    def is_active(self, channel: str, thread_ts: str) -> bool:
        """スレッドがアクティブ（TTL内にボットが返信済み）かチェック"""
        if not thread_ts:
            return False
        key = f"{channel}:{thread_ts}"
        last = self._threads.get(key)
        if last is None:
            return False
        if time.time() - last > self._ttl:
            del self._threads[key]
            return False
        return True

    def _cleanup(self):
        now = time.time()
        expired = [k for k, v in self._threads.items() if now - v > self._ttl]
        for k in expired:
            del self._threads[k]


class MessageHandler:
    """Slackメッセージの処理を担当"""

    def __init__(self, app: AsyncApp, settings: Settings, ai_engine, tool_executor: ToolExecutor, memory: Memory = None):
        self.app = app
        self.settings = settings
        self.ai_engine = ai_engine
        self.tool_executor = tool_executor
        self.memory = memory or Memory()
        self.memory_extractor = MemoryExtractor(ai_engine.client, self.memory)
        # 書き込み確認の保留タスク {channel:ts -> pending_action}
        self._pending_confirmations: Dict[str, dict] = {}
        # メモリ保存カウンタ
        self._interaction_count = 0
        # イベント重複排除（Slackリトライ対策）
        self._processed_events: OrderedDict = OrderedDict()
        self._event_ttl_seconds = 300  # 5分間保持
        self._max_processed_events = 1000

        # ── スレッド単位のDebounce（連続メッセージ結合） ──
        self._thread_debounce: Dict[str, dict] = {}
        self._debounce_seconds = 3.0  # 次のメッセージを待つ秒数
        self._debounce_lock = asyncio.Lock()
        # リマインダー（永続先は durable な data_dir）
        from tools.reminder_tools import ReminderManager
        self.reminder_manager = ReminderManager(
            persist_path=os.path.join(settings.data_dir or settings.workspace_dir, "reminders.json"))
        self.tool_executor.reminder_manager = self.reminder_manager
        # ActiveThreadTracker: ボットが返信したスレッドは10分間トリアージスキップ
        self._active_threads = ActiveThreadTracker(ttl_seconds=600)
        # Clarify（意図確認ボタン）の保留ストア
        from utils.assistant_outcome import ClarificationStore
        self.clarify_store = ClarificationStore()
        self._register_handlers()


    def _is_duplicate_event(self, event: dict, event_type: str = "") -> bool:
        """イベント重複検出（Slackリトライ対策）
        event_type を含めて、message と app_mention が同じclient_msg_idで衝突しないようにする
        """
        # イベントのユニークキー: event_type + client_msg_id > event_ts > ts
        base_key = (
            event.get("client_msg_id")
            or f"{event.get('channel', '')}-{event.get('event_ts', event.get('ts', ''))}"
        )
        event_key = f"{event_type}:{base_key}" if event_type else base_key
        now = time.time()

        # 古いエントリを削除
        while self._processed_events:
            oldest_key, oldest_time = next(iter(self._processed_events.items()))
            if now - oldest_time > self._event_ttl_seconds:
                self._processed_events.pop(oldest_key)
            else:
                break

        # サイズ上限
        while len(self._processed_events) >= self._max_processed_events:
            self._processed_events.popitem(last=False)

        if event_key in self._processed_events:
            logger.info(f"Duplicate event detected, skipping: {event_key}")
            return True

        self._processed_events[event_key] = now
        return False



    def _register_handlers(self):
        """Slackイベントハンドラーを登録（業務固有のボタンは機能を足す時にここへ追加する）"""

        @self.app.event("app_mention")
        async def handle_mention(event, say, client):
            logger.info(f"[EventRaw] app_mention event: user={event.get('user','?')}")
            await self._handle_mention(event, say, client)

        @self.app.event("message")
        async def handle_message(event, say, client):
            subtype = event.get("subtype")
            # file_share はユーザーがファイル添付したメッセージなので通常処理する
            if event.get("bot_id") or (subtype and subtype != "file_share"):
                await self._handle_bot_message(event, say, client)
                return
            await self._handle_message(event, say, client)

        @self.app.event("file_shared")
        async def handle_file_shared(event, say, client):
            pass

        @self.app.event("reaction_added")
        async def handle_reaction(event, say, client):
            await self._handle_reaction(event, say, client)

        # 満足度ボタン
        @self.app.action(re.compile(r"^feedback_\d$"))
        async def handle_feedback_action(ack, body, client):
            await ack()
            await self._handle_feedback_button(body, client)

        # 停止ボタン（ESC 相当）と文脈リセットボタン
        @self.app.action("stop_agent")
        async def handle_stop(ack, body, client):
            await ack()
            await self._handle_stop_agent(body, client)

        @self.app.action("reset_agent_context")
        async def handle_reset(ack, body, client):
            await ack()
            await self._handle_reset_agent_context(body, client)

        # Clarify（意図確認）ボタン
        @self.app.action(re.compile(r"^agent_clarify_choice"))
        async def handle_clarify_choice(ack, body, client):
            await ack()
            await self._handle_clarify_choice(body, client)

        # 承認/却下ボタン
        @self.app.action(re.compile(r"^(approve|reject)_"))
        async def handle_approval_action(ack, body, client):
            await ack()
            await self._handle_approval_button(body, client)

        # 書き込み確認ボタン（外部への書き込み・送信の前に必ず確認を取る）
        @self.app.action("write_confirm_approve")
        async def handle_write_confirm_approve(ack, body, client):
            await ack()
            await self._handle_write_confirmation_button(body, client, approved=True)

        @self.app.action("write_confirm_reject")
        async def handle_write_confirm_reject(ack, body, client):
            await ack()
            await self._handle_write_confirmation_button(body, client, approved=False)

        # 自発的な提案の承認/却下ボタン
        @self.app.action("proactive_approve")
        async def handle_proactive_approve(ack, body, client):
            await ack()
            await self._handle_proactive_approval(body, client, approved=True)

        @self.app.action("proactive_reject")
        async def handle_proactive_reject(ack, body, client):
            await ack()
            await self._handle_proactive_approval(body, client, approved=False)

        # 自発的な介入へのフィードバックボタン
        @self.app.action("intervention_feedback_good")
        async def handle_intervention_good(ack, body, client):
            await ack()
            await self._handle_intervention_feedback(body, client, is_good=True)

        @self.app.action("intervention_feedback_bad")
        async def handle_intervention_bad(ack, body, client):
            await ack()
            await self._handle_intervention_feedback(body, client, is_good=False)


    # ──────────────────────────────────────────────
    # メンション対応
    # ──────────────────────────────────────────────
    def _extract_mentioned_users(self, text: str) -> list:
        """テキストから <@U...> 形式のメンションされたユーザーIDを抽出"""
        return re.findall(r"<@([A-Z0-9]+)>", text)

    async def _handle_mention(self, event: dict, say, client):
        """メンション時の処理（debounce対応）"""
        if self._is_duplicate_event(event, event_type="app_mention"):
            return

        channel = event["channel"]
        thread_ts = event.get("thread_ts", event["ts"])
        user = event.get("user", "unknown")
        raw_text = event.get("text", "")
        text = self._clean_mention(raw_text)

        logger.info(f"[Mention] Received: user={user}, channel={channel}, text={text[:100]}")

        # 2026-05-29: 文脈リセットは Block Kit ボタン (🧹) のみで完結する方針に。
        # テキストキーワード検出は廃止（status 廃止と同じく、長文中の偶然一致リスクを避けるため）。

        # 書き込みの確認は、ボタン（承認／キャンセル）でだけ受け取る。「はい」「ok」等の発言は
        # 拾わない（2026-09-30 オーナー指示: 別の意味の「ok」で書き込みが走りうるため）

        # リアクション非表示(ネガティブフィードバック)チェック
        if await self._check_feedback(text, channel, user):
            return

        # リアクション管理: :eyes: で処理開始を可視化
        reaction_mgr = ReactionManager(client, channel, event["ts"])
        await reaction_mgr.start()

        # ── Debounce: スレッド単位でメッセージをバッファリング ──
        thread_key = f"{channel}:{thread_ts}"
        files = await self._extract_files(event, client)

        async with self._debounce_lock:
            debounce = self._thread_debounce.get(thread_key)

            if debounce and debounce.get("processing"):
                # 処理中のスレッドに追加メッセージ → キューに追加
                debounce["queued_messages"].append({"text": text, "user": user, "files": files, "event": event})
                debounce.setdefault("reaction_managers", []).append(reaction_mgr)
                logger.info(f"[Debounce] Queued message while processing: {thread_key} ({len(debounce['queued_messages'])} queued)")
                return

            if debounce and debounce.get("timer"):
                # タイマー稼働中 → メッセージ追加 + タイマーリセット
                debounce["timer"].cancel()
                debounce["messages"].append({"text": text, "user": user, "files": files, "event": event})
                debounce.setdefault("reaction_managers", []).append(reaction_mgr)
                logger.info(f"[Debounce] Added message, resetting timer: {thread_key} ({len(debounce['messages'])} buffered)")
            else:
                # 新規スレッド → バッファ作成
                self._thread_debounce[thread_key] = {
                    "messages": [{"text": text, "user": user, "files": files, "event": event}],
                    "queued_messages": [],
                    "reaction_managers": [reaction_mgr],
                    "processing": False,
                    "timer": None,
                    "channel": channel,
                    "thread_ts": thread_ts,
                    "say": say,
                    "client": client,
                }
                debounce = self._thread_debounce[thread_key]

            # Debounceタイマーを（再）設定
            debounce["timer"] = asyncio.create_task(
                self._debounce_fire(thread_key)
            )

        return  # 処理は _debounce_fire → _process_mention_batch で実行

    async def _debounce_fire(self, thread_key: str):
        """Debounceタイマー満了 → バッファされたメッセージをまとめて処理"""
        await asyncio.sleep(self._debounce_seconds)

        async with self._debounce_lock:
            debounce = self._thread_debounce.get(thread_key)
            if not debounce or not debounce["messages"]:
                return
            debounce["processing"] = True
            debounce["timer"] = None

        try:
            await self._process_mention_batch(thread_key)
        finally:
            # 処理完了 → キューに溜まったメッセージがあれば次のバッチへ
            async with self._debounce_lock:
                debounce = self._thread_debounce.get(thread_key)
                if debounce:
                    debounce["processing"] = False
                    if debounce["queued_messages"]:
                        # キューをメッセージバッファに移して再度タイマー起動
                        debounce["messages"] = debounce["queued_messages"]
                        debounce["queued_messages"] = []
                        debounce["timer"] = asyncio.create_task(
                            self._debounce_fire(thread_key)
                        )
                        logger.info(f"[Debounce] Processing queued messages: {thread_key} ({len(debounce['messages'])})")
                    else:
                        # クリーンアップ
                        del self._thread_debounce[thread_key]

    async def _process_mention_batch(self, thread_key: str):
        """Debounceバッファのメッセージをまとめて処理"""
        debounce = self._thread_debounce.get(thread_key)
        if not debounce:
            return

        messages = debounce["messages"]
        channel = debounce["channel"]
        thread_ts = debounce["thread_ts"]
        say = debounce["say"]
        client = debounce["client"]
        reaction_managers: list[ReactionManager] = debounce.get("reaction_managers", [])

        # メッセージを結合
        combined_texts = []
        all_files = []
        primary_user = messages[0]["user"]
        primary_event = messages[0]["event"]

        for msg in messages:
            combined_texts.append(msg["text"])
            if msg["files"]:
                all_files.extend(msg["files"])

        if len(messages) > 1:
            # 複数ユーザーが同一バッチに混在する場合は発話者を明示する。
            # (旧実装は全メッセージを primary_user の発話として結合し、誰の発言か失われ、
            #  敬語/タメ語の適用やパーソナライズが先頭ユーザーに誤帰属していた)
            distinct_users = {m["user"] for m in messages}
            if len(distinct_users) > 1:
                # 発話者は実名でラベル付け（生ID <@U..> だと LLM が名前を取り違える）
                name_map = {}
                for _uid in distinct_users:
                    try:
                        name_map[_uid] = await self.memory.resolve_user_name(_uid, slack_client=client)
                    except Exception:
                        name_map[_uid] = _uid
                text = "\n\n".join(f"{name_map.get(m['user'], m['user'])}: {m['text']}" for m in messages)
            else:
                text = "\n\n".join(combined_texts)
            logger.info(f"[Debounce] Combined {len(messages)} messages ({len(distinct_users)} users) for {thread_key}: {text[:150]}")
        else:
            text = combined_texts[0]

        user = primary_user
        had_error = False

        # リアクション: SDK処理開始 → :hourglass_flowing_sand: に切替
        for rm in reaction_managers:
            await rm.processing()

        # ── 一次返事を最優先で投稿（ファイルDL・メモリ構築より先） ──
        # 2026-05-29: 🛑 停止 / 🧹 文脈リセット ボタンを併設（ESC 相当 + コンテキストリセット）
        progress_ts = None
        thread_key_for_btn = f"{channel}:{thread_ts}" if thread_ts else channel
        progress_blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": "🔎 確認しています…少々お待ちください✨"}},
            {"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "🛑 停止"},
                 "action_id": "stop_agent", "value": thread_key_for_btn, "style": "danger"},
                {"type": "button", "text": {"type": "plain_text", "text": "🧹 文脈リセット"},
                 "action_id": "reset_agent_context", "value": thread_key_for_btn},
            ]},
        ]
        try:
            progress_msg = await client.chat_postMessage(
                channel=channel, text="🔎 確認しています…少々お待ちください✨",
                thread_ts=thread_ts, blocks=progress_blocks,
            )
            progress_ts = progress_msg["ts"]
            logger.info(f"[Mention] Initial progress message posted: ts={progress_ts}")
        except Exception as e:
            logger.error(f"[Mention] Failed to post initial progress message: {e}", exc_info=True)

        async def update_progress(status_text: str):
            nonlocal progress_ts
            try:
                # 2026-07-16 修正: blocks無しのchat_updateはblocksを落とすため、
                # 最初の進捗更新で🛑停止/🧹リセットボタンが消えていた。
                # sectionに進捗文を入れ、ボタン行を常に維持する。
                _blocks = [
                    {"type": "section", "text": {"type": "mrkdwn", "text": status_text[:2900]}},
                    progress_blocks[1],  # 🛑停止 / 🧹文脈リセット ボタン行
                ]
                if progress_ts:
                    await client.chat_update(
                        channel=channel, ts=progress_ts, text=status_text, blocks=_blocks
                    )
                else:
                    # progress_tsがない場合は新規投稿してtsを取得
                    msg = await client.chat_postMessage(
                        channel=channel, text=status_text, thread_ts=thread_ts, blocks=_blocks
                    )
                    progress_ts = msg["ts"]
                    logger.info(f"[Mention] Progress fallback: new message ts={progress_ts}")
            except Exception as e:
                logger.warning(f"[Mention] Progress update failed: {e}")

        files = all_files

        try:
            # ユーザー名をSlack APIで解決（キャッシュ付き）
            await self.memory.resolve_user_name(user, slack_client=client)

            # メモリにコンテキスト蓄積
            self.memory.update_channel_context(channel, text, user)
            self.memory.record_interaction(user, "mention")

            # 会話参加者追跡（結合テキストからメンションを抽出）
            mentioned = self._extract_mentioned_users(text)
            self.memory.track_participant(channel, thread_ts, user, text, mentioned)

            # files は debounce バッファから結合済み

            # メモリからコンテキストを構築
            memory_context = self._build_memory_context(channel, user, text, thread_ts=thread_ts)

            # スレッド内で聞かれた場合は、エージェント 自身の投稿（議事録など）も含めて
            # スレッド全文を読み直してから答える
            thread_transcript = await self._build_thread_transcript(channel, thread_ts)
            if thread_transcript:
                memory_context = f"{thread_transcript}\n\n{memory_context}"

            # タスクコマンドの検出
            task_cmd = parse_task_command(text)
            if task_cmd and hasattr(self.tool_executor, 'task_manager'):
                cmd_result = await handle_task_command(
                    command=task_cmd,
                    task_manager=self.tool_executor.task_manager,
                    user=user,
                )
                if cmd_result:
                    if progress_ts:
                        await client.chat_update(channel=channel, ts=progress_ts, text=cmd_result)
                    else:
                        await client.chat_postMessage(channel=channel, text=cmd_result, thread_ts=thread_ts)
                    return

            # ファイルDL中なら進捗更新
            if files:
                await update_progress(f"🔎 確認しています…📎 ファイル{len(files)}件を読み込みました")

            result = await self.ai_engine.process_message(
                text=text,
                channel=channel,
                thread_ts=thread_ts,
                files=files,
                tool_executor=self.tool_executor,
                context={"user": user, "channel": channel, "thread_ts": thread_ts, "memory": self.memory, "slack_client": client,
                "skill_manager": getattr(self.tool_executor, "skill_manager", None), "is_dm": False,
            },
                memory_context=memory_context,
                progress_callback=update_progress,
            )

            # Opus Routerがshould_respond=Falseを返した場合はスキップ
            if not result.get("should_respond", True) and not result.get("text"):
                logger.info(f"[Router] Skipping response (should_respond=False)")
                # プログレスメッセージがある場合は削除
                if progress_ts:
                    try:
                        await client.chat_delete(channel=channel, ts=progress_ts)
                    except Exception:
                        pass
                return

            logger.info(f"[Mention] process_message returned: text={repr(result.get('text', '')[:200])}, keys={list(result.keys())}")

            # ── Clarify: モデルが意図確認を求めた場合はボタンUIに変換（2026-07-16） ──
            if await self._maybe_handle_clarify(
                result, original_text=text, channel=channel, thread_ts=thread_ts,
                user=user, client=client, progress_ts=progress_ts,
            ):
                self._active_threads.mark_replied(channel, thread_ts)
                return

            await self._send_response(result, channel, thread_ts, say, client, user=user, progress_ts=progress_ts, mention_back=True)

            # ActiveThreadTracker: このスレッドにボットが返信した記録
            self._active_threads.mark_replied(channel, thread_ts)

            # 会話ログを記録（タイムスタンプ付き）
            self.memory.log_conversation(
                user=user, channel=channel, text=text,
                response_summary=result.get("text", "")[:200],
                thread_ts=thread_ts,
            )

            # パーソナライズ自動学習
            self.memory.auto_learn_user_style(user, text, result.get("text", ""))

            # ── ランダムフィードバック収集 ──
            # ⚠️ エラー・警告で終わった応答にフィードバックは求めない
            # （ユーザーからの要望: エラー時はフィードバック催促を出さない）
            response_text = result.get("text", "")
            _error_markers = (
                "☁️ 処理中にエラー",
                "⚠️ 処理ステップが上限",
                "⏰ 処理ステップが",
                "⏰ タイムアウト",
                "途中で終了しました",
                "ここで処理が中断しました",
                "処理が途中で止まってしまいました",
                "応答を生成できませんでした",
            )
            has_error = any(marker in response_text for marker in _error_markers)
            # フィードバックUIは _send_response 内の _send_feedback_buttons に一本化済み。
            # 旧 _send_feedback_request は channel/thread_ts を持たず低評価分析が永久スキップ
            # になるため呼ばない（二重UI解消 + 自己改善ループの入力データを正常化）。

            # ナレッジインデックスに追加（重要度高め：直接の依頼）
            self.memory.index_message(channel, user, text, importance=0.7)

            # 非同期で知識抽出・保存
            self._maybe_save_and_extract(channel, user)

        except Exception as e:
            had_error = True
            logger.error(f"Mention handling error: {e}", exc_info=True)
            error_text = "☁️ 処理中にエラーが発生しました。もう一度お試しいただくか、管理者にご連絡ください。"
            for _attempt in range(3):
                try:
                    if progress_ts:
                        await client.chat_update(channel=channel, ts=progress_ts, text=error_text)
                    else:
                        await client.chat_postMessage(channel=channel, text=error_text, thread_ts=thread_ts)
                    break
                except Exception as _notify_err:
                    if _attempt == 2:
                        logger.critical(f"エラー通知の送信に3回失敗（ユーザーに無応答）: {_notify_err}")
                    else:
                        await asyncio.sleep(2 * (_attempt + 1))

        # リアクション管理: 完了/エラーのリアクション付与
        for rm in reaction_managers:
            if had_error:
                await rm.error()
            else:
                await rm.complete()

    # ──────────────────────────────────────────────
    # DM Debounce処理
    # ──────────────────────────────────────────────
    async def _debounce_fire_dm(self, thread_key: str):
        """DM用Debounceタイマー満了 → バッファされたメッセージをまとめて処理"""
        await asyncio.sleep(self._debounce_seconds)

        async with self._debounce_lock:
            debounce = self._thread_debounce.get(thread_key)
            if not debounce or not debounce["messages"]:
                return
            debounce["processing"] = True
            debounce["timer"] = None

        try:
            await self._process_dm_batch(thread_key)
        finally:
            async with self._debounce_lock:
                debounce = self._thread_debounce.get(thread_key)
                if debounce:
                    debounce["processing"] = False
                    if debounce["queued_messages"]:
                        debounce["messages"] = debounce["queued_messages"]
                        debounce["queued_messages"] = []
                        debounce["timer"] = asyncio.create_task(
                            self._debounce_fire_dm(thread_key)
                        )
                    else:
                        del self._thread_debounce[thread_key]

    async def _process_dm_batch(self, thread_key: str):
        """DM用: Debounceバッファのメッセージをまとめて処理"""
        debounce = self._thread_debounce.get(thread_key)
        if not debounce:
            return

        messages = debounce["messages"]
        channel = debounce["channel"]
        effective_thread_ts = debounce["thread_ts"]
        say = debounce["say"]
        client = debounce["client"]

        combined_texts = []
        all_files = []
        primary_user = messages[0]["user"]

        for msg in messages:
            combined_texts.append(msg["text"])
            if msg["files"]:
                all_files.extend(msg["files"])

        if len(messages) > 1:
            text = "\n\n".join(combined_texts)
            logger.info(f"[Debounce/DM] Combined {len(messages)} messages for {thread_key}")
        else:
            text = combined_texts[0]

        user = primary_user
        files = all_files

        # ── 一次返事を最優先で投稿 ──
        progress_ts = None
        # 2026-05-29: DM 側も 🛑 停止 / 🧹 文脈リセット ボタン併設
        dm_thread_key_for_btn = f"{channel}:{effective_thread_ts}" if effective_thread_ts else channel
        dm_progress_blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": "🔎 確認しています…少々お待ちください✨"}},
            {"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "🛑 停止"},
                 "action_id": "stop_agent", "value": dm_thread_key_for_btn, "style": "danger"},
                {"type": "button", "text": {"type": "plain_text", "text": "🧹 文脈リセット"},
                 "action_id": "reset_agent_context", "value": dm_thread_key_for_btn},
            ]},
        ]
        try:
            progress_msg = await client.chat_postMessage(
                channel=channel, text="🔎 確認しています…少々お待ちください✨",
                thread_ts=effective_thread_ts, blocks=dm_progress_blocks,
            )
            progress_ts = progress_msg["ts"]
        except Exception as e:
            logger.error(f"[DM] Failed to post initial progress message: {e}", exc_info=True)

        async def update_progress(status_text: str):
            nonlocal progress_ts
            try:
                # 2026-07-16 修正: blocksを維持しないと🛑/🧹ボタンが最初の更新で消える
                _blocks = [
                    {"type": "section", "text": {"type": "mrkdwn", "text": status_text[:2900]}},
                    dm_progress_blocks[1],
                ]
                if progress_ts:
                    await client.chat_update(channel=channel, ts=progress_ts, text=status_text, blocks=_blocks)
                else:
                    msg = await client.chat_postMessage(channel=channel, text=status_text, thread_ts=effective_thread_ts, blocks=_blocks)
                    progress_ts = msg["ts"]
            except Exception as e:
                logger.warning(f"[DM] Progress update failed: {e}")

        try:
            # ユーザー名をSlack APIで解決（キャッシュ付き）
            await self.memory.resolve_user_name(user, slack_client=client)

            self.memory.update_channel_context(channel, text, user)
            self.memory.record_interaction(user, "dm")

            mentioned = self._extract_mentioned_users(text)
            self.memory.track_participant(channel, effective_thread_ts, user, text, mentioned)

            memory_context = self._build_memory_context(channel, user, text, thread_ts=effective_thread_ts)

            if files:
                await update_progress(f"🔎 確認しています…📎 ファイル{len(files)}件を読み込みました")

            result = await self.ai_engine.process_message(
                text=text,
                channel=channel,
                thread_ts=effective_thread_ts,
                files=files,
                tool_executor=self.tool_executor,
                context={"user": user, "channel": channel, "thread_ts": effective_thread_ts, "memory": self.memory, "slack_client": client, "is_dm": True},
                memory_context=memory_context,
                progress_callback=update_progress,
            )
            logger.info(f"[DM] process_message returned: text={repr(result.get('text', '')[:200])}, keys={list(result.keys())}")
            await self._send_response(result, channel, effective_thread_ts, say, client, user=user, progress_ts=progress_ts)

            # ActiveThreadTracker: DMスレッドにも返信記録
            self._active_threads.mark_replied(channel, effective_thread_ts)

            self.memory.log_conversation(
                user=user, channel=channel, text=text,
                response_summary=result.get("text", "")[:200],
                thread_ts=effective_thread_ts,
            )
            self.memory.auto_learn_user_style(user, text, result.get("text", ""))
            self.memory.index_message(channel, user, text, importance=0.6)
            self._maybe_save_and_extract(channel, user)
        except Exception as e:
            logger.error(f"DM handling error: {e}", exc_info=True)
            try:
                error_text = "☁️ 処理中にエラーが発生しました。もう一度お試しいただくか、管理者にご連絡ください。"
                if progress_ts:
                    await client.chat_update(channel=channel, ts=progress_ts, text=error_text)
                else:
                    await client.chat_postMessage(channel=channel, text=error_text, thread_ts=effective_thread_ts)
            except Exception:
                pass

    # ──────────────────────────────────────────────
    # 自発的タスク検出
    # ──────────────────────────────────────────────
    async def _handle_message(self, event: dict, say, client):
        """通常メッセージの処理"""
        if self._is_duplicate_event(event, event_type="message"):
            return

        channel = event["channel"]
        channel_type = event.get("channel_type", "")
        text = event.get("text", "")
        user = event.get("user", "unknown")
        thread_ts = event.get("thread_ts")
        logger.info(f"[Message] Received: user={user}, channel={channel}, type={channel_type}, text={text[:80]}")

        # botメンション付きメッセージはapp_mentionハンドラに任せる（二重処理防止）
        # ※DMではapp_mentionが発火しないため、DM(im)は除外して通す
        bot_uid = self.settings.bot_user_id or ""
        if bot_uid and f"<@{bot_uid}>" in text and channel_type != "im":
            logger.info(f"[Message] Skipping bot-mentioned message, app_mention handler will process: {text[:80]}")
            return

        # 確認への返答（execute_code承認など）はボタンでだけ受け取る（2026-09-30）

        # X投稿・X返信・Gmail返信・メール送信の修正版と、dev_task のタスク内容は、
        # スレッドの発言からは拾わない。ボタン → 入力フォームでだけ受け取る
        # （2026-09-30 オーナー指示。スレッドのコメントが修正版として外部に送られうるため）

        # DMの場合は常に応答（ただし他ユーザー宛メンションは無視）— debounce対応
        if channel_type == "im":
            mentioned_users = self._extract_mentioned_users(text)
            bot_uid = self.settings.bot_user_id or ""
            # メンションが含まれていて、エージェント宛でない場合は無視
            if mentioned_users and bot_uid and bot_uid not in mentioned_users:
                logger.info(f"DM contains mention to other users {mentioned_users}, ignoring")
                return

            # app_mentionイベントが同一メッセージを既に処理していないかチェック
            # （一部のSlack構成ではDMでもapp_mentionが発火する）
            base_key = event.get("client_msg_id") or f"{channel}-{event.get('event_ts', event.get('ts', ''))}"
            if f"app_mention:{base_key}" in self._processed_events:
                logger.info(f"[DM] Same message already processed by app_mention, skipping: {base_key}")
                return

            effective_thread_ts = thread_ts or event["ts"]
            thread_key = f"{channel}:{effective_thread_ts}"
            files = await self._extract_files(event, client)

            # ── Debounce: DM もスレッド単位でバッファリング ──
            async with self._debounce_lock:
                debounce = self._thread_debounce.get(thread_key)

                if debounce and debounce.get("processing"):
                    debounce["queued_messages"].append({"text": text, "user": user, "files": files, "event": event})
                    logger.info(f"[Debounce/DM] Queued message while processing: {thread_key}")
                    return

                if debounce and debounce.get("timer"):
                    debounce["timer"].cancel()
                    debounce["messages"].append({"text": text, "user": user, "files": files, "event": event})
                    logger.info(f"[Debounce/DM] Added message, resetting timer: {thread_key}")
                else:
                    self._thread_debounce[thread_key] = {
                        "messages": [{"text": text, "user": user, "files": files, "event": event}],
                        "queued_messages": [],
                        "processing": False,
                        "timer": None,
                        "channel": channel,
                        "thread_ts": effective_thread_ts,
                        "say": say,
                        "client": client,
                        "is_dm": True,
                    }
                    debounce = self._thread_debounce[thread_key]

                debounce["timer"] = asyncio.create_task(
                    self._debounce_fire_dm(thread_key)
                )
            return

        # ── スレッド内のエージェント処理中に追加メッセージが来た場合、キューに追加 ──
        if thread_ts:
            thread_key = f"{channel}:{thread_ts}"
            async with self._debounce_lock:
                debounce = self._thread_debounce.get(thread_key)
                if debounce and debounce.get("processing"):
                    # エージェントが処理中 → メンションなしでも同スレッドならキューに追加
                    dm_files = await self._extract_files(event, client)
                    debounce["queued_messages"].append({"text": text, "user": user, "files": dm_files, "event": event})
                    logger.info(f"[Debounce] Non-mention message queued to active thread: {thread_key}")
                    return
                elif debounce and debounce.get("timer"):
                    # debounceタイマー中 → 追加メッセージとしてバッファに追加
                    debounce["timer"].cancel()
                    dm_files = await self._extract_files(event, client)
                    debounce["messages"].append({"text": text, "user": user, "files": dm_files, "event": event})
                    debounce["timer"] = asyncio.create_task(
                        self._debounce_fire(thread_key)
                    )
                    logger.info(f"[Debounce] Non-mention message added to debounce buffer: {thread_key}")
                    return

        # ユーザー名をSlack APIで解決（キャッシュ付き・全メッセージで蓄積）
        await self.memory.resolve_user_name(user, slack_client=self.app.client)

        # メモリにチャンネルコンテキストを常に蓄積（反応しなくても学習する）
        self.memory.update_channel_context(channel, text, user)

        # チャンネル横断ナレッジに低重要度でインデックス（全メッセージから学習）
        self.memory.index_message(channel, user, text, importance=0.3)

        # ── スレッド内のエージェント宛て承認リアクション ──
        if thread_ts:
            await self._maybe_react_to_thread_ack(event, client)

        # ── トリアージ: チャンネルメッセージの空気読み（silent/react/reply 3択） ──
        await self._triage_and_act(event, text, user, channel, thread_ts, say, client)

    # ──────────────────────────────────────────────
    # Bot メッセージ処理（特定botへの自動応答）
    # ──────────────────────────────────────────────
    async def _handle_bot_message(self, event: dict, say, client):
        """botメッセージの処理（既定では何もしない。自分自身の発言は必ず無視する）"""
        text = event.get("text", "")
        username = event.get("username", "")
        bot_id = event.get("bot_id", "")
        bot_profile_name = event.get("bot_profile", {}).get("name", "")
        channel = event.get("channel", "")

        # 自分自身のメッセージは無視（複数手段で判定: 無限ループ防止）
        # 1. bot_user_idで判定（起動時にauth_testで取得済み）
        if self.settings.bot_user_id and event.get("user") == self.settings.bot_user_id:
            return
        # 2. bot_user_idが未取得の場合 → 安全のため全botメッセージをスキップ
        if not self.settings.bot_user_id:
            logger.warning("bot_user_id not yet resolved; skipping bot message to prevent loop")
            return

        # 業務で反応したい bot（通知bot等）がある時は、ここで判定して処理を足す

    # ── スレッド内 承認/了解リアクション ──
    # 好意的なメッセージパターン → ❤️ heart
    _ACK_POSITIVE = re.compile(
        r"^(おk|ok|おけ|了解|りょ|りょうかい|ありがとう|あざす|あざっす|さんきゅ|thanks|thx|"
        r"わかった|分かった|把握|承知|おっけー|オッケー|いいね|ナイス|nice|"
        r"直す|直しとく|やっとく|対応する|対応します|やります|やるね|"
        r"👍|👌|✅|🙏|ありがと|あり|助かる|いい感じ|完璧|great|good|perfect).*$",
        re.IGNORECASE,
    )
    # ネガティブなメッセージパターン → 😢 crying_cat_face
    _ACK_NEGATIVE = re.compile(
        r"^(ごめん|すまん|すみません|sorry|残念|だめ|ダメ|うまくいかない|できない|"
        r"むずい|難しい|厳しい|無理|つらい|困った|まじか|えぇ|うーん).*$",
        re.IGNORECASE,
    )

    async def _maybe_react_to_thread_ack(self, event: dict, client):
        """
        スレッド内でエージェントの直後の返信が確認・承認系メッセージの場合、スタンプでリアクション。

        判定ルール（すべて満たす場合のみ反応）:
        1. スレッド内の返信である（thread_ts あり）
        2. エージェントの投稿が直前のメッセージである（直接の返信）
        3. 短い承認/確認系メッセージである（パターンマッチ）
        4. 30文字以内（長文は会話の続きなので無視）
        """
        text = event.get("text", "").strip()
        channel = event.get("channel", "")
        thread_ts = event.get("thread_ts", "")
        message_ts = event.get("ts", "")

        # 条件3 & 4: 短い承認/感情系メッセージか + リアクション種別判定
        cleaned = self._clean_mention(text).strip()
        if len(cleaned) > 30:
            return

        if self._ACK_POSITIVE.match(cleaned):
            reaction_emoji = "heart"
        elif self._ACK_NEGATIVE.match(cleaned):
            reaction_emoji = "crying_cat_face"
        else:
            return  # どちらにもマッチしない → 反応しない

        # 条件2: 直前のメッセージがエージェントか確認
        try:
            result = await client.conversations_replies(
                channel=channel,
                ts=thread_ts,
                limit=50,
            )
            messages = result.get("messages", [])
            if len(messages) < 2:
                return

            # 現在のメッセージの直前のメッセージを探す
            prev_msg = None
            for i, msg in enumerate(messages):
                if msg.get("ts") == message_ts and i > 0:
                    prev_msg = messages[i - 1]
                    break

            if not prev_msg:
                return

            # 直前のメッセージがエージェントの投稿か
            bot_uid = self.settings.bot_user_id or ""
            prev_user = prev_msg.get("user", "")
            prev_bot_id = prev_msg.get("bot_id", "")
            is_agent_prev = (bot_uid and prev_user == bot_uid) or bool(prev_bot_id and prev_user == bot_uid)

            if not is_agent_prev:
                return

            # すべての条件を満たした → 内容に応じたリアクション
            await client.reactions_add(
                channel=channel,
                name=reaction_emoji,
                timestamp=message_ts,
            )
            logger.info(f"Thread ack reaction '{reaction_emoji}' sent in {channel}")

        except Exception as e:
            logger.debug(f"Thread ack reaction check failed: {e}")



    # ──────────────────────────────────────────────
    # トリアージ: 空気読みシステム（OpenRyoko inspired）
    # silent / react / reply の3択で「そもそも反応すべきか」を判定
    # ──────────────────────────────────────────────

    # react モード用の絵文字マッピング（感情に応じたリアクション）
    _TRIAGE_REACT_EMOJI = {
        "agree": "white_check_mark",
        "thanks": "heart",
        "celebrate": "tada",
        "sad": "crying_cat_face",
        "funny": "joy",
        "good_news": "raised_hands",
        "neutral": "eyes",
    }

    # ── proactive 返信の重複防止 (channel:ts 単位で1回だけ) ──
    def _ensure_proactive_store(self) -> None:
        if getattr(self, "_proactive_store_inited", False):
            return
        import os as _os, json as _json
        base = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "memory_data")
        self._proactive_store_path = _os.path.join(base, "proactive_responded.json")
        self._proactive_responded_order = []
        self._proactive_responded_set = set()
        try:
            with open(self._proactive_store_path) as f:
                data = _json.load(f)
            self._proactive_responded_order = list(dict.fromkeys(data.get("keys", [])))
            self._proactive_responded_set = set(self._proactive_responded_order)
        except Exception:
            pass
        self._proactive_store_inited = True
        logger.info(f"[Triage] proactive-dedup store loaded: {len(self._proactive_responded_set)} keys")

    def _proactive_already(self, channel: str, ts: str) -> bool:
        if not ts:
            return False
        self._ensure_proactive_store()
        return f"{channel}:{ts}" in self._proactive_responded_set

    def _proactive_mark(self, channel: str, ts: str) -> None:
        if not ts:
            return
        self._ensure_proactive_store()
        key = f"{channel}:{ts}"
        if key in self._proactive_responded_set:
            return
        self._proactive_responded_set.add(key)
        self._proactive_responded_order.append(key)
        if len(self._proactive_responded_order) > 3000:
            self._proactive_responded_order = self._proactive_responded_order[-3000:]
            self._proactive_responded_set = set(self._proactive_responded_order)
        try:
            import os as _os, json as _json
            _os.makedirs(_os.path.dirname(self._proactive_store_path), exist_ok=True)
            tmp = self._proactive_store_path + ".tmp"
            with open(tmp, "w") as f:
                _json.dump({"keys": self._proactive_responded_order}, f)
            _os.replace(tmp, self._proactive_store_path)
        except Exception as e:
            logger.debug(f"[Triage] proactive-dedup save failed: {e}")

    def _is_addressed_to_other(self, text: str) -> bool:
        """メッセージが他ユーザー/他bot宛か（決定論判定）。
        プロアクティブ経路に来る時点で エージェント 宛メンションは app_mention 経路へ分岐済みなので、
        <@U...> が残っていれば宛先は エージェント 以外（Notion 等の他 bot も <@U...> 形式）。"""
        mentioned = self._extract_mentioned_users(text)
        if not mentioned:
            return False
        bot_uid = self.settings.bot_user_id or ""
        return not (bot_uid and bot_uid in mentioned)

    def _should_propose(self, judgment: dict) -> bool:
        """judge結果を実際に投稿すべきか。respondは確信75以上、suggestは85以上を要求。
        （suggest=「確信がない」バケットが respond と同格で投稿されていた構造矛盾を解消）。"""
        action = judgment.get("action", "ignore")
        raw = judgment.get("confidence", None)
        try:
            conf = int(raw) if raw is not None else None
        except (TypeError, ValueError):
            conf = None
        # respond は明確な依頼なので confidence 欠落時は従来通り通す（明白ケースを潰さない）
        if action == "respond" and (conf is None or conf >= 75):
            return True
        # suggest は「必要そう」レベル。明示的に高確信(85+)のときだけ投稿
        if action == "suggest" and (conf is not None and conf >= 85):
            return True
        logger.info(f"[Triage→Judge] suppressed: action={action}, confidence={conf}")
        return False

    # ──────────────────────────────────────────────
    # Tier1 軽反応: エージェントへの名前言及(非@)への自発リアクション
    # ──────────────────────────────────────────────

    def _ensure_reply_emoji(self, reply: str, emoji_name: str) -> str:
        """非メンション応答(reply本文)に絵文字が無ければ末尾に1つ添える（オーナー指示 2026-09-03）。"""
        if not reply:
            return reply
        # 既に絵文字(:shortcode: or Unicode絵文字)を含むなら触らない
        if re.search(r":[a-z0-9_+\-]+:", reply) or re.search(
            "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF"
            "\U00002190-\U000021FF\U00002300-\U000023FF\U0000FE0F]",
            reply,
        ):
            return reply
        name_to_char = {
            "blush": "☺️", "relaxed": "☺️", "smile": "😊",
            "smiley": "😊", "grin": "😄", "pray": "🙏",
            "bow": "🙏", "tada": "🎉", "sparkles": "✨",
            "muscle": "💪", "cry": "😢", "sob": "😭",
            "heart": "❤️", "hearts": "💕", "umbrella": "☂️",
            "raised_hands": "🙌", "clap": "👏", "wink": "😉",
            "star-struck": "🤩", "thumbsup": "👍", "+1": "👍",
            "sweat_smile": "😅", "joy": "😂",
        }
        char = name_to_char.get((emoji_name or "").strip().strip(":"), "✨")
        return f"{reply} {char}"


    async def _triage_and_act(self, event: dict, text: str, user: str,
                               channel: str, thread_ts: Optional[str],
                               say, client):
        """チャンネルメッセージのトリアージ。silent/react/reply の3択で空気を読む。

        バイパス条件（LLM呼び出しスキップ）:
        - proactive_channels 外 → silent
        - ActiveThread（TTL 10分） → reply（ボットが返信済みのスレッドは即応答）
        - 軽量フィルタ（_quick_relevance_check）で明らかに無関係 → silent

        LLMトリアージ:
        - Haikuで高速判定（旧方式の Opus → Haiku に変更、コスト1/10以下）
        - silent: 何もしない
        - react: 絵文字リアクションのみ
        - reply: 既存の _judge_with_claude → _propose_proactive_task フローへ
        """
        # バイパス1: proactive_channels 外は即 silent
        if self.settings.proactive_channels and channel not in self.settings.proactive_channels:
            return

        # 重複防止: 同じメッセージ(channel:ts)へのproactive処理は1回きり
        # （内部の再判定/リトライ/ActiveThread再入で同じ要約を複数回投稿する事故を防ぐ）
        _msg_ts = event.get("ts", "")
        if self._proactive_already(channel, _msg_ts):
            logger.info(f"[Triage] skip — already proactively handled {channel}:{_msg_ts}")
            return

        # バイパス2: ActiveThread — ボットが直近10分以内に返信したスレッド
        if thread_ts and self._active_threads.is_active(channel, thread_ts):
            # ActiveThread でも他者宛メンションは人同士の会話なので割り込まない
            if self._is_addressed_to_other(text):
                logger.info("[Triage] ActiveThread but 他者宛メンション → silent")
                return
            # 軽量足切りも適用（挨拶・相槌・共有のみ等で judge を呼ばない）
            if not self._quick_relevance_check(text, event):
                return
            logger.info(f"[Triage] ActiveThread bypass → judge (thread={channel}:{thread_ts})")
            judgment = await self._judge_with_claude(text, event)
            if judgment and self._should_propose(judgment):
                await self._propose_proactive_task(event, client, judgment)
            return

        # ステップ1: 軽量フィルタ（LLM呼び出しなしでコスト節約）
        if not self._quick_relevance_check(text, event):
            return

        # ステップ2: Haikuでトリアージ（silent/react/reply）
        triage_result = await self._triage_with_haiku(text, event)
        action = triage_result.get("action", "silent")
        _traw = triage_result.get("confidence", None)
        try:
            _tconf = int(_traw) if _traw is not None else None
        except (TypeError, ValueError):
            _tconf = None
        if action == "reply" and _tconf is not None and _tconf < 70:
            logger.info(f"[Triage] reply but confidence={_tconf} < 70 → silent")
            action = "silent"
        logger.info(f"[Triage] result={action}, text={text[:60]}")

        if action == "silent":
            return

        if action == "react":
            # 絵文字リアクションのみ付与
            emoji_type = triage_result.get("emoji", "neutral")
            emoji = self._TRIAGE_REACT_EMOJI.get(emoji_type, "eyes")
            try:
                await client.reactions_add(
                    channel=channel,
                    name=emoji,
                    timestamp=event.get("ts", ""),
                )
                logger.info(f"[Triage] React with :{emoji}: in {channel}")
            except Exception as e:
                logger.debug(f"[Triage] React failed: {e}")
            self._proactive_mark(channel, event.get("ts", ""))
            return

        if action == "reply":
            # 既存のClaude判断フローへ（高品質な介入判定）
            judgment = await self._judge_with_claude(text, event)
            if not judgment:
                return
            reason = judgment.get("reason", "")
            logger.info(f"[Triage→Judge] action={judgment.get('action','ignore')}, reason={reason[:80]}")
            if self._should_propose(judgment):
                await self._propose_proactive_task(event, client, judgment)

    async def _triage_with_haiku(self, text: str, event: dict) -> dict:
        """Haikuで高速トリアージ。silent/react/reply の3択。
        OpenRyoko の設計を踏襲: 沈黙寄り、確信60%未満は silent。
        """
        user = event.get("user", "unknown")
        has_files = bool(event.get("files"))

        prompt = f"""あなたはSlackチームのAIアシスタント「エージェント」です。
以下のチャンネルメッセージを見て、AIとしてどう反応すべきか3択で判定してください。

## メッセージ
{text[:500]}

## 添付ファイル
{"あり" if has_files else "なし"}

## 判定基準（沈黙寄り — 迷ったら必ず silent）

### silent（何もしない）— デフォルト
- ただの報告・共有・雑談・感想・独り言
- 特定の人・他のbotに向けた会話（<@...> 宛。あなた宛ではない）
- ファイル・画像・資料の共有そのもの（「〜です！」と渡しているだけ。受け渡しは人間同士で完結）
- メッセージの言い換え・確認・「要約しましょうか？」の申し出しかできない場合（情報価値ゼロ）
- 短い相槌・挨拶（おはよう、おつかれ、了解等）
- 人間の判断や感性が必要な議論
- すでに他の人が対応中・解決済みの会話
- 確信が60%未満の場合

### react（絵文字リアクションのみ）
- 良いニュースの共有 → emoji: good_news
- 感謝の言葉 → emoji: thanks
- 成果報告・お祝い事 → emoji: celebrate
- 面白い投稿 → emoji: funny
- 残念なニュース → emoji: sad
- 同意・了解 → emoji: agree

### reply（AIとして返信すべき）
- AIの得意領域の「明示的な」依頼（画像生成・翻訳・データ分析・調べ物等）。依頼の言葉が無いなら reply しない
- 「誰かやってくれない？」系でAIが対応可能
- 発言者が明確に困っている/助けを求めていて、AIが新しい事実や具体アクションを追加できる場合のみ

必ず以下のJSON形式のみで回答:
{{"action": "silent" | "react" | "reply", "emoji": "(reactの場合のみ: good_news/thanks/celebrate/funny/sad/agree/neutral)", "confidence": (0-100の整数)}}"""

        try:
            from utils.claude_engine import sdk_query
            result_text = await sdk_query(
                prompt=prompt,
                model="claude-haiku-4-5-20251001",
            )
            match = re.search(r'\{[^}]+\}', result_text)
            if match:
                return json.loads(match.group())
            return {"action": "silent"}
        except Exception as e:
            logger.error(f"[Triage] Haiku triage error: {e}")
            # fail-open: bot_user_id が判明していれば silent（割り込み防止）
            return {"action": "silent"}

    # ──────────────────────────────────────────────
    # 人間味のあるタスク検出（トリアージから呼ばれる）
    # ──────────────────────────────────────────────
    def _quick_relevance_check(self, text: str, event: dict) -> bool:
        """
        軽量な事前フィルタ。明らかに無関係なメッセージを弾く。
        ここを通過したものだけClaude判断に回す（API呼び出しコスト節約）。
        """
        if not text or len(text) < 5:
            return False

        # 決定論ルール: 他ユーザー/他bot宛メンションを含む＝人同士(or他bot)の会話。
        # エージェント は宛先ではないので、添付ファイルの有無に関わらず silent（無価値な割り込み防止）。
        if self._is_addressed_to_other(text):
            logger.info("[Triage] quick-check: 他者宛メンション → silent")
            return False

        # 雑談・挨拶・1単語メッセージなどは弾く
        trivial_patterns = [
            r"^(おはよう|おつかれ|お疲れ|ありがとう|了解|ok|lgtm|w+|草+|笑)$",
            r"^[\U0001F600-\U0001F64F]+$",  # 絵文字のみ
        ]
        for p in trivial_patterns:
            if re.match(p, text.strip(), re.IGNORECASE):
                return False

        # ファイル添付がある場合は通す
        if event.get("files"):
            return True

        # ある程度の長さ or 依頼っぽいキーワードがある場合は通す
        if len(text) > 20:
            return True

        # 短くても疑問形・依頼形なら通す
        if re.search(r"[?？]|して|ほしい|お願い|できる|教えて|誰か", text):
            return True

        return False

    async def _judge_with_claude(self, text: str, event: dict) -> Optional[dict]:
        """
        Claude Haikuを使って「介入してバリューを出せるか」を判断。
        基準: 自分が入ることで明確に価値が生まれるときだけ反応する。
        """
        channel = event.get("channel", "")
        user = event.get("user", "unknown")
        has_files = bool(event.get("files"))
        file_types = []
        if has_files:
            file_types = [f.get("mimetype", "") for f in event.get("files", [])]

        # メモリからコンテキストを取得
        channel_summary = self.memory.get_channel_summary(channel)
        user_ctx = self.memory.get_user_context(user)
        intervention_ctx = self.memory.get_intervention_context()

        prompt = f"""あなたはSlackチームの一員であるAIアシスタントです。
以下のチャンネルメッセージを見て、「自分が介入することで明確にバリューを出せるか」を判断してください。

## メッセージ
{text}

## 添付ファイル
{"あり: " + ", ".join(file_types) if has_files else "なし"}

{channel_summary}

{user_ctx}

{intervention_ctx}

## 判断基準（最重要: バリューベース）

### 大原則（迷ったら ignore）
- 「相手のメッセージの言い換え」「当たり前の確認」「要約・お手伝いの申し出だけ」は価値ゼロ = 必ず ignore
- 介入できるのは (1)発言者が明確に助け/作業を求めている、かつ (2)新しい事実・具体的な成果物・実行可能なアクションを追加できる、の両方を満たす時だけ
- 他のユーザーやbot宛（<@...>）のメッセージは、内容に関わらず ignore

### 介入する（respond）= 自分にしかできない or 自分がベスト
- 画像生成・画像編集・リサイズ・変換・加工が「明示的に依頼」されている（画像が添付されているだけでは依頼ではない）
- 資料作成・プレゼン作成の話をしている
- 翻訳・データ分析・コード生成など、AIの得意領域の依頼
- Notion/スプシの操作を明示的に頼まれている
- 調べ物・検索が必要な場面
- リマインダーの設定が必要な場面（「〇〇にリマインドして」「忘れないように通知して」）
- 今日/今週の活動サマリーが求められている
- 明確に困っていて、かつAIが最適な解決手段である

### 提案する（suggest）= 助けられるかもしれないが確信がない
- タスクの依頼っぽいが、人間同士で解決した方が良いかもしれない
- 「誰かやってくれない？」系で、AIが対応可能な内容
- 「〇〇さんに伝えて」「〇〇に連絡しないと」→ リマインダー提案

### 無視する（ignore）= バリューを出せない、または割り込みになる
- ただの報告・共有・雑談・感想・独り言
- ファイル・画像・資料を特定の人に渡しているだけのメッセージ（受け渡しは完結している）
- 自分の返信が「〜というメッセージですね」等の言い換えや、「〜しましょうか？」の申し出で終わる場合
- 特定の人・他bot に向けた会話（<@...> 宛等）
- 人間の判断や感性が必要な議論（意見、評価、感想を求めている）
- すでに他の人が対応中・解決済みの会話
- 一般的な質問（ググれば分かるレベル）— 押しつけがましいので無視
- 短い相槌やリアクション的なメッセージ
- 迷ったら必ずignore（余計な介入は信頼を損なう）

必ず以下のJSON形式のみで回答:
{{"action": "respond" | "suggest" | "ignore", "confidence": (0-100の整数), "reason": "判断理由を一文で", "value": "提供できる価値を一文で(ignoreの場合は空)", "skill": "画像生成|資料作成|データ分析|調べ物|翻訳|要約|画像編集|Notion|スプシ|リマインダー|活動サマリー|その他(該当するスキル名。ignoreの場合は空)", "proposal": "ユーザーに提案するメッセージ（2文以内。例: この資料、スライドにまとめましょうか？）"}}"""

        try:
            intervention_model = self.settings.intervention_model
            logger.info(f"Intervention judgment using model: {intervention_model}")
            response = await self.ai_engine.async_client.messages.create(
                model=intervention_model,
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            result_text = response.content[0].text.strip()
            match = re.search(r'\{[^}]+\}', result_text)
            if match:
                judgment = json.loads(match.group())
                # 介入パターンを記録（学習用）
                self.memory.record_intervention(
                    text[:200], judgment.get("action", "ignore")
                )
                return judgment
            return None
        except Exception as e:
            logger.error(f"Claude judgment error: {e}")
            return None

    # スキルごとの絵文字マッピング
    _SKILL_EMOJI = {
        "画像生成": "🎨", "資料作成": "📊", "データ分析": "📈",
        "調べ物": "🔍", "翻訳": "🌐", "要約": "📝",
        "画像編集": "🖼️", "Notion": "📓", "スプシ": "📗",
        "リマインダー": "⏰", "活動サマリー": "📋", "その他": "✨",
    }

    async def _propose_proactive_task(self, event: dict, client, judgment: dict):
        """スキル介入の提案をボタン付きで送信（承認後に実行）"""
        channel = event["channel"]
        thread_ts = event.get("thread_ts", event["ts"])
        text = event.get("text", "")
        user = event.get("user", "unknown")
        # 重複防止: このメッセージへの proactive 反応を「処理済み」として記録
        self._proactive_mark(channel, event.get("ts", ""))

        skill = judgment.get("skill", "その他")
        proposal = judgment.get("proposal", "")
        value = judgment.get("value", "")
        emoji = self._SKILL_EMOJI.get(skill, "✨")

        # 提案メッセージ（Claude生成のproposalを使う、なければvalue）
        display_text = proposal or value or f"{skill}でお手伝いできそうです"

        # 保留アクション情報をJSON化
        action_data = json.dumps({
            "original_text": text[:500],
            "skill": skill,
            "user": user,
            "channel": channel,
            "thread_ts": thread_ts,
        }, ensure_ascii=False)
        # Slack value上限は2000バイト
        if len(action_data.encode("utf-8")) > 1900:
            action_data = json.dumps({
                "original_text": text[:200],
                "skill": skill,
                "user": user,
                "channel": channel,
                "thread_ts": thread_ts,
            }, ensure_ascii=False)

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"{emoji} {display_text}",
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✅ お願い！"},
                        "action_id": "proactive_approve",
                        "style": "primary",
                        "value": action_data,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "🙅 大丈夫"},
                        "action_id": "proactive_reject",
                        "value": action_data,
                    },
                ],
            },
        ]

        await client.chat_postMessage(
            channel=channel,
            text=f"{emoji} {display_text}",
            blocks=blocks,
            thread_ts=thread_ts,
        )
        logger.info(f"Proactive proposal sent: skill={skill}, user={user}, channel={channel}")

    async def _handle_proactive_approval(self, body: dict, client, approved: bool):
        """プロアクティブ介入の承認/却下ボタン処理"""
        action = body["actions"][0]
        action_data = json.loads(action["value"])
        original_text = action_data["original_text"]
        skill = action_data["skill"]
        user = action_data["user"]
        channel = action_data["channel"]
        thread_ts = action_data["thread_ts"]
        clicker = body["user"]["id"]

        # ボタンメッセージを更新（ボタン除去）
        msg_ts = body["message"]["ts"]
        msg_channel = body["channel"]["id"]
        emoji = self._SKILL_EMOJI.get(skill, "✨")

        if approved:
            # ボタンを「実行中」に更新
            await client.chat_update(
                channel=msg_channel,
                ts=msg_ts,
                text=f"{emoji} 承認されました！対応中です…✨",
                blocks=[{
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"{emoji} 承認されました！対応中です…✨"},
                }],
            )

            # タスク実行
            try:
                files = []  # プロアクティブ介入時はファイルは元イベントから取れない場合が多い
                memory_context = self._build_memory_context(channel, clicker, original_text, thread_ts=thread_ts)
                result = await self.ai_engine.process_message(
                    text=(
                        "以下はSlackチャンネルで見かけたメッセージです。"
                        "チームメイトとして自然に手助けしてください。"
                        "押しつけがましくならず、でも具体的に役立つ形で。\n\n"
                        f"{original_text}"
                    ),
                    channel=channel,
                    thread_ts=thread_ts,
                    files=files,
                    tool_executor=self.tool_executor,
                    context={"user": clicker, "channel": channel, "thread_ts": thread_ts, "memory": self.memory, "slack_client": client, "proactive": True},
                    memory_context=memory_context,
                )

                # 結果を送信（フィードバックボタン付き）
                response_text = result.get("text", "")
                feedback_data = json.dumps({
                    "skill": skill,
                    "original_text": original_text[:200],
                    "user": clicker,
                }, ensure_ascii=False)

                # まず結果を送信
                await self._send_response(result, channel, thread_ts, None, client, user=clicker)

                # フィードバックボタンを送信
                feedback_blocks = [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "この介入は役に立ちましたか？ 今後の改善に活かします",
                        },
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "👍 役立った"},
                                "action_id": "intervention_feedback_good",
                                "value": feedback_data,
                            },
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "👎 不要だった"},
                                "action_id": "intervention_feedback_bad",
                                "value": feedback_data,
                            },
                        ],
                    },
                ]
                await client.chat_postMessage(
                    channel=channel,
                    text="この介入は役に立ちましたか？",
                    blocks=feedback_blocks,
                    thread_ts=thread_ts,
                )

            except Exception as e:
                logger.error(f"Proactive task execution error: {e}", exc_info=True)
                await client.chat_postMessage(
                    channel=channel,
                    text=f"☁️ 対応中にエラーが発生しました…: {str(e)[:200]}",
                    thread_ts=thread_ts,
                )

            # 介入パターンを記録（承認済み）
            self.memory.record_intervention(original_text[:200], "approved", skill=skill)

        else:
            # 却下 → ボタンを更新
            await client.chat_update(
                channel=msg_channel,
                ts=msg_ts,
                text=f"{emoji} 了解です！必要なときはメンションしてください",
                blocks=[{
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"{emoji} 了解です！必要なときはメンションしてください"},
                }],
            )
            # 介入パターンを記録（却下）
            self.memory.record_intervention(original_text[:200], "rejected", skill=skill)

    async def _handle_intervention_feedback(self, body: dict, client, is_good: bool):
        """介入フィードバックボタンの処理"""
        action = body["actions"][0]
        feedback_data = json.loads(action["value"])
        skill = feedback_data.get("skill", "")
        original_text = feedback_data.get("original_text", "")
        user = feedback_data.get("user", "")
        clicker = body["user"]["id"]

        msg_ts = body["message"]["ts"]
        msg_channel = body["channel"]["id"]

        if is_good:
            feedback_label = "👍 役立った"
            self.memory.record_intervention_feedback(original_text[:200], skill, "good", clicker)
            updated_text = "👍 フィードバックありがとうございます！今後も積極的にお手伝いします✨"
        else:
            feedback_label = "👎 不要だった"
            self.memory.record_intervention_feedback(original_text[:200], skill, "bad", clicker)
            updated_text = "🙏 フィードバックありがとうございます。次回の介入判断に活かします"

        # 介入フィードバックを即永続化（再起動で was_helpful が消えるのを防ぐ）
        # スコアボタン(_handle_feedback_button)は save() するが、ここは漏れていた
        self.memory.save()

        # ボタンをフィードバック結果に更新
        await client.chat_update(
            channel=msg_channel,
            ts=msg_ts,
            text=updated_text,
            blocks=[{
                "type": "section",
                "text": {"type": "mrkdwn", "text": updated_text},
            }],
        )
        logger.info(f"Intervention feedback: skill={skill}, feedback={feedback_label}, user={clicker}")

    # ──────────────────────────────────────────────
    # X投稿承認ボタン処理（投稿案 → 直接Xに投稿）
    # ──────────────────────────────────────────────






    # ──────────────────────────────────────────────
    # X返信承認ボタン処理
    # ──────────────────────────────────────────────


    # ──────────────────────────────────────────────
    # 好意的Xポストへのいいね/RTボタン処理
    # ──────────────────────────────────────────────


    # ──────────────────────────────────────────────
    # Gmail問い合わせ返信ボタン処理
    # ──────────────────────────────────────────────









    # ──────────────────────────────────────────────
    # dev_task: Slack経由のClaude Code開発機能
    # ──────────────────────────────────────────────

    # 開発系キーワード
    _DEV_KEYWORDS = [
        "開発して", "コード修正して", "バグ直して", "機能追加して",
        "実装して", "コード書いて", "修正して", "デプロイして",
    ]

    # dev_task入力待ちの状態: {thread_ts: {project_id, channel, user}}
    _dev_task_pending: dict = {}


    # ナレッジベース更新キーワード
    _KNOWLEDGE_REFRESH_KEYWORDS = [
        "ナレッジ更新", "ナレッジ再取得", "ナレッジリフレッシュ",
        "knowledge更新", "knowledgeリフレッシュ", "knowledge refresh",
        "ナレッジベース更新", "ナレッジを更新", "knowledge_refresher",
    ]







    # ──────────────────────────────────────────────
    # 重複議論検知
    # ──────────────────────────────────────────────



    # ──────────────────────────────────────────────
    # FAQ受動蓄積
    # ──────────────────────────────────────────────





    # ──────────────────────────────────────────────
    # FAQ自動蓄積・介入システム
    # ──────────────────────────────────────────────


    async def request_write_confirmation(
        self,
        channel: str,
        thread_ts: str,
        user: str,
        service: str,
        action_description: str,
        pending_action: dict,
        client,
    ):
        """外部サービスへの書き込み前にボタンで確認を求める"""
        confirm_key = f"{channel}:{thread_ts}"
        self._pending_confirmations[confirm_key] = {
            "user": user,
            "service": service,
            "action": pending_action,
        }

        if service == "コード実行":
            confirm_msg = (
                f"*🐍 Pythonコード* を実行します。よろしいですか？\n\n"
                f"{action_description}"
            )
        else:
            confirm_msg = (
                f"*{service}* への書き込みを行います。実行してよろしいですか？\n\n"
                f"> {action_description}"
            )

        # ボタン用ペイロード
        button_value = json.dumps({
            "confirm_key": confirm_key,
            "user": user,
        }, ensure_ascii=False)

        # Slack value上限は2000バイト
        if len(button_value.encode("utf-8")) > 1900:
            button_value = json.dumps({"confirm_key": confirm_key, "user": user}, ensure_ascii=False)

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": confirm_msg,
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✅ 実行する"},
                        "action_id": "write_confirm_approve",
                        "style": "primary",
                        "value": button_value,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "❌ キャンセル"},
                        "action_id": "write_confirm_reject",
                        "value": button_value,
                    },
                ],
            },
        ]

        await client.chat_postMessage(
            channel=channel,
            text=confirm_msg,
            blocks=blocks,
            thread_ts=thread_ts,
        )

    async def _check_confirmation_reply(
        self, text: str, channel: str, thread_ts: str, user: str, say, client
    ) -> bool:
        """確認への返答を処理。処理した場合Trueを返す。"""
        confirm_key = f"{channel}:{thread_ts}"
        pending = self._pending_confirmations.get(confirm_key)

        if not pending or pending["user"] != user:
            return False

        # メンションを除去して判定（\"ok @エージェント\" → \"ok\"）
        cleaned = self._clean_mention(text).strip().lower()
        affirmative_words = {"はい", "yes", "ok", "おk", "実行", "進めて", "お願い", "頼む", "おけ", "いいよ", "どうぞ"}
        negative_words = {"いいえ", "no", "キャンセル", "やめて", "中止", "cancel", "やめる", "だめ", "ダメ"}
        affirmative = cleaned in affirmative_words
        negative = cleaned in negative_words

        if affirmative:
            del self._pending_confirmations[confirm_key]
            action = pending["action"]
            tool_name = action["tool_name"]

            try:
                result = await self.tool_executor.execute(
                    tool_name, action["tool_input"],
                    context={"user": user, "channel": channel, "confirmed": True},
                )
            except Exception as e:
                await client.chat_postMessage(
                    channel=channel,
                    text=f"実行中にエラーが発生しました: {e}",
                    thread_ts=thread_ts,
                )
                return True

            # execute_code の場合 → 結果をAIエンジンに戻して分析コメントを生成
            if tool_name == "execute_code":
                # ツール結果を会話履歴に追加してAIに続きを生成させる
                result_text = json.dumps(result, ensure_ascii=False) if isinstance(result, dict) else str(result)

                # 進捗表示
                progress_msg = await client.chat_postMessage(
                    channel=channel, text="📊 結果を分析しています…✨", thread_ts=thread_ts
                )

                memory_context = self._build_memory_context(channel, user, "", thread_ts=thread_ts)

                # AIにツール結果を渡して分析コメントを生成
                ai_result = await self.ai_engine.process_message(
                    text=f"execute_codeの実行結果です。この結果を分析してユーザーにわかりやすく説明してください:\n\n{result_text[:3000]}",
                    channel=channel,
                    thread_ts=thread_ts,
                    tool_executor=self.tool_executor,
                    context={"user": user, "channel": channel, "thread_ts": thread_ts, "memory": self.memory, "slack_client": client,
                "skill_manager": getattr(self.tool_executor, "skill_manager", None),
            },
                    memory_context=memory_context,
                )
                await self._send_response(ai_result, channel, thread_ts, None, client, user=user, progress_ts=progress_msg["ts"])
            else:
                # Notion/Sheets等 → エージェント風の完了メッセージ
                response_text = self._format_write_result(tool_name, result)
                await client.chat_postMessage(
                    channel=channel, text=response_text, thread_ts=thread_ts
                )
            return True

        elif negative:
            del self._pending_confirmations[confirm_key]
            await client.chat_postMessage(
                channel=channel, text="了解です、キャンセルしました。", thread_ts=thread_ts
            )
            return True

        return False

    async def _handle_write_confirmation_button(self, body: dict, client, approved: bool):
        """書き込み確認ボタンが押されたときの処理"""
        action = body.get("actions", [{}])[0]
        try:
            payload = json.loads(action.get("value", "{}"))
        except json.JSONDecodeError:
            return

        confirm_key = payload.get("confirm_key", "")
        button_user = body.get("user", {}).get("id", "")
        channel = body["channel"]["id"]
        msg_ts = body["message"]["ts"]
        original_text = body["message"].get("text", "")

        pending = self._pending_confirmations.get(confirm_key)
        if not pending:
            # 既に処理済み or タイムアウト
            try:
                await client.chat_update(
                    channel=channel, ts=msg_ts,
                    text="⏰ この確認は期限切れです。",
                    blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "⏰ この確認は期限切れです。"}}],
                )
            except Exception:
                pass
            return

        # ボタンメッセージを更新（ボタン除去）
        status_emoji = "✅" if approved else "❌"
        status_text = "実行中…" if approved else "キャンセルされました"
        try:
            await client.chat_update(
                channel=channel, ts=msg_ts,
                text=f"{status_emoji} {status_text}（by <@{button_user}>）",
                blocks=[{
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"{status_emoji} *{status_text}*（by <@{button_user}>）"},
                }],
            )
        except Exception as e:
            logger.debug(f"Failed to update write confirmation message: {e}")

        if not approved:
            del self._pending_confirmations[confirm_key]
            return

        # 承認 → 実行
        del self._pending_confirmations[confirm_key]
        user = pending["user"]
        action_data = pending["action"]
        tool_name = action_data["tool_name"]
        # thread_ts を confirm_key から復元
        parts = confirm_key.split(":", 1)
        thread_ts = parts[1] if len(parts) > 1 else msg_ts

        try:
            result = await self.tool_executor.execute(
                tool_name, action_data["tool_input"],
                context={"user": user, "channel": channel, "confirmed": True},
            )
        except Exception as e:
            await client.chat_postMessage(
                channel=channel,
                text=f"⚠️ 実行中にエラーが発生しました…\n> {e}",
                thread_ts=thread_ts,
            )
            return

        # execute_code の場合 → 結果をAIエンジンに戻して分析コメントを生成
        if tool_name == "execute_code":
            result_text = json.dumps(result, ensure_ascii=False) if isinstance(result, dict) else str(result)
            progress_msg = await client.chat_postMessage(
                channel=channel, text="📊 結果を分析しています…✨", thread_ts=thread_ts
            )
            memory_context = self._build_memory_context(channel, user, "", thread_ts=thread_ts)
            ai_result = await self.ai_engine.process_message(
                text=f"execute_codeの実行結果です。この結果を分析してユーザーにわかりやすく説明してください:\n\n{result_text[:3000]}",
                channel=channel,
                thread_ts=thread_ts,
                tool_executor=self.tool_executor,
                context={"user": user, "channel": channel, "thread_ts": thread_ts, "memory": self.memory, "slack_client": client,
                    "skill_manager": getattr(self.tool_executor, "skill_manager", None),
                },
                memory_context=memory_context,
            )
            await self._send_response(ai_result, channel, thread_ts, None, client, user=user, progress_ts=progress_msg["ts"])
        else:
            # Notion/Sheets等 → エージェント風の完了メッセージ
            response_text = self._format_write_result(tool_name, result)
            await client.chat_postMessage(
                channel=channel, text=response_text, thread_ts=thread_ts
            )

    @staticmethod
    def _format_write_result(tool_name: str, result: dict) -> str:
        """書き込み操作の結果をエージェント風メッセージに変換"""
        if result.get("error"):
            return f"⚠️ 実行中にエラーが発生しました…\n> {result['error'][:300]}"

        url = result.get("url", "")
        title = result.get("title", "")
        message = result.get("message", "")

        if "notion_create_page" in tool_name or "create_page" in tool_name:
            link = f"\n<{url}|📎 Notionで開く>" if url else ""
            return f"✅ Notionにページを作成しました！\n📝 *{title}*{link}"
        elif "notion_update_page" in tool_name or "update_page" in tool_name:
            link = f"\n<{url}|📎 Notionで開く>" if url else ""
            return f"✅ Notionページを更新しました！{link}"
        elif "notion_write_database" in tool_name or "write_database" in tool_name:
            link = f"\n<{url}|📎 Notionで開く>" if url else ""
            return f"✅ データベースに追加しました！\n📝 *{title}*{link}" if title else f"✅ データベースに追加しました！{link}"
        elif "gsheet" in tool_name:
            return f"✅ スプレッドシートを更新しました！📊"
        elif message:
            return f"✅ {message}"
        else:
            return f"✅ 完了しました！"

    # ──────────────────────────────────────────────
    # リアクション
    # ──────────────────────────────────────────────
    async def _handle_reaction(self, event: dict, say, client):
        """リアクションによるタスクトリガー"""
        reaction = event.get("reaction", "")
        item = event.get("item", {})

        trigger_reactions = {
            "robot_face": "このメッセージの内容を分析してください",
            # 2026-07-06: :memo: 自動要約は無効化（利用者依頼）。
            # お礼など些末なメッセージにも無価値な要約を公開投稿してしまうため。
            "globe_with_meridians": "このメッセージを英語に翻訳してください",
            "jp": "このメッセージを日本語に翻訳してください",
        }

        if item.get("type") != "message":
            return

        channel = item["channel"]
        message_ts = item["ts"]

        if reaction not in trigger_reactions:
            return

        try:
            result = await client.conversations_history(
                channel=channel, latest=message_ts, limit=1, inclusive=True
            )
            if not result["messages"]:
                return

            original_text = result["messages"][0].get("text", "")
            prompt = f"{trigger_reactions[reaction]}\n\n対象メッセージ:\n{original_text}"

            ai_result = await self.ai_engine.process_message(
                text=prompt,
                channel=channel,
                thread_ts=message_ts,
                tool_executor=self.tool_executor,
                context={"channel": channel},
            )
            await self._send_response(ai_result, channel, message_ts, None, client)

        except Exception as e:
            logger.error(f"Reaction handler error: {e}")


    # ──────────────────────────────────────────────
    # ユーティリティ
    # ──────────────────────────────────────────────
    # テキスト系ファイルのMIMEタイプ（CSV/JSON/TSV等をダウンロードして中身を渡す）
    TEXT_MIMETYPES = {
        "text/csv", "text/plain", "text/tab-separated-values",
        "application/json", "application/csv",
        "text/comma-separated-values",
        "text/html",  # 2026-07-22: HTML添付をテキストとして読む（社内公開HTML等・並行修正を取り込み）
    }
    TEXT_EXTENSIONS = {".csv", ".json", ".tsv", ".txt", ".jsonl", ".ndjson", ".md", ".html", ".htm"}

    # バイナリドキュメント（テキスト抽出が必要）
    DOCUMENT_MIMETYPES = {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # .docx
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # .xlsx
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",  # .pptx
        "application/vnd.ms-excel",  # .xls (legacy)
        "application/msword",  # .doc (legacy)
    }
    DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".pptx", ".doc", ".xls"}

    @staticmethod
    def _extract_text_from_pdf(raw_data: bytes) -> str:
        """PDFからテキストを抽出"""
        try:
            from pypdf import PdfReader
        except ImportError:
            try:
                from PyPDF2 import PdfReader  # 古い環境向け
            except ImportError:
                return "(PDFの読み取りに必要な pypdf が入っていません。テキストファイル(.md, .txt)での入力をお試しください)"
        try:
            import io
            reader = PdfReader(io.BytesIO(raw_data))
            pages = []
            for i, page in enumerate(reader.pages):
                text = page.extract_text() or ""
                if text.strip():
                    pages.append(f"--- ページ {i+1} ---\n{text.strip()}")
            return "\n\n".join(pages) if pages else "(PDFからテキストを抽出できませんでした。スキャン画像のみのPDFの可能性があります)"
        except Exception as e:
            logger.error(f"PDF extraction error: {e}")
            return f"(PDF読み取りエラー: {e})"

    @staticmethod
    def _extract_text_from_docx(raw_data: bytes) -> str:
        """Word (.docx) からテキストを抽出"""
        try:
            from docx import Document
        except ImportError:
            return "(Wordの読み取りにはpython-docxが必要です。現在この環境では利用できません。テキストファイル(.md, .txt)での入力をお試しください)"
        try:
            import io
            doc = Document(io.BytesIO(raw_data))
            parts = []
            for para in doc.paragraphs:
                if para.text.strip():
                    # 見出しスタイルの場合はマークダウン形式に
                    style_name = para.style.name if para.style else ""
                    if "Heading 1" in style_name:
                        parts.append(f"# {para.text.strip()}")
                    elif "Heading 2" in style_name:
                        parts.append(f"## {para.text.strip()}")
                    elif "Heading 3" in style_name:
                        parts.append(f"### {para.text.strip()}")
                    else:
                        parts.append(para.text.strip())
            # テーブルも抽出
            for table in doc.tables:
                rows = []
                for row in table.rows:
                    cells = [cell.text.strip() for cell in row.cells]
                    rows.append(" | ".join(cells))
                if rows:
                    parts.append("\n".join(rows))
            return "\n\n".join(parts) if parts else "(Wordドキュメントからテキストを抽出できませんでした)"
        except Exception as e:
            logger.error(f"DOCX extraction error: {e}")
            return f"(Word読み取りエラー: {e})"

    @staticmethod
    def _extract_text_from_xlsx(raw_data: bytes) -> str:
        """Excel (.xlsx) からテキストを抽出"""
        try:
            from openpyxl import load_workbook
        except ImportError:
            return "(Excelの読み取りにはopenpyxlが必要です。現在この環境では利用できません。テキストファイル(.md, .txt, .csv)での入力をお試しください)"
        try:
            import io
            wb = load_workbook(io.BytesIO(raw_data), read_only=True, data_only=True)
            parts = []
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                rows = []
                for row in ws.iter_rows(values_only=True):
                    cells = [str(c) if c is not None else "" for c in row]
                    if any(c for c in cells):  # 空行をスキップ
                        rows.append(" | ".join(cells))
                if rows:
                    parts.append(f"## シート: {sheet_name}\n" + "\n".join(rows))
            wb.close()
            return "\n\n".join(parts) if parts else "(Excelからデータを抽出できませんでした)"
        except Exception as e:
            logger.error(f"XLSX extraction error: {e}")
            return f"(Excel読み取りエラー: {e})"

    @staticmethod
    def _extract_text_from_pptx(raw_data: bytes) -> str:
        """PowerPoint (.pptx) からテキストを抽出"""
        try:
            from pptx import Presentation
        except ImportError:
            return "(PowerPointの読み取りにはpython-pptxが必要です。現在この環境では利用できません。テキストファイル(.md, .txt)での入力をお試しください)"
        try:
            import io
            prs = Presentation(io.BytesIO(raw_data))
            parts = []
            for i, slide in enumerate(prs.slides):
                texts = []
                for shape in slide.shapes:
                    if shape.has_text_frame:
                        for para in shape.text_frame.paragraphs:
                            if para.text.strip():
                                texts.append(para.text.strip())
                    if shape.has_table:
                        for row in shape.table.rows:
                            cells = [cell.text.strip() for cell in row.cells]
                            texts.append(" | ".join(cells))
                if texts:
                    parts.append(f"--- スライド {i+1} ---\n" + "\n".join(texts))
            return "\n\n".join(parts) if parts else "(PowerPointからテキストを抽出できませんでした)"
        except Exception as e:
            logger.error(f"PPTX extraction error: {e}")
            return f"(PowerPoint読み取りエラー: {e})"

    # ファイルダウンロードの上限（メモリ枯渇防止）
    MAX_FILE_DOWNLOAD_BYTES = 10 * 1024 * 1024  # 10MB（画像・テキスト共通）
    MAX_TEXT_DOWNLOAD_BYTES = 5 * 1024 * 1024    # 5MB（テキスト系）

    async def _extract_files(self, event: dict, client) -> List[dict]:
        """イベントからファイルを抽出。画像はbase64、テキスト系はそのまま文字列で返す"""
        files_data = []
        for f in event.get("files", []):
            if not f.get("url_private"):
                continue
            mimetype = f.get("mimetype", "")
            filename = f.get("name", "unknown")
            ext = os.path.splitext(filename)[1].lower() if filename else ""
            file_size = f.get("size", 0)  # Slackが返すファイルサイズ

            is_image = mimetype.startswith("image/")
            is_text = (mimetype in self.TEXT_MIMETYPES or ext in self.TEXT_EXTENSIONS)
            is_document = (mimetype in self.DOCUMENT_MIMETYPES or ext in self.DOCUMENT_EXTENSIONS)

            if not is_image and not is_text and not is_document:
                continue

            # ── ダウンロード前のサイズチェック（Slackメタデータベース）──
            size_limit = self.MAX_FILE_DOWNLOAD_BYTES if (is_image or is_document) else self.MAX_TEXT_DOWNLOAD_BYTES
            if file_size and file_size > size_limit:
                logger.warning(
                    f"File too large, skipping download: {filename} "
                    f"({file_size:,} bytes > {size_limit:,} limit)"
                )
                files_data.append({
                    "filename": filename,
                    "type": "text" if is_text else "image",
                    "error": f"ファイルサイズが大きすぎます（{file_size:,} bytes）。"
                             f"上限は{size_limit // 1024 // 1024}MBです。",
                    "skipped": True,
                })
                continue

            try:
                logger.info(f"[FILE] Downloading: {filename} (mime={mimetype}, ext={ext}, size={file_size}, is_text={is_text}, is_doc={is_document}, is_img={is_image})")
                async with aiohttp.ClientSession() as session:
                    headers = {"Authorization": f"Bearer {self.settings.slack_bot_token}"}
                    async with session.get(f["url_private"], headers=headers) as resp:
                        if resp.status == 200:
                            # ── ストリーミング読み込み（サイズ制限付き）──
                            chunks = []
                            downloaded = 0
                            async for chunk in resp.content.iter_chunked(64 * 1024):
                                downloaded += len(chunk)
                                if downloaded > size_limit:
                                    logger.warning(
                                        f"File exceeded size limit during download: {filename} "
                                        f"({downloaded:,} bytes > {size_limit:,})"
                                    )
                                    break
                                chunks.append(chunk)

                            if downloaded > size_limit:
                                files_data.append({
                                    "filename": filename,
                                    "type": "text" if is_text else "image",
                                    "error": f"ダウンロード中にサイズ上限を超えました（{downloaded:,} bytes）。",
                                    "skipped": True,
                                })
                                continue

                            raw_data = b"".join(chunks)

                            if is_image:
                                # 画像もワークスペースにファイル保存して、Read tool 経由でLLMに見せる
                                # （SDKの user_prompt は str のみで画像のbase64を直接渡せないため）
                                workspace_dir = self.settings.workspace_dir or os.path.join(os.path.dirname(os.path.dirname(__file__)), "workspace")
                                os.makedirs(workspace_dir, exist_ok=True)
                                # 衝突回避のためタイムスタンプ付きで保存
                                safe_filename = f"{int(time.time())}_{filename}"
                                saved_path = os.path.join(workspace_dir, safe_filename)
                                try:
                                    with open(saved_path, "wb") as wf:
                                        wf.write(raw_data)
                                    logger.info(f"[FILE] Saved image to workspace: {saved_path} ({len(raw_data)} bytes)")
                                except Exception as save_err:
                                    logger.warning(f"[FILE] Failed to save image to {saved_path}: {save_err}")
                                    saved_path = ""

                                files_data.append({
                                    "mimetype": mimetype,
                                    "data": base64.b64encode(raw_data).decode("utf-8"),
                                    "filename": filename,
                                    "url": f["url_private"],
                                    "type": "image",
                                    "workspace_path": saved_path,
                                })
                            elif is_text:
                                # テキスト系ファイルはデコードして文字列で渡す
                                try:
                                    text_content = raw_data.decode("utf-8")
                                except UnicodeDecodeError:
                                    text_content = raw_data.decode("shift_jis", errors="replace")

                                # ワークスペースにも保存（execute_codeで分析用）
                                workspace_dir = self.settings.workspace_dir or os.path.join(os.path.dirname(os.path.dirname(__file__)), "workspace")
                                os.makedirs(workspace_dir, exist_ok=True)
                                saved_path = os.path.join(workspace_dir, filename)
                                try:
                                    with open(saved_path, "w", encoding="utf-8") as wf:
                                        wf.write(text_content)
                                    logger.info(f"[FILE] Saved text file to workspace: {saved_path} ({len(text_content)} chars)")
                                except Exception as save_err:
                                    logger.warning(f"[FILE] Failed to save text file to {saved_path}: {save_err}")
                                    saved_path = ""

                                MAX_TEXT_CHARS = 50000
                                truncated = len(text_content) > MAX_TEXT_CHARS
                                if truncated:
                                    text_content = text_content[:MAX_TEXT_CHARS]

                                files_data.append({
                                    "mimetype": mimetype,
                                    "text_content": text_content,
                                    "filename": filename,
                                    "url": f["url_private"],
                                    "type": "text",
                                    "truncated": truncated,
                                    "original_size": len(raw_data),
                                    "workspace_path": saved_path,
                                })
                            elif is_document:
                                # バイナリドキュメント → テキスト抽出
                                ext_lower = ext.lower()
                                if ext_lower == ".pdf":
                                    text_content = self._extract_text_from_pdf(raw_data)
                                elif ext_lower == ".docx":
                                    text_content = self._extract_text_from_docx(raw_data)
                                elif ext_lower == ".xlsx" or ext_lower == ".xls":
                                    text_content = self._extract_text_from_xlsx(raw_data)
                                elif ext_lower == ".pptx":
                                    text_content = self._extract_text_from_pptx(raw_data)
                                else:
                                    text_content = f"(未対応のドキュメント形式: {ext})"

                                # ワークスペースにバイナリ保存（execute_codeで分析用）
                                workspace_dir = self.settings.workspace_dir or os.path.join(os.path.dirname(os.path.dirname(__file__)), "workspace")
                                os.makedirs(workspace_dir, exist_ok=True)
                                saved_path = os.path.join(workspace_dir, filename)
                                try:
                                    with open(saved_path, "wb") as wf:
                                        wf.write(raw_data)
                                    logger.info(f"[FILE] Saved document to workspace: {saved_path} ({len(raw_data)} bytes)")
                                except Exception as save_err:
                                    logger.warning(f"[FILE] Failed to save document to {saved_path}: {save_err}")
                                    saved_path = ""

                                MAX_TEXT_CHARS = 50000
                                truncated = len(text_content) > MAX_TEXT_CHARS
                                if truncated:
                                    text_content = text_content[:MAX_TEXT_CHARS]

                                files_data.append({
                                    "mimetype": mimetype,
                                    "text_content": text_content,
                                    "filename": filename,
                                    "url": f["url_private"],
                                    "type": "text",
                                    "source_format": ext_lower,
                                    "truncated": truncated,
                                    "original_size": len(raw_data),
                                    "workspace_path": saved_path,
                                })
                                logger.info(f"Extracted {len(text_content)} chars from {filename} ({ext_lower})")
            except Exception as e:
                logger.error(f"[FILE] File download error for {filename}: {e}", exc_info=True)
                files_data.append({
                    "filename": filename,
                    "type": "text",
                    "error": f"ファイルダウンロードエラー: {str(e)[:200]}",
                    "skipped": True,
                })
        return files_data

    @staticmethod
    def _sanitize_slack_mrkdwn(text: str) -> str:
        """Markdown の **bold** を Slack mrkdwn の *bold* に変換。
        コードブロック（``` ... ```）内はそのまま保持する。"""
        import re
        parts = text.split("```")
        for i in range(len(parts)):
            if i % 2 == 0:  # コードブロック外
                parts[i] = re.sub(r'\*\*(.+?)\*\*', r'*\1*', parts[i])
        return "```".join(parts)

    @staticmethod
    def _split_for_slack(text: str, max_len: int = 3000) -> list:
        """Slack 投稿用にテキストを max_len 以下のチャンクへ分割。
        - 改行境界を優先（語やコードの途中で切らない）
        - コードブロック(```)を跨ぐ場合は閉じ、次チャンク冒頭で開き直して非破壊にする
        旧実装は固定 max_len でぶつ切りし、コードブロックや mrkdwn を途中で割っていた。
        """
        if len(text) <= max_len:
            return [text]
        fence = "```"
        chunks = []
        buf = ""
        in_code = False
        for line in text.split("\n"):
            candidate = (buf + "\n" + line) if buf else line
            # コードブロック内なら閉じフェンス("\n```")の分(4字)も max_len 内に収める
            close_cost = 4 if in_code else 0
            if len(candidate) + close_cost > max_len and buf:
                # 現在のバッファを確定（コードブロック内なら閉じる）
                chunks.append(buf + ("\n" + fence if in_code else ""))
                buf = (fence + "\n" + line) if in_code else line
            else:
                buf = candidate
            if line.lstrip().startswith(fence):
                in_code = not in_code
            # 単一行が max_len を超える稀なケースは強制分割
            while len(buf) > max_len:
                chunks.append(buf[:max_len])
                buf = buf[max_len:]
        if buf:
            chunks.append(buf)
        return chunks

    # ── Clarify（意図確認ボタン、2026-07-16 ハーネス改善） ──
    # モデルが「解釈の分岐が高価」と判断した時だけ <agent_clarify> JSONを応答末尾に
    # 付ける契約（persona側で定義）。ここでBlock Kitボタンに変換する。

    async def _maybe_handle_clarify(self, result: dict, original_text: str, channel: str,
                                    thread_ts: str, user: str, client, progress_ts) -> bool:
        """応答にclarify envelopeがあればボタンUIに変換してTrueを返す。

        envelopeが壊れている場合は result["text"] からブロックを除去だけして
        False（通常応答にフォールバック。生マーカーは絶対に見せない）。
        """
        try:
            from utils.assistant_outcome import parse_clarify
            cleaned, clarify = parse_clarify(result.get("text", "") or "")
            result["text"] = cleaned
            if not clarify or os.getenv("AGENT_CLARIFY", "1") != "1":
                return False
            cid = self.clarify_store.create(
                channel=channel, thread_ts=thread_ts, requester=user,
                original_request=original_text, question=clarify["question"],
                options=clarify["options"],
            )
            intro = cleaned.strip()
            q_text = (f"{intro}\n\n" if intro else "") + f"❓ {clarify['question']}"
            # action_id は1メッセージ内で一意でないと Slack が invalid_blocks で弾く
            # （2026-07-22 バグ修正: 全ボタン同一idで選択肢が描画されず消えていた）。
            # ハンドラ側は正規表現 ^agent_clarify_choice で受ける。
            buttons = [
                {"type": "button", "text": {"type": "plain_text", "text": o["label"][:70]},
                 "action_id": f"agent_clarify_choice_{i}",
                 "value": json.dumps({"cid": cid, "oid": o["id"]}, ensure_ascii=False)}
                for i, o in enumerate(clarify["options"])
            ]
            blocks = [
                {"type": "section", "text": {"type": "mrkdwn", "text": q_text[:2900]}},
                {"type": "actions", "elements": buttons},
            ]
            if progress_ts:
                await client.chat_update(channel=channel, ts=progress_ts,
                                         text=q_text[:2900], blocks=blocks)
            else:
                await client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                              text=q_text[:2900], blocks=blocks)
            logger.info(f"[Clarify] question rendered cid={cid} options={len(buttons)}")
            return True
        except Exception:
            logger.exception("[Clarify] render failed — falling back to plain response")
            return False

    async def _handle_clarify_choice(self, body, client):
        """Clarifyボタンのクリック処理（依頼者本人のみ・一回消費・期限つき）"""
        try:
            action = (body.get("actions") or [{}])[0]
            payload = json.loads(action.get("value") or "{}")
            cid, oid = payload.get("cid", ""), payload.get("oid", "")
            clicked_by = (body.get("user") or {}).get("id", "")
            channel = (body.get("channel") or {}).get("id", "")
            msg_ts = (body.get("message") or {}).get("ts", "")
            consumed = self.clarify_store.consume(cid, oid, clicked_by)
            if not consumed:
                return  # 依頼者以外/期限切れ/二重クリック → 静かに無視
            entry, option = consumed["entry"], consumed["option"]
            # 質問メッセージを選択結果に更新（ボタンを消す）
            try:
                await client.chat_update(
                    channel=channel, ts=msg_ts,
                    text=f"→ 「{option['label']}」で進めますね", blocks=[],
                )
            except Exception:
                pass
            continuation = (
                "以下は先ほどの確認への回答です。\n\n"
                f"元の依頼:\n{entry['original_request']}\n\n"
                f"確認質問: {entry['question']}\n"
                f"ユーザーが選んだ前提: {option.get('text') or option['label']}\n\n"
                "この前提で依頼を続行してください。同じ点を再確認せず、"
                "必要な読み取り・作成を実行してください。"
            )
            await self._run_clarify_continuation(
                channel=entry["channel"], thread_ts=entry["thread_ts"],
                user=entry["requester"], text=continuation, client=client,
            )
        except Exception:
            logger.exception("[Clarify] choice handling failed")

    async def _run_clarify_continuation(self, channel: str, thread_ts: str, user: str,
                                        text: str, client):
        """Clarify選択後の続行実行（メンションバッチの簡略版・同一スレッドでsession継続）"""
        progress_ts = None
        thread_key_for_btn = f"{channel}:{thread_ts}" if thread_ts else channel
        progress_blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": "選んでいただいた前提で進めますね…"}},
            {"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "🛑 停止"},
                 "action_id": "stop_agent", "value": thread_key_for_btn, "style": "danger"},
            ]},
        ]
        try:
            msg = await client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text="選んでいただいた前提で進めますね…", blocks=progress_blocks,
            )
            progress_ts = msg["ts"]
        except Exception:
            logger.exception("[Clarify] continuation progress post failed")

        async def update_progress(status_text: str):
            nonlocal progress_ts
            try:
                _blocks = [
                    {"type": "section", "text": {"type": "mrkdwn", "text": status_text[:2900]}},
                    progress_blocks[1],
                ]
                if progress_ts:
                    await client.chat_update(channel=channel, ts=progress_ts,
                                             text=status_text, blocks=_blocks)
            except Exception as e:
                logger.warning(f"[Clarify] progress update failed: {e}")

        try:
            memory_context = self._build_memory_context(channel, user, text, thread_ts=thread_ts)
            result = await self.ai_engine.process_message(
                text=text, channel=channel, thread_ts=thread_ts, files=None,
                tool_executor=self.tool_executor,
                context={"user": user, "channel": channel, "thread_ts": thread_ts,
                         "memory": self.memory, "slack_client": client,
                         "skill_manager": getattr(self.tool_executor, "skill_manager", None),
                         "is_dm": False},
                memory_context=memory_context, progress_callback=update_progress,
            )
            await self._send_response(result, channel, thread_ts, None, client, user=user,
                                      progress_ts=progress_ts, mention_back=False)
            self._active_threads.mark_replied(channel, thread_ts)
        except Exception:
            logger.exception("[Clarify] continuation failed")
            try:
                await client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts,
                    text="☁️ ごめんなさい、続行処理でエラーが出ました。もう一度依頼してもらえますか？",
                )
            except Exception:
                pass

    async def resume_orphan_run(self, run: dict, client) -> bool:
        """再起動で殺された走行中タスク（孤児ラン）を自動で続きから再開する。

        2026-07-27: 外部再起動（デプロイ/手動bootout/日次/クラッシュ）が長時間タスクを
        無防備に殺し、ユーザーが「続きやって」を手打ちしないと止まったままになる問題への対策。
        起動時sweepから呼ばれ、同一スレッドのsession/履歴を引き継いで最後まで完走させる。
        戻り値: 再開を実行したら True / 対象外（情報不足）なら False。
        """
        channel = run.get("channel")
        thread_ts = run.get("thread_ts") or None
        user = run.get("user") or ""
        orig_text = run.get("text") or ""
        if not channel:
            return False

        thread_key_for_btn = f"{channel}:{thread_ts}" if thread_ts else channel
        progress_blocks = [
            {"type": "section", "text": {"type": "mrkdwn",
             "text": "再起動が入ったので、中断した作業を自動で続きから進めますね…"}},
            {"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "🛑 停止"},
                 "action_id": "stop_agent", "value": thread_key_for_btn, "style": "danger"},
            ]},
        ]
        progress_ts = None
        try:
            msg = await client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text="再起動が入ったので、中断した作業を自動で続きから進めますね…",
                blocks=progress_blocks,
            )
            progress_ts = msg["ts"]
        except Exception:
            logger.exception("[OrphanResume] progress post failed")

        async def update_progress(status_text: str):
            nonlocal progress_ts
            try:
                _blocks = [
                    {"type": "section", "text": {"type": "mrkdwn", "text": status_text[:2900]}},
                    progress_blocks[1],
                ]
                if progress_ts:
                    await client.chat_update(channel=channel, ts=progress_ts,
                                             text=status_text, blocks=_blocks)
            except Exception as e:
                logger.warning(f"[OrphanResume] progress update failed: {e}")

        # 続行プロンプト: スレッド履歴は _build_user_prompt が自動注入するので、
        # 「すでに終わった部分はやり直さず残りだけ仕上げる」ことを明示する。
        recap = f"\n元の依頼: {orig_text}" if orig_text else ""
        resume_prompt = (
            "【システム】直前までこのスレッドで実行していた作業が、サーバーの再起動で中断しました。"
            "このスレッドのここまでのやり取りと、あなた自身がすでに進めた内容を踏まえて、"
            "続きから最後まで完了させてください。すでに終わっている部分はやり直さず、"
            "残りだけを仕上げて成果物を提示してください。" + recap
        )
        try:
            memory_context = self._build_memory_context(channel, user, resume_prompt, thread_ts=thread_ts)
            result = await self.ai_engine.process_message(
                text=resume_prompt, channel=channel, thread_ts=thread_ts, files=None,
                tool_executor=self.tool_executor,
                context={"user": user, "channel": channel, "thread_ts": thread_ts,
                         "memory": self.memory, "slack_client": client,
                         "skill_manager": getattr(self.tool_executor, "skill_manager", None),
                         "is_dm": False},
                memory_context=memory_context, progress_callback=update_progress,
            )
            await self._send_response(result, channel, thread_ts, None, client, user=user,
                                      progress_ts=progress_ts, mention_back=False)
            if thread_ts:
                self._active_threads.mark_replied(channel, thread_ts)
            logger.info(f"[OrphanResume] resumed & completed: {run.get('thread_key')}")
            return True
        except Exception:
            logger.exception(f"[OrphanResume] resume failed: {run.get('thread_key')}")
            # 自動再開に失敗したら手動案内にフォールバック（progressを上書き）
            try:
                fallback = ("☁️ 自動での続き実行に失敗しました。お手数ですが、"
                            "このスレッドで「続きやって」と送ってもらえれば続きから再開します")
                if progress_ts:
                    await client.chat_update(channel=channel, ts=progress_ts,
                                             text=fallback,
                                             blocks=[{"type": "section",
                                                      "text": {"type": "mrkdwn", "text": fallback}}])
                else:
                    await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=fallback)
            except Exception:
                pass
            return True

    async def _send_response(self, result: dict, channel: str, thread_ts: str, say, client, user: str = "", progress_ts: str = None, mention_back: bool = False):
        """AIの応答をSlackに送信。progress_tsがあれば進捗メッセージを最終応答で上書き。
        mention_back=True の場合、応答の先頭にユーザーメンションを付与。"""
        # メンション返し: 応答テキストの先頭に <@user> を付ける
        if mention_back and user and result.get("text"):
            text = result["text"]
            # すでにメンションが含まれていなければ追加
            if f"<@{user}>" not in text:
                result = {**result, "text": f"<@{user}> {text}"}

        # 書き込み確認が必要な場合
        if result.get("requires_confirmation") and user:
            pending = result["pending_action"]
            tool_name = pending["tool_name"]
            if "notion" in tool_name:
                service = "Notion"
            elif "gsheet" in tool_name:
                service = "Google Sheets"
            elif tool_name == "execute_code":
                service = "コード実行"
            else:
                service = tool_name
            # 進捗メッセージを確認テキストで上書き
            if result.get("text") and progress_ts:
                await client.chat_update(
                    channel=channel, ts=progress_ts, text=result["text"]
                )
            elif result.get("text"):
                await client.chat_postMessage(
                    channel=channel, text=result["text"], thread_ts=thread_ts
                )
            elif progress_ts:
                # テキストなしの場合は進捗メッセージを削除
                try:
                    await client.chat_delete(channel=channel, ts=progress_ts)
                except Exception:
                    pass
            await self.request_write_confirmation(
                channel=channel,
                thread_ts=thread_ts,
                user=user,
                service=service,
                action_description=pending["description"],
                pending_action=pending,
                client=client,
            )
            return

        text = result.get("text", "")
        files = result.get("files", [])

        # <thinking> タグとその内容を除去（Slackに表示させない）
        if text:
            text = re.sub(r'<thinking>.*?</thinking>\s*', '', text, flags=re.DOTALL)
            text = text.strip()

        # 添付するファイルの生の絶対パスは本文から消し、ファイル名だけ残して綺麗に見せる。
        # 「`/path/to/project/.../資料.pptx`」→「`資料.pptx`」（本体は下に添付されるので冗長）
        if text and files:
            for fp in files:
                if fp and fp in text:
                    text = text.replace(fp, os.path.basename(fp))

        # Clarify envelopeの取りこぼし除去（DM等、ボタン変換を通らない経路の保険）
        if text and "<agent_clarify>" in text:
            from utils.assistant_outcome import parse_clarify
            text, _ = parse_clarify(text)

        # Slack mrkdwn サニタイザ: **bold** → *bold*（コードブロック外のみ）
        if text:
            text = self._sanitize_slack_mrkdwn(text)

        # 既知の未検証数値主張のコード側ネット（2026-07-16）
        if text:
            from utils.assistant_outcome import apply_forbidden_claims
            text = apply_forbidden_claims(text)

        if text:
            max_len = 3000
            if len(text) > max_len:
                # 長文: 境界を考慮して分割。最初のチャンクで進捗メッセージを上書き、残りは新規投稿。
                chunks = self._split_for_slack(text, max_len)
                first = True
                for idx, chunk in enumerate(chunks):
                    if idx > 0:
                        await asyncio.sleep(1.1)  # Slack の 1msg/sec/channel 制限を避ける
                    sent = False
                    for attempt in range(3):
                        try:
                            if first and progress_ts:
                                # blocks=[] で進捗ブロック（🛑ボタン等）を明示的にクリア
                                await client.chat_update(channel=channel, ts=progress_ts, text=chunk, blocks=[])
                            else:
                                await client.chat_postMessage(channel=channel, text=chunk, thread_ts=thread_ts)
                            first = False
                            sent = True
                            break
                        except Exception as e:
                            logger.warning(f"Failed to send chunk (attempt {attempt+1}/3): {e}")
                            # chat_update 失敗時は postMessage にフォールバック
                            if first and progress_ts:
                                try:
                                    await client.chat_postMessage(channel=channel, text=chunk, thread_ts=thread_ts)
                                    first = False
                                    sent = True
                                    break
                                except Exception:
                                    pass
                            await asyncio.sleep(1.5 * (attempt + 1))  # 指数バックオフ
                    if not sent:
                        # 黙って捨てると応答が中抜けするので、明示的に通知する
                        logger.error("chunk send failed after retries — notifying user")
                        try:
                            await client.chat_postMessage(
                                channel=channel,
                                text="（応答の一部を送信できませんでした。お手数ですがもう一度お試しください）",
                                thread_ts=thread_ts,
                            )
                        except Exception:
                            pass
            else:
                try:
                    if progress_ts:
                        # blocks=[] で進捗ブロック（🛑ボタン等）を明示的にクリア
                        await client.chat_update(channel=channel, ts=progress_ts, text=text, blocks=[])
                    else:
                        await client.chat_postMessage(channel=channel, text=text, thread_ts=thread_ts)
                except Exception as e:
                    logger.warning(f"Failed to send response: {e}")
                    # chat_updateが失敗した場合はpostMessageにフォールバック
                    try:
                        if progress_ts:
                            # 進捗メッセージを削除してから新規投稿
                            try:
                                await client.chat_delete(channel=channel, ts=progress_ts)
                            except Exception:
                                pass
                        await client.chat_postMessage(channel=channel, text=text[:3000], thread_ts=thread_ts)
                    except Exception:
                        pass
        elif progress_ts:
            # テキストなしの場合は進捗メッセージを削除
            try:
                await client.chat_delete(channel=channel, ts=progress_ts)
            except Exception:
                pass

        for filepath in files:
            try:
                await client.files_upload_v2(channel=channel, file=filepath, thread_ts=thread_ts)
            except Exception as e:
                logger.error(f"File upload error: {e}")
                await client.chat_postMessage(
                    channel=channel, text=f"ファイルのアップロードに失敗しました: {e}", thread_ts=thread_ts
                )

        # 満足度フィードバックボタンを送信（メンション/DM応答のとき）
        # ⚠️ エラー/警告/タイムアウトで終わった応答にはフィードバックを求めない
        _error_markers = (
            "☁️ 処理中にエラー",
            "⚠️ 処理ステップが上限",
            "⏰ 処理ステップが",
            "⏰ タイムアウト",
            "途中で終了しました",
            "ここで処理が中断しました",
            "処理が途中で止まってしまいました",
            "応答を生成できませんでした",
        )
        has_error = bool(text) and any(marker in text for marker in _error_markers)
        # フィードバックUIの一本化: channel/thread_ts を持つ5段階ボタンのみ使う。
        # 毎回ではなく should_ask_feedback の確率で出す（ノイズ抑制 + 低評価分析の入力確保）。
        if user and (text or files) and not has_error:
            try:
                ask = self.memory.should_ask_feedback(user, channel)
            except Exception:
                ask = False
            if ask:
                await self._send_feedback_buttons(channel, thread_ts, user, client)

    async def _send_feedback_buttons(self, channel: str, thread_ts: str, user: str, client):
        """5段階満足度ボタンを送信"""
        try:
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="この回答は役に立ちましたか？",
                blocks=[
                    {
                        "type": "actions",
                        "block_id": f"fb_{thread_ts}_{user}",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": e["label"]},
                                "action_id": f"feedback_{e['score']}",
                                "value": json.dumps({
                                    "score": e["score"],
                                    "user": user,
                                    "channel": channel,
                                    "thread_ts": thread_ts,
                                }),
                                "style": e.get("style"),
                            }
                            for e in [
                                {"score": 1, "label": "1 😞"},
                                {"score": 2, "label": "2 🙁"},
                                {"score": 3, "label": "3 😐"},
                                {"score": 4, "label": "4 🙂"},
                                {"score": 5, "label": "5 😍", "style": "primary"},
                            ]
                        ],
                    }
                ],
            )
        except Exception as e:
            logger.debug(f"Failed to send feedback buttons: {e}")

    # ── 停止/リセット ボタンハンドラ（2026-05-29 追加） ──

    async def _handle_stop_agent(self, body: dict, client):
        """停止ボタン: 現在の SDK 処理を中断（部分出力は破棄）。"""
        try:
            actions = body.get("actions", [])
            thread_key = actions[0].get("value", "") if actions else ""
            channel = body.get("channel", {}).get("id", "")
            message_ts = body.get("message", {}).get("ts", "")
            user = body.get("user", {}).get("id", "")

            cancelled = False
            if hasattr(self.ai_engine, "cancel_thread") and thread_key:
                cancelled = await self.ai_engine.cancel_thread(thread_key)

            if message_ts and channel:
                try:
                    new_text = (
                        f"⛔ 中断しました（<@{user}>）"
                        if cancelled
                        else "（すでに処理は終わっていました）"
                    )
                    await client.chat_update(channel=channel, ts=message_ts, text=new_text, blocks=[])
                except Exception as e:
                    logger.warning(f"[Stop] chat_update failed: {e}")
            logger.info(f"[Stop] handler: thread_key={thread_key} cancelled={cancelled} user={user}")
        except Exception as e:
            logger.error(f"[Stop] handler error: {e}", exc_info=True)

    async def _handle_reset_agent_context(self, body: dict, client):
        """文脈リセットボタン: SessionStore entry を削除して次の対話を新 session に。"""
        try:
            actions = body.get("actions", [])
            value = actions[0].get("value", "") if actions else ""
            # value format: "channel:thread_ts" or just "channel"
            if ":" in value:
                channel_id, thread_ts = value.split(":", 1)
            else:
                channel_id, thread_ts = value or body.get("channel", {}).get("id", ""), None

            user = body.get("user", {}).get("id", "")

            if hasattr(self.ai_engine, "clear_conversation"):
                self.ai_engine.clear_conversation(channel_id, thread_ts)

            try:
                await client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts if thread_ts else None,
                    text=f"📭 文脈をリセットしました（<@{user}>）。新しく始めましょう",
                )
            except Exception as e:
                logger.warning(f"[Reset] postMessage failed: {e}")
            logger.info(f"[Reset] handler: channel={channel_id} thread_ts={thread_ts} user={user}")
        except Exception as e:
            logger.error(f"[Reset] handler error: {e}", exc_info=True)

    async def _handle_feedback_button(self, body: dict, client):
        """満足度ボタンが押されたときの処理"""
        action = body.get("actions", [{}])[0]
        try:
            payload = json.loads(action.get("value", "{}"))
        except json.JSONDecodeError:
            return

        score = payload.get("score", 3)
        user = payload.get("user", "")
        channel = payload.get("channel", "")
        thread_ts = payload.get("thread_ts", "")

        # メモリに記録
        self.memory.record_feedback(
            score=score,
            user=user,
            channel=channel,
            thread_ts=thread_ts,
        )

        # 会話フィードバックにも記録（週次レポート用）
        self.memory.record_conversation_feedback(
            user=user,
            channel=channel,
            score=score,
            text_snippet=payload.get("text", ""),
            response_snippet=payload.get("response", ""),
            feedback_type="button",
        )

        # フィードバックスコアからパーソナライズ学習
        self.memory.auto_learn_user_style(user, "", "", score=score)

        self.memory.save()

        logger.info(f"Feedback from {user}: score={score}")

        # ボタンメッセージを更新して結果表示に差し替え
        stars = "⭐" * score + "☆" * (5 - score)
        try:
            await client.chat_update(
                channel=body["channel"]["id"],
                ts=body["message"]["ts"],
                text=f"フィードバックありがとうございます！ {stars}",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"フィードバック: {stars}（{score}/5）ありがとうございます！",
                        },
                    }
                ],
            )
        except Exception as e:
            logger.debug(f"Failed to update feedback message: {e}")

    async def _handle_approval_button(self, body: dict, client):
        """承認/却下ボタンが押されたときの処理"""
        action = body.get("actions", [{}])[0]
        action_id = action.get("action_id", "")
        try:
            payload = json.loads(action.get("value", "{}"))
        except json.JSONDecodeError:
            return

        approval_id = payload.get("approval_id", "")
        requester = payload.get("requester_id", "")
        approver = body.get("user", {}).get("id", "")
        approved = action_id.startswith("approve_")

        result_text = "承認" if approved else "却下"
        logger.info(f"Approval {result_text}: {approval_id} by {approver}")

        # ボタンメッセージを更新
        emoji = ":white_check_mark:" if approved else ":x:"
        try:
            await client.chat_update(
                channel=body["channel"]["id"],
                ts=body["message"]["ts"],
                text=f"{emoji} {result_text}されました（by <@{approver}>）",
                blocks=[{
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"{emoji} *{result_text}* されました（by <@{approver}>）",
                    },
                }],
            )
        except Exception as e:
            logger.debug(f"Failed to update approval message: {e}")

        # 承認後のアクション通知
        on_target = payload.get("on_approve_target", "")
        on_action = payload.get("on_approve_action", "")
        if requester:
            try:
                notify_text = (
                    f":bell: あなたの承認リクエスト（{approval_id}）が *{result_text}* されました。"
                )
                if approved and on_action:
                    notify_text += f"\n次のアクション: {on_action}"
                from tools.slack_tools import send_dm
                await send_dm(
                    user_id=requester,
                    message=notify_text,
                    slack_token=self.settings.slack_bot_token,
                )
            except Exception as e:
                logger.error(f"Approval notification error: {e}")



    # ──────────────────────────────────────────────
    # フィードバック収集
    # ──────────────────────────────────────────────
    async def _send_feedback_request(
        self, client, channel: str, thread_ts: str,
        text_snippet: str, response_snippet: str, user: str,
    ):
        """会話のフィードバックをリアクションで収集するメッセージを送信"""
        try:
            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "この回答、役に立ちましたか？自己改善に活かします",
                    },
                },
                {
                    "type": "actions",
                    "block_id": f"feedback_{int(time.time())}",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "⭐ とても良い"},
                            "action_id": "feedback_5",
                            "value": json.dumps({"user": user, "score": 5,
                                "text": text_snippet, "response": response_snippet}, ensure_ascii=False)[:1900],
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "👍 良い"},
                            "action_id": "feedback_4",
                            "value": json.dumps({"user": user, "score": 4,
                                "text": text_snippet, "response": response_snippet}, ensure_ascii=False)[:1900],
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "😐 普通"},
                            "action_id": "feedback_3",
                            "value": json.dumps({"user": user, "score": 3,
                                "text": text_snippet, "response": response_snippet}, ensure_ascii=False)[:1900],
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "👎 微妙"},
                            "action_id": "feedback_2",
                            "value": json.dumps({"user": user, "score": 2,
                                "text": text_snippet, "response": response_snippet}, ensure_ascii=False)[:1900],
                        },
                    ],
                },
            ]
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                blocks=blocks,
                text="さっきの回答はいかがでしたか？",
            )
        except Exception as e:
            logger.debug(f"Feedback request failed (non-critical): {e}")

    # ──────────────────────────────────────────────
    # メモリ関連ヘルパー
    # ──────────────────────────────────────────────
    async def _build_thread_transcript(self, channel: str, thread_ts: str, limit_chars: int = 12000) -> str:
        """スレッド全文を読み直して文脈にする。

        エージェント 自身が投稿した内容（会議の議事録など）はイベントとして流れてこないため
        memory に残らず、そのスレッドで質問されても文脈を持てない。
        conversations.replies でスレッド全体を読み直して補う。
        スレッド外（thread_ts が親自身）の場合は 1 件しか返らないので何もしない。
        """
        if not thread_ts:
            return ""
        token = os.getenv("SLACK_BOT_TOKEN", "")
        if not token:
            return ""
        try:
            from tools.thread_tools import crawl_thread
            messages = await crawl_thread(channel, thread_ts, token)
        except Exception as e:
            logger.warning(f"スレッド全文の取得に失敗: {e}")
            return ""
        if len(messages) <= 1:
            return ""
        lines = []
        for m in messages:
            body = (m.get("text") or "").strip()
            if not body:
                continue
            who = m.get("username") or m.get("user") or m.get("bot_id") or "unknown"
            lines.append(f"[{who}] {body}")
        if not lines:
            return ""
        transcript = "\n".join(lines)
        if len(transcript) > limit_chars:
            transcript = transcript[:limit_chars] + "\n…(以降省略)"
        return (
            "## このスレッドの全文（議事録などを含む。最優先で参照してください）\n"
            f"{transcript}"
        )

    def _build_memory_context(self, channel: str, user: str, text: str, thread_ts: str = "") -> str:
        """AIに渡すメモリコンテキストを構築（優先順位付き）

        優先順位:
        1. ユーザー永続メモ（常に注入）
        2. ユーザープロフィール（パーソナライズ）
        3. 同スレッドの参加者・会話状況（最優先コンテキスト）
        4. 同チャンネルの直近コンテキスト
        5. 関連チーム知識（テキストマッチ）
        6. プロジェクト記憶（テキストマッチ）
        7. チャンネル横断ナレッジ（優先度低：明確な関連性がある場合のみ）
        8. 過去の会話履歴（同ユーザーとの過去のやり取り）
        """
        high_priority = []  # 常に注入
        thread_priority = []  # 同スレッド関連（最重要コンテキスト）
        channel_priority = []  # 同チャンネル関連
        low_priority = []  # チャンネル横断・過去履歴（低優先度）

        # ── 最高優先: ユーザー永続メモ（本人 + オーナー + global のみ） ──
        notes_ctx = self.memory.get_notes_context(user)
        if notes_ctx:
            high_priority.append(notes_ctx)

        # ── 高優先: ユーザープロフィール ──
        user_ctx = self.memory.get_user_context(user)
        if user_ctx:
            high_priority.append(f"## このユーザーについて\n{user_ctx}")

        # ── 最重要コンテキスト: 同スレッドの会話状況 ──
        if thread_ts:
            thread_ctx = self.memory.get_thread_context(channel, thread_ts)
            if thread_ctx:
                thread_priority.append(f"## このスレッドの会話状況（最優先で参照してください）\n{thread_ctx}")

        # ── 同チャンネルの直近コンテキスト ──
        channel_ctx = self.memory.get_channel_summary(channel)
        if channel_ctx:
            channel_priority.append(channel_ctx)

        # ── 関連チーム知識（テキストマッチ） ──
        knowledge_ctx = self.memory.get_knowledge_context(text)
        if knowledge_ctx:
            channel_priority.append(knowledge_ctx)

        # ── プロジェクト記憶 ──
        project_ctx = self.memory.get_project_context(text)
        if project_ctx:
            channel_priority.append(project_ctx)

        # ── 低優先: チャンネル横断ナレッジ ──
        # 同スレッドにコンテキストがある場合は横断検索を抑制
        if not thread_ts:
            cross_ctx = self.memory.get_cross_channel_context(text)
            if cross_ctx:
                low_priority.append(
                    "## 参考情報（他チャンネル — 今の会話と直接関係ない場合は無視してください）\n"
                    + cross_ctx.replace("## チーム全体のナレッジ（チャンネル横断）\n", "")
                )

        # ── 低優先: 過去の会話履歴 ──
        conversation_ctx = self.memory.get_conversation_history_context(user, text)
        if conversation_ctx:
            low_priority.append(conversation_ctx)

        # 組み立て: コンテキストバジェット。優先度順に詰めて総文字数上限で打ち切る。
        # 低優先（チャンネル横断・14日分の過去履歴）が無関係に肥大して
        # 高優先（永続メモ・スレッド文脈）やペルソナ指示を薄めるのを防ぐ。
        MAX_CONTEXT_CHARS = 6000
        ordered = high_priority + thread_priority + channel_priority + low_priority
        budgeted = []
        used = 0
        for part in ordered:
            if not part:
                continue
            if used >= MAX_CONTEXT_CHARS:
                break
            remaining = MAX_CONTEXT_CHARS - used
            if len(part) > remaining:
                part = part[:remaining] + "\n…(コンテキスト上限のため省略)"
            budgeted.append(part)
            used += len(part) + 2
        return "\n\n".join(budgeted) if budgeted else ""

    def _maybe_save_and_extract(self, channel: str, user: str):
        """毎回メモリを保存、一定回数ごとに知識を抽出"""
        self._interaction_count += 1
        # 毎回保存（プロセス再起動で会話データが消失するのを防止）
        self.memory.save()
        # 知識抽出は10回に1回（コスト節約）
        # ClaudeEngineはSDK側でセッション管理しており conversations dict を持たないためスキップ
        if self._interaction_count % 10 == 0:
            conv_key = channel
            conversations = getattr(self.ai_engine, "conversations", None)
            if conversations is not None:
                messages = conversations.get(conv_key, [])
                if messages:
                    import asyncio
                    asyncio.create_task(
                        self.memory_extractor.extract_from_conversation(messages, channel, user)
                    )

    async def _check_feedback(self, text: str, channel: str, user: str) -> bool:
        """ユーザーからのフィードバックを検出して記録"""
        text_lower = text.strip().lower()
        # ポジティブフィードバック
        if text_lower in ("ありがとう", "助かった", "いいね", "ナイス", "perfect", "thanks", "great"):
            self.memory.mark_intervention_helpful(True)
            self.memory.save()
            return False  # フィードバック記録するだけで会話は続行

        # 2026-05-29: ネガティブフィードバック検出（沈黙判定）は廃止。
        # 長文中の偶然一致による誤沈黙リスクと、短文限定でも本質的に AI が
        # 自然に解釈すべき領域なので、テキスト keyword 検出から外す。

        return False



    def _clean_mention(self, text: str) -> str:
        """メンションタグを除去"""
        return re.sub(r"<@[A-Z0-9]+>", "", text).strip()


    # ── 起動時: ダウンタイム中の未応答メンション復旧 ──

    _RECOVERY_MAX_MESSAGES = 5  # 一度に再処理する最大件数

    async def check_missed_mentions(self) -> int:
        """起動時にダウンタイム中の未応答メンションを検出し、AI処理を再開する。

        Returns:
            再処理した件数
        """
        heartbeat_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "memory_data", "last_alive.txt"
        )

        if not os.path.exists(heartbeat_path):
            logger.info("[Recovery] No heartbeat file, skipping missed mention check")
            return 0

        try:
            with open(heartbeat_path, "r") as f:
                last_alive_ts = float(f.read().strip())
        except Exception:
            return 0

        downtime = time.time() - last_alive_ts
        if downtime < 120:
            logger.info(f"[Recovery] Downtime {downtime:.0f}s < 120s, Socket Mode redelivery should cover it")
            return 0

        # 最大1時間前まで遡る
        oldest = max(last_alive_ts, time.time() - 3600)

        bot_user_id = self.settings.bot_user_id
        if not bot_user_id:
            logger.warning("[Recovery] bot_user_id not set, skipping")
            return 0

        logger.info(f"[Recovery] Downtime detected: {downtime:.0f}s. Checking for missed mentions (oldest={oldest:.0f})")

        client = self.app.client

        # ボットが参加しているチャンネル一覧
        try:
            channels = []
            cursor = None
            while True:
                result = await client.users_conversations(
                    user=bot_user_id,
                    types="public_channel,private_channel,mpim,im",
                    limit=200,
                    cursor=cursor,
                )
                channels.extend(result.get("channels", []))
                cursor = result.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break
            logger.info(f"[Recovery] Bot is in {len(channels)} channels")
        except Exception as e:
            logger.error(f"[Recovery] Failed to list channels: {e}")
            return 0

        # まず全チャンネルから未応答メンションを収集
        missed_messages = []
        for i, ch in enumerate(channels):
            channel_id = ch["id"]
            is_dm = ch.get("is_im", False)

            if i > 0 and i % 30 == 0:
                await asyncio.sleep(1)

            try:
                history = await client.conversations_history(
                    channel=channel_id,
                    oldest=str(oldest),
                    limit=50,
                )
            except Exception as e:
                logger.debug(f"[Recovery] Skipped channel {channel_id}: {e}")
                continue

            for msg in history.get("messages", []):
                if msg.get("bot_id") or msg.get("user") == bot_user_id:
                    continue

                text = msg.get("text", "")

                # DMの場合: 全メッセージが対象
                # チャンネルの場合: 明示的に@メンションされたもののみ
                if not is_dm and f"<@{bot_user_id}>" not in text:
                    continue

                msg_ts = msg["ts"]
                thread_ts = msg.get("thread_ts", msg_ts)

                # スレッド内でボットが返信済みか確認
                already_replied = False
                try:
                    replies = await client.conversations_replies(
                        channel=channel_id,
                        ts=thread_ts,
                        limit=50,
                    )
                    for reply in replies.get("messages", []):
                        if reply["ts"] == msg_ts:
                            continue
                        if reply.get("user") == bot_user_id:
                            already_replied = True
                            break
                except Exception:
                    pass

                if already_replied:
                    continue

                missed_messages.append({
                    "channel": channel_id,
                    "msg": msg,
                    "is_dm": is_dm,
                })

        if not missed_messages:
            logger.info("[Recovery] No missed mentions found")
            return 0

        logger.info(f"[Recovery] Found {len(missed_messages)} missed mentions, processing up to {self._RECOVERY_MAX_MESSAGES}")

        # 古い順にソート（ts昇順）して上限まで処理
        missed_messages.sort(key=lambda m: float(m["msg"]["ts"]))
        to_process = missed_messages[:self._RECOVERY_MAX_MESSAGES]
        skipped = missed_messages[self._RECOVERY_MAX_MESSAGES:]

        # 上限超過分は定型文だけ返す
        for item in skipped:
            msg = item["msg"]
            channel_id = item["channel"]
            msg_ts = msg["ts"]
            thread_ts = msg.get("thread_ts", msg_ts)
            base_key = msg.get("client_msg_id", f"{channel_id}-{msg_ts}")
            self._processed_events[f"app_mention:{base_key}"] = time.time()
            self._processed_events[f"message:{base_key}"] = time.time()
            try:
                await client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text="ごめんなさい、システム再起動で反応できませんでした :bow:\nお手数ですが、改めてメンションしてもらえると嬉しいです！",
                )
            except Exception:
                pass

        # 本処理: _handle_mention に流してAI処理を再開
        processed = 0
        for item in to_process:
            msg = item["msg"]
            channel_id = item["channel"]
            msg_ts = msg["ts"]

            # 重複排除に登録（Socket Mode再配信との二重処理防止）
            base_key = msg.get("client_msg_id", f"{channel_id}-{msg_ts}")
            self._processed_events[f"app_mention:{base_key}"] = time.time()
            self._processed_events[f"message:{base_key}"] = time.time()

            # Slack Boltのevent互換dictを構築
            event = {
                "type": "app_mention",
                "user": msg.get("user", "unknown"),
                "text": msg.get("text", ""),
                "ts": msg_ts,
                "channel": channel_id,
                "event_ts": msg.get("event_ts", msg_ts),
            }
            if msg.get("thread_ts"):
                event["thread_ts"] = msg["thread_ts"]
            if msg.get("client_msg_id"):
                event["client_msg_id"] = msg["client_msg_id"]
            if msg.get("files"):
                event["files"] = msg["files"]

            # sayの代替（client.chat_postMessageのラッパー）
            async def _say(text="", **kwargs):
                kwargs.setdefault("channel", channel_id)
                kwargs.setdefault("thread_ts", msg.get("thread_ts", msg_ts))
                if text:
                    kwargs["text"] = text
                return await client.chat_postMessage(**kwargs)

            logger.info(f"[Recovery] Re-processing mention: channel={channel_id} ts={msg_ts} text={msg.get('text', '')[:80]}")

            try:
                # 重複排除をバイパスするため直接debounceフローへ
                # _handle_mention の先頭で _is_duplicate_event が弾くので、
                # 直接 _process_mention_batch 相当の処理を行う
                thread_ts = msg.get("thread_ts", msg_ts)
                text = self._clean_mention(msg.get("text", ""))
                user = msg.get("user", "unknown")
                files = await self._extract_files(event, client)

                # 「さっきは反応できなくてごめん」+ AI処理の進捗メッセージ
                progress_msg = await client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text="ごめんなさい、さっきシステム再起動中でした🙇\n今から対応しますね！",
                )
                progress_ts = progress_msg["ts"]

                async def update_progress(status_text: str):
                    try:
                        await client.chat_update(
                            channel=channel_id, ts=progress_ts, text=status_text
                        )
                    except Exception:
                        pass

                # ユーザー名解決 + メモリ更新
                await self.memory.resolve_user_name(user, slack_client=client)
                self.memory.update_channel_context(channel_id, text, user)
                self.memory.record_interaction(user, "mention")

                memory_context = self._build_memory_context(channel_id, user, text, thread_ts=thread_ts)

                result = await self.ai_engine.process_message(
                    text=text,
                    channel=channel_id,
                    thread_ts=thread_ts,
                    files=files,
                    tool_executor=self.tool_executor,
                    context={
                        "user": user,
                        "channel": channel_id,
                        "thread_ts": thread_ts,
                        "memory": self.memory,
                        "slack_client": client,
                        "skill_manager": getattr(self.tool_executor, "skill_manager", None),
                        "is_dm": item["is_dm"],
                    },
                    memory_context=memory_context,
                    progress_callback=update_progress,
                )

                if not result.get("should_respond", True) and not result.get("text"):
                    try:
                        await client.chat_delete(channel=channel_id, ts=progress_ts)
                    except Exception:
                        pass
                else:
                    await self._send_response(
                        result, channel_id, thread_ts, _say, client,
                        user=user, progress_ts=progress_ts, mention_back=True,
                    )

                processed += 1
                logger.info(f"[Recovery] Successfully re-processed mention in {channel_id} ts={msg_ts}")

            except Exception as e:
                logger.error(f"[Recovery] Failed to re-process mention: {e}", exc_info=True)
                # AI処理に失敗した場合はフォールバックで定型文
                try:
                    await client.chat_postMessage(
                        channel=channel_id,
                        thread_ts=msg.get("thread_ts", msg_ts),
                        text="ごめんなさい、システム再起動で反応できませんでした :bow:\nお手数ですが、改めてメンションしてもらえると嬉しいです！",
                    )
                except Exception:
                    pass

        total = processed + len(skipped)
        logger.info(f"[Recovery] Done. AI-processed={processed}, canned-reply={len(skipped)}, total={total}")
        return total
