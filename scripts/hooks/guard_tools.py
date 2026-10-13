#!/usr/bin/env python3
"""実行前フック（Claude CLI の PreToolUse）: 危険なツール呼び出しを、実行の直前で止める。

エージェントが起動する claude CLI にだけ --settings config/claude_guard_settings.json で渡す
（機械全体の ~/.claude/settings.json は触らない）。ツール側の制限だけだと、Claude が自分で書いた
スクリプトで抜けられるので、コマンド・書き込む中身・実行するスクリプトの中身まで見て止める。

ルール:
1. 誰の依頼でも止める
   - 秘密情報の読み出し（.env・認証情報・Claude の設定・キーチェーン・環境変数の一覧）
     … ページやメッセージに仕込まれた指示に乗せられて、トークンを Slack に貼る事故を防ぐ
   - この安全装置自身（フック・フック設定）の書き換え
2. 管理者（ADMIN_USER_IDS）以外の依頼では止める
   - 取り消せない危険な操作（広範囲の削除・git の強制 push・履歴の書き換え）
3. 依頼者が分からない（定時ジョブ等）時は 2 は止めずに記録だけ残す（1 は常に止める）

依頼者は本体が CLI に渡す環境変数 AGENT_REQUEST_USER（Slack のイベントから決めた ID）。
止めた時は exit 2 と理由を返す（Claude はその理由を読んで、依頼者に説明する）。
業務のルールを足す時は RULES に (名前, 正規表現, 管理者なら許すか, 理由) を足す。
"""
import datetime
import json
import os
import re
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOG = os.path.join(PROJECT_ROOT, "logs", "guard.log")
ADMINS = {u.strip() for u in os.environ.get("ADMIN_USER_IDS", "").split(",") if u.strip()}

# (名前, 正規表現, 管理者なら許すか, 依頼者向けの理由)
RULES = [
    ("read_secrets",
     re.compile(r"(^|[\s/'\"=])\.env(?!\.example)(\b|$)|credentials/|\.claude/(\.credentials|settings)|"
                r"\bsecurity\s+(find|dump)-|\bprintenv\b|(^|[;&|]\s*)env\s*($|[;&|>])|keychain|"
                r"\b(SLACK_BOT_TOKEN|SLACK_APP_TOKEN|CLAUDE_CODE_OAUTH_TOKEN|ANTHROPIC_API_KEY)\b", re.I),
     False, "秘密情報（トークン・認証情報・設定）の読み出しは、誰の依頼でもできないルールです"),
    ("self_modify",
     re.compile(r"scripts/hooks/|claude_guard_settings\.json"),
     False, "安全装置（実行前フック）の書き換えはできないルールです"),
    ("destructive",
     re.compile(r"\brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+(/|~|\$HOME|\*|\.\.?/?\s*$)|\bgit\s+push\b[^\n]*(--force|-f\b)|"
                r"\bgit\s+(reset\s+--hard|filter-branch|filter-repo)|\bmkfs\b|\bdd\s+if=", re.I),
     True, "取り消せない危険な操作は、管理者の依頼の時だけ実行できるルールです"),
]
_SCRIPT = re.compile(r"[\w./~-]+\.(?:py|sh|mjs|js)\b")


def _log(msg: str) -> None:
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (datetime.datetime.now().isoformat(timespec="seconds"), msg))
    except Exception:
        pass


def _texts(tool: str, inp: dict, cwd: str):
    """判定に使う文字列（コマンド・書き込む先と中身・実行するスクリプトの中身・読むファイル）"""
    out = []
    if tool == "Bash":
        cmd = inp.get("command") or ""
        out.append(cmd)
        for p in _SCRIPT.findall(cmd):
            path = os.path.expanduser(p)
            if not os.path.isabs(path):
                path = os.path.join(cwd, path)
            try:
                if os.path.isfile(path) and os.path.getsize(path) < 500_000:
                    out.append(open(path, encoding="utf-8", errors="ignore").read())
            except Exception:
                pass
    elif tool in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit"):
        out.append(inp.get("file_path") or inp.get("notebook_path") or "")
        if tool == "Write":
            out.append(inp.get("content") or "")
        elif tool == "Edit":
            out.append(inp.get("new_string") or "")
        elif tool == "MultiEdit":
            out.extend(e.get("new_string") or "" for e in inp.get("edits") or [])
    elif tool in ("Grep", "Glob"):
        out.append(inp.get("path") or "")
        out.append(inp.get("pattern") or "")
    return out


def check(tool: str, inp: dict, cwd: str, requester: str):
    """止めるなら (rule名, 理由) を返す。通すなら None"""
    texts = _texts(tool, inp, cwd)
    for name, rx, admin_ok, reason in RULES:
        if not any(rx.search(t) for t in texts if t):
            continue
        if admin_ok and requester in ADMINS:
            _log("allow admin=%s rule=%s tool=%s" % (requester, name, tool))
            continue
        if admin_ok and not requester:
            _log("allow-no-requester rule=%s tool=%s（定時ジョブ等。要確認）" % (name, tool))
            continue
        return name, reason
    return None


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    tool = data.get("tool_name") or ""
    inp = data.get("tool_input") or {}
    cwd = data.get("cwd") or os.getcwd()
    req = os.environ.get("AGENT_REQUEST_USER", "").strip()
    hit = check(tool, inp, cwd, req)
    if not hit:
        return 0
    name, reason = hit
    _log("BLOCK requester=%s rule=%s tool=%s" % (req or "-", name, tool))
    sys.stderr.write(
        "このツール呼び出しは止めました: " + reason + "。"
        "依頼者にはそのように伝え、この操作をせずにできる範囲で答えてください。"
        "別の書き方で同じ操作をしようとしないでください。\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
