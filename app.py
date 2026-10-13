"""
Slack AI エージェント - メインアプリケーション

社内向け Slack エージェントの核を複製し、業務固有の機能を外した土台。
- メンション・DM で依頼を受け、Claude Code（サブスク枠）で考えて返す
- スレッド = 1 会話。再起動で中断した処理は起動時に自動で続きから再開する
- 業務固有の機能は tools/・mcp_agent_tools/・skills_content/・scheduler/ に足していく
"""
import asyncio
import logging
import os
import sys
import time

from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from config.settings import Settings
from utils.claude_engine import ClaudeEngine
from utils.claude_pty_engine import ClaudePtyEngine
from utils.memory import Memory
from tools.executor import ToolExecutor
from handlers.message_handler import MessageHandler
from utils.skills import SkillManager

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ログに秘密情報（トークン等）が混ざっても伏せ字にする
from utils import log_redact  # noqa: E402

log_redact.install()

# 常駐バックグラウンドタスクの参照保持（弱参照によるGC消滅・沈黙死の防止）
_BG_TASKS: set = set()


def _spawn(coro, name: str = "task"):
    """常駐タスクを参照保持して起動。例外で静かに死んだ場合は必ずログに残す。"""
    t = asyncio.create_task(coro)
    try:
        t.set_name(name)
    except Exception:
        pass
    _BG_TASKS.add(t)

    def _done(task: asyncio.Task):
        _BG_TASKS.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error(f"[bg-task] '{task.get_name()}' が異常終了しました: {exc}", exc_info=exc)

    t.add_done_callback(_done)
    return t


def memory_dir() -> str:
    """記憶の保存先（/tmp は macOS の定期掃除で消えるので使わない）。MEMORY_DIR で上書き可"""
    d = os.getenv("MEMORY_DIR", os.path.join(PROJECT_ROOT, "memory_data"))
    os.makedirs(d, exist_ok=True)
    return d


def create_app():
    """アプリケーションを初期化して返す"""
    settings = Settings.from_env()
    os.makedirs(settings.workspace_dir, exist_ok=True)

    # Slack Boltアプリ初期化（DNSキャッシュ付きWebクライアント）
    import aiohttp as _aiohttp
    from slack_sdk.web.async_client import AsyncWebClient
    _web_session = _aiohttp.ClientSession(connector=_aiohttp.TCPConnector(use_dns_cache=True, ttl_dns_cache=300))
    app = AsyncApp(token=settings.slack_bot_token, client=AsyncWebClient(token=settings.slack_bot_token, session=_web_session))

    # AIエンジン: claude_pty（Claude Code の対話画面を裏で操作＝サブスク枠）が標準。claude_code は Agent SDK 経由
    if settings.ai_engine_type == "claude_code":
        logger.info("Using ClaudeEngine (Agent SDK / CLI)")
        engine_cls = ClaudeEngine
    else:
        logger.info("Using ClaudePtyEngine (PTY interactive — subscription pool)")
        engine_cls = ClaudePtyEngine
    ai_engine = engine_cls(
        model=settings.claude_model_medium,
        model_light=settings.claude_model,
        model_heavy=settings.claude_model_heavy,
    )

    # スキル（skills_content/*.md を置くだけで増える）
    skill_manager = SkillManager(
        skills_dir=os.path.join(settings.workspace_dir, "skills"),
        flat_skill_dirs=[os.path.join(PROJECT_ROOT, "skills_content")],
    )
    ai_engine.skill_manager = skill_manager

    tool_executor = ToolExecutor(
        workspace=settings.workspace_dir,
        slack_token=settings.slack_bot_token,
        anthropic_client=ai_engine.client,
        async_anthropic_client=ai_engine.async_client,
        skill_manager=skill_manager,
    )

    memory = Memory(persist_path=os.path.join(memory_dir(), "memory.json"))

    message_handler = MessageHandler(
        app=app,
        settings=settings,
        ai_engine=ai_engine,
        tool_executor=tool_executor,
        memory=memory,
    )

    @app.command("/agent-clear")
    async def handle_clear(ack, command, say):
        """会話履歴クリアコマンド"""
        await ack()
        ai_engine.clear_conversation(command["channel_id"])
        await say("会話履歴をクリアしました。")

    @app.command("/agent-help")
    async def handle_help(ack, command, say):
        """ヘルプコマンド"""
        await ack()
        await say(text=(
            f"*{settings.agent_name} ヘルプ*\n\n"
            f"• `@{settings.agent_name} <依頼>` — メンションで依頼する（スレッドで会話が続きます）\n"
            "• DM で直接依頼する\n"
            "• `/agent-clear` — 会話履歴をクリア\n"
            "• `/agent-help` — このヘルプ"
        ))

    @app.event("app_home_opened")
    async def handle_app_home(event, client):
        # bot_user_id 未取得時のフォールバック
        if not settings.bot_user_id:
            try:
                settings.bot_user_id = (await client.auth_test())["user_id"]
            except Exception as e:
                logger.error(f"Failed to get bot user ID: {e}")

    return app, settings, message_handler, memory


