"""
agent-tools MCPサーバー: エージェント固有のツールを MCP プロトコルで Claude に公開する

含むもの（エージェント から引き継いだ汎用ツール）:
- メモ (save_note, list_notes, delete_note, search_notes)
- リマインダー (set_reminder, list_reminders, cancel_reminder)
- プロジェクト記憶 (manage_project)
- Slack操作 (send_dm, fetch_slack_message)
- スキル管理 (list_skills, use_skill, add_skill, update_skill, delete_skill)
- ナレッジ (save_knowledge, search_knowledge)
- PDF編集 (edit_pdf)

業務のツールを足す時: list_tools() に Tool(...) を1つ、_dispatch() に分岐を1つ足す。
外部への書き込み・送信を伴うものは、実行前に Slack で確認を取る作りにする（勝手に送らない）。
依頼者は環境変数 AGENT_REQUEST_USER（本体が Slack のイベントから渡す）で判定し、ツール引数の user は信じない。
"""
import json
import logging
import os
import re
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agent-tools")

_AGENT_ROOT = Path(__file__).resolve().parent.parent  # プロジェクトのルート
_ENV_FILE = _AGENT_ROOT / ".env"
# .env を正とする（MCP の設定ファイルに秘密情報を書かないため、ここで読む）
if _ENV_FILE.exists():
    try:
        for _line in _ENV_FILE.read_text().splitlines():
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _key, _, _val = _line.partition("=")
            os.environ[_key.strip()] = _val.strip().strip('"').strip("'")
    except Exception as _e:
        logger.warning(f"failed to load .env: {_e}")

WORKSPACE_DIR = os.getenv("WORKSPACE_DIR", f"/tmp/{os.getenv('AGENT_ID', 'slack-agent')}-workspace")
# メモ・リマインダーは消えない場所（MEMORY_DIR）に置く。/tmp は macOS の定期掃除で消える
DATA_DIR = os.getenv("MEMORY_DIR", str(_AGENT_ROOT / "memory_data"))
MEMORY_PATH = os.path.join(DATA_DIR, "mcp_memory.json")
REMINDERS_PATH = os.path.join(DATA_DIR, "reminders.json")
SKILLS_DIR = os.path.join(WORKSPACE_DIR, "skills")
SKILLS_CONTENT_DIR = str(_AGENT_ROOT / "skills_content")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")

server = Server("agent-tools")


