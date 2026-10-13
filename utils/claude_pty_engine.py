"""Claude PTY Engine — Claude Code の対話画面（TUI）を裏で操作し、サブスクの枠で動かすエンジン。

OpenRyoko の手法（PTY + interactive TUI + positional prompt + transcript JSONL 読み）
を Python で実装。claude_agent_sdk を使わず、claude CLI を pexpect で直接起動する。

ClaudeEngine の process_message 互換。MCP/Skill/Tool 等の高度な機能は段階的に戻す。

参考: https://github.com/rsensui2/OpenRyoko packages/jimmy/src/engines/claude-interactive.ts
"""
import asyncio
import glob
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

import pexpect

from config import models
from utils.claude_engine import ClaudeEngine
from utils.output_filter import show_reasoning, strip_leading_english_meta, strip_meta_lines

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _bundled_claude_bin() -> str:
    """claude-agent-sdk に同梱の claude CLI（CLAUDE_BIN が無い時の既定）"""
    try:
        import claude_agent_sdk
        return os.path.join(os.path.dirname(claude_agent_sdk.__file__), "_bundled", "claude")
    except Exception:
        return "claude"


_BUNDLED_CLAUDE_BIN = _bundled_claude_bin()


# 実行前フック（危険なツール呼び出しを止める）の設定。機械全体の ~/.claude/settings.json は触らず、
# このエージェントが起動する CLI にだけ --settings で渡す
_GUARD_SETTINGS = os.path.join(PROJECT_ROOT, "config", "claude_guard_settings.json")


def claude_config_dir() -> str:
    """Claude Code の設定フォルダ。CLAUDE_CONFIG_DIR で分けると、同じ機械でもアカウントを分けられる"""
    return os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "").strip() or "~/.claude")


def _resolve_claude_bin() -> str:
    """使用する claude CLI を実行時に決める。

    load_dotenv() は Settings.from_env() の中で走るので、import 時に
    os.environ を読むと .env の CLAUDE_BIN を取りこぼす。呼び出し時に読むこと。
    指定パスが存在しなければ bundled にフォールバックする（無言で落とさない）。
    """
    p = os.environ.get("CLAUDE_BIN", "").strip()
    if p and os.path.exists(p):
        return p
    if p:
        logger.warning(
            "[PTY] CLAUDE_BIN が存在しないので bundled にフォールバック: " + p
        )
    return _BUNDLED_CLAUDE_BIN


# 後方互換（import 時点の値。実処理では _resolve_claude_bin() を使う）
DEFAULT_CLAUDE_BIN = os.environ.get("CLAUDE_BIN", _BUNDLED_CLAUDE_BIN)


def _project_dir_for(cwd: str) -> str:
    """Claude Code の transcript JSONL 保存先を cwd から導出する。
    例: /Users/me/agent → <CLAUDE_CONFIG_DIR>/projects/-Users-me-agent
    """
    # 先頭スラッシュも含めて - に変換
    encoded = cwd.replace("/", "-")
    return os.path.join(claude_config_dir(), "projects", encoded)


_SAFE_ATTACH_ROOTS: Optional[List[str]] = None


def _safe_attach_roots() -> List[str]:
    """成果物として添付してよいファイルの所在ルート（realpath 正規化済み）。"""
    global _SAFE_ATTACH_ROOTS
    if _SAFE_ATTACH_ROOTS is None:
        roots = [
            PROJECT_ROOT,
            "/tmp",
            "/private/tmp",
            "/var/folders",
            "/private/var/folders",
        ]
        ws = os.environ.get("WORKSPACE_DIR")
        if ws:
            roots.append(ws)
        normalized = []
        for r in roots:
            try:
                normalized.append(os.path.realpath(r))
            except Exception:
                pass
        _SAFE_ATTACH_ROOTS = normalized
    return _SAFE_ATTACH_ROOTS


def _is_safe_attach_path(fp: str) -> bool:
    """fp が成果物置き場（プロジェクト・ワークスペース・tmp）配下かどうか。
    最終応答で言及されたファイルを mtime 無視で添付する際の安全弁。
    任意のシステムファイルを名前だけで添付してしまうのを防ぐ。"""
    try:
        rp = os.path.realpath(fp)
    except Exception:
        return False
    for root in _safe_attach_roots():
        if rp == root or rp.startswith(root + os.sep):
            return True
    return False


def _extract_generated_files(jsonl_path: str, final_text: Optional[str] = None) -> List[str]:
    """このセッションで実際に生成されたファイルのみ抽出。
    過去のファイル（jsonl で言及されただけのもの）は除外する。
    フィルタ:
      1. Write tool の file_path に出現したもの (最優先・mtime 無視)
      2. 最終応答テキストで エージェント が明示提示したファイル
         (成果物置き場配下かつ実在なら mtime 無視で添付。
          「1時間前に作った資料を後から添付して」も確実に上がるようにする保証)
      3. それ以外で jsonl 開始時刻以降に mtime があるファイル
    """
    import re as _re
    try:
        try:
            from utils.output_filter import is_attachment_allowed
        except Exception:
            is_attachment_allowed = lambda x: True
        # session 開始時刻 = jsonl 作成時刻
        try:
            session_start = os.path.getctime(jsonl_path)
        except Exception:
            try:
                session_start = os.path.getmtime(jsonl_path)
            except Exception:
                session_start = 0
        text = open(jsonl_path).read()
    except Exception:
        return []

    PATTERN = _re.compile(r"(/[\w\-_/.()&]+\.(pptx|pdf|xlsx|docx|png|jpg|jpeg|mp3|mp4|csv|html))", _re.IGNORECASE)
    # まず Write tool の file_path を最優先で集める
    write_paths = set()
    try:
        for line in open(jsonl_path):
            try:
                msg = json.loads(line.strip())
            except Exception:
                continue
            if msg.get("type") != "assistant":
                continue
            for c in msg.get("message", {}).get("content", []):
                if c.get("type") != "tool_use":
                    continue
                inp = c.get("input", {})
                if isinstance(inp, dict):
                    fp = inp.get("file_path") or inp.get("output_path") or inp.get("output")
                    if fp and isinstance(fp, str) and "/" in fp:
                        for m in PATTERN.finditer(fp):
                            write_paths.add(m.group(1))
    except Exception:
        pass

    files = []
    seen = set()
    # Write tool 由来のファイル (最優先、mtime チェックなし)
    for fp in write_paths:
        if fp in seen:
            continue
        seen.add(fp)
        try:
            if os.path.exists(fp) and os.path.isfile(fp) and is_attachment_allowed(fp):
                files.append(fp)
        except Exception:
            pass
    # 最終応答テキストで エージェント が明示提示したファイル (成果物置き場配下なら mtime 無視)
    # 「添付します」と言いながら本体が付かない / 過去に作った資料を後から添付できない、を根治する
    if final_text:
        for m in PATTERN.finditer(final_text):
            fp = m.group(1)
            if fp in seen:
                continue
            seen.add(fp)
            try:
                if not (os.path.exists(fp) and os.path.isfile(fp)):
                    continue
                if not is_attachment_allowed(fp):
                    continue
                if not _is_safe_attach_path(fp):
                    continue
                files.append(fp)
            except Exception:
                pass
    # regex 拾い分は session_start 以降の mtime のみ
    for m in PATTERN.finditer(text):
        fp = m.group(1)
        if fp in seen:
            continue
        seen.add(fp)
        try:
            if not (os.path.exists(fp) and os.path.isfile(fp)):
                continue
            if not is_attachment_allowed(fp):
                continue
            mt = os.path.getmtime(fp)
            # session_start - 60秒 以降に更新されたもののみ
            if mt < session_start - 60:
                continue
            files.append(fp)
        except Exception:
            pass
    return files