async def _resolve_bot_user_id(app: AsyncApp, settings: Settings) -> None:
    """起動時にauth_testでbot_user_idを即座に取得する（自分の発言への反応ループ防止）"""
    try:
        settings.bot_user_id = (await app.client.auth_test())["user_id"]
        logger.info(f"Bot user ID: {settings.bot_user_id}")
    except Exception as e:
        logger.error(f"Failed to get bot user ID at startup: {e}")


async def main():
    """メイン起動処理"""
    logger.info("Starting Slack AI Agent...")
    app, settings, message_handler, memory = create_app()
    await _resolve_bot_user_id(app, settings)

    if settings.proactive_channels:
        logger.info(f"Proactive channels: {settings.proactive_channels}")
    else:
        logger.info("PROACTIVE_CHANNELS が空のため、チャンネルへの自発的な反応はしません（メンション・DMのみ）")

    # リマインダーチェック（30秒ごと）
    async def reminder_check_loop():
        from slack_sdk.web.async_client import AsyncWebClient
        slack_client = AsyncWebClient(token=settings.slack_bot_token)
        while True:
            try:
                sent = await message_handler.reminder_manager.check_and_send(slack_client)
                if sent > 0:
                    logger.info(f"Sent {sent} reminders")
            except Exception as e:
                logger.error(f"Reminder check error: {e}")
            await asyncio.sleep(30)

    _spawn(reminder_check_loop(), "reminder_check_loop")

    # 定期メモリ保存（SIGKILL/クラッシュでの消失を防ぐ安全網。最大3分前までは残る）
    async def periodic_memory_save():
        while True:
            await asyncio.sleep(180)
            try:
                memory.save()
            except Exception as e:
                logger.error(f"Periodic memory save failed: {e}")

    _spawn(periodic_memory_save(), "periodic_memory_save")

    # Socket Mode（外部公開URL不要）。内部セッションをDNSキャッシュ付きに差し替え
    import aiohttp
    handler = AsyncSocketModeHandler(app, settings.slack_app_token)
    old_session = handler.client.aiohttp_client_session
    handler.client.aiohttp_client_session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(use_dns_cache=True, ttl_dns_cache=300, keepalive_timeout=30))
    if old_session and not old_session.closed:
        asyncio.get_event_loop().create_task(old_session.close())

    # ── ハートビート（ダウンタイム検出用） ──
    heartbeat_path = os.path.join(memory_dir(), "last_alive.txt")

    def _write_heartbeat():
        try:
            with open(heartbeat_path, "w") as f:
                f.write(str(time.time()))
        except Exception:
            pass

    _write_heartbeat()

    # グレースフルシャットダウン（launchd SIGTERM対応）。終了前にメモリを保存する
    import signal
    shutdown_event = asyncio.Event()

    def _signal_handler(sig, frame):
        logger.info(f"Received signal {sig}, shutting down gracefully...")
        _write_heartbeat()
        try:
            memory.save()
        except Exception as e:
            logger.error(f"Memory save on shutdown failed: {e}")
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    def _is_ws_session_closed():
        try:
            session = getattr(handler.client, "aiohttp_client_session", None)
            return bool(session and session.closed)
        except Exception:
            return False

    async def _is_ws_connected():
        """SDKのis_connected()（ping/pongベース）で判定する。メッセージの有無には依存しない"""
        try:
            return await handler.client.is_connected()
        except Exception:
            return False

    async def connection_watchdog():
        """Socket Mode接続の見張り。SDKの自動再接続を信頼し、それが失敗した時だけプロセスを再起動する
        - aiohttpセッション閉塞 → 即再起動
        - WebSocket切断が3分続く（APIは通る） → 再起動
        - API不通が15分続く → 再起動（一時的なDNS障害には耐える）
        """
        ws_fail = api_fail = loop_count = 0
        while not shutdown_event.is_set():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break
            _write_heartbeat()
            loop_count += 1
            if loop_count % 5 == 0:
                logger.info(f"[Heartbeat] alive (loop={loop_count}, ws_fail={ws_fail}, api_fail={api_fail})")
            try:
                if _is_ws_session_closed():
                    logger.error("aiohttp ClientSession is closed — forcing restart")
                    memory.save()
                    os._exit(1)
                if await _is_ws_connected():
                    if ws_fail or api_fail:
                        logger.info(f"Connection recovered (ws_fail={ws_fail}, api_fail={api_fail})")
                    ws_fail = api_fail = 0
                    continue
                ws_fail += 1
                try:
                    await app.client.auth_test()
                    api_ok = True
                except Exception as e:
                    api_ok = False
                    logger.warning(f"API health check failed: {e}")
                if api_ok and ws_fail >= 3:
                    logger.error(f"WebSocket disconnected for {ws_fail} min — forcing restart")
                    memory.save()
                    os._exit(1)
                if not api_ok:
                    api_fail += 1
                    if api_fail >= 15:
                        logger.error(f"API unreachable for {api_fail} min — forcing restart")
                        memory.save()
                        os._exit(1)
            except Exception as e:
                logger.error(f"connection_watchdog loop error (continuing): {e}", exc_info=True)

    _spawn(connection_watchdog(), "connection_watchdog")

    # ── 起動時: 再起動で中断された処理（孤児ラン）を自動で続きから再開する ──
    # 無限ループ防止に、1スレッド最大 AGENT_ORPHAN_RESUME_MAX(既定2)回まで。超えたら手動案内
    async def _sweep_orphan_runs():
        try:
            await asyncio.sleep(10)  # Bolt接続の安定を待つ
            from utils.active_runs import pop_orphans, note_resume_attempt, clear_resume_attempts
            max_auto = int(os.getenv("AGENT_ORPHAN_RESUME_MAX", "2"))
            auto_enabled = os.getenv("AGENT_ORPHAN_AUTO_RESUME", "1") == "1"
            for run in pop_orphans():
                thread_key = run.get("thread_key", "")
                has_context = bool(run.get("channel") and run.get("text"))
                attempts = note_resume_attempt(thread_key) if (auto_enabled and has_context) else 99
                try:
                    if auto_enabled and has_context and attempts <= max_auto:
                        logger.info(f"[OrphanSweep] auto-resuming {thread_key} (attempt {attempts}/{max_auto})")
                        await message_handler.resume_orphan_run(run, app.client)
                    else:
                        clear_resume_attempts(thread_key)
                        await app.client.chat_postMessage(
                            channel=run["channel"], thread_ts=run.get("thread_ts") or None,
                            text="⚠️ システムの再起動で処理が途中で止まりました。このスレッドで「続きやって」と送ってもらえれば続きから再開します")
                except Exception:
                    logger.exception(f"[OrphanSweep] handling failed: {thread_key}")
        except Exception:
            logger.exception("[OrphanSweep] sweep failed")

    _spawn(_sweep_orphan_runs(), "orphan_run_sweep")

    # ── 起動時: 止まっていた間に来たメンションに返事をする ──
    async def _recover_missed_mentions():
        try:
            await asyncio.sleep(10)
            count = await message_handler.check_missed_mentions()
            if count:
                logger.info(f"[Recovery] Responded to {count} missed mentions on startup")
        except Exception as e:
            logger.error(f"[Recovery] Error during missed mention check: {e}", exc_info=True)

    _spawn(_recover_missed_mentions(), "recover_missed_mentions")

    logger.info("Agent is ready! Listening for events...")
    try:
        await handler.start_async()
    except Exception as e:
        if not shutdown_event.is_set():
            logger.error(f"Socket Mode handler crashed: {e}", exc_info=True)
            raise
    finally:
        try:
            await handler.close_async()
        except Exception:
            pass


def _acquire_singleton_lock(retries: int = 3, delay: float = 2.0):
    """二重起動防止の排他ロック。ロックファイル名は AGENT_ID ごとに分ける（同じ機械の別エージェントと衝突しない）"""
    import fcntl
    lock_path = os.path.expanduser(f"~/.{os.getenv('AGENT_ID', 'slack-agent')}.lock")
    lock_file = open(lock_path, "w")
    for attempt in range(retries):
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_file.write(str(os.getpid()))
            lock_file.flush()
            return lock_file
        except OSError:
            if attempt < retries - 1:
                time.sleep(delay)
    return None


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
    _singleton_lock = _acquire_singleton_lock()
    if _singleton_lock is None:
        logger.error("同じエージェントが既に稼働中のため起動を中止します（二重起動防止）")
        sys.exit(0)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Agent stopped by user.")
    except Exception as e:
        logger.error(f"Agent crashed: {e}", exc_info=True)
        sys.exit(1)
