"""土台のテスト（ネットワーク・Slack・Claude には出ない）。 .venv/bin/python -m pytest -q tests"""
import asyncio
import importlib
import json
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DUMMY_ENV = {
    "SLACK_BOT_TOKEN": "xo" + "xb-dummy-for-test",  # テスト用のダミー値（本物と同じ形の文字列を置かない）
    "SLACK_APP_TOKEN": "xa" + "pp-dummy-for-test",
    "AGENT_ID": "test-agent",
    "AGENT_NAME": "TestAgent",
}


@pytest.fixture
def env(monkeypatch, tmp_path):
    for k, v in DUMMY_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.delenv("AGENT_OWNER_SLACK_ID", raising=False)
    monkeypatch.delenv("ADMIN_USER_IDS", raising=False)
    # .env が置かれていても読まないように（load_dotenv(override=True) 対策）
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    import config.settings as cs
    monkeypatch.setattr(cs, "load_dotenv", lambda *a, **k: False)
    return tmp_path


# ── 1. すべて import できる ──
@pytest.mark.parametrize("mod", [
    "config.settings", "config.models", "utils.claude_engine", "utils.claude_pty_engine", "utils.memory",
    "utils.skills", "utils.active_runs", "utils.session_store", "utils.output_filter", "utils.log_redact",
    "tools.executor", "tools.reminder_tools", "tools.slack_tools", "tools.thread_tools", "tools.text_tools",
    "handlers.message_handler",
])
def test_import(mod):
    importlib.import_module(mod)


# ── 2. 設定の既定値（同じ機械の他のエージェントと取り違えない識別子・サブスク枠のエンジン） ──
def test_settings_defaults(env):
    from config.settings import Settings
    s = Settings.from_env()
    assert s.agent_id == "test-agent" and s.agent_name == "TestAgent"
    assert s.ai_engine_type == "claude_pty"
    assert s.workspace_dir.endswith("/ws")
    assert s.admin_user_ids == [] and s.proactive_channels == []


# ── 3. アプリが組み立てられる（Slack には接続しない。業務別の機能が混ざっていない） ──
def test_create_app(env):
    import app as app_mod

    async def go():
        app, settings, handler, memory = app_mod.create_app()
        assert handler.reminder_manager is not None
        assert settings.agent_name == "TestAgent"
        # 登録された Slack イベント・ボタンに、外したはずの業務機能が無い
        names = " ".join(str(getattr(l, "matchers", "")) + repr(l) for l in app._async_listeners)
        for gone in ("x_post", "gmail_reply", "dev_push", "kaizen", "gsheet"):
            assert gone not in names, gone

    asyncio.run(go())


# ── 4. 実行前フック ──
def _guard():
    spec = importlib.util.spec_from_file_location("guard_tools", ROOT / "scripts/hooks/guard_tools.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.mark.parametrize("tool,inp", [
    ("Bash", {"command": "cat .env"}),
    ("Bash", {"command": "printenv"}),
    ("Bash", {"command": "echo $SLACK_BOT_TOKEN"}),
    ("Read", {"file_path": "/x/project/.env"}),
    ("Bash", {"command": "security find-generic-password -s claude"}),
    ("Edit", {"file_path": "/x/project/scripts/hooks/guard_tools.py", "new_string": "pass"}),
    ("Write", {"file_path": "/x/project/config/claude_guard_settings.json", "content": "{}"}),
])
def test_guard_blocks_for_everyone(tool, inp):
    g = _guard()
    g.ADMINS = {"UADMIN"}
    assert g.check(tool, inp, "/tmp", "UADMIN") is not None
    assert g.check(tool, inp, "/tmp", "UOTHER") is not None


def test_guard_destructive_admin_only():
    g = _guard()
    g.ADMINS = {"UADMIN"}
    inp = {"command": "git push --force origin main"}
    assert g.check("Bash", inp, "/tmp", "UOTHER") is not None
    assert g.check("Bash", inp, "/tmp", "UADMIN") is None


@pytest.mark.parametrize("tool,inp", [
    ("Bash", {"command": "ls -la && python3 tools/x.py"}),
    ("Read", {"file_path": "/x/project/README.md"}),
    ("Read", {"file_path": "/x/project/.env.example"}),
    ("Write", {"file_path": "/tmp/report.csv", "content": "a,b\n1,2"}),
])
def test_guard_allows_normal(tool, inp):
    assert _guard().check(tool, inp, "/tmp", "UOTHER") is None