# 応答が「途中の実況で終わっている＝続きが必要」かを判定する（2026-07-17, 07-22 堅牢化）
# 語尾の固定リストは取りこぼす（「読み込みます」等）ため、
# 「実況・作業中マーカー」＋「行動宣言の語尾（〜ます/する）」の組合せで判定する。
_ACTION_VERB_TAIL = re.compile(r"(ます|する|します|しますね)[。\.、,！!…ねよ)）\s]*$")
_NARRATION_MARKER = re.compile(
    # 2026-07-22: 並行セッションのホットパッチ語彙も合流（イタチごっこ回避）
    r"(確認|読み込|読みます|ロード|実行|探し|取得|アップロード|保存|生成|"
    r"スクリプト|ツール|まず|次に|続いて|これから|並行|順番に|いったん|一旦|優先|"
    r"仕組み|把握|特定|調べ|見てみ|見て|見ます|洗い出|抽出|着手|"
    r"チェック|作成|取り込|開い|開きます|進めます|まとめ|整理|分析|当たり|確かめ)"
)
# 純粋なメタノイズ（回答として表に出てはいけない独白）
_META_NOISE = re.compile(
    r"^(no response requested|let me\b|i'?ll\b|i will\b|the user\b|okay,? let)",
    re.IGNORECASE,
)
_DELIVERABLE_HINT = re.compile(r"https?://|```|\bfile://|/[\w./-]+\.(pptx|pdf|xlsx|docx|csv|png|jpg|html)\b")
# 2026-08-20: サブエージェント/バッチ処理の「完了待ち」で終わっている＝続きが必要。
# 「〜済みです。」等の完成した文で終わっても、待ち宣言があればターンを終えず結果を拾いに行く。
# （例:「調査エージェント4体の完了待ちです」で止まってしまう）
# ※「お待たせしました」(=完了時の枕)は 待たせ で除外される（待ち/待って とは別）。
_AWAITING_MARKER = re.compile(
    r"(完了待ち|完了を待|結果.{0,4}待ち|結果.{0,4}待っ|終わるのを待|返ってくるのを待|"
    r"待機(中|です)|待ちです|待っています|待っている(状態|ところ)|"
    r"(エージェント|サブエージェント|並列|バッチ|調査|生成|処理).{0,15}"
    r"(完了待ち|待ちです|待っています|待機|走らせて(います|おり)|実行中|処理中))"
)


def _proc_tree_cputime(pid: int):
    """pid とその子孫の累積CPU時間(秒)を返す。psutil無し。失敗時 None。
    番人が『長時間かかっているが実際に仕事している(CPU消費)』タスクを止めないための活動シグナル。"""
    try:
        import subprocess
        pids = [pid]
        seen = set()
        i = 0
        while i < len(pids) and len(seen) < 200:
            cur = pids[i]; i += 1
            if cur in seen:
                continue
            seen.add(cur)
            try:
                out = subprocess.run(["pgrep", "-P", str(cur)], capture_output=True,
                                     text=True, timeout=3).stdout
                pids.extend(int(x) for x in out.split())
            except Exception:
                pass
        total = 0.0
        for pp in seen:
            try:
                out = subprocess.run(["ps", "-o", "time=", "-p", str(pp)], capture_output=True,
                                     text=True, timeout=3).stdout.strip()
                if not out:
                    continue
                sec = 0.0
                for x in out.replace("-", ":").split(":"):
                    sec = sec * 60 + float(x)
                total += sec
            except Exception:
                pass
        return total
    except Exception:
        return None


def _looks_non_terminal(text: str, files: list) -> bool:
    """成果物が出ておらず、実況の途中で終わっている応答か？（自動継続の判定）

    保守的に: URL・ファイル・コードブロック等の成果物があれば継続しない。
    - 空応答
    - 英語メタ独白で始まる（No response requested 等）
    - 短め(<400字) かつ 実況マーカーを含み 行動宣言の語尾で終わる
    のいずれかで True（＝続きが必要）。
    """
    t = (text or "").strip()
    if not t:
        return True  # 空応答（tool実行だけで回答無し）も継続対象
    if files:
        return False
    # サブエージェント/バッチの「完了待ち」で終わっている＝結果を拾って続ける必要あり。
    # 成果物URL(進捗シート等)が本文にあっても、待ち宣言が優先で継続対象にする。
    if _AWAITING_MARKER.search(t):
        return True
    if _DELIVERABLE_HINT.search(t):
        return False
    if _META_NOISE.match(t):
        return True
    # 実況マーカーを含み、かつ「行動宣言の語尾」or「文末記号なしで途中切れ」なら継続対象
    if len(t) < 400 and _NARRATION_MARKER.search(t):
        ends_abruptly = not re.search(r"[。．！!？?…」)）]$", t)
        if _ACTION_VERB_TAIL.search(t) or ends_abruptly:
            return True
    return False


def _count_jsonl_lines(path: str) -> int:
    try:
        with open(path) as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def _extract_assistant_text(jsonl_path: str, since_line: int = 0) -> Dict[str, Any]:
    """transcript JSONL から assistant 応答テキスト、usage、生成ファイルを抽出。

    since_line: この行番号より後の行だけを対象にする（resume継続時、前回までの
    ナレーションを混ぜず、継続ターンの出力だけを取り出すため）。
    """
    text_parts: List[str] = []
    usage: Dict[str, Any] = {}
    last_result: Dict[str, Any] = {}
    try:
        with open(jsonl_path) as f:
            for _lineno, line in enumerate(f):
                if _lineno < since_line:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                t = msg.get("type")
                if t == "assistant":
                    content = msg.get("message", {}).get("content", [])
                    u = msg.get("message", {}).get("usage")
                    if u:
                        usage = u
                    # ツール呼び出しを含む assistant ターンの text は
                    # 「では検索します」「Let me check…」等の独白なので最終応答に含めない。
                    # tool_use を含まない（= 純粋な回答ターン）の text のみ採用する。
                    has_tool_use = any(c.get("type") == "tool_use" for c in content)
                    if has_tool_use:
                        continue
                    msg_text = "".join(
                        c.get("text", "") for c in content if c.get("type") == "text"
                    )
                    if msg_text.strip():
                        text_parts.append(msg_text)
                elif t == "result":
                    last_result = {k: msg.get(k) for k in ("subtype", "duration_ms", "is_error", "num_turns", "result")}
    except Exception as e:
        logger.warning(f"_extract_assistant_text failed: {e}")
    # メタ独白行（No response requested. 等）を除去してから返す（2026-07-22）
    raw_text = "\n".join(text_parts)
    final_text = strip_meta_lines(raw_text)
    # 全部メタだった＝モデルが「返す必要なし」と判断しただけで、中身が無い状態
    meta_only = bool(raw_text.strip()) and not final_text.strip()
    files = _extract_generated_files(jsonl_path, final_text=final_text)
    return {
        "text": final_text,
        "usage": usage,
        "result": last_result,
        "files": files,
        "meta_only": meta_only,
    }



_BG_LAUNCH_RE = re.compile(
    r"(?:running in background with ID|moved to the background \(ID|Monitor started \(task|agentId)[:\s]+([A-Za-z0-9_-]+)"
)
_BG_NOTIFY_RE = re.compile(r"<task-id>([^<]+)</task-id>")


def _scan_turn_state(jsonl_path: str, since_line: int = 0):
    """jsonl の since_line 以降を見て (assistant有無, 最後のassistantのstop_reason, 未完了の裏ジョブID集合) を返す。

    2026-10-09: 裏に回したBash/Monitor/Agentの完了待ちで end_turn した瞬間に kill すると
    裏ジョブごと死に、「〜の完了待ち」で止まる（安田不動産のメール検索で3回連続発生）。
    task-notification が届くまでは完了扱いにしない。resume 時は前回までの end_turn を無視する。
    """
    has_assistant = False
    last_sr = None
    launched, notified = set(), set()
    with open(jsonl_path) as f:
        for i, line in enumerate(f):
            if i < since_line:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            t = msg.get("type")
            m = msg.get("message") or {}
            c = m.get("content")
            if t == "assistant":
                has_assistant = True
                last_sr = m.get("stop_reason")
            elif t == "user":
                # ツール結果や裏ジョブの通知が来た＝ターンは続いている
                last_sr = None
                blobs = []
                if isinstance(c, str):
                    blobs.append(c)
                else:
                    for b in c or []:
                        if b.get("type") == "text":
                            blobs.append(b.get("text") or "")
                        elif b.get("type") == "tool_result":
                            cc = b.get("content")
                            if isinstance(cc, str):
                                blobs.append(cc)
                            else:
                                for x in cc or []:
                                    if isinstance(x, dict):
                                        blobs.append(x.get("text") or "")
                for txt in blobs:
                    if "<task-notification>" in txt:
                        notified.update(x.strip() for x in _BG_NOTIFY_RE.findall(txt))
                    else:
                        launched.update(_BG_LAUNCH_RE.findall(txt))
            elif t == "queue-operation":
                txt = msg.get("content") or ""
                if "<task-notification>" in txt:
                    notified.update(x.strip() for x in _BG_NOTIFY_RE.findall(txt))
    return has_assistant, last_sr, launched - notified


