"""
Slack操作ツール: DM送信、Canvas作成、チャンネル操作等
"""
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


async def send_dm(
    user_id: str,
    message: str,
    slack_token: str,
) -> dict:
    """特定ユーザーにDMを送信"""
    from slack_sdk.web.async_client import AsyncWebClient

    client = AsyncWebClient(token=slack_token)
    try:
        # DMチャンネルを開く
        conv = await client.conversations_open(users=[user_id])
        dm_channel = conv["channel"]["id"]

        # メッセージ送信
        result = await client.chat_postMessage(
            channel=dm_channel,
            text=message,
        )
        return {
            "success": True,
            "channel": dm_channel,
            "ts": result["ts"],
            "message": f"<@{user_id}> にDMを送信しました",
        }
    except Exception as e:
        logger.error(f"DM send error: {e}")
        return {
            "success": False,
            "error": str(e),
            "message": f"DM送信エラー: {str(e)[:200]}",
        }


async def save_to_canvas(
    title: str,
    content: str,
    channel: Optional[str] = None,
    slack_token: str = "",
) -> dict:
    """Slack Canvasにコンテンツを保存"""
    from slack_sdk.web.async_client import AsyncWebClient

    client = AsyncWebClient(token=slack_token)
    try:
        result = await client.canvases_create(
            title=title,
            document_content={
                "type": "markdown",
                "markdown": content,
            },
        )
        canvas_id = result.get("canvas_id", "")

        # チャンネルが指定されていれば共有通知
        if channel and canvas_id:
            await client.chat_postMessage(
                channel=channel,
                text=f":page_facing_up: *{title}* をCanvasに保存しました",
            )

        return {
            "success": True,
            "canvas_id": canvas_id,
            "title": title,
            "message": f"Canvas「{title}」を作成しました",
        }
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Canvas create error: {error_msg}")

        if "missing_scope" in error_msg or "not_allowed" in error_msg:
            return {
                "success": False,
                "error": "Canvas APIのスコープが不足しています。",
                "setup_hint": "Slack Appに canvases:write スコープを追加してください。",
            }
        return {
            "success": False,
            "error": error_msg,
            "message": f"Canvas作成エラー: {error_msg[:200]}",
        }


async def fetch_slack_message(
    url: str,
    slack_token: str,
) -> dict:
    """SlackメッセージURLから投稿内容を取得"""
    import re
    from urllib.parse import urlparse, parse_qs
    from slack_sdk.web.async_client import AsyncWebClient

    # SlackメッセージURLのパース
    # 形式: https://xxx.slack.com/archives/C12345/p1234567890123456
    # スレッド返信: 上記 + ?thread_ts=1234567890.123456&cid=C12345
    match = re.search(r'/archives/([A-Z0-9]+)/p(\d+)', url)
    if not match:
        match = re.search(r'/([A-Z0-9]+)/p(\d+)', url)
    if not match:
        return {"error": f"SlackメッセージURLを解析できません: {url[:100]}"}

    channel_id = match.group(1)
    # pXXXX形式のタイムスタンプをSlack API用に変換（XXXX → XX.XXXX）
    raw_ts = match.group(2)
    if len(raw_ts) > 6:
        message_ts = raw_ts[:10] + "." + raw_ts[10:]
    else:
        message_ts = raw_ts

    # URLクエリパラメータからthread_tsを取得（スレッド返信URL対応）
    url_thread_ts = ""
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        url_thread_ts = qs.get("thread_ts", [""])[0]
    except Exception:
        pass

    client = AsyncWebClient(token=slack_token)

    try:
        msg = None
        thread_ts = ""
        replies_text = ""

        # スレッド返信URLの場合: conversations_repliesで全スレッドを取得
        if url_thread_ts:
            try:
                replies_result = await client.conversations_replies(
                    channel=channel_id,
                    ts=url_thread_ts,
                    limit=30,
                )
                all_messages = replies_result.get("messages", [])

                # 指定されたメッセージを特定
                for m in all_messages:
                    if m.get("ts") == message_ts:
                        msg = m
                        break

                # 見つからない場合は最後のメッセージを使用
                if not msg and all_messages:
                    msg = all_messages[-1]

                # スレッド全体の返信テキストを構築
                thread_ts = url_thread_ts
                if len(all_messages) > 1:
                    reply_texts = []
                    for r in all_messages[:15]:  # 最大15件
                        r_user = r.get("user", "unknown")
                        r_text = r.get("text", "")
                        marker = " 👈 *この投稿*" if r.get("ts") == message_ts else ""
                        reply_texts.append(f"<@{r_user}>: {r_text[:500]}{marker}")
                    replies_text = "\n".join(reply_texts)
            except Exception as e:
                logger.warning(f"Thread fetch failed, falling back to history: {e}")

        # 通常のメッセージ取得（親メッセージ or スレッド取得失敗時のフォールバック）
        if not msg:
            result = await client.conversations_history(
                channel=channel_id,
                latest=message_ts,
                limit=1,
                inclusive=True,
            )

            if not result.get("messages"):
                return {"error": "指定されたメッセージが見つかりませんでした"}

            msg = result["messages"][0]

            # スレッド返信がある場合は取得
            thread_ts = msg.get("thread_ts", "")
            if thread_ts:
                try:
                    replies = await client.conversations_replies(
                        channel=channel_id,
                        ts=thread_ts,
                        limit=15,
                    )
                    if replies.get("messages") and len(replies["messages"]) > 1:
                        reply_texts = []
                        for r in replies["messages"][:15]:
                            r_user = r.get("user", "unknown")
                            r_text = r.get("text", "")
                            reply_texts.append(f"<@{r_user}>: {r_text[:500]}")
                        replies_text = "\n".join(reply_texts)
                except Exception:
                    pass

        user = msg.get("user", "unknown")
        text = msg.get("text", "")

        return {
            "success": True,
            "channel": channel_id,
            "user": user,
            "text": text,
            "ts": message_ts,
            "thread_ts": thread_ts,
            "replies": replies_text,
            "message": f"SlackメッセージURL の投稿内容を取得しました",
        }
    except Exception as e:
        logger.error(f"Slack message fetch error: {e}")
        return {"error": f"Slackメッセージの取得に失敗: {str(e)[:200]}"}


