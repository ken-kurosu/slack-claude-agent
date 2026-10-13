"""
スレッドツール: スレッドのクロール・要約・アクションアイテム抽出
"""
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)


async def crawl_thread(
    channel: str, thread_ts: str, slack_token: str
) -> List[dict]:
    """スレッド内の全メッセージを取得（conversations.replies API）"""
    from slack_sdk.web.async_client import AsyncWebClient

    client = AsyncWebClient(token=slack_token)
    all_messages = []
    cursor = None

    while True:
        kwargs = {
            "channel": channel,
            "ts": thread_ts,
            "limit": 200,
        }
        if cursor:
            kwargs["cursor"] = cursor

        resp = await client.conversations_replies(**kwargs)
        all_messages.extend(resp.get("messages", []))

        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    return all_messages


async def summarize_thread(
    channel: str,
    thread_ts: str,
    slack_token: str,
    anthropic_client=None,
    style: str = "bullet",
) -> dict:
    """スレッドを要約する

    Args:
        style: bullet / narrative / action_items
    """
    if not anthropic_client:
        return {"error": "AI エンジンが利用できません"}

    messages = await crawl_thread(channel, thread_ts, slack_token)

    if len(messages) < 2:
        return {
            "success": False,
            "error": "スレッドが短すぎます（2件未満）",
            "message": "要約するほどのメッセージがありません。",
        }

    # ユーザーID集合
    user_ids = set(m.get("user", "") for m in messages if m.get("user"))

    # テキスト化
    conv_lines = []
    for m in messages:
        user = m.get("user", "bot")
        text = m.get("text", "")[:300]
        conv_lines.append(f"[{user}] {text}")

    conv_text = "\n".join(conv_lines)

    style_instructions = {
        "bullet": "箇条書きで要点をまとめてください。各ポイントは1文で簡潔に。",
        "narrative": "時系列に沿って会話の流れをストーリー形式で説明してください。",
        "action_items": (
            "以下を抽出してください:\n"
            "1. 決定事項（何が決まったか）\n"
            "2. アクションアイテム（誰が何をするか）\n"
            "3. 未解決の論点（まだ決まっていないこと）"
        ),
    }

    prompt = (
        f"以下のSlackスレッド（{len(messages)}件、参加者{len(user_ids)}人）を要約してください。\n"
        f"{style_instructions.get(style, style_instructions['bullet'])}\n\n"
        f"スレッド内容:\n{conv_text[:8000]}"
    )

    try:
        response = anthropic_client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        summary = response.content[0].text.strip()

        return {
            "success": True,
            "summary": summary,
            "message_count": len(messages),
            "participants": len(user_ids),
            "style": style,
            "message": f"スレッド要約（{len(messages)}件のメッセージ、{len(user_ids)}人参加）",
        }
    except Exception as e:
        logger.error(f"Thread summarize error: {e}")
        return {
            "success": False,
            "error": str(e),
            "message": f"スレッド要約中にエラー: {str(e)[:200]}",
        }