def _run_pty_sync(
    bin_path: str,
    args: List[str],
    cwd: str,
    env: Dict[str, str],
    timeout: int,
    project_dir: str,
    before_files: set,
    expected_jsonl: str = None,
) -> Dict[str, Any]:
    """PTY 実行 + transcript JSONL 監視。result 行検知で自前 kill。
    Returns: {elapsed: float, jsonl_path: str or None, killed_via: str}
    """
    t0 = time.time()
    child = pexpect.spawn(
        bin_path, args=args,
        encoding="utf-8", timeout=timeout, cwd=cwd, env=env,
        dimensions=(40, 200),
    )
    jsonl_path = None
    killed_via = "unknown"
    try:
        # Bypass Permissions 確認ダイアログ突破 (初回のみ)
        try:
            idx = child.expect([r"accept", pexpect.EOF, pexpect.TIMEOUT], timeout=15)
            if idx == 0:
                time.sleep(1.5)
                child.send("\x1b[B")
                time.sleep(0.3)
                child.send("\r")
                time.sleep(1)
        except Exception as e:
            logger.debug(f"Bypass dialog: {e}")

        # transcript JSONL polling: stop_reason 付き assistant 検知で完了
        # 段階: (1) jsonl 出現 (2) assistant 行出現 (3) stop_reason 付き = streaming 完了
        poll_interval = 0.3
        grace_after_stop = 0.5    # stop_reason 検知後の追加待機（usage 書き込み完了まで）
        grace_after_assistant_only = 5.0  # stop_reason 出なかった場合の fallback
        assistant_seen_at = None
        stall_kill_after = float(os.environ.get("AGENT_PTY_STALL_KILL_SEC", "1200"))
        last_jsonl_sig = None
        last_activity_at = t0
        last_cpu = None
        last_stall_check = t0
        deadline = t0 + timeout
        while time.time() < deadline:
            if not child.isalive():
                killed_via = "natural_exit"
                break
            # 無反応ハング番人（バグ①: stop_reason=tool_useで永久固着しsemaphoreを握り続ける事象を塞ぐ）
            # 誤kill防止: jsonl進捗 or プロセスツリーCPU活動のどちらか有れば『実行中』→ killしない。
            # 長時間かかる正当タスク(長Bash/deep research/レンダ等でCPU消費)は絶対に止めない。2026-09-03
            _now_wd = time.time()
            if _now_wd - last_stall_check >= 20.0:
                last_stall_check = _now_wd
                _active = False
                if jsonl_path and os.path.exists(jsonl_path):
                    try:
                        _st = os.stat(jsonl_path)
                        _sig = (_st.st_mtime, _st.st_size)
                    except OSError:
                        _sig = None
                    if _sig is not None and _sig != last_jsonl_sig:
                        last_jsonl_sig = _sig
                        _active = True
                _cpu = _proc_tree_cputime(child.pid)
                if _cpu is not None and (last_cpu is None or _cpu > last_cpu + 0.5):
                    last_cpu = _cpu
                    _active = True
                if _active:
                    last_activity_at = _now_wd
                elif _now_wd - last_activity_at > stall_kill_after:
                    killed_via = "stall_no_activity"
                    logger.warning("PTY stall: no jsonl/CPU activity for " + str(int(stall_kill_after)) + "s, killing hung child")
                    try:
                        child.kill(15)
                        time.sleep(0.3)
                        if child.isalive():
                            child.kill(9)
                    except Exception:
                        pass
                    break
            # expected_jsonl があればそれを優先（並列衝突回避）
            if expected_jsonl and os.path.exists(expected_jsonl):
                jsonl_path = expected_jsonl
            else:
                after = set(glob.glob(project_dir + "/*.jsonl"))
                new_files = after - before_files
                if new_files:
                    jsonl_path = sorted(new_files, key=os.path.getmtime, reverse=True)[0]
            if jsonl_path:
                try:
                    has_assistant = False
                    has_stop_reason = False
                    with open(jsonl_path) as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                msg = json.loads(line)
                            except Exception:
                                continue
                            if msg.get("type") == "assistant":
                                has_assistant = True
                                m = msg.get("message", {})
                                sr = m.get("stop_reason")
                                if sr == "end_turn":
                                    has_stop_reason = True
                                elif sr == "tool_use":
                                    assistant_seen_at = None
                    if has_stop_reason:
                        # stop_reason ある = 完全な response 出揃った
                        time.sleep(grace_after_stop)
                        killed_via = "stop_reason_detected"
                        try:
                            child.kill(15)
                            time.sleep(0.3)
                            if child.isalive():
                                child.kill(9)
                        except Exception:
                            pass
                        break
                    elif has_assistant:
                        if assistant_seen_at is None:
                            assistant_seen_at = time.time()
                            logger.debug("[PTY] assistant text appeared, waiting for stop_reason")
                        elif time.time() - assistant_seen_at >= grace_after_assistant_only:
                            # fallback: stop_reason 来ないが assistant 出てから 5s 経過
                            killed_via = "assistant_grace_fallback"
                            try:
                                child.kill(15)
                                time.sleep(0.3)
                                if child.isalive():
                                    child.kill(9)
                            except Exception:
                                pass
                            break
                except Exception as e:
                    logger.debug("poll read err: " + str(e))
            try:
                child.read_nonblocking(size=4096, timeout=0.05)
            except Exception:
                pass
            time.sleep(poll_interval)
        else:
            killed_via = "timeout"
            logger.warning("PTY timeout after " + str(timeout) + "s")
        # kill 後の最終 jsonl flush 待ち
        time.sleep(0.3)
    finally:
        try:
            child.close(force=True)
        except Exception:
            pass
    return {"elapsed": time.time() - t0, "jsonl_path": jsonl_path, "killed_via": killed_via}