# ── ツール定義 ──

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="save_note",
            description="ユーザーのメモを永続保存。「覚えて」「メモして」で使用",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "メモの内容"},
                    "user": {"type": "string", "description": "ユーザーID（省略可）"},
                    "category": {
                        "type": "string",
                        "description": "カテゴリ (general/preference/work/personal)",
                        "default": "general",
                    },
                },
                "required": ["text"],
            },
        ),
        Tool(
            name="list_notes",
            description="保存されたメモの一覧を取得",
            inputSchema={
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "カテゴリでフィルタ（省略可）"},
                },
            },
        ),
        Tool(
            name="delete_note",
            description="メモを削除",
            inputSchema={
                "type": "object",
                "properties": {
                    "note_id": {"type": "integer", "description": "メモID"},
                },
                "required": ["note_id"],
            },
        ),
        Tool(
            name="search_notes",
            description="メモをキーワード検索",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "検索キーワード"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="set_reminder",
            description="リマインダーを設定。自分宛てでも他者宛てでも可",
            inputSchema={
                "type": "object",
                "properties": {
                    "target_user": {"type": "string", "description": "リマインド先ユーザーID"},
                    "message": {"type": "string", "description": "リマインドメッセージ"},
                    "remind_at": {
                        "type": "string",
                        "description": "リマインド時刻（例: '30分後', '明日 10:00', '2025-04-01 09:00'）",
                    },
                    "context": {"type": "string", "description": "追加のコンテキスト（省略可）"},
                    "requester": {"type": "string", "description": "設定者ユーザーID（省略可）"},
                    "channel": {"type": "string", "description": "チャンネルID（省略可）"},
                },
                "required": ["target_user", "message", "remind_at"],
            },
        ),
        Tool(
            name="list_reminders",
            description="有効なリマインダーの一覧を取得",
            inputSchema={
                "type": "object",
                "properties": {
                    "user": {"type": "string", "description": "ユーザーIDでフィルタ（省略可）"},
                },
            },
        ),
        Tool(
            name="cancel_reminder",
            description="リマインダーをキャンセル",
            inputSchema={
                "type": "object",
                "properties": {
                    "reminder_id": {"type": "string", "description": "リマインダーID"},
                },
                "required": ["reminder_id"],
            },
        ),
        Tool(
            name="manage_project",
            description="プロジェクトのコンテキスト・決定事項を管理",
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "update", "get"],
                        "description": "アクション",
                    },
                    "project_name": {"type": "string", "description": "プロジェクト名（update/get時必須）"},
                    "update_type": {
                        "type": "string",
                        "enum": ["context", "decision", "question", "status"],
                        "description": "更新タイプ（update時）",
                        "default": "context",
                    },
                    "content": {"type": "string", "description": "更新内容（update時）"},
                    "user": {"type": "string", "description": "ユーザーID"},
                },
                "required": ["action"],
            },
        ),
        Tool(
            name="send_dm",
            description="指定ユーザーにSlack DMを送信",
            inputSchema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "送信先ユーザーID"},
                    "message": {"type": "string", "description": "メッセージ内容"},
                },
                "required": ["user_id", "message"],
            },
        ),
        Tool(
            name="fetch_slack_message",
            description="Slackメッセージリンク(URL)からメッセージ内容を取得",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "SlackメッセージURL"},
                },
                "required": ["url"],
            },
        ),
        Tool(
            name="list_skills",
            description="利用可能なスキル（手順テンプレート）の一覧",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="use_skill",
            description="指定スキルの手順書を読み込み。手順に従って実行",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "スキル名（部分一致検索可）"},
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="add_skill",
            description=(
                "新しいスキル（手順テンプレート）を追加。YAML frontmatter付きのSKILL.md形式。"
                "類似既存スキルが見つかった場合は draft_proposal を返すので、"
                "ユーザーに(A)更新 / (B)追記 / (C)別名新規 / (D)キャンセル を尋ねる。"
                "ユーザーが (C) と答えたら confirm_action='rename_new' で再呼び出し。"
                "(A)(B) はオーナー承認が必要なので別フローで扱う。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "スキル名（英数字・ハイフン・アンダースコア）"},
                    "content": {"type": "string", "description": "スキル定義の全文（YAML frontmatter付き推奨）"},
                    "overwrite": {"type": "boolean", "description": "同名スキルが存在する場合に上書きするか（デフォルト: false、オーナー承認後のみ true）"},
                    "user_id": {"type": "string", "description": "追加者の Slack User ID（監査用、自動付与）"},
                    "channel_id": {"type": "string", "description": "追加リクエストが来たチャンネル ID（監査用）"},
                    "thread_ts": {"type": "string", "description": "追加リクエストが来たスレッド ts（監査用）"},
                    "confirm_action": {
                        "type": "string",
                        "enum": ["", "rename_new", "approved_overwrite"],
                        "description": "ユーザー判断の伝達。空=初回（draft検証）、rename_new=別名で新規、approved_overwrite=オーナー承認後の更新",
                    },
                },
                "required": ["name", "content"],
            },
        ),
        Tool(
            name="update_skill",
            description=(
                "既存スキルの内容を更新（オーナー承認必須）。"
                "request_approval(approver_id=<AGENT_OWNER_SLACK_ID>) で承認を取り、user_id に "
                "承認者の Slack ID を渡すこと。違反すると拒否される。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "更新するスキル名"},
                    "content": {"type": "string", "description": "新しいスキル定義の全文（YAML frontmatter付き推奨）"},
                    "user_id": {"type": "string", "description": "承認者（オーナー＝AGENT_OWNER_SLACK_ID）の Slack User ID"},
                    "channel_id": {"type": "string", "description": "更新リクエスト元チャンネル（監査用）"},
                    "thread_ts": {"type": "string", "description": "更新リクエスト元スレッド ts（監査用）"},
                },
                "required": ["name", "content"],
            },
        ),
        Tool(
            name="delete_skill",
            description=(
                "スキルを削除（不可逆・オーナー承認必須）。"
                "request_approval(approver_id=<AGENT_OWNER_SLACK_ID>) で承認を取ってから呼ぶこと。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "削除するスキル名"},
                    "user_id": {"type": "string", "description": "承認者（オーナー＝AGENT_OWNER_SLACK_ID）の Slack User ID"},
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="edit_pdf",
            description=(
                "既存PDFの中身をレイアウトを保ったまま書き換えて、新しいPDFを返す。"
                "文言・数値・日付・宛名などの差し替えに使う（例: 金額10,000→12,000、宛名変更、日付差し替え）。"
                "Slackに添付されたPDFの workspace 保存パス（system promptのファイル情報にある保存パス）を pdf_path に渡す。"
                "編集内容は replacements に {find:元の文字列, replace:新しい文字列} の配列で指定する。"
                "find はPDF本文に実在する文字列を正確に（原文と一致する形で）指定すること（部分一致検索）。"
                "⚠️ スキャン画像PDF（テキスト層なし）は編集不可。replaceが元より大幅に長いと隣の文字と重なる場合がある。"
                "生成された編集後PDFは自動でSlackに添付される。呼び出し後に本文をチャットに重複展開しないこと。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pdf_path": {"type": "string", "description": "編集元PDFの保存パス（Slack添付ファイルのworkspaceパス）"},
                    "replacements": {
                        "type": "array",
                        "description": "置換の配列。各要素 {find, replace, page(任意,1始まり)}",
                        "items": {
                            "type": "object",
                            "properties": {
                                "find": {"type": "string", "description": "PDF本文に実在する元の文字列（完全一致・部分検索）"},
                                "replace": {"type": "string", "description": "置き換え後の文字列"},
                                "page": {"type": "integer", "description": "対象ページ番号(1始まり,任意)。省略で全ページ"},
                            },
                            "required": ["find", "replace"],
                        },
                    },
                    "output_name": {"type": "string", "description": "出力PDFファイル名（例: 御見積書_修正版.pdf）省略可"},
                },
                "required": ["pdf_path", "replacements"],
            },
        ),
        Tool(
            name="save_knowledge",
            description=(
                "会話の中で学んだ知識をMarkdownナレッジストアに永続保存。"
                "チーム情報、プロジェクト決定事項、技術ノウハウ、ビジネス知見等を記録。"
                "「覚えておいて」「記録して」「これは重要」等のタイミングで使用。"
                "保存した知識は次回以降の会話で自動的にコンテキストとして注入される。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["learning", "team", "decision", "project", "skill"],
                        "description": (
                            "知識の種類: "
                            "'learning'=日々の学び, "
                            "'team'=チーム・メンバー情報, "
                            "'decision'=重要な決定事項, "
                            "'project'=プロジェクト固有の知識, "
                            "'skill'=スキル・ノウハウ"
                        ),
                    },
                    "content": {"type": "string", "description": "知識の内容"},
                    "category": {
                        "type": "string",
                        "description": "カテゴリ（type=learningの場合: tech/business/team/process等）",
                        "default": "general",
                    },
                    "project": {
                        "type": "string",
                        "description": "プロジェクト名（type=project/decisionの場合に使用）",
                    },
                    "topic": {
                        "type": "string",
                        "description": "トピック名（type=skillの場合に使用）",
                    },
                    "member_name": {
                        "type": "string",
                        "description": "メンバー名（type=teamの場合、既存セクションを更新）",
                    },
                    "source": {
                        "type": "string",
                        "description": "情報源（チャンネル名、ユーザー名等）",
                    },
                },
                "required": ["type", "content"],
            },
        ),
        Tool(
            name="search_knowledge",
            description=(
                "蓄積されたナレッジをキーワード検索。"
                "過去に記録した学び、チーム情報、決定事項、プロジェクト知識を横断検索。"
                "「前に話した〇〇」「〇〇について知ってる？」等で使用。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "検索キーワード（スペース区切りでAND検索）"},
                    "max_results": {"type": "integer", "description": "最大件数", "default": 5},
                },
                "required": ["query"],
            },
        ),
    ]


# ── ヘルパー ──