# ── 5. 書き込み系ツールは確認なしでは実行しない ──
def test_executor_requires_confirmation():
    from tools.executor import ToolExecutor
    ex = ToolExecutor(workspace="/tmp", slack_token="x")
    r = asyncio.run(ex.execute("send_dm", {"user_id": "U1", "message": "hi"}, {"user": "U2"}))
    assert r.get("requires_confirmation") is True
    assert "error" in asyncio.run(ex.execute("no_such_tool", {}))


def test_pending_prompt_requester_check():
    from tools.executor import ToolExecutor
    ex = ToolExecutor(workspace="/tmp", slack_token="x")
    pid = ex.store_pending_prompt({"a": 1}, requester="U1")
    assert ex.get_pending_prompt(pid, requester="U2") is None
    assert ex.get_pending_prompt(pid, requester="U1")["a"] == 1


# ── 6. ログの伏せ字 ──
def test_log_redact():
    from utils.log_redact import redact
    fake = "xo" + "xb-" + "1" * 12 + "-" + "abcdefghijklmnop"  # 偽トークンは実行時に組み立てる
    t = redact(f"token={fake} end")
    assert "abcdefghijklmnop" not in t and "REDACTED" in t


# ── 7. MCP サーバー: ツールがそろっていて、メモが使える ──
def test_mcp_server_tools(env):
    os.environ["MEMORY_DIR"] = str(env / "memory")
    if "mcp_agent_tools.server" in sys.modules:
        del sys.modules["mcp_agent_tools.server"]
    srv = importlib.import_module("mcp_agent_tools.server")
    tools = asyncio.run(srv.list_tools())
    names = {t.name for t in tools}
    assert len(names) == 18 and {"save_note", "set_reminder", "list_skills", "search_knowledge"} <= names
    asyncio.run(srv._dispatch("save_note", {"text": "テストのメモ", "user": "U1"}))
    found = asyncio.run(srv._dispatch("search_notes", {"query": "テスト"}))
    assert json.dumps(found, ensure_ascii=False).count("テストのメモ") >= 1
    assert "error" in asyncio.run(srv._dispatch("no_such_tool", {}))


# ── 8. 秘密情報の検査パターン ──
def test_secret_patterns():
    spec = importlib.util.spec_from_file_location("check_secrets", ROOT / "scripts/check_secrets.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    # 偽の値は実行時に組み立てる（本物と同じ形の文字列をリポジトリに置かない）
    samples = ["xo" + "xb-" + "1" * 10 + "-abcdefghijkl", "sk-" + "ant-oat01-" + "a" * 30, "gh" + "p_" + "a" * 36,
               "-----BEGIN " + "PRIVATE KEY-----"]
    for s in samples:
        assert any(rx.search(s) for _, rx in m.PATTERNS), s
    assert not any(rx.search("SLACK_BOT_TOKEN=") for _, rx in m.PATTERNS)


# ── 9. 混ぜてはいけない語（社名・人名・社内ID・端末名など）が残っていない ──
# 語の一覧はリポジトリに入れない（入れると一覧そのものが漏れる）。
# 手元は tests/forbidden_words.local.txt（gitignore 済み・1行1語）、CI は環境変数 FORBIDDEN_WORDS（カンマ区切り）で渡す
def _forbidden_words() -> list[str]:
    words = [w for w in os.getenv("FORBIDDEN_WORDS", "").split(",") if w.strip()]
    local = ROOT / "tests" / "forbidden_words.local.txt"
    if local.exists():
        for line in local.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#"):
                words.append(line)
    return [w.lower() for w in words]


def test_no_forbidden_words():
    words = _forbidden_words()
    if not words:
        pytest.skip("禁止語の一覧が無い（tests/forbidden_words.local.txt か FORBIDDEN_WORDS）")
    hits = []
    for f in ROOT.rglob("*"):
        if not f.is_file() or any(p in f.parts for p in (".venv", ".git", "__pycache__", ".pytest_cache")):
            continue
        if f.suffix in (".png", ".jpg", ".pyc") or f.name.endswith(".local.txt") or f.name == ".env":
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            low = line.lower()
            if any(w in low for w in words):
                hits.append(f"{f.relative_to(ROOT)}:{i}")  # 語そのものはログに出さない
    assert not hits, "\n".join(hits[:20])
