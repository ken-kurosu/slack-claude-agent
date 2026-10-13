"""
ツールエグゼキュータ: ツール名 → 実処理の登録表

- 読み取り系: そのまま実行
- 書き込み系（WRITE_TOOLS）: 呼び出し側が確認ボタンを出し、承認後に context["confirmed"]=True で再実行する
- 業務のツールを足す時は、関数を tools/ に書いて _registry() に1行足し、
  書き込み・送信を伴うなら WRITE_TOOLS にも入れる

※ 会話中の Claude は MCP サーバー（mcp_agent_tools/server.py）経由でツールを使う。
  ここは Slack のボタン（確認後の実行）などアプリ側から呼ぶ経路。
"""
import json
import logging
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, Optional

from .slack_tools import send_dm, save_to_canvas, request_approval, fetch_slack_message
from .text_tools import translate_text
from .thread_tools import summarize_thread
from .task_manager import TaskManager
from utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# 外部への書き込み・送信を伴うツール（実行前に必ず確認ボタンを出す）
WRITE_TOOLS = {
    "send_dm",
    "save_to_canvas",
    "request_approval",
}

ToolFn = Callable[[dict, dict], Awaitable[Any]]


class ToolExecutor:
    """ツール呼び出しを実行するディスパッチャー"""

    _MAX_TOOL_INPUT_SIZE = 100_000  # ツール入力の最大サイズ（文字数）

    def __init__(
        self,
        workspace: str,
        slack_token: str,
        anthropic_client=None,
        async_anthropic_client=None,
        skill_manager=None,
    ):
        self.workspace = workspace
        self.slack_token = slack_token
        self.anthropic_client = anthropic_client
        self.async_anthropic_client = async_anthropic_client
        self.skill_manager = skill_manager
        self.task_manager = TaskManager()
        self.reminder_manager = None  # MessageHandler が注入する
        # 承認待ちの依頼 {prompt_id -> {data, created_at, requester}}（10分で失効）
        self._pending_prompts: Dict[str, dict] = {}
        self._prompt_ttl_seconds = 600
        self.rate_limiter = RateLimiter()

    # ── 承認待ちの一時保存（依頼者の照合つき） ──
    def store_pending_prompt(self, data: dict, requester: str = "") -> str:
        self._cleanup_expired_prompts()
        prompt_id = str(uuid.uuid4())  # 推測困難なID
        self._pending_prompts[prompt_id] = {**data, "_created_at": time.time(), "_requester": requester}
        return prompt_id

    def get_pending_prompt(self, prompt_id: str, requester: str = "") -> Optional[dict]:
        """保存された依頼を取得（期限切れ・依頼者不一致なら None）"""
        entry = self._pending_prompts.get(prompt_id)
        if not entry:
            return None
        if time.time() - entry.get("_created_at", 0) > self._prompt_ttl_seconds:
            self._pending_prompts.pop(prompt_id, None)
            return None
        stored = entry.get("_requester", "")
        if stored and requester and stored != requester:
            logger.warning("Prompt requester mismatch")
            return None
        return entry

    def remove_pending_prompt(self, prompt_id: str):
        self._pending_prompts.pop(prompt_id, None)

    def _cleanup_expired_prompts(self):
        now = time.time()
        for pid in [p for p, d in self._pending_prompts.items() if now - d.get("_created_at", 0) > self._prompt_ttl_seconds]:
            del self._pending_prompts[pid]

    # ── 書き込み判定と説明 ──
    def is_write_operation(self, tool_name: str) -> bool:
        return tool_name in WRITE_TOOLS

    def describe_write_action(self, tool_name: str, tool_input: dict) -> str:
        """確認ボタンに出す説明（人が読める形）"""
        if tool_name == "send_dm":
            return f"<@{tool_input.get('user_id', '?')}> さんに DM を送ります"
        if tool_name == "save_to_canvas":
            return f"Canvas「{tool_input.get('title', '?')}」を保存します"
        if tool_name == "request_approval":
            return f"<@{tool_input.get('approver_id', '?')}> さんに承認を依頼します"
        return f"{tool_name} を実行します"

    # ── 実行 ──
    def _registry(self) -> Dict[str, ToolFn]:
        st = self.slack_token
        return {
            "fetch_slack_message": lambda i, c: fetch_slack_message(url=i["url"], slack_token=st),
            "send_dm": lambda i, c: send_dm(user_id=i["user_id"], message=i["message"], slack_token=st),
            "save_to_canvas": lambda i, c: save_to_canvas(
                title=i["title"], content=i["content"], channel=c.get("channel", ""), slack_token=st),
            "request_approval": lambda i, c: request_approval(
                approver_id=i["approver_id"], content=i["content"],
                requester_id=c.get("user", ""),  # 依頼者は Slack の文脈からだけ取る（引数は信じない）
                on_approve_action=i.get("on_approve_action", ""),
                on_approve_target=i.get("on_approve_target", ""), slack_token=st),
            "summarize_thread": lambda i, c: summarize_thread(
                channel=c.get("channel", i.get("channel", "")), thread_ts=c.get("thread_ts", i.get("thread_ts", "")),
                slack_token=st, anthropic_client=self.anthropic_client, style=i.get("style", "bullet")),
            "translate_text": lambda i, c: translate_text(
                text=i["text"], target_language=i["target_language"], source_language=i.get("source_language"),
                anthropic_client=self.anthropic_client),
        }

    async def execute(self, tool_name: str, tool_input: dict, context: Optional[dict] = None) -> dict:
        ctx = context or {}
        if len(json.dumps(tool_input, ensure_ascii=False, default=str)) > self._MAX_TOOL_INPUT_SIZE:
            return {"error": "ツール入力が大きすぎます"}
        fn = self._registry().get(tool_name)
        if not fn:
            return {"error": f"未登録のツールです: {tool_name}"}
        if self.is_write_operation(tool_name) and not ctx.get("confirmed"):
            return {"requires_confirmation": True, "description": self.describe_write_action(tool_name, tool_input)}
        try:
            return await fn(tool_input, ctx)
        except KeyError as e:
            return {"error": f"必須の入力がありません: {e}"}
        except Exception as e:
            logger.exception(f"tool {tool_name} failed")
            return {"error": f"{type(e).__name__}: {str(e)[:300]}"}