async def request_approval(
    approver_id: str,
    content: str,
    requester_id: str,
    on_approve_action: str = "",
    on_approve_target: str = "",
    slack_token: str = "",
) -> dict:
    """承認リクエストをDMで送信"""
    from slack_sdk.web.async_client import AsyncWebClient
    import time

    client = AsyncWebClient(token=slack_token)
    approval_id = f"appr_{int(time.time())}_{requester_id[:8]}"

    try:
        # 承認者にDM送信
        conv = await client.conversations_open(users=[approver_id])
        dm_channel = conv["channel"]["id"]

        approve_value = json.dumps({
            "approval_id": approval_id,
            "requester_id": requester_id,
            "on_approve_action": on_approve_action,
            "on_approve_target": on_approve_target,
        }, ensure_ascii=False)
        reject_value = json.dumps({
            "approval_id": approval_id,
            "requester_id": requester_id,
        }, ensure_ascii=False)

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*:clipboard: 承認リクエスト*\n"
                        f"<@{requester_id}> からの承認依頼:\n\n"
                        f">{content[:500]}"
                    ),
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✅ 承認"},
                        "action_id": f"approve_{approval_id}",
                        "style": "primary",
                        "value": approve_value,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "❌ 却下"},
                        "action_id": f"reject_{approval_id}",
                        "style": "danger",
                        "value": reject_value,
                    },
                ],
            },
        ]

        await client.chat_postMessage(
            channel=dm_channel,
            text=f"<@{requester_id}> から承認リクエストがあります",
            blocks=blocks,
        )

        return {
            "success": True,
            "approval_id": approval_id,
            "approver": approver_id,
            "message": f"<@{approver_id}> に承認リクエストを送信しました。承認待ちです。",
        }
    except Exception as e:
        logger.error(f"Approval request error: {e}")
        return {
            "success": False,
            "error": str(e),
            "message": f"承認リクエスト送信エラー: {str(e)[:200]}",
        }


async def download_slack_file(
    file_url: str = "",
    file_id: str = "",
    filename: str = "",
    slack_token: str = "",
    workspace_dir: str = "",
    **kwargs,
) -> dict:
    """Slackファイルをダウンロードしてワークスペースに保存する（フォールバック用）

    _extract_files での自動ダウンロードが失敗した場合に、
    AIが明示的にファイルを再ダウンロードするために使用。
    """
    import aiohttp
    import os

    if not file_url and not file_id:
        return {"success": False, "error": "file_url または file_id が必要です"}
    if not slack_token:
        return {"success": False, "error": "Slack token が未設定です"}

    # file_id から URL を取得
    if file_id and not file_url:
        try:
            from slack_sdk.web.async_client import AsyncWebClient
            client = AsyncWebClient(token=slack_token)
            info = await client.files_info(file=file_id)
            file_url = info["file"].get("url_private", "")
            if not filename:
                filename = info["file"].get("name", f"file_{file_id}")
        except Exception as e:
            return {"success": False, "error": f"ファイル情報取得エラー: {e}"}

    if not file_url:
        return {"success": False, "error": "ファイルURLを取得できませんでした"}
    if not filename:
        filename = file_url.split("/")[-1].split("?")[0] or "downloaded_file"

    # ダウンロード
    try:
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {slack_token}"}
            async with session.get(file_url, headers=headers) as resp:
                if resp.status != 200:
                    return {"success": False, "error": f"HTTP {resp.status}"}
                data = await resp.read()
    except Exception as e:
        return {"success": False, "error": f"ダウンロードエラー: {e}"}

    # ワークスペースに保存
    if not workspace_dir:
        workspace_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "workspace")
    os.makedirs(workspace_dir, exist_ok=True)
    save_path = os.path.join(workspace_dir, filename)

    try:
        with open(save_path, "wb") as f:
            f.write(data)
    except Exception as e:
        return {"success": False, "error": f"保存エラー: {e}"}

    return {
        "success": True,
        "path": save_path,
        "filename": filename,
        "size": len(data),
        "message": f"ファイルを {save_path} に保存しました。execute_code でこのパスを使って読み込めます。",
    }