def _run_pty_sync_with_tracking(
    bin_path: str,
    args,
    cwd: str,
    env,
    timeout: int,
    project_dir: str,
    before_files,
    active_children,
    session_key: str,
    expected_jsonl: str = None,
    since_line: int = 0,
):
    """_run_pty_sync のラッパ: active_children に child を登録/削除する版。
    ESC / stop 機能のために spawn 直後に child を辞書に入れる。
    """
    t0 = time.time()
    child = pexpect.spawn(
        bin_path, args=args,
        encoding="utf-8", timeout=timeout, cwd=cwd, env=env,
        dimensions=(40, 200),
    )
    active_children[session_key] = child
    jsonl_path = None
    killed_via = "unknown"
    try:
        # Bypass Permissions ダイアログ突破
        try:
            idx = child.expect([r"accept", pexpect.EOF, pexpect.TIMEOUT], timeout=15)
            if idx == 0:
                time.sleep(1.5)
                child.send("\x1b[B")
                time.sleep(0.3)
                child.send("\r")
                time.sleep(1)
        except Exception as e:
            logger.debug("Bypass dialog: " + str(e))

        # poll loop (同 _run_pty_sync)
        poll_interval = 0.3
        grace_after_stop = 0.5
        grace_after_assistant_only = 5.0
        assistant_seen_at = None
        stall_kill_after = float(os.environ.get("AGENT_PTY_STALL_KILL_SEC", "1200"))
        bg_wait_max = float(os.environ.get("AGENT_PTY_BG_WAIT_SEC", "2400"))
        bg_wait_since = None
        last_jsonl_sig = None
        last_activity_at = t0
        last_cpu = None
        last_stall_check = t0
        deadline = t0 + timeout
        while time.time() < deadline:
            if not child.isalive():
                killed_via = "natural_exit_or_stopped"
                break
            # 無反応ハング番人（バグ①: stop_reason=tool_useで永久固着しsemaphoreを握り続ける事象を塞ぐ）
            # 誤kill防止: jsonl進捗 or プロセスツリーCPU活動のどちらか有れば『実行中』→ killしない。
            # 長時間かかる正当タスク(長Bash/deep research/レンダ等でCPU消費)は絶対に止めない。2026-09-03
            _now_wd = time.time()
            if _now_wd - last_stall_check >= 20.0:
                last_stall_check = _now_wd
                _active = False
                if jsonl_path and os.path.exists(jsonl_path):
                    try:
                        _st = os.stat(jsonl_path)
                        _sig = (_st.st_mtime, _st.st_size)
                    except OSError:
                        _sig = None
                    if _sig is not None and _sig != last_jsonl_sig:
                        last_jsonl_sig = _sig
                        _active = True
                _cpu = _proc_tree_cputime(child.pid)
                if _cpu is not None and (last_cpu is None or _cpu > last_cpu + 0.5):
                    last_cpu = _cpu
                    _active = True
                if _active:
                    last_activity_at = _now_wd
                elif _now_wd - last_activity_at > stall_kill_after:
                    killed_via = "stall_no_activity"
                    logger.warning("PTY stall: no jsonl/CPU activity for " + str(int(stall_kill_after)) + "s, killing hung child")
                    try:
                        child.kill(15)
                        time.sleep(0.3)
                        if child.isalive():
                            child.kill(9)
                    except Exception:
                        pass
                    break
            # expected_jsonl があればそれを優先（並列衝突回避）
            if expected_jsonl and os.path.exists(expected_jsonl):
                jsonl_path = expected_jsonl
            else:
                after = set(glob.glob(project_dir + "/*.jsonl"))
                new_files = after - before_files
                if new_files:
                    jsonl_path = sorted(new_files, key=os.path.getmtime, reverse=True)[0]
            if jsonl_path:
                try:
                    has_assistant, _last_sr, _pending_bg = _scan_turn_state(
                        jsonl_path, since_line if jsonl_path == expected_jsonl else 0
                    )
                    has_stop_reason = _last_sr == "end_turn"
                    if _last_sr == "tool_use":
                        assistant_seen_at = None
                    if has_stop_reason and _pending_bg:
                        # 裏ジョブの完了通知が来れば CLI が次のターンを自動で始めるので、それまで待つ
                        if bg_wait_since is None:
                            bg_wait_since = time.time()
                            logger.info("[PTY] end_turn with pending background tasks " + str(sorted(_pending_bg)) + " — waiting")
                        if time.time() - bg_wait_since < bg_wait_max:
                            has_stop_reason = False
                            has_assistant = False
                            assistant_seen_at = None
                        else:
                            logger.warning("[PTY] background tasks still pending after " + str(int(bg_wait_max)) + "s — finishing")
                    elif not _pending_bg:
                        bg_wait_since = None
                    if has_stop_reason:
                        time.sleep(grace_after_stop)
                        killed_via = "end_turn_detected"
                        try:
                            child.kill(15)
                            time.sleep(0.3)
                            if child.isalive():
                                child.kill(9)
                        except Exception:
                            pass
                        break
                    elif has_assistant:
                        if assistant_seen_at is None:
                            assistant_seen_at = time.time()
                        elif time.time() - assistant_seen_at >= grace_after_assistant_only:
                            killed_via = "assistant_grace_fallback"
                            try:
                                child.kill(15)
                                time.sleep(0.3)
                                if child.isalive():
                                    child.kill(9)
                            except Exception:
                                pass
                            break
                except Exception as e:
                    logger.debug("poll read err: " + str(e))
            try:
                child.read_nonblocking(size=4096, timeout=0.05)
            except Exception:
                pass
            time.sleep(poll_interval)
        else:
            killed_via = "timeout"
            logger.warning("PTY timeout after " + str(timeout) + "s")
        time.sleep(0.3)
    finally:
        try:
            child.close(force=True)
        except Exception:
            pass
        active_children.pop(session_key, None)
    return {"elapsed": time.time() - t0, "jsonl_path": jsonl_path, "killed_via": killed_via}


async def _progress_loop(callback, t_start, project_dir, before_files, skill_names=None, expected_jsonl=None):
    """プログレスバー + リーゾニング tail。
    - ETA をスキルごとに推定（ETA_BY_SKILL。業務のスキルを足したら秒数も足す）
    - 進捗は 99% まで。100% は完了時のみ (cancellation 前に最終 update が必要)
    - thinking と tool_use を Slack edit で可視化
    """
    import asyncio, time, glob, json, os
    # ETA テーブル（秒）
    ETA_BY_SKILL = {
        "research_workflow": 240,
    }
    DEFAULT_ETA = 90
    eta = DEFAULT_ETA
    if skill_names:
        for sn in skill_names:
            if sn in ETA_BY_SKILL:
                eta = max(eta, ETA_BY_SKILL[sn])
    # tool ベースの ETA 動的調整候補
    HEAVY_TOOLS: set = set()  # 時間のかかるツール名（業務のツールを足したら追加）

    last_text = None
    update_interval = 5.0
    try:
        while True:
            await asyncio.sleep(update_interval)
            elapsed = int(time.time() - t_start)
            # tool 使用状況から ETA 補正
            reasoning_summary = ""
            tool_in_use = ""
            tool_count = 0
            heavy_tool_seen = False
            jsonl_path = None
            if expected_jsonl and os.path.exists(expected_jsonl):
                jsonl_path = expected_jsonl
            else:
                after = set(glob.glob(project_dir + "/*.jsonl"))
                new_files = after - before_files
                if new_files:
                    jsonl_path = sorted(new_files, key=os.path.getmtime, reverse=True)[0]
            if jsonl_path:
                try:
                    last_thinking = None
                    last_tool = None
                    for line in open(jsonl_path):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            msg = json.loads(line)
                        except Exception:
                            continue
                        if msg.get("type") == "assistant":
                            for c in msg.get("message", {}).get("content", []):
                                if c.get("type") == "thinking":
                                    th = c.get("thinking", "")
                                    if th:
                                        last_thinking = th
                                elif c.get("type") == "tool_use":
                                    nm = c.get("name", "")
                                    last_tool = nm
                                    tool_count += 1
                                    if any(h in str(nm) + str(c.get("input", "")) for h in HEAVY_TOOLS):
                                        heavy_tool_seen = True
                    if last_thinking:
                        reasoning_summary = last_thinking[-280:].strip()
                    if last_tool:
                        tool_in_use = last_tool
                except Exception:
                    pass
            # heavy tool 検出時は ETA を 600s 以上に
            effective_eta = max(eta, 600) if heavy_tool_seen else eta
            # tool 件数で動的延長 (大量 tool 実行は時間かかる)
            if tool_count > 20:
                effective_eta = max(effective_eta, 900)
            elif tool_count > 10:
                effective_eta = max(effective_eta, 480)
            elif tool_count > 5:
                effective_eta = max(effective_eta, 240)
            # ETA 接近時の自動延長 (95% 超えたら 1.5倍に延ばす)
            if elapsed > effective_eta * 0.95:
                effective_eta = max(effective_eta, int(elapsed * 1.5))

            # パーセント計算（99% cap）
            if elapsed < effective_eta * 0.9:
                pct = int(elapsed / effective_eta * 90)
            elif elapsed < effective_eta:
                pct = 90 + int((elapsed - effective_eta * 0.9) / (effective_eta * 0.1) * 9)
            else:
                pct = 99
            pct = max(0, min(99, pct))
            n_filled = pct // 10
            bar = "▰" * n_filled + "▱" * (10 - n_filled)

            parts = ["⏳ 処理中... [" + bar + "] " + str(pct) + "%  (" + str(elapsed) + "秒経過 / 想定 " + str(effective_eta) + "秒)"]
            if tool_count:
                parts.append(" / 🔧 " + str(tool_count) + "件実行")
            if tool_in_use:
                parts.append("\n直近: " + tool_in_use)
            # 生の thinking/独白は本番では Slack に出さない (AGENT_SHOW_REASONING=true の時のみ)
            if reasoning_summary and show_reasoning():
                parts.append("\n\n💭 考え中:\n\u0060\u0060\u0060\n" + reasoning_summary + "\n\u0060\u0060\u0060")
            text = "".join(parts)
            if text != last_text:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(text)
                    else:
                        callback(text)
                    last_text = text
                except Exception:
                    pass
            if elapsed > 60:
                update_interval = 10.0
            if elapsed > 300:
                update_interval = 20.0
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