def _load_memory() -> dict:
    """memory.json を読み込み"""
    if os.path.exists(MEMORY_PATH):
        with open(MEMORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_memory(data: dict):
    """memory.json に書き込み"""
    os.makedirs(os.path.dirname(MEMORY_PATH) or ".", exist_ok=True)
    with open(MEMORY_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_reminders() -> list:
    """reminders.json を読み込み"""
    if os.path.exists(REMINDERS_PATH):
        with open(REMINDERS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def _save_reminders(data: list):
    """reminders.json に書き込み"""
    os.makedirs(os.path.dirname(REMINDERS_PATH) or ".", exist_ok=True)
    with open(REMINDERS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _parse_remind_at(remind_at: str) -> Optional[float]:
    """リマインド時刻を解析してUnixTimestampを返す"""
    now = datetime.now()
    # 相対指定 (例: "30分後", "1時間後", "2日後")
    m = re.match(r"(\d+)\s*(分|時間|日)後", remind_at)
    if m:
        num = int(m.group(1))
        unit = m.group(2)
        if unit == "分":
            dt = now + timedelta(minutes=num)
        elif unit == "時間":
            dt = now + timedelta(hours=num)
        elif unit == "日":
            dt = now + timedelta(days=num)
        return dt.timestamp()
    # 絶対指定 (例: "2025-04-01 09:00", "14:30", "明日 10:00")
    if "明日" in remind_at:
        time_part = remind_at.replace("明日", "").strip()
        try:
            t = datetime.strptime(time_part, "%H:%M")
            dt = (now + timedelta(days=1)).replace(hour=t.hour, minute=t.minute, second=0)
            return dt.timestamp()
        except ValueError:
            pass
    # ISO形式
    for fmt in ["%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%H:%M"]:
        try:
            dt = datetime.strptime(remind_at, fmt)
            if fmt == "%H:%M":
                dt = now.replace(hour=dt.hour, minute=dt.minute, second=0)
                if dt < now:
                    dt += timedelta(days=1)
            return dt.timestamp()
        except ValueError:
            continue
    return None


_TRANSIENT_ERROR_PATTERNS = (
    "rate limit", "ratelimit", "429", "timeout", "timed out",
    "connection reset", "connection aborted", "temporarily unavailable",
    "503", "502", "504", "econnreset",
)


def _is_transient_error(err_msg: str) -> bool:
    """ネットワーク・レート制限などの一過性エラーか判定。"""
    low = err_msg.lower()
    return any(p in low for p in _TRANSIENT_ERROR_PATTERNS)


def _save_note(text: str, user: str, category: str) -> dict:
    mem = _load_memory()
    notes = mem.get("user_notes", [])
    max_id = max((n.get("id", 0) for n in notes), default=0)
    new_note = {
        "id": max_id + 1,
        "text": text,
        "user": user,
        "category": category,
        "created_at": time.time(),
        "created_date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "active": True,
    }
    notes.append(new_note)
    mem["user_notes"] = notes
    _save_memory(mem)
    return {"success": True, "note_id": new_note["id"], "message": f"メモを保存しました（ID: {new_note['id']}）"}


def _list_notes(category: str) -> dict:
    mem = _load_memory()
    notes = [n for n in mem.get("user_notes", []) if n.get("active", True)]
    if category:
        notes = [n for n in notes if n.get("category") == category]
    if not notes:
        return {"success": True, "notes": [], "message": "メモはまだ保存されていません。"}
    return {
        "success": True,
        "notes": [
            {"id": n["id"], "text": n["text"], "category": n.get("category", ""), "date": n.get("created_date", "")}
            for n in notes
        ],
        "count": len(notes),
    }


def _delete_note(note_id: int) -> dict:
    mem = _load_memory()
    notes = mem.get("user_notes", [])
    for n in notes:
        if n.get("id") == note_id:
            n["active"] = False
            mem["user_notes"] = notes
            _save_memory(mem)
            return {"success": True, "message": f"メモ（ID: {note_id}）を削除しました"}
    return {"error": f"メモ（ID: {note_id}）が見つかりません"}


def _search_notes(query: str) -> dict:
    mem = _load_memory()
    notes = [n for n in mem.get("user_notes", []) if n.get("active", True)]
    query_lower = query.lower()
    results = [n for n in notes if query_lower in n.get("text", "").lower()]
    return {
        "success": True,
        "results": [
            {"id": n["id"], "text": n["text"], "category": n.get("category", "")}
            for n in results
        ],
        "count": len(results),
    }


def _set_reminder(args: dict) -> dict:
    remind_at_str = args["remind_at"]
    ts = _parse_remind_at(remind_at_str)
    if ts is None:
        return {"error": f"時刻を解析できません: {remind_at_str}（例: '30分後', '明日 10:00', '2025-04-01 09:00'）"}

    reminders = _load_reminders()
    target = args["target_user"]
    reminder_id = f"rem_{int(time.time())}_{target[:6]}"
    new_rem = {
        "id": reminder_id,
        "target_user": target,
        "message": args["message"],
        "context": args.get("context", ""),
        "remind_at": remind_at_str,
        "remind_at_ts": ts,
        "requester": args.get("requester", ""),
        "channel": args.get("channel", ""),
        "thread_ts": "",
        "workspace": "main",
        "status": "pending",
        "created_at": time.time(),
    }
    reminders.append(new_rem)
    _save_reminders(reminders)

    dt = datetime.fromtimestamp(ts)
    return {
        "success": True,
        "reminder_id": reminder_id,
        "remind_at_formatted": dt.strftime("%Y-%m-%d %H:%M"),
        "message": f"⏰ <@{target}> へのリマインダーを {dt.strftime('%m/%d %H:%M')} に設定しました",
    }


def _list_reminders(user: str) -> dict:
    reminders = _load_reminders()
    active = [r for r in reminders if r.get("status") == "pending"]
    if user:
        active = [r for r in active if r.get("target_user") == user or r.get("requester") == user]
    if not active:
        return {"success": True, "reminders": [], "message": "有効なリマインダーはありません。"}
    return {
        "success": True,
        "reminders": [
            {
                "id": r["id"],
                "target_user": r["target_user"],
                "message": r["message"],
                "remind_at": r.get("remind_at", ""),
                "requester": r.get("requester", ""),
            }
            for r in active
        ],
        "count": len(active),
    }


def _cancel_reminder(reminder_id: str) -> dict:
    reminders = _load_reminders()
    for r in reminders:
        if r.get("id") == reminder_id:
            r["status"] = "cancelled"
            _save_reminders(reminders)
            return {"success": True, "message": f"リマインダー {reminder_id} をキャンセルしました"}
    return {"error": f"リマインダー {reminder_id} が見つかりません"}


def _manage_project(args: dict) -> dict:
    action = args.get("action", "list")
    mem = _load_memory()
    projects = mem.get("project_memory", {})

    if action == "list":
        if not projects:
            return {"success": True, "projects": [], "message": "プロジェクトはまだ登録されていません。"}
        return {
            "success": True,
            "projects": [
                {"name": k, "status": v.get("status", "active"), "description": v.get("description", "")}
                for k, v in projects.items()
            ],
            "count": len(projects),
        }
    elif action == "get":
        name = args.get("project_name", "")
        if name not in projects:
            return {"error": f"プロジェクト「{name}」が見つかりません"}
        return {"success": True, "project": projects[name]}
    elif action == "update":
        name = args.get("project_name", "")
        if not name:
            return {"error": "project_name は必須です"}
        update_type = args.get("update_type", "context")
        content = args.get("content", "")
        user = args.get("user", "")

        if name not in projects:
            projects[name] = {
                "name": name,
                "description": "",
                "created_at": time.time(),
                "last_updated": time.time(),
                "decisions": [],
                "open_questions": [],
                "key_context": [],
                "participants": [],
                "status": "active",
            }

        proj = projects[name]
        proj["last_updated"] = time.time()
        entry = {"content": content, "by": user, "at": datetime.now().isoformat()}

        if update_type == "decision":
            proj.setdefault("decisions", []).append(entry)
        elif update_type == "question":
            proj.setdefault("open_questions", []).append(entry)
        elif update_type == "status":
            proj["status"] = content
        else:
            proj.setdefault("key_context", []).append(entry)

        if user and user not in proj.get("participants", []):
            proj.setdefault("participants", []).append(user)

        mem["project_memory"] = projects
        _save_memory(mem)
        return {"success": True, "message": f"プロジェクト「{name}」を更新しました"}

    return {"error": f"未知のアクション: {action}"}


async def _send_dm(user_id: str, message: str) -> dict:
    if not SLACK_BOT_TOKEN:
        return {"error": "SLACK_BOT_TOKEN が設定されていません"}
    try:
        from slack_sdk.web.async_client import AsyncWebClient
        client = AsyncWebClient(token=SLACK_BOT_TOKEN)
        conv = await client.conversations_open(users=[user_id])
        dm_channel = conv["channel"]["id"]
        result = await client.chat_postMessage(channel=dm_channel, text=message)
        return {"success": True, "channel": dm_channel, "ts": result["ts"],
                "message": f"<@{user_id}> にDMを送信しました"}
    except Exception as e:
        return {"error": f"DM送信エラー: {str(e)[:300]}"}


async def _fetch_slack_message(url: str) -> dict:
    if not SLACK_BOT_TOKEN:
        return {"error": "SLACK_BOT_TOKEN が設定されていません"}
    try:
        from slack_sdk.web.async_client import AsyncWebClient
        # URL パース (例: https://xxx.slack.com/archives/C01ABC/p1234567890123456)
        parts = url.rstrip("/").split("/")
        channel_id = None
        message_ts = None
        for i, part in enumerate(parts):
            if part == "archives" and i + 1 < len(parts):
                channel_id = parts[i + 1]
                if i + 2 < len(parts) and parts[i + 2].startswith("p"):
                    # p1234567890123456 → 1234567890.123456
                    raw = parts[i + 2][1:]
                    message_ts = raw[:10] + "." + raw[10:]
        if not channel_id or not message_ts:
            return {"error": f"SlackメッセージURLを解析できません: {url}"}
        client = AsyncWebClient(token=SLACK_BOT_TOKEN)
        # スレッド内のメッセージも取得
        result = await client.conversations_replies(channel=channel_id, ts=message_ts, limit=10)
        messages = result.get("messages", [])
        if not messages:
            return {"error": "メッセージが見つかりません"}
        return {
            "success": True,
            "messages": [
                {"user": m.get("user", ""), "text": m.get("text", ""), "ts": m.get("ts", "")}
                for m in messages
            ],
            "count": len(messages),
        }
    except Exception as e:
        return {"error": f"メッセージ取得エラー: {str(e)[:300]}"}


def _scan_all_skills() -> list[dict]:
    """全スキルをスキャンして一覧を返す（SKILLS_DIR + SKILLS_CONTENT_DIR 両対応）"""
    seen = set()
    skills = []

    # --- 1. skills_content/ フラット.md + サブディレクトリ ---
    if os.path.isdir(SKILLS_CONTENT_DIR):
        for entry in sorted(os.listdir(SKILLS_CONTENT_DIR)):
            full = os.path.join(SKILLS_CONTENT_DIR, entry)
            # フラット.md
            if entry.endswith(".md") and os.path.isfile(full) and not entry.endswith(".bak"):
                name = entry[:-3]  # .md除去
                if name not in seen:
                    meta = _parse_skill_meta(full)
                    skills.append({"name": name, "path": full, **meta})
                    seen.add(name)
            # サブディレクトリ形式
            elif os.path.isdir(full):
                skill_file = os.path.join(full, "SKILL.md")
                if os.path.exists(skill_file):
                    if entry not in seen:
                        meta = _parse_skill_meta(skill_file)
                        skills.append({"name": entry, "path": skill_file, **meta})
                        seen.add(entry)

    # --- 2. SKILLS_DIR（/tmp/workspace/skills/）サブディレクトリ形式 ---
    if os.path.isdir(SKILLS_DIR):
        for entry in sorted(os.listdir(SKILLS_DIR)):
            skill_dir = os.path.join(SKILLS_DIR, entry)
            skill_file = os.path.join(skill_dir, "SKILL.md")
            if os.path.isdir(skill_dir) and os.path.exists(skill_file):
                if entry not in seen:
                    meta = _parse_skill_meta(skill_file)
                    skills.append({"name": entry, "path": skill_file, **meta})
                    seen.add(entry)

    return skills


def _find_skill_file(name: str) -> tuple[str | None, str | None]:
    """スキル名からファイルパスを検索。(path, actual_name) を返す"""
    name_lower = name.lower().replace(" ", "_").replace("-", "_")

    # 完全一致: skills_content/name.md
    flat_file = os.path.join(SKILLS_CONTENT_DIR, f"{name}.md")
    if os.path.isfile(flat_file):
        return flat_file, name
    # ハイフン/アンダースコア変換
    for variant in [name, name.replace("_", "-"), name.replace("-", "_")]:
        flat_file = os.path.join(SKILLS_CONTENT_DIR, f"{variant}.md")
        if os.path.isfile(flat_file):
            return flat_file, variant

    # 完全一致: skills_content/name/SKILL.md
    subdir_file = os.path.join(SKILLS_CONTENT_DIR, name, "SKILL.md")
    if os.path.isfile(subdir_file):
        return subdir_file, name

    # 完全一致: SKILLS_DIR/name/SKILL.md
    legacy_file = os.path.join(SKILLS_DIR, name, "SKILL.md")
    if os.path.isfile(legacy_file):
        return legacy_file, name

    # 部分一致検索
    for scan_dir in [SKILLS_CONTENT_DIR, SKILLS_DIR]:
        if not os.path.isdir(scan_dir):
            continue
        for entry in os.listdir(scan_dir):
            entry_lower = entry.lower().replace("-", "_").replace(".md", "")
            if name_lower in entry_lower:
                full = os.path.join(scan_dir, entry)
                if entry.endswith(".md") and os.path.isfile(full):
                    return full, entry[:-3]
                elif os.path.isdir(full):
                    sf = os.path.join(full, "SKILL.md")
                    if os.path.isfile(sf):
                        return sf, entry

    return None, None


def _list_skills_impl() -> dict:
    """全スキル一覧を返す（skills_content/ + SKILLS_DIR 横断）"""
    skills = _scan_all_skills()
    if not skills:
        return {"success": True, "skills": [], "message": "スキルはまだ登録されていません。"}
    # pathは内部情報なので返さない
    result = [
        {"name": s["name"], "description": s.get("description", ""), "triggers": s.get("triggers", [])}
        for s in skills
    ]
    return {"success": True, "skills": result, "count": len(result)}


def _use_skill_impl(name: str) -> dict:
    """スキルの内容を読み込み（skills_content/ + SKILLS_DIR 横断検索）"""
    path, actual_name = _find_skill_file(name)
    if path and os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        return {"success": True, "name": actual_name, "content": content,
                "message": f"スキル「{actual_name}」を読み込みました。この手順に従って実行してください。"}
    return {"error": f"スキル「{name}」が見つかりません。list_skills で一覧を確認してください。"}


def _validate_skill_content(content: str) -> tuple[bool, str]:
    """スキル内容のバリデーション"""
    if not content or not content.strip():
        return False, "スキル内容が空です"
    if len(content) > 100_000:
        return False, "スキル内容が大きすぎます（上限100KB）"
    # YAML frontmatter推奨チェック（警告レベル、エラーにはしない）
    has_frontmatter = content.strip().startswith("---")
    if not has_frontmatter:
        logger.warning("スキルにYAML frontmatterがありません（推奨: --- で囲んだメタデータ）")
    return True, ""


SIMILARITY_THRESHOLD = 5


def _format_skill_brief(skill: dict) -> str:
    """既存スキルの機能概要を1スキル5〜8行でフォーマット。

    追加者は既存スキルの中身を知らないため、被りと判定したスキルの「何ができるか」を
    必ず提示する。description が空ならファイル本文先頭から抽出してフォールバック。
    """
    name = skill.get("name", "(no name)")
    desc = (skill.get("description") or "").strip()
    triggers = skill.get("triggers") or []
    tags = skill.get("tags") or []

    # description が空なら本文先頭1段落を100字に丸めてフォールバック
    if not desc:
        path = skill.get("path") or ""
        try:
            with open(path, "r", encoding="utf-8") as f:
                body = f.read()
            # frontmatter 除去
            if body.startswith("---"):
                end = body.find("---", 3)
                if end > 0:
                    body = body[end + 3:]
            # 先頭の見出し / 空行をスキップして最初の意味ある段落
            for para in body.split("\n\n"):
                para = para.strip()
                if not para or para.startswith("#"):
                    continue
                desc = para[:100] + ("…" if len(para) > 100 else "")
                break
        except Exception:
            pass

    lines = [f"【既存スキル: {name}】"]
    if desc:
        lines.append(f" ・できること: {desc}")
    if triggers:
        lines.append(f" ・トリガー: {', '.join(triggers[:8])}")
    if tags:
        lines.append(f" ・タグ: {', '.join(tags[:5])}")
    return "\n".join(lines)


def _has_common_substring(a: str, b: str, min_len: int = 4) -> bool:
    """a と b に min_len 文字以上の共通連続文字列があれば True。

    日本語スキル名（pptx_creation_guide ↔ pptx_creation_helper など）の判定用。
    """
    if not a or not b or len(a) < min_len or len(b) < min_len:
        return False
    for i in range(len(a) - min_len + 1):
        if a[i:i + min_len] in b:
            return True
    return False


def _find_similar_skills(name: str, content: str, triggers: list[str]) -> list[dict]:
    """新規追加リクエストに類似する既存スキルを上位3件まで返す。

    スコア:
    - trigger 完全一致: +10 each
    - trigger 部分一致（一方が他方を含む）: +5 each
    - スキル名 完全部分一致: +5
    - スキル名 4文字以上の共通サブストリング: +5
    - 説明文の単語overlap (空白ベース): +N（最大5）
    - 説明文の日本語文字 set overlap >= 30文字: +N（最大5）
    """
    new_triggers_lower = [t.lower() for t in (triggers or [])]
    name_lower = (name or "").lower()
    content_lower = (content or "").lower()
    content_words = {w for w in content_lower.split() if len(w) > 2}
    content_chars = set(content_lower[:500])

    existing = _scan_all_skills()
    results = []
    for s in existing:
        score = 0
        s_name = (s.get("name") or "").lower()
        s_triggers_lower = [t.lower() for t in (s.get("triggers") or [])]
        s_desc = (s.get("description") or "").lower()
        s_desc_words = {w for w in s_desc.split() if len(w) > 2}

        # トリガー完全一致 / 部分一致
        for nt in new_triggers_lower:
            for st in s_triggers_lower:
                if nt == st:
                    score += 10
                elif nt in st or st in nt:
                    score += 5

        # 名前 完全部分一致
        if name_lower and (name_lower in s_name or s_name in name_lower):
            score += 5
        # 名前 共通サブストリング（5文字以上 — 偶然一致を避ける）
        elif _has_common_substring(name_lower, s_name, min_len=5):
            score += 5

        # 説明文 単語overlap
        if content_words and s_desc_words:
            overlap = len(content_words & s_desc_words)
            score += min(overlap, 5)

        # 説明文 日本語文字 set overlap（trigger/name が部分的に当たっている時のみ加点）
        # 単独で効くと、頻出ひらがなの偶然overlapで誤検出が増えるため、補助スコア扱い
        if score > 0 and content_chars and s_desc:
            desc_chars = set(s_desc[:500])
            char_overlap = len(content_chars & desc_chars)
            if char_overlap >= 40:
                score += min((char_overlap - 40) // 5, 5)

        if score >= SIMILARITY_THRESHOLD:
            results.append({**s, "similarity_score": score})

    results.sort(key=lambda x: x["similarity_score"], reverse=True)
    return results[:3]


def _check_trigger_conflicts(new_triggers: list[str], existing_skills: list[dict]) -> list[dict]:
    """新triggers と既存スキルの triggers が完全一致する箇所を検出。

    [{"trigger": "プレゼン作成", "owner_skill": "pptx_creation_guide"}, ...] を返す。
    """
    if not new_triggers:
        return []
    new_set = {t.lower(): t for t in new_triggers}
    conflicts = []
    for s in existing_skills:
        for t in (s.get("triggers") or []):
            t_lower = t.lower()
            if t_lower in new_set:
                conflicts.append({
                    "trigger": new_set[t_lower],
                    "owner_skill": s.get("name", ""),
                })
    return conflicts


def _inject_audit_frontmatter(content: str, user_id: str = "",
                              channel_id: str = "", thread_ts: str = "",
                              action: str = "created") -> str:
    """frontmatter に created_by / created_at / source_thread を埋め込む。

    既存frontmatter があれば該当キーを追加/更新。なければ新規frontmatter を先頭に追加。
    action が "updated" の場合は updated_by / updated_at を使う。
    """
    from datetime import datetime
    if not user_id and not channel_id and not thread_ts:
        return content

    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S%z") or datetime.now().isoformat()
    prefix = "updated" if action == "updated" else "created"

    audit_lines = []
    if user_id:
        audit_lines.append(f"{prefix}_by: {user_id}")
    audit_lines.append(f"{prefix}_at: {now_iso}")
    if channel_id and thread_ts:
        audit_lines.append(f"source_thread: {channel_id}/{thread_ts}")

    body = content
    if body.startswith("---"):
        # 既存frontmatter の閉じ "---" を見つけて手前に挿入
        end_idx = body.find("\n---", 3)
        if end_idx > 0:
            head = body[:end_idx]
            tail = body[end_idx:]
            # 既存に同名キーがあれば重複しないように除外
            head_lines = head.split("\n")
            existing_keys = {l.split(":", 1)[0].strip() for l in head_lines if ":" in l}
            audit_lines = [l for l in audit_lines if l.split(":", 1)[0].strip() not in existing_keys]
            if audit_lines:
                return head + "\n" + "\n".join(audit_lines) + tail
        return body
    # frontmatter なし → 新規追加
    fm = "---\n" + "\n".join(audit_lines) + "\n---\n\n"
    return fm + body


def _add_skill_impl(name: str, content: str, overwrite: bool = False,
                    user_id: str = "", channel_id: str = "", thread_ts: str = "",
                    confirm_action: str = "") -> dict:
    """新しいスキルを skills_content/ に追加。

    2026-05-29 改修（A の MVP）:
    - confirm_action="" で類似既存スキルが見つかった場合、即追加せずに
      draft_proposal を返す。エージェント は人間に「どうする?」を尋ねる
    - confirm_action="rename_new" → 別名で新規追加（衝突避け）
    - overwrite=True or confirm_action="approved_overwrite" → 既存上書き
      （ただしオーナー Slack ID チェック必須 — AGENT_OWNER_SLACK_ID 環境変数）
    - frontmatter に created_by / created_at / source_thread を自動記録（監査）
    - triggers の完全一致衝突を検出して draft_proposal に警告として同梱
    """
    import re
    # 名前のサニタイズ
    safe_name = re.sub(r"[^\w\-]", "_", name.lower().strip())
    if not safe_name:
        return {"error": "スキル名が無効です"}

    # ── 既存上書きにはオーナー承認必須 ──
    if (overwrite or confirm_action == "approved_overwrite"):
        owner_id = os.getenv("AGENT_OWNER_SLACK_ID", "").strip()
        if owner_id and user_id != owner_id:
            return {
                "error": (
                    f"既存スキルの上書きはオーナー（{owner_id}）の承認が必要です。"
                    f"request_approval ツールで承認を取り、承認者の user_id を渡してください。"
                ),
                "requires_owner_approval": True,
            }
        if not owner_id:
            logger.warning(
                "AGENT_OWNER_SLACK_ID 未設定のため上書き承認チェックをスキップ。"
                ".env に AGENT_OWNER_SLACK_ID=<オーナーのSlackユーザーID> を設定してください"
            )

    # バリデーション
    valid, err_msg = _validate_skill_content(content)
    if not valid:
        return {"error": err_msg}

    # 既存チェック
    os.makedirs(SKILLS_CONTENT_DIR, exist_ok=True)
    target_path = os.path.join(SKILLS_CONTENT_DIR, f"{safe_name}.md")
    subdir_path = os.path.join(SKILLS_CONTENT_DIR, safe_name)
    name_collision = (
        os.path.exists(target_path)
        or (os.path.isdir(subdir_path) and os.path.exists(os.path.join(subdir_path, "SKILL.md")))
    )

    # 提案された content から triggers を抽出（draft フェーズで類似検出に使う）
    proposed_triggers = []
    if content.startswith("---"):
        end_idx = content.find("\n---", 3)
        if end_idx > 0:
            for fm_line in content[:end_idx].split("\n")[1:]:
                if fm_line.strip().startswith("triggers:"):
                    triggers_str = fm_line.split(":", 1)[1].strip()
                    proposed_triggers = [t.strip() for t in triggers_str.split(",") if t.strip()]
                    break

    # ── 名前完全一致のとき: overwrite フラグの有無で分岐 ──
    if name_collision and not overwrite and confirm_action != "approved_overwrite":
        existing = _scan_all_skills()
        # 名前一致の既存スキル詳細
        existing_match = next((s for s in existing if s.get("name") == safe_name), None)
        return {
            "draft_proposal": True,
            "reason": "name_collision",
            "safe_name": safe_name,
            "existing_skill_brief": _format_skill_brief(existing_match) if existing_match else "",
            "message": (
                f"スキル「{safe_name}」は既に存在します。\n\n"
                f"{_format_skill_brief(existing_match) if existing_match else ''}\n\n"
                f"どうしますか?\n"
                f"  (A) 既存を更新（オーナー承認必須）\n"
                f"  (B) 既存に追記（オーナー承認必須）\n"
                f"  (C) 別名で新規追加 → confirm_action=\"rename_new\" で呼び直し\n"
                f"  (D) キャンセル"
            ),
            "choices": ["update_existing", "append_existing", "rename_new", "cancel"],
        }

    # ── 類似検出 + trigger衝突: confirm_action が無いときだけ（draft フェーズ） ──
    if not confirm_action and not overwrite:
        similar = _find_similar_skills(safe_name, content, proposed_triggers)
        if similar:
            # 既存全スキル取得は重いので、類似検出で取った existing を再利用したいが
            # _find_similar_skills は score 適合のみ返すので、trigger衝突は別途検出
            all_existing = _scan_all_skills()
            conflicts = _check_trigger_conflicts(proposed_triggers, all_existing)

            briefs = "\n\n".join(_format_skill_brief(s) for s in similar)
            conflict_warn = ""
            if conflicts:
                lines = ["⚠️ trigger 衝突あり:"]
                for c in conflicts:
                    lines.append(f"  ・「{c['trigger']}」は既存「{c['owner_skill']}」と重複")
                conflict_warn = "\n\n" + "\n".join(lines)

            return {
                "draft_proposal": True,
                "reason": "similar_existing",
                "safe_name": safe_name,
                "similar_skills": [{"name": s["name"], "score": s["similarity_score"]} for s in similar],
                "trigger_conflicts": conflicts,
                "message": (
                    f"似たスキルが既に存在するかもしれません。\n\n"
                    f"{briefs}{conflict_warn}\n\n"
                    f"どうしますか?\n"
                    f"  (A) 既存を更新（オーナー承認必須）\n"
                    f"  (B) 既存に追記（オーナー承認必須）\n"
                    f"  (C) 別名「{safe_name}」で新規追加 → confirm_action=\"rename_new\" で呼び直し\n"
                    f"  (D) キャンセル"
                ),
                "choices": ["update_existing", "append_existing", "rename_new", "cancel"],
            }

    # ── 「別名で新規」確定 → name 衝突しない名前を選ぶ ──
    if confirm_action == "rename_new" and name_collision:
        for suffix in range(2, 100):
            candidate = f"{safe_name}_v{suffix}"
            if not os.path.exists(os.path.join(SKILLS_CONTENT_DIR, f"{candidate}.md")) \
               and not os.path.isdir(os.path.join(SKILLS_CONTENT_DIR, candidate)):
                safe_name = candidate
                target_path = os.path.join(SKILLS_CONTENT_DIR, f"{safe_name}.md")
                break

    # ── 監査メタデータを frontmatter に注入 ──
    action = "updated" if (overwrite or confirm_action == "approved_overwrite") else "created"
    content_with_audit = _inject_audit_frontmatter(
        content, user_id=user_id, channel_id=channel_id,
        thread_ts=thread_ts, action=action,
    )

    # 保存
    with open(target_path, "w", encoding="utf-8") as f:
        f.write(content_with_audit)

    meta = _parse_skill_meta(target_path)
    audit_tag = f" by {user_id}" if user_id else ""
    logger.info(f"スキル{action}: {safe_name}{audit_tag}")
    return {
        "success": True,
        "name": safe_name,
        "path": target_path,
        "description": meta.get("description", ""),
        "audit": {"user_id": user_id, "action": action},
        "message": f"スキル「{safe_name}」を{action == 'updated' and '更新' or '追加'}しました",
    }


def _update_skill_impl(name: str, content: str, user_id: str = "",
                       channel_id: str = "", thread_ts: str = "") -> dict:
    """既存スキルの内容を更新（P3-1: オーナー承認チェック追加）"""
    path, actual_name = _find_skill_file(name)
    if not path:
        return {"error": f"スキル「{name}」が見つかりません。list_skills で一覧を確認してください"}

    # P3-1 (2026-05-29): 既存スキル更新はオーナー承認必須
    owner_id = os.getenv("AGENT_OWNER_SLACK_ID", "").strip()
    if owner_id and user_id != owner_id:
        return {
            "error": (
                f"既存スキルの更新はオーナー（{owner_id}）の承認が必要です。"
                f"request_approval(approver_id='{owner_id}') で承認を取り、"
                f"user_id に承認者の Slack ID を渡してください。"
            ),
            "requires_owner_approval": True,
            "next_action": "request_approval",
        }

    # バリデーション
    valid, err_msg = _validate_skill_content(content)
    if not valid:
        return {"error": err_msg}

    # バックアップ作成
    import shutil
    from datetime import datetime
    backup_path = f"{path}.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(path, backup_path)

    # 監査メタデータを frontmatter に注入（add_skill と同じパターン）
    content_with_audit = _inject_audit_frontmatter(
        content, user_id=user_id, channel_id=channel_id,
        thread_ts=thread_ts, action="updated",
    )

    # 更新
    with open(path, "w", encoding="utf-8") as f:
        f.write(content_with_audit)

    audit_tag = f" by {user_id}" if user_id else ""
    logger.info(f"スキル更新: {actual_name}{audit_tag} (backup: {backup_path})")
    return {
        "success": True,
        "name": actual_name,
        "audit": {"user_id": user_id, "action": "updated"},
        "next_action": "notify_thread",
        "message": f"スキル「{actual_name}」を更新しました。バックアップ: {os.path.basename(backup_path)}",
    }


def _delete_skill_impl(name: str, user_id: str = "") -> dict:
    """スキルを削除（P3-1: オーナー承認チェック追加・不可逆）"""
    import shutil

    # P3-1 (2026-05-29): スキル削除は不可逆なのでオーナー承認必須
    owner_id = os.getenv("AGENT_OWNER_SLACK_ID", "").strip()
    if owner_id and user_id != owner_id:
        return {
            "error": (
                f"スキル削除は不可逆なためオーナー（{owner_id}）の承認が必要です。"
                f"request_approval(approver_id='{owner_id}') で承認を取ってから呼んでください。"
            ),
            "requires_owner_approval": True,
            "next_action": "request_approval",
        }

    path, actual_name = _find_skill_file(name)
    if not path:
        return {"error": f"スキル「{name}」が見つかりません"}

    # フラット.md → ファイル削除
    if path.endswith(".md") and os.path.isfile(path):
        os.remove(path)
        logger.info(f"スキル削除: {actual_name} ({path})")
        return {"success": True, "message": f"スキル「{actual_name}」を削除しました"}

    # サブディレクトリ形式 → ディレクトリごと削除
    skill_dir = os.path.dirname(path)
    if os.path.isdir(skill_dir):
        shutil.rmtree(skill_dir, ignore_errors=True)
        logger.info(f"スキルディレクトリ削除: {actual_name} ({skill_dir})")
        return {"success": True, "message": f"スキル「{actual_name}」を削除しました"}

    return {"error": f"スキル「{name}」の削除に失敗しました"}


def _parse_skill_meta(filepath: str) -> dict:
    """SKILL.md のメタデータを解析（YAML frontmatter + 旧形式対応）"""
    meta = {"description": "", "triggers": [], "tags": []}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        lines = content.split("\n")

        # --- YAML frontmatter ---
        if lines and lines[0].strip() == "---":
            for i, line in enumerate(lines[1:], 1):
                if line.strip() == "---":
                    # インライン形式とブロックリスト形式の両方に対応（外部依存なし）。
                    # 旧パーサは "triggers:\n  - x" 形式を読めず triggers が空になっていた。
                    list_keys = {"triggers", "tags"}
                    current_list_key = None
                    for fm_line in lines[1:i]:
                        stripped = fm_line.strip()
                        if not stripped or stripped.startswith("#"):
                            continue
                        if stripped.startswith("- ") or stripped == "-":
                            if current_list_key:
                                item = stripped[1:].strip().strip('"').strip("'")
                                if item:
                                    meta[current_list_key].append(item)
                            continue
                        if ":" not in stripped:
                            continue
                        key, val = stripped.split(":", 1)
                        key = key.strip().lower()
                        val = val.strip()
                        current_list_key = None
                        if key in list_keys:
                            if val:
                                if val.startswith("[") and val.endswith("]"):
                                    val = val[1:-1]
                                meta[key] = [t.strip().strip('"').strip("'") for t in val.split(",") if t.strip()]
                            else:
                                current_list_key = key
                                meta[key] = []
                        elif key == "description" and val:
                            meta["description"] = val
                    return meta

        # --- 旧形式（先頭100行のキーワード解析） ---
        for line in lines[:100]:
            stripped = line.strip()
            if stripped.startswith("Description:") or stripped.startswith("概要:"):
                meta["description"] = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("Triggers:") or stripped.startswith("トリガー:"):
                triggers_str = stripped.split(":", 1)[1].strip()
                meta["triggers"] = [t.strip() for t in triggers_str.split(",") if t.strip()]
            elif stripped.startswith("Tags:") or stripped.startswith("タグ:"):
                tags_str = stripped.split(":", 1)[1].strip()
                meta["tags"] = [t.strip() for t in tags_str.split(",") if t.strip()]
        # 説明がなければ先頭のヘッダーを使用
        if not meta["description"]:
            for line in lines[:20]:
                if line.startswith("# "):
                    meta["description"] = line[2:].strip()
                    break
    except Exception:
        pass
    return meta


async def _edit_pdf(pdf_path: str, replacements: list, output_name: str) -> dict:
    """既存PDFの文字列をレイアウト維持で置換して新しいPDFを保存（PyMuPDF redaction+再挿入）。

    実証済み: redactionで旧テキストを白塗り除去 → TextWriter + fitz.Font("cjk")(日本語対応・
    macOSフォント非依存) で元のbaseline/サイズ/色で再挿入。生成PDFは自動でSlack添付される。
    """
    import asyncio
    import os as _os
    if not pdf_path or not _os.path.exists(pdf_path):
        return {"success": False, "error": f"PDFが見つかりません: {pdf_path}"}
    if not replacements:
        return {"success": False, "error": "replacements が空です（find/replace のペアが必要）"}

    def _work():
        import fitz
        doc = fitz.open(pdf_path)
        font = fitz.Font("cjk")  # 日本語対応・環境非依存
        for rep in replacements:
            rep["_hits"] = 0
        for pno in range(len(doc)):
            page = doc[pno]
            spans = [sp for blk in page.get_text("dict")["blocks"]
                     for ln in blk.get("lines", []) for sp in ln.get("spans", [])]
            edits = []
            for rep in replacements:
                find = rep.get("find") or ""
                replace = rep.get("replace") or ""
                tgt = rep.get("page")
                if not find:
                    continue
                if tgt and int(tgt) != (pno + 1):
                    continue
                for rect in page.search_for(find):
                    size, color, baseline = 11.0, (0, 0, 0), rect.y1
                    for sp in spans:
                        if fitz.Rect(sp["bbox"]).intersects(rect):
                            size = sp.get("size", 11.0)
                            ci = sp.get("color", 0)
                            color = ((ci >> 16 & 255) / 255, (ci >> 8 & 255) / 255, (ci & 255) / 255)
                            baseline = sp.get("origin", (rect.x0, rect.y1))[1]
                            break
                    page.add_redact_annot(rect, fill=(1, 1, 1))
                    edits.append((rect.x0, baseline, replace, size, color))
                    rep["_hits"] += 1
            if edits:
                page.apply_redactions()
                for x, y, text, size, color in edits:
                    if text:
                        tw = fitz.TextWriter(page.rect)
                        tw.append((x, y), text, font=font, fontsize=size)
                        tw.write_text(page, color=color)
        out_dir = _os.environ.get("WORKSPACE_DIR", "/tmp/slack-agent-workspace")
        _os.makedirs(out_dir, exist_ok=True)
        name = output_name or "edited.pdf"
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        out_path = _os.path.join(out_dir, name)
        doc.save(out_path, garbage=4, deflate=True)
        doc.close()
        report = [{"find": r.get("find"), "replace": r.get("replace"), "hits": r.get("_hits", 0)} for r in replacements]
        return out_path, report

    try:
        out_path, report = await asyncio.to_thread(_work)
    except Exception as e:
        logger.error(f"edit_pdf error: {e}", exc_info=True)
        return {"success": False, "error": f"PDF編集エラー: {str(e)[:300]}"}

    not_found = [r["find"] for r in report if r["hits"] == 0]
    applied = sum(r["hits"] for r in report)
    if applied == 0:
        return {
            "success": False,
            "output_path": out_path,
            "report": report,
            "not_found": not_found,
            "error": (
                f"置換対象が1件も見つかりませんでした（find が原文と一致していない可能性）。"
                f"見つからなかった検索語: {not_found}。PDF本文の実際の文字列を確認して find を修正してください。"
            ),
        }
    return {
        "success": True,
        "output_path": out_path,
        "output_file": out_path,
        "applied_count": applied,
        "report": report,
        "not_found": not_found,
        "message": (
            f"PDFを編集しました（{applied}箇所置換）。編集後PDF: {out_path}"
            + (f" ／ ⚠️見つからなかった検索語: {not_found}（原文と完全一致していない可能性、要確認）" if not_found else "")
        ),
    }


def _get_knowledge_store():
    """KnowledgeStoreのシングルトン取得"""
    if not hasattr(_get_knowledge_store, "_instance"):
        from utils.knowledge_store import KnowledgeStore
        # memory_data/knowledge/ に保存
        base_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "memory_data", "knowledge")
        _get_knowledge_store._instance = KnowledgeStore(base_dir=base_dir)
    return _get_knowledge_store._instance


def _dispatch_knowledge_save(args: dict) -> dict:
    """save_knowledge MCPツールのディスパッチ"""
    ks = _get_knowledge_store()
    knowledge_type = args.get("type", "learning")
    content = args.get("content", "")
    source = args.get("source", "")

    if not content:
        return {"error": "content は必須です"}

    success = False
    if knowledge_type == "learning":
        category = args.get("category", "general")
        success = ks.add_learning(category, content, source)
    elif knowledge_type == "team":
        member_name = args.get("member_name", "")
        success = ks.add_team_info(content, member_name)
    elif knowledge_type == "decision":
        project = args.get("project", "")
        success = ks.add_decision(content, project)
    elif knowledge_type == "project":
        project = args.get("project", "")
        if not project:
            return {"error": "type=project の場合、project パラメータは必須です"}
        success = ks.add_project_knowledge(project, content)
    elif knowledge_type == "skill":
        topic = args.get("topic", "")
        if not topic:
            return {"error": "type=skill の場合、topic パラメータは必須です"}
        success = ks.add_skill_knowledge(topic, content)
    else:
        return {"error": f"不明な type: {knowledge_type}"}

    if success:
        # インデックスを再生成
        try:
            ks.generate_index()
        except Exception as e:
            logger.debug(f"Index generation failed: {e}")
        return {"status": "saved", "type": knowledge_type, "content_preview": content[:100]}
    else:
        return {"status": "skipped", "reason": "重複 or 空コンテンツ"}


def _dispatch_knowledge_search(args: dict) -> dict:
    """search_knowledge MCPツールのディスパッチ"""
    ks = _get_knowledge_store()
    query = args.get("query", "")
    max_results = args.get("max_results", 5)

    if not query:
        return {"error": "query は必須です"}

    results = ks.search(query, max_results=max_results)
    if not results:
        return {"results": [], "message": f"'{query}' に一致するナレッジは見つかりませんでした"}

    return {
        "results": results,
        "count": len(results),
        "query": query,
    }

# ── ツール実行 ──

_TRANSIENT_ERROR_PATTERNS = (
    "rate limit", "ratelimit", "429", "timeout", "timed out",
    "connection reset", "connection aborted", "temporarily unavailable",
    "503", "502", "504", "econnreset",
)


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """一過性エラー（レート制限・タイムアウト等）は1回だけ再試行し、失敗は構造化して返す"""
    last_err: Exception | None = None
    for attempt in (1, 2):
        try:
            result = await _dispatch(name, arguments)
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
        except Exception as e:
            last_err = e
            if attempt == 1 and _is_transient_error(str(e)):
                import asyncio as _asyncio
                await _asyncio.sleep(1.5)
                continue
            logger.error(f"Tool {name} error: {e}", exc_info=True)
            return [TextContent(type="text", text=json.dumps({
                "error": str(e)[:500], "why_failed": type(e).__name__,
                "retryable": _is_transient_error(str(e)),
                "next_action": "retry_later" if _is_transient_error(str(e)) else "fix_arguments_or_inform_user",
                "tool": name}, ensure_ascii=False))]
    return [TextContent(type="text", text=json.dumps({"error": str(last_err)}, ensure_ascii=False))]


async def _dispatch(name: str, args: dict) -> dict:
    # ── メモ ──
    if name == "save_note":
        return _save_note(args["text"], args.get("user", ""), args.get("category", "general"))
    if name == "list_notes":
        return _list_notes(args.get("category", ""))
    if name == "delete_note":
        return _delete_note(args["note_id"])
    if name == "search_notes":
        return _search_notes(args["query"])
    # ── リマインダー ──
    if name == "set_reminder":
        return _set_reminder(args)
    if name == "list_reminders":
        return _list_reminders(args.get("user", ""))
    if name == "cancel_reminder":
        return _cancel_reminder(args["reminder_id"])
    # ── プロジェクト ──
    if name == "manage_project":
        return _manage_project(args)
    # ── Slack ──
    if name == "send_dm":
        return await _send_dm(args["user_id"], args["message"])
    if name == "fetch_slack_message":
        return await _fetch_slack_message(args["url"])
    # ── スキル ──
    if name == "list_skills":
        return _list_skills_impl()
    if name == "use_skill":
        return _use_skill_impl(args["name"])
    if name == "add_skill":
        return _add_skill_impl(
            args["name"], args["content"], overwrite=args.get("overwrite", False),
            user_id=args.get("user_id", ""), channel_id=args.get("channel_id", ""),
            thread_ts=args.get("thread_ts", ""), confirm_action=args.get("confirm_action", ""))
    if name == "update_skill":
        return _update_skill_impl(
            args["name"], args["content"], user_id=args.get("user_id", ""),
            channel_id=args.get("channel_id", ""), thread_ts=args.get("thread_ts", ""))
    if name == "delete_skill":
        return _delete_skill_impl(args["name"], user_id=args.get("user_id", ""))
    # ── ナレッジ ──
    if name == "save_knowledge":
        return _dispatch_knowledge_save(args)
    if name == "search_knowledge":
        return _dispatch_knowledge_search(args)
    # ── PDF編集（中身書き換え・レイアウト維持） ──
    if name == "edit_pdf":
        return await _edit_pdf(pdf_path=args.get("pdf_path", ""), replacements=args.get("replacements", []),
                               output_name=args.get("output_name", ""))
    return {"error": f"未知のツール: {name}"}



async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