class ClaudePtyEngine(ClaudeEngine):
    """ClaudeEngine の subclass。process_message を PTY 経由実装に置換する。

    継承する機能:
      - _get_system_prompt, _build_user_prompt, _fetch_thread_messages
      - _route_task, _select_skills_llm
      - SessionStore, semaphore, MCP config path 等
    オーバーライド:
      - process_message — 内部の claude_agent_sdk 呼び出しを PTY 版に置換
    """

    def __init__(
        self,
        *args,
        claude_bin: Optional[str] = None,
        cwd: Optional[str] = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.claude_bin = claude_bin or _resolve_claude_bin()
        self.cwd = cwd or PROJECT_ROOT
        # ESC / stop 用: 稼働中の PTY child プロセス管理
        # key: thread_key (channel:thread_ts), value: pexpect.spawn
        self._active_pty_children = {}
        # 停止ボタンで kill された thread_key を記録 (retry での再実行を防ぐ)
        self._cancelled_keys = set()
        # モニタリング用カウンタ (運用統計)
        self._pty_stats = {
            "total_invocations": 0,
            "successes": 0,
            "errors": 0,
            "total_elapsed_s": 0.0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_cache_read_tokens": 0,
        }
        logger.info(
            f"ClaudePtyEngine initialized "
            f"(bin={self.claude_bin}, cwd={self.cwd}, model_heavy={self.model_heavy})"
        )

    def get_pty_stats(self):
        """運用モニタリング用統計取得 (Slack /stats 等から呼ぶ想定)"""
        n = self._pty_stats["total_invocations"]
        avg_elapsed = (self._pty_stats["total_elapsed_s"] / n) if n > 0 else 0.0
        return {**self._pty_stats, "avg_elapsed_s": round(avg_elapsed, 1)}

    def stop_pty_session(self, session_key: str = "default") -> bool:
        """指定 session の PTY child を kill する (ESC / stop 機能)。
        Returns: True if killed, False if not found.
        """
        child = self._active_pty_children.get(session_key)
        if not child:
            logger.info("[PTY] stop_pty_session: no active child for key=" + str(session_key))
            return False
        try:
            child.kill(15)
            logger.warning("[PTY] stop_pty_session: SIGTERM sent to key=" + str(session_key))
            return True
        except Exception as e:
            logger.warning("[PTY] stop_pty_session error: " + str(e))
            return False

    def stop_all_pty_sessions(self) -> int:
        """全 PTY child を kill。Returns: killed count"""
        killed = 0
        for key, child in list(self._active_pty_children.items()):
            try:
                child.kill(15)
                killed += 1
            except Exception:
                pass
        logger.warning("[PTY] stop_all_pty_sessions: killed " + str(killed))
        return killed

    async def cancel_thread(self, thread_key: str) -> bool:
        """ESC / 停止ボタンハンドラから呼ばれる。
        ClaudeEngine.cancel_thread を override し、PTY child を kill する。
        既存 ESC handler は thread_key ("channel:thread_ts") で呼ぶ。
        """
        # retry ループでの再実行を防ぐため、キャンセル済みとして記録
        self._cancelled_keys.add(thread_key)
        # 親 (ClaudeEngine) の cancel も試す（_active_sdk_clients 経路）
        parent_killed = False
        try:
            parent_killed = await super().cancel_thread(thread_key)
        except Exception as e:
            logger.debug("[PTY] super().cancel_thread err: " + str(e))
        # PTY child を kill
        pty_killed = self.stop_pty_session(thread_key)
        if parent_killed or pty_killed:
            logger.info("[PTY] cancel_thread succeeded: thread_key=" + thread_key + " parent=" + str(parent_killed) + " pty=" + str(pty_killed))
            return True
        return False

    async def _invoke_via_pty(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 180,
        progress_callback=None,
        session_key: Optional[str] = None,
        skill_names: Optional[List[str]] = None,
        resume_session_id: Optional[str] = None,
        requester_id: str = "", source_channel: str = "", source_thread: str = "",
    ) -> Dict[str, Any]:
        """PTY 経由で claude TUI を 1-shot 起動 → 応答取得。

        resume_session_id 指定時: 新規セッションでなく既存セッションを --resume で継続
        （成果物が出るまでの自動継続に使う。前回までの行はsince_lineで除外して抽出）。

        Returns: {"text": str, "usage": dict, "result": dict, "elapsed": float, "session_id": str}
        """
        cwd = self.cwd
        project_dir = _project_dir_for(cwd)
        before_files = set(glob.glob(project_dir + "/*.jsonl")) if os.path.isdir(project_dir) else set()

        import uuid as _uuid
        if resume_session_id:
            # 既存セッションの継続（同じ jsonl に追記される）
            session_uuid = resume_session_id
            expected_jsonl = os.path.join(project_dir, session_uuid + ".jsonl")
            since_line = _count_jsonl_lines(expected_jsonl)
            args: List[str] = [
                prompt,
                "--resume", session_uuid,
                "--dangerously-skip-permissions",
                "--disallowedTools", "AskUserQuestion", "ExitPlanMode",
            ]
        else:
            # session_id 固定: 並列実行時の jsonl 衝突を避ける
            session_uuid = str(_uuid.uuid4())
            expected_jsonl = os.path.join(project_dir, session_uuid + ".jsonl")
            since_line = 0
            args = [
                prompt,  # positional — MUST come first
                "--session-id", session_uuid,
                "--dangerously-skip-permissions",
                "--disallowedTools", "AskUserQuestion", "ExitPlanMode",
            ]
        if model:
            args += ["--model", model]
        if system_prompt:
            args += ["--append-system-prompt", system_prompt]
        if self.mcp_config_path:
            args += ["--mcp-config", self.mcp_config_path]
        if os.path.exists(_GUARD_SETTINGS):
            # 実行前フック（scripts/hooks/guard_tools.py）で危険なツール呼び出しを止める
            args += ["--settings", _GUARD_SETTINGS]

        env = dict(os.environ)
        # 依頼者は Slack のイベントから決めて環境変数で渡す（ツールの引数では受け取らない＝なりすまし防止）
        env.update({"AGENT_REQUEST_USER": requester_id, "AGENT_REQUEST_CHANNEL": source_channel,
                    "AGENT_REQUEST_THREAD": source_thread, "AGENT_REQUIRE_CONTEXT": "1"})
        env["TERM"] = "xterm-256color"

        model_disp = model or "default"
        logger.info(f"[PTY] spawning claude (model={model_disp}, prompt_len={len(prompt)})")
        # session_key で active_children に登録できる形にする
        # 引数で渡された値を優先 (並列実行時のレース回避)。後方互換で属性にフォールバック。
        if session_key is None:
            session_key = getattr(self, "_current_session_key", "default")
        if skill_names is None:
            skill_names = getattr(self, "_current_skill_names", None)
        loop = asyncio.get_event_loop()
        # progress task (プログレスバー + リーゾニング tail)
        import time as _t
        t_start = _t.time()
        progress_task = None
        if progress_callback:
            progress_task = asyncio.create_task(
                _progress_loop(progress_callback, t_start, project_dir, before_files, skill_names=skill_names, expected_jsonl=expected_jsonl)
            )
        try:
            run_result = await loop.run_in_executor(
                None, _run_pty_sync_with_tracking, self.claude_bin, args, cwd, env, timeout, project_dir, before_files, self._active_pty_children, session_key, expected_jsonl, since_line,
            )
        finally:
            if progress_task:
                progress_task.cancel()
                try:
                    await progress_task
                except Exception:
                    pass
        elapsed = run_result["elapsed"]
        # session_id 固定方式: expected_jsonl を優先、なければ run_result の fallback
        jsonl_path = expected_jsonl if os.path.exists(expected_jsonl) else run_result.get("jsonl_path")
        logger.info("[PTY] elapsed=" + str(round(elapsed, 1)) + "s killed_via=" + str(run_result.get("killed_via")) + " jsonl=" + str(jsonl_path))

        if not jsonl_path:
            # fallback: 最新 mtime
            existing = sorted(glob.glob(project_dir + "/*.jsonl"), key=os.path.getmtime, reverse=True)
            if existing and existing[0] not in before_files:
                jsonl_path = existing[0]
            else:
                return {"text": "", "usage": {}, "result": {}, "elapsed": elapsed,
                        "killed_via": run_result.get("killed_via"), "session_id": session_uuid}

        # resume時は expected_jsonl（同一ファイル）を確実に対象にする（新規mtime fallbackだと誤検出）
        if resume_session_id and os.path.exists(expected_jsonl):
            jsonl_path = expected_jsonl
        extracted = _extract_assistant_text(
            jsonl_path, since_line=(since_line if jsonl_path == expected_jsonl else 0)
        )
        # usage 空のとき1回リトライ (jsonl flush タイミング対策)
        if not extracted.get("usage"):
            import time as _t
            _t.sleep(1.0)
            extracted = _extract_assistant_text(jsonl_path)
            if extracted.get("usage"):
                logger.debug("[PTY] usage recovered on retry")
        extracted["elapsed"] = elapsed
        extracted["transcript"] = jsonl_path
        # 2026-07-17: kill理由を上位へ伝搬（timeout時の失敗ナラティブに使う）
        extracted["killed_via"] = run_result.get("killed_via")
        extracted["session_id"] = session_uuid
        return extracted

    async def _pty_query(
        self,
        prompt: str,
        system_prompt=None,
        model=None,
        timeout: int = 90,
    ) -> str:
        """軽量 LLM 呼び出し (sdk_query 互換)。スキル選定などサブタスクで使用。"""
        result = await self._invoke_via_pty(
            prompt=prompt,
            system_prompt=system_prompt,
            model=model or self.model_light,
            timeout=timeout,
        )
        return result.get("text", "")

    async def _select_skills_llm(self, text: str):
        """スキル選定 (PTY 版)"""
        skill_manager = getattr(self, "skill_manager", None)
        if not skill_manager:
            return []
        skills = skill_manager.list_skills()
        if not skills:
            return []
        catalog_lines = []
        for sk in skills:
            name = sk.get("name", "")
            desc = sk.get("description", "")
            triggers = ", ".join(sk.get("triggers", []))
            caps = ", ".join(sk.get("capabilities", []))
            line = "- " + name + ": " + desc
            if triggers:
                line += " (triggers: " + triggers + ")"
            if caps:
                line += " [できること: " + caps + "]"
            catalog_lines.append(line)
        catalog = "\n".join(catalog_lines)
        prompt = (
            "以下はAIアシスタントが持つスキル（専門知識・手順書）の一覧です。\n\n"
            + catalog
            + "\n\nユーザーのメッセージ:\n「" + text[:500] + "」\n\n"
            + "このメッセージに対応するために必要なスキルを選んでください。\n\n"
            + "## 優先順位ルール\n"
            + "- 依頼の主題に直接合うスキルを選ぶ。名前が似ているだけのスキルは選ばない\n"
            + "- 業務固有の優先順位があれば、ここに追記する\n\n"
            + "## 出力ルール\n"
            + "- 関連するスキルがあれば、そのname値をJSON配列で返す（例: [\"research_workflow\"]）\n"
            + "- 該当スキルがなければ空配列 [] を返す\n"
            + "- 最大3つまで\n"
            + "- JSON配列のみ出力し、説明は不要"
        )
        try:
            response_text = await self._pty_query(
                prompt=prompt,
                model=models.PREFLIGHT,  # スキル選定はSonnet系固定（Opus不要・Haiku化はしない方針）
                system_prompt="あなたはスキル選定の専門家です。JSON配列のみ返してください。",
                timeout=120,
            )
            import re as _re
            m = _re.search(r"\[.*?\]", response_text, _re.DOTALL)
            valid = []
            if m:
                try:
                    selected = json.loads(m.group(0))
                    if isinstance(selected, list):
                        valid = [x for x in selected if isinstance(x, str) and skill_manager.get_skill(x)]
                except Exception:
                    pass
            # キーワード fallback (LLM が拾えない時の保険)
            if not valid:
                text_lower = text.lower()
                fb_skills = []
                # （キーワード → スキル名）。業務のスキルを足したら、ここにも足す
                for kws, skill in (
                    (("調べて", "リサーチ", "調査", "競合", "最新"), "research_workflow"),
                    (("ファイル", "csv", "pdf", "まとめて"), "output_format_guide"),
                ):
                    if any(k in text_lower for k in kws) and skill_manager.get_skill(skill) and skill not in fb_skills:
                        fb_skills.append(skill)
                if fb_skills:
                    logger.info("[PTY_SkillLLM] LLM空返却→keyword fallback: " + str(fb_skills))
                    return fb_skills
            if valid:
                logger.info("[PTY_SkillLLM] Selected: " + str(valid))
                return valid
            logger.info("[PTY_SkillLLM] No skills selected (LLM and keyword both empty)")
            return []
        except Exception as e:
            logger.warning("[PTY_SkillLLM] selection failed: " + str(e))
            return []

    async def process_message(
        self,
        text: str,
        channel: str,
        thread_ts: Optional[str] = None,
        files: Optional[List[dict]] = None,
        tool_executor=None,
        context: Optional[dict] = None,
        memory_context: Optional[str] = None,
        progress_callback=None,
    ) -> dict:
        """ClaudeEngine.process_message と互換シグネチャ。
        内部 LLM 呼び出しを PTY 版に置換した最小実装。

        段階的に戻す機能（フェーズ2以降）:
          - skill selection / injection
          - thread context fetch
          - tool_use / hooks
          - streaming progress_callback
        """
        # スキル選定 (PTY 版)
        skill_contents = []
        try:
            selected_skill_names = await self._select_skills_llm(text or "")
            sm = getattr(self, "skill_manager", None)
            if sm:
                # always_inject: true のスキルは選定結果に関わらず必ずマージ（料金事故ガード等）
                if hasattr(sm, "get_always_inject_names"):
                    for nm in sm.get_always_inject_names():
                        if nm not in selected_skill_names:
                            selected_skill_names.append(nm)
                for sname in selected_skill_names:
                    content = sm.get_skill_content(sname)
                    if content:
                        skill_contents.append(content)
                if selected_skill_names:
                    logger.info("[PTY_Skills] Injecting " + str(len(skill_contents)) + " skill(s): " + str(selected_skill_names))
            # progress_loop が ETA 計算に使うので保存
            self._current_skill_names = list(selected_skill_names)
        except Exception as e:
            logger.warning("[PTY_Skills] selection error: " + str(e))

        # システムプロンプト構築 (skill content 込み)
        try:
            system_prompt = self._get_system_prompt(
                memory_context,
                skill_contents=skill_contents or None,
                channel_id=channel,
                thread_ts=thread_ts,
            )
        except Exception as e:
            logger.warning("[PTY] _get_system_prompt failed: " + str(e))
            system_prompt = None

        # スレッドコンテキスト
        thread_context = ""
        if thread_ts and context and context.get("slack_client"):
            try:
                thread_context = await self._fetch_thread_messages(
                    context["slack_client"], channel, thread_ts
                )
            except Exception as e:
                logger.warning(f"[PTY] _fetch_thread_messages failed: {e}")

        # ユーザープロンプト構築
        try:
            user_prompt = self._build_user_prompt(text, files, context, thread_context=thread_context)
        except Exception as e:
            logger.warning(f"[PTY] _build_user_prompt failed: {e}")
            user_prompt = text

        # PTY 起動 (semaphore で同時実行制御 — エージェント の並列メンション競合防止)
        import time as _t_module
        # ESC / stop 用に session_key 保持。ClaudeEngine の thread_key と同形式。
        # ClaudeEngine の慣例: f"{channel}:{thread_ts or 'default'}"
        thread_key = str(channel) + ":" + str(thread_ts or "default")
        self._current_session_key = thread_key
        # 新規リクエスト開始時に前回のキャンセル状態をクリア
        self._cancelled_keys.discard(thread_key)
        self._pty_stats["total_invocations"] += 1
        # 走行中ラン台帳に登録（再起動で殺された場合、起動時sweepが自動再開する）
        try:
            from utils.active_runs import register_run
            _run_user = (context or {}).get("user") if context else None
            register_run(thread_key, str(channel), thread_ts, user=_run_user, text=text)
        except Exception:
            logger.debug("[ActiveRuns] register failed", exc_info=True)
        result = None
        response_text = ""
        usage = {}
        # PTY 起動失敗 / 空応答に対する 1 回までの retry
        # 旧 SDK の自動リトライ相当を最小限カバーする目的
        last_invoke_error: Optional[Exception] = None
        # 別タスク実行中(semaphore=1)で待たされる時は「今別の作業中なので待ってて」を先に伝える
        # （オーナー指示 2026-09-03: 重いタスクで依頼が止まりそうな時、待機を案内する）
        if self._sdk_semaphore.locked() and progress_callback:
            try:
                from utils.active_runs import _load as _load_runs
                _ahead = max(0, len(_load_runs()) - 1)
            except Exception:
                _ahead = 0
            _busy_msg = (
                "ごめんなさい、今ちょうど別の作業で手が塞がっていて…！ "
                + (f"今、他に{_ahead}件の作業を処理中です。それが終わり次第すぐ取りかかるので、"
                   if _ahead >= 1 else "順番に対応するので、")
                + "このまま少々お待ちくださいね🙇"
            )
            try:
                await progress_callback(_busy_msg)
            except Exception:
                pass
        try:
            async with self._sdk_semaphore:
                self._active_sdk_user = channel
                logger.info("[PTY] acquired semaphore for channel=" + str(channel))
                for attempt in range(2):
                    try:
                        result = await self._invoke_via_pty(
                            prompt=user_prompt,
                            system_prompt=system_prompt,
                            model=self.model_heavy,
                            # 2026-07-17: 900秒固定が長時間タスクを無言で切っていた
                            # （長いデータ移行が timeout after 900s で切れた）
                            # → env可変・既定45分。timeout時は下でナラティブを必ず付ける
                            timeout=int(os.getenv("AGENT_PTY_TIMEOUT", "2700")),
                            progress_callback=progress_callback if attempt == 0 else None,
                            session_key=thread_key,
                            requester_id=(context or {}).get("user", ""), source_channel=channel, source_thread=thread_ts or "",
                            skill_names=getattr(self, "_current_skill_names", None),
                        )
                        text_part = (result.get("text") if result else "") or ""
                        if text_part.strip():
                            if attempt > 0:
                                logger.info("[PTY] retry succeeded on attempt=" + str(attempt + 1))
                            break
                        # 停止ボタンで kill された場合は再実行せず中断扱いにする
                        if thread_key in self._cancelled_keys:
                            logger.info("[PTY] empty response after cancel — not retrying")
                            break
                        # 空応答 = 何かしらの転送失敗 / kill タイミング不良。1 回だけ retry
                        logger.warning("[PTY] empty response on attempt=" + str(attempt + 1) + ", retrying")
                    except asyncio.CancelledError:
                        raise
                    except Exception as inner_e:
                        last_invoke_error = inner_e
                        logger.warning("[PTY] invoke failed on attempt=" + str(attempt + 1) + ": " + str(inner_e))
                        if attempt == 1:
                            raise

                # ── 自動継続: 成果物が出ず実況の途中で終わっていたら、同じセッションを
                #    resume して「最終成果物を出すまで」エージェント自身が続ける（2026-07-17）。
                #    Slackは1メンション=1ターンで再発火しないため、相手の再依頼なしに
                #    止まる事象を構造的に塞ぐ。停止ボタン・上限で必ず止まる。
                # 2026-09-24: これまでで最も実質的な本文を保持しておく。
                # 継続で実況だけの短い応答に上書きされ、先に出ていたまともな回答が
                # 捨てられる事故があった（221字の回答が32字に置き換わった）。
                _best_text = ((result or {}).get("text") or "").strip()
                _meta_retry = 0
                if os.getenv("AGENT_AUTO_CONTINUE", "1") == "1" and result:
                    _max_cont = int(os.getenv("AGENT_AUTO_CONTINUE_MAX", "5"))
                    _cont = 0
                    while (
                        _cont < _max_cont
                        and thread_key not in self._cancelled_keys
                        and result.get("session_id")
                        and result.get("killed_via") not in ("timeout", "stall_no_activity")
                        and _looks_non_terminal(result.get("text", ""), result.get("files"))
                    ):
                        _cont += 1
                        logger.info(
                            f"[PTY] auto-continue {_cont}/{_max_cont} "
                            f"(non-terminal response, session={result.get('session_id')[:8]})"
                        )
                        if progress_callback:
                            try:
                                await progress_callback("もう少しで仕上がります…最後まで進めています")
                            except Exception:
                                pass
                        # 作業の途中で止まったのか、そもそも「返す必要なし」と判断して
                        # 黙っただけなのかで、続きの促し方を変える（後者に作業催促は無意味）
                        if result.get("meta_only") and not result.get("files"):
                            _cont_prompt = (
                                "直前の相手の発言に対して、エージェントとして自然な返事を1〜2文で返してください。"
                                "作業や調査が不要な会話なら、相槌・感想・一言のコメントで構いません。"
                                "英語の独白やメタコメント（No response requested 等）は絶対に出力しないこと。"
                            )
                        else:
                            _cont_prompt = (
                                "ここまでの作業を最後までやり切ってください。"
                                "サブエージェントやバッチ処理の完了を待っている状態で止まっていた場合は、"
                                "その結果を必ず取り込み（まだなら完了を待って回収し）、次のバッチも含めて残りを進めてください。"
                                "途中経過の実況（「〜します」「〜待ちです」等）で終わらせず、完成した成果物"
                                "（発行したURL・ファイルパス・最終回答）だけを返すこと。"
                                "もう作業が完了しているなら、その成果物をこのまま提示してください。"
                                "まだ途中なら、残りを全部実行してから成果物を出してください。"
                            )
                        try:
                            cont_result = await self._invoke_via_pty(
                                prompt=_cont_prompt,
                                system_prompt=None,
                                model=self.model_heavy,
                                timeout=int(os.getenv("AGENT_PTY_TIMEOUT", "2700")),
                                progress_callback=progress_callback,
                                session_key=thread_key,
                                requester_id=(context or {}).get("user", ""), source_channel=channel, source_thread=thread_ts or "",
                                resume_session_id=result.get("session_id"),
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as _cont_e:
                            logger.warning(f"[PTY] auto-continue failed: {_cont_e}")
                            break
                        # 継続の成果物で置き換え（実況だけの旧textは捨てる）
                        _new_text = (cont_result.get("text") or "").strip()
                        _new_files = cont_result.get("files") or []
                        if _new_text and len(_new_text) > len(_best_text):
                            _best_text = _new_text
                        if _new_text or _new_files:
                            cont_result.setdefault("session_id", result.get("session_id"))
                            # ファイルは累積（前段で生成済みのものも保持）
                            merged_files = list(dict.fromkeys((result.get("files") or []) + _new_files))
                            cont_result["files"] = merged_files
                            result = cont_result
                        elif cont_result.get("meta_only") and _meta_retry < 2:
                            # 「No response requested.」等のメタ独白だけが返った状態。
                            # 以前はここで break して "続けて" 案内に落ちていたが、
                            # それは「作業が終わった」のではなく「返す必要なしと誤判断した」だけ。
                            # 会話として返すよう促して、上限内でもう一度粘る（2026-09-24）。
                            _meta_retry += 1
                            logger.info(
                                f"[PTY] continuation was meta-only — retrying as conversation "
                                f"({_meta_retry}/2)"
                            )
                            result = dict(result)
                            result["meta_only"] = True
                            result["killed_via"] = cont_result.get("killed_via")
                        else:
                            # 継続しても本当に空 → これ以上粘らない
                            break

            response_text = result.get("text", "") if result else ""
            # 実況だけの短い応答で終わった場合、途中で出ていたより実質的な本文を復元する
            if _best_text and len(_best_text) > len((response_text or "").strip()) \
                    and _looks_non_terminal(response_text, (result or {}).get("files")):
                logger.info(
                    "[PTY] restoring the most substantive earlier text "
                    f"({len((response_text or '').strip())} -> {len(_best_text)} chars)"
                )
                response_text = _best_text
            usage = result.get("usage", {}) if result else {}
            # 統計記録
            if result:
                self._pty_stats["total_elapsed_s"] += result.get("elapsed", 0)
            if response_text:
                self._pty_stats["successes"] += 1
            else:
                self._pty_stats["errors"] += 1
            if usage:
                try:
                    self._pty_stats["total_input_tokens"] += int(usage.get("input_tokens") or 0)
                    self._pty_stats["total_output_tokens"] += int(usage.get("output_tokens") or 0)
                    self._pty_stats["total_cache_read_tokens"] += int(usage.get("cache_read_input_tokens") or 0)
                except Exception:
                    pass
            in_tok = usage.get("input_tokens")
            out_tok = usage.get("output_tokens")
            cache_r = usage.get("cache_read_input_tokens")
            tier = usage.get("service_tier")
            logger.info(
                "[PTY] response_len=" + str(len(response_text))
                + " input=" + str(in_tok)
                + " output=" + str(out_tok)
                + " cache_read=" + str(cache_r)
                + " tier=" + str(tier)
            )
        except asyncio.CancelledError:
            self._pty_stats["errors"] += 1
            logger.warning("[PTY] cancelled (ESC / stop requested)")
            self._cancelled_keys.add(thread_key)
            response_text = "承知しました、いったん止めますね"
        except Exception as e:
            self._pty_stats["errors"] += 1
            logger.error("[PTY] _invoke_via_pty failed: " + str(e), exc_info=True)
            response_text = "ごめんなさい、処理がうまくいかなかったみたいですもう一度試してもらえますか？"

        cancelled = thread_key in self._cancelled_keys
        self._cancelled_keys.discard(thread_key)
        used_fallback = False
        if cancelled:
            # 停止された場合は部分応答を捨てて確定文1通だけ返す
            response_text = "承知しました、いったん止めますね"
        elif not (response_text or "").strip():
            # 無言落ち防止: text も空なら必ず謝罪文を返す (A4 最終防衛線)
            logger.warning("[PTY] empty final response — returning fallback apology")
            response_text = "ごめんなさい、うまく応答を作れなかったみたいですもう一度メンションしてもらえますか？"
            used_fallback = True

        # 2026-07-17: タイムアウトkillを「無言の途中停止」で終わらせない。
        # 途中経過テキストだけがポツンと残ると、ユーザーには「フリーズした」ようにしか
        # 見えない。何が起きたか＋再開導線を必ず語る。
        if (not cancelled) and result and result.get("killed_via") == "timeout":
            _mins = int(int(os.getenv("AGENT_PTY_TIMEOUT", "2700")) // 60)
            response_text = (response_text or "").rstrip() + (
                f"\n\n⏰ ここで制限時間（{_mins}分）に達したので一区切りしました"
                "ここまでの作業状態・ワークスペースは保存されてる。"
                "このスレッドで「続きやって」と言ってもらえれば、続きから再開しますね"
            )
        # 自動継続の上限に達してもまだ実況で終わっている場合の仕上げ（無言の途中停止を防ぐ最終ネット）
        elif (not cancelled) and (not used_fallback) and result \
                and result.get("killed_via") != "timeout" \
                and _looks_non_terminal(response_text, result.get("files")):
            logger.info("[PTY] still non-terminal after auto-continue cap — appending resume hint")
            response_text = (response_text or "").rstrip() + (
                "\n\nここまで進めました。この続きが必要なら「続けて」と言ってもらえれば、"
                "同じ流れで最後まで仕上げますね"
            )

        # 応答返却まで到達＝正常系。走行中ラン台帳から削除
        try:
            from utils.active_runs import unregister_run
            unregister_run(thread_key)
        except Exception:
            logger.debug("[ActiveRuns] unregister failed", exc_info=True)
        else:
            # 英語メタ独白の先頭除去 (SDK 経路と同じフェイルセーフ)
            response_text = strip_leading_english_meta(response_text)

        # 生成ファイルを return に含める
        generated_files = []
        if result and not cancelled:
            generated_files = result.get("files", []) or []
        if generated_files:
            logger.info("[PTY] returning " + str(len(generated_files)) + " file(s): " + str(generated_files[:5]))
        return {
            "text": response_text,
            "files": generated_files,
            "thread_ts": thread_ts,
        }


# ====================================================================
# sdk_query monkey patch: 全 import 箇所が自動で PTY 経由になる
# ====================================================================
import uuid as _uuid_mod
import pexpect as _pexpect_mod
from utils import claude_engine as _claude_engine_module

_original_sdk_query = _claude_engine_module.sdk_query


async def _pty_sdk_query(
    prompt: str,
    model: str = models.LIGHT,
    system_prompt: str = "",
    max_turns: int = 1,
    effort: str = "",
) -> str:
    """sdk_query の PTY 版。subscription pool 維持。"""
    session_uuid = str(_uuid_mod.uuid4())
    cwd = PROJECT_ROOT
    project_dir = _project_dir_for(cwd)
    expected_jsonl = os.path.join(project_dir, session_uuid + ".jsonl")

    args = [
        prompt,
        "--session-id", session_uuid,
        "--dangerously-skip-permissions",
        "--disallowedTools", "AskUserQuestion", "ExitPlanMode",
        "--model", model,
    ]
    if system_prompt:
        args += ["--append-system-prompt", system_prompt]
    # effort: low/medium/high/xhigh/max。無言の長考で 120 秒の壁に当たるのを避けるため
    # 呼び出し側が "low" を指定できるようにする（CLI 2.1.119 も --effort に対応済み）。
    if effort:
        args += ["--effort", effort]
    if os.path.exists(_GUARD_SETTINGS):
        args += ["--settings", _GUARD_SETTINGS]

    env = dict(os.environ)
    env["TERM"] = "xterm-256color"

    bin_path = _resolve_claude_bin()

    def _run():
        import time as _t
        t0 = _t.time()
        child = _pexpect_mod.spawn(
            bin_path, args=args,
            encoding="utf-8", timeout=120, cwd=cwd, env=env,
            dimensions=(40, 200),
        )
        try:
            try:
                idx = child.expect([r"accept", _pexpect_mod.EOF, _pexpect_mod.TIMEOUT], timeout=15)
                if idx == 0:
                    _t.sleep(1.5)
                    child.send("\x1b[B")
                    _t.sleep(0.3)
                    child.send("\r")
                    _t.sleep(1)
            except Exception:
                pass
            poll_interval = 0.3
            deadline = t0 + 120
            while _t.time() < deadline:
                if not child.isalive():
                    break
                if os.path.exists(expected_jsonl):
                    try:
                        has_end = False
                        for line in open(expected_jsonl):
                            try:
                                msg = json.loads(line.strip())
                            except Exception:
                                continue
                            if msg.get("type") == "assistant":
                                m = msg.get("message", {})
                                if m.get("stop_reason") == "end_turn":
                                    has_end = True
                        if has_end:
                            _t.sleep(0.5)
                            child.kill(15)
                            _t.sleep(0.3)
                            if child.isalive():
                                child.kill(9)
                            break
                    except Exception:
                        pass
                try:
                    child.read_nonblocking(size=4096, timeout=0.05)
                except Exception:
                    pass
                _t.sleep(poll_interval)
        finally:
            try:
                child.close(force=True)
            except Exception:
                pass

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _run)

    if os.path.exists(expected_jsonl):
        text_parts = []
        try:
            for line in open(expected_jsonl):
                try:
                    msg = json.loads(line.strip())
                except Exception:
                    continue
                if msg.get("type") == "assistant":
                    for c in msg.get("message", {}).get("content", []):
                        if c.get("type") == "text":
                            text_parts.append(c.get("text", ""))
        except Exception:
            pass
        return "\n".join(text_parts)
    return ""


# 置換: 全 import 箇所が PTY 経由になる
_claude_engine_module.sdk_query = _pty_sdk_query
logger.info("[claude_pty_engine] sdk_query monkey-patched to PTY mode (subscription pool)")



# ====================================================================
# v17: _MessagesShim.create を PTY 化
# _CLIClientShim / _AsyncCLIClientShim 経由の anthropic.messages.create() 互換呼び出しも
# subscription pool で動かす。tool_executor / Memory / handlers すべてが対象。
# ====================================================================

def _pty_messages_create(self, **kwargs):
    """_MessagesShim.create の PTY 版。同期呼び出し用。
    内部で _pty_sdk_query (PTY) を asyncio.run で呼ぶ。
    """
    prompt_parts = []
    messages = kwargs.get("messages", [])
    system = kwargs.get("system", "")
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                prompt_parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        prompt_parts.append(block.get("text", ""))
    prompt = "\n".join(prompt_parts).strip() or "（入力なし）"
    model = kwargs.get("model", models.LIGHT)
    sys_str = system if isinstance(system, str) else ""

    # _pty_sdk_query (PTY 版) を sync で呼ぶ
    # v18: async コンテキストから呼ばれた場合 (event loop already running) は
    #      別 thread で asyncio.run() を回して衝突を回避する
    import asyncio as _asyncio
    import concurrent.futures as _cf

    def _run_pty_in_fresh_loop():
        return _asyncio.run(_pty_sdk_query(
            prompt=prompt,
            model=model,
            system_prompt=sys_str,
        ))

    try:
        try:
            _asyncio.get_running_loop()
            # 既存 loop あり → 別 thread の clean loop で実行
            with _cf.ThreadPoolExecutor(max_workers=1) as _ex:
                text = _ex.submit(_run_pty_in_fresh_loop).result()
        except RuntimeError:
            # 既存 loop なし (sync context) → そのまま asyncio.run() OK
            text = _run_pty_in_fresh_loop()
    except Exception as e:
        logger.error("[_pty_messages_create] error: " + str(e), exc_info=True)
        text = "(PTY call error: " + str(e) + ")"

    return _claude_engine_module._FakeResponse(text)


# Monkey patch: _MessagesShim.create を PTY 版に置換
# _AsyncMessagesShim.create は内部で _MessagesShim.create を呼ぶので自動で PTY 化される
_claude_engine_module._MessagesShim.create = _pty_messages_create
logger.info("[claude_pty_engine] _MessagesShim.create monkey-patched to PTY mode (v18: async-safe)")
