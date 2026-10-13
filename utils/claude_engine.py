"""Claude Code Engine: Agent SDK経由でClaude Codeを呼び出すAIエンジン

Phase 2移行: anthropic SDK直接呼び出し → Agent SDK（Max月額内）に切り替え。
ai_engine.pyと同じprocess_message()インターフェースを維持。
"""

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from utils.output_filter import (
    show_reasoning,
    strip_leading_english_meta,
    is_attachment_allowed,
)
from utils.session_store import SessionStore
from config import models

logger = logging.getLogger(__name__)

# 書き込みMCPツール（確認フロー判定用）
WRITE_MCP_TOOLS = {
    "mcp__notion__create_page",
    "mcp__notion__update_page",
    "mcp__notion__create_database",
    "mcp__gsheets__update_spreadsheet",
    "mcp__gsheets__append_spreadsheet",
    "notion_write_database",
    "notion_update_page",
    "gsheet_write",
    "gsheet_append",
}


class ClaudeEngine:
    """Claude Agent SDK を使ったAIエンジン（ai_engine.AIEngine互換）"""

    def __init__(
        self,
        model: str = models.MEDIUM,
        model_light: str = models.LIGHT,
        model_heavy: str = models.HEAVY,
        mcp_config_path: Optional[str] = None,
        system_prompt_path: Optional[str] = None,
    ):
        self.model = model  # Medium (Sonnet) — デフォルト
        self.model_light = model_light  # Light (Haiku)
        self.model_heavy = model_heavy  # Heavy (Opus)
        self.mcp_config_path = mcp_config_path or self._find_mcp_config()
        self.system_prompt_path = system_prompt_path or self._find_system_prompt()
        # SDK セッション管理（短期コンテキスト: スレッド = セッション）
        # key: "channel:thread_ts" → value: {"session_id": str, "tier": str, "last_used": ISO8601}
        # P2-2 (2026-05-29): in-memory dict から SessionStore に移行。
        # bot restart 越しでも last_used が 24h 以内なら resume 可能。
        self._thread_sessions = SessionStore()
        logger.info(f"[Init] SessionStore loaded with {len(self._thread_sessions)} active session(s)")
        self._sdk_client = None
        self._sdk_available = None
        # SDK同時実行制限（Max課金のレート制限対策）
        # 同時実行数は env で切替（既定1。上げる時はメモリ余力とレート制限に注意）
        _conc = max(1, int(os.getenv("AGENT_SDK_CONCURRENCY", "1")))
        self._sdk_semaphore = asyncio.Semaphore(_conc)
        logger.info(f"[SDK] concurrency limit = {_conc}")
        self._active_sdk_user: Optional[str] = None  # 現在処理中のユーザー名
        # 停止ボタン用: thread_key → 稼働中の SDK Client（disconnect で中断可）
        # 2026-05-29 追加（ESC 相当の停止機能）
        self._active_sdk_clients: Dict[str, Any] = {}
        # Slack user_id → 表示名 の解決キャッシュ（スレッド履歴で名前を取り違えないため）
        self._name_cache: Dict[str, str] = {}
        # anthropicクライアント互換シム（message_handlerが参照する箇所用）
        self.client = _CLIClientShim(self)
        self.async_client = _AsyncCLIClientShim(self)
        logger.info(f"ClaudeEngine initialized (model={model}, light={model_light}, heavy={model_heavy})")

    def _find_mcp_config(self) -> str:
        candidates = [
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".mcp.json"),
            os.path.expanduser("~/.claude/mcp.json"),
            os.path.join(os.getcwd(), ".mcp.json"),
        ]
        for path in candidates:
            if os.path.exists(path):
                logger.info(f"MCP config found: {path}")
                return path
        logger.warning("No .mcp.json found")
        return ""

    def _find_system_prompt(self) -> str:
        """prompts/persona.txt を探す"""
        candidates = [
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts", "persona.txt"),
            os.path.expanduser("~/エージェント/prompts/persona.txt"),
        ]
        for path in candidates:
            if os.path.exists(path):
                logger.info(f"System prompt found: {path}")
                return path
        logger.warning("No persona.txt found, using default prompt")
        return ""

    async def _ensure_sdk(self):
        if self._sdk_available is not None:
            return self._sdk_available
        try:
            from claude_agent_sdk import ClaudeSDKClient
            self._sdk_client = ClaudeSDKClient()
            self._sdk_available = True
            logger.info("Agent SDK initialized")
        except ImportError:
            logger.warning("claude-agent-sdk not installed, using CLI fallback")
            self._sdk_available = False
        except Exception as e:
            logger.error(f"Agent SDK init failed: {e}")
            self._sdk_available = False
        return self._sdk_available


    def _get_system_prompt(self, memory_context: Optional[str] = None,
                           skill_contents: Optional[List[str]] = None,
                           channel_id: Optional[str] = None,
                           thread_ts: Optional[str] = None) -> str:
        now = datetime.now()
        today = now.strftime("%Y年%m月%d日")
        current_year = now.strftime("%Y")
        if self.system_prompt_path and os.path.exists(self.system_prompt_path):
            with open(self.system_prompt_path, "r", encoding="utf-8") as f:
                base_prompt = f.read()
            # ── personaサニティガード（2026-07-16 導入） ──
            # 実事故: 2026-07-06 に自己修正の書き込み失敗で persona 全文（424行）が
            # AIの作業報告文8行に置換され、10日間「人格なし」で稼働していた。
            # 短すぎる/アンカー欠落の persona を検知したら内蔵デフォルトに退避する。
            if len(base_prompt) < 400:
                logger.critical(
                    f"[PersonaGuard] persona file looks CORRUPTED "
                    f"(len={len(base_prompt)}, path={self.system_prompt_path}) — "
                    f"falling back to built-in default persona. 至急ファイルを復旧してください"
                )
                base_prompt = self._default_system_prompt()
        else:
            base_prompt = self._default_system_prompt()
        prompt = base_prompt.replace("{today}", today)
        prompt = prompt.replace("{current_year}", current_year)
        prompt = prompt.replace("{current_time}", now.strftime("%H:%M"))
        prompt = prompt.replace("{agent_name}", os.getenv("AGENT_NAME", "Agent"))
        # Slack実行コンテキスト注入（MCPツールが channel_id / thread_ts を必要とする場合に使う）
        if channel_id:
            prompt += (
                f"\n\n## Slack実行コンテキスト\n"
                f"- 現在のチャンネルID: `{channel_id}`\n"
                f"- スレッドTS: `{thread_ts or ''}`\n"
                f"MCPツールが channel_id / thread_ts 引数を要求する場合は、"
                f"必ず上記の値を渡してください。\n"
            )
        if memory_context:
            prompt += f"\n\n## 記憶コンテキスト\n{memory_context}"
        # スキル注入（2026-07-16 改善: 旧「2つ目以降は先頭500字」は limitations や
        # 後半の必須手順を落とす欠陥だったため、各スキルを上限字数で頭から保持する方式に変更）
        if skill_contents:
            prompt += "\n\n## 適用スキル（この依頼に関連する手順書・ドメイン知識）\n"
            prompt += "以下のスキル文書に従って対応してください。スキルに「できないこと」が定義されている場合は正直に伝え、理由と代替案を提示してください。\n"
            # 1つ目: 全文注入（最も関連度が高いスキル。上限12000字）
            primary = skill_contents[0]
            if len(primary) > 12000:
                primary = primary[:12000] + "\n…（スキル文書が長いため以降省略）"
            prompt += f"\n---\n{primary}\n"
            # 2つ目以降: 各6000字まで（limitations等の重要部が落ちにくい上限）
            for sc in skill_contents[1:]:
                body = sc if len(sc) <= 6000 else sc[:6000] + "\n…（以降省略）"
                prompt += f"\n---\n{body}\n"

        # ── プロンプト予算の可視化（最小版）──
        total = len(prompt)
        if total > 60000:
            logger.warning(f"[PromptBudget] system prompt is large: {total} chars — 削減を検討")
        else:
            logger.info(f"[PromptBudget] system prompt: {total} chars")
        return prompt

    def _default_system_prompt(self) -> str:
        """prompts/persona.txt が無い・壊れている時の最小限のペルソナ"""
        name = os.getenv("AGENT_NAME", "Agent")
        return f"""あなたは Slack 上で働く社内AIアシスタント「{name}」です。
## 基本
- 依頼には、まず実行して結果で返す。曖昧な時だけ短く確認する
- できないことは正直に伝え、理由と代わりの方法を出す
- 外部への送信・書き込み・削除は、実行前に内容を見せて依頼者の承認をもらう
## Slackフォーマット（厳守）
- 見出し→*太字*、テーブル→使わない、**→*、[text](url)→<url|text>
## 現在日時
今日は {{today}}、{{current_year}}年、{{current_time}} JST。
"""

    # ── PreFlightプランナー（旧: LLMベースのスキル選定を拡張） ──
    # 2026-07-16 ハーネス改善: 既存のスキル選定LLM呼び出しを「前処理プランナー」に拡張。
    # 同じ1コールで ①スキル選定 ②意図エコー（理解の宣言）③tierヒント ④曖昧度 を取得する。
    # 追加のLLMコストはゼロ（従来のスキル選定コールを置換しただけ）。

    def _skill_catalog(self) -> str:
        skill_manager = getattr(self, "skill_manager", None)
        if not skill_manager:
            return ""
        catalog_lines = []
        for s in skill_manager.list_skills():
            name = s.get("name", "")
            desc = s.get("description", "")
            triggers = ", ".join(s.get("triggers", []))
            caps = ", ".join(s.get("capabilities", []))
            lims = ", ".join(s.get("limitations", []))
            line = f"- {name}: {desc}"
            if triggers:
                line += f" (triggers: {triggers})"
            if caps:
                line += f" [できること: {caps}]"
            if lims:
                line += f" [できないこと: {lims}]"
            catalog_lines.append(line)
        return "\n".join(catalog_lines)

    def _keyword_skills(self, text: str) -> List[str]:
        """キーワードベースのスキル選定（PreFlight失敗時のフォールバック）"""
        skill_manager = getattr(self, "skill_manager", None)
        if not skill_manager:
            return []
        matches = skill_manager.find_matching_skills(text)
        return [m["name"] for m in matches[:3] if m.get("score", 0) > 3]

    async def _preflight(self, text: str, speaker_is_owner: bool = False,
                         thread_tail: str = "") -> dict:
        """前処理プランナー: スキル選定＋意図エコー＋tierヒント＋曖昧度を1コールで取得

        戻り値: {"skills": [...], "intent_echo": str, "tier_hint": str,
                 "ambiguity": float, "clarify_options": [...]}
        失敗時は skills=キーワードフォールバック、他フィールドは空。
        """
        fallback = {"skills": self._keyword_skills(text), "intent_echo": "",
                    "tier_hint": "", "ambiguity": 0.0, "clarify_options": [],
                    "capability": "ok", "capability_id": ""}
        if os.getenv("AGENT_PREFLIGHT", "1") != "1":
            return fallback
        skill_manager = getattr(self, "skill_manager", None)
        if not skill_manager or not skill_manager.list_skills():
            return fallback

        catalog = self._skill_catalog()
        cap_catalog = self._get_capability_map().compact_prompt()
        tone = ("オーナー（オーナー）宛なので親しみのあるタメ語"
                if speaker_is_owner else "オーナー以外なので親しみのある砕けた敬語（です・ます。タメ語厳禁）")
        echo_example = ("『先月の売上、表にまとめるね✨』"
                        if speaker_is_owner else "『先月の売上、表にまとめますね✨』")
        prompt = f"""あなたはSlack秘書AI「エージェント」の前処理プランナーです。JSONオブジェクトのみ出力してください。

## スキル一覧（エージェントが持つ手順書・ドメイン知識）
{catalog}

## 能力マップ（エージェントができないこと[✕]・条件付き[△]）
{cap_catalog}

## スレッド直近の文脈（あれば）
{thread_tail[:500]}

## ユーザーのメッセージ
「{(text or '')[:800]}」

## 出力（このJSONのみ。説明・前置き禁止）
{{
 "skills": ["対応に必要なスキルのname。最大3つ。無ければ[]"],
 "intent_echo": "エージェントがこれから何をするかの宣言1文。{tone}で、エージェントの明るい口調（絵文字1つ可）。疑問形にせず宣言する。例:{echo_example}",
 "tier": "light|medium|heavy",
 "ambiguity": 0.0,
 "clarify_options": [],
 "capability": "ok|blocked|partial",
 "capability_id": ""
}}

## capabilityの基準
- 依頼が能力マップの[✕]項目に該当 → "blocked"、[△]項目に該当 → "partial"
- blocked/partial のときは capability_id に *上のマップに完全一致するid* を入れる。
  マップに無い理由で「できない」と判断してはいけない（その場合は "ok"）

## tierの基準
- light: 挨拶・雑談・一言で答えられる質問のみ
- heavy: データ分析＋成果物作成の組み合わせ / スライド・提案書等の長尺成果物 / Deep Research / 「徹底的に」「最高品質」等の明示
- 迷ったら medium

## ambiguityの基準
「解釈が複数あり、選択を誤ると成果物を作り直しになる」度合い（0.0-1.0）。
情報が少ないだけで常識的なデフォルトで進められるなら0.3以下。
0.7以上のときのみ clarify_options に解釈候補を2-4個（各候補はそのまま依頼文として実行できる具体的な文で）。"""

        try:
            response_text = await sdk_query(
                prompt=prompt,
                model=models.PREFLIGHT,
                system_prompt="JSONオブジェクトのみ返すこと。",
                effort="low",
            )
            import re as _re
            match = _re.search(r'\{.*\}', response_text, _re.DOTALL)
            if not match:
                logger.info(f"[Preflight] No JSON in response, keyword fallback: {response_text[:100]}")
                return fallback
            data = json.loads(match.group(0))
            skills = data.get("skills") or []
            valid = [s for s in skills if isinstance(s, str) and skill_manager.get_skill(s)]
            # 能力判定: マップに実在するidのときだけ有効（LLMの根拠なき「できない」を決定的に抑止）
            cap_status = data.get("capability") if data.get("capability") in ("ok", "blocked", "partial") else "ok"
            cap_id = str(data.get("capability_id") or "")
            if cap_status != "ok" and not self._get_capability_map().get(cap_id):
                logger.info(f"[Preflight] capability={cap_status} with unknown id={cap_id!r} → forced ok")
                cap_status, cap_id = "ok", ""
            result = {
                "skills": valid,
                "intent_echo": str(data.get("intent_echo") or "")[:200],
                "tier_hint": data.get("tier") if data.get("tier") in ("light", "medium", "heavy") else "",
                "ambiguity": float(data.get("ambiguity") or 0.0),
                "clarify_options": [str(o)[:200] for o in (data.get("clarify_options") or [])[:4]],
                "capability": cap_status,
                "capability_id": cap_id,
            }
            logger.info(
                f"[Preflight] skills={valid} tier_hint={result['tier_hint']} "
                f"ambiguity={result['ambiguity']:.2f} capability={cap_status}:{cap_id} "
                f"echo={result['intent_echo'][:60]!r} clarify={len(result['clarify_options'])}"
            )
            return result
        except Exception as e:
            logger.warning(f"[Preflight] failed, falling back to keyword: {e}")
            return fallback

    # ── 相槌の即応答（LLMを呼ばない決定論応答） ──
    # 2026-07-16 ハーネス改善: 完全一致で判定できる相槌にLLMを使わない（quota/レイテンシ削減）
    _ACK_RESPONSES = {
        "ありがとう": "どういたしまして",
        "ありがとうございます": "どういたしまして！ また何でも言ってくださいね",
        "ありがと": "どういたしまして",
        "了解": "はーい",
        "了解です": "はーい",
        "りょうかい": "はーい",
        "ok": "OK",
        "おけ": "OK",
        "おっけー": "OK",
        "助かる": "よかった！ いつでもどうぞ",
        "助かりました": "よかったです！ いつでもどうぞ",
        "おつかれ": "おつかれさま",
        "おつかれさま": "おつかれさま",
        "お疲れ様です": "お疲れ様です",
        "ナイス": "✨",
        "さすが": "えへへ、ありがとう",
    }

    def _check_simple_ack(self, text: str) -> Optional[str]:
        """完全一致の相槌なら定型応答を返す（LLM不要）。該当しなければNone"""
        t = (text or "").strip().rstrip("！!〜~。．.☂️✨🙏")
        return self._ACK_RESPONSES.get(t) or self._ACK_RESPONSES.get(t.lower())

    def _get_capability_map(self):
        """能力マップ（lazy load・fail-open）"""
        if not hasattr(self, "_cap_map"):
            try:
                from utils.capability_map import CapabilityMap
                self._cap_map = CapabilityMap()
            except Exception:
                logger.exception("[CapabilityMap] init failed")
                class _Empty:
                    def get(self, _): return None
                    def compact_prompt(self): return ""
                    def injection_note(self, _): return ""
                self._cap_map = _Empty()
        return self._cap_map

    def _record_capability_candidate(self, request_text: str, response_text: str):
        """「できませんでした」系で終わった未登録依頼を能力マップ候補として記録（週次レビュー用）"""
        try:
            path = os.path.join(
                os.getenv("MEMORY_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "memory_data")),
                "capability_candidates.jsonl",
            )
            entry = {
                "ts": datetime.now().isoformat(),
                "request": (request_text or "")[:200],
                "response_tail": (response_text or "")[-200:],
            }
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            logger.info("[Capability] candidate recorded")
        except Exception:
            logger.debug("[Capability] candidate record failed", exc_info=True)

    def _route_task(self, text: str, files: Optional[List[dict]] = None) -> dict:
        """タスクルーティングv2（2026-07-16 ハーネス改善）

        デフォルト = medium（Sonnet 5.5 ≒ Opus級）。heavyは明示シグナルのみ。
        旧lightルート（<100字→Haiku）は品質事故の原因だったため廃止。
        相槌は _check_simple_ack で本メソッド到達前に処理される。
        PreFlightのtierヒントが本判定より重い場合は呼び出し側で昇格される。

        Returns: {"model": str, "tier": str, "tool_context": str, "should_respond": bool}
        """
        t = (text or "").lower()
        has_files = bool(files)
        has_data_files = has_files and any(
            f.get("name", f.get("filename", "")).lower().endswith(ext)
            for f in (files or [])
            for ext in (".csv", ".xlsx", ".xls", ".tsv")
        )

        # ── Heavy (Opus): 明示シグナルのみ ──
        # 長尺成果物 / Deep Research / 明示的な深さ要求 / データ分析×成果物
        heavy_artifact_kw = [
            "資料", "スライド", "プレゼン", "提案書", "要件定義",
            "deep research", "リサーチ", "戦略",
        ]
        heavy_depth_kw = ["徹底的", "深く分析", "最高品質", "最終版", "詳細に分析"]
        heavy_reasons = []
        if any(kw in t for kw in heavy_artifact_kw):
            heavy_reasons.append("artifact")
        if any(kw in t for kw in heavy_depth_kw):
            heavy_reasons.append("explicit_depth")
        if has_data_files and any(kw in t for kw in ["分析", "集計", "グラフ", "レポート", "まとめ"]):
            heavy_reasons.append("data_analysis")

        if heavy_reasons:
            tier = "heavy"
            model = self.model_heavy
            logger.info(f"[Router] heavy reasons={heavy_reasons}")
        # ── Medium (Sonnet 5.5): デフォルト ──
        else:
            tier = "medium"
            model = self.model

        # ツールコンテキスト判定
        tool_context = self._detect_tool_context(t)

        # ── コンテキスト別の最低tier保証 ──
        # SNS投稿作成はトレンド取得 + 複数ツール呼び出し + 文体判断が必要なので
        # 最低でも Sonnet(medium) 以上にする。Haikuでは能力不足で途中打ち切りになる
        tier_order = {"light": 0, "medium": 1, "heavy": 2}
        if tool_context == "sns":
            if tier_order[tier] < tier_order["medium"]:
                logger.info(f"[Router] SNS context → upgrading tier to medium (Sonnet)")
                tier = "medium"
                model = self.model
        # プレゼン・画像・SQL・データ系も Sonnet 以上推奨
        elif tool_context in ("presentation", "image", "sql", "data"):
            if tier_order[tier] < tier_order["medium"]:
                logger.info(f"[Router] {tool_context} context → upgrading tier to medium")
                tier = "medium"
                model = self.model

        logger.info(f"[Router] tier={tier}, model={model}, context={tool_context}")
        return {
            "model": model,
            "tier": tier,
            "tool_context": tool_context,
            "should_respond": True,
        }

    @staticmethod
    def _detect_tool_context(text: str) -> str:
        """メッセージ内容からツールコンテキストを推定"""
        t = text.lower() if text else ""
        if any(kw in t for kw in ["資料作", "スライド", "プレゼン", "提案書", "pptx", "デッキ"]):
            return "presentation"
        if any(kw in t for kw in ["画像", "イラスト", "ロゴ", "写真", "リサイズ", "生成して"]):
            return "image"
        if any(kw in t for kw in ["sqlを", "sql書", "sql作", "クエリを作", "クエリを書", "bigquery"]):
            return "sql"
        if any(kw in t for kw in ["分析", "グラフ", "csv", "excel", "データ"]):
            return "data"
        if any(kw in t for kw in [
            "x投稿", "ツイート", "tweet", "sns",
        ]):
            return "sns"
        if len(t) < 200 and not any(kw in t for kw in [
            "スキル", "メモして", "覚えて", "リマインド", "まとめて", "改善",
        ]):
            return "light"
        return "full"

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
        """メッセージ処理（ai_engine互換）→ {text, files, thread_ts}"""
        # ── 相槌の即応答（LLM不要・quota/レイテンシ節約） ──
        ack = self._check_simple_ack(text)
        if ack and not files:
            logger.info("[Router] simple ack → deterministic response (no LLM)")
            return {"text": ack, "files": [], "thread_ts": thread_ts}

        # モデルルーティング
        route = self._route_task(text, files)
        selected_model = route["model"]

        # ── PreFlight: スキル選定＋意図エコー＋tierヒント＋曖昧度を1コールで ──
        _owner_id = os.getenv("AGENT_OWNER_SLACK_ID", "")
        speaker_is_owner = bool(_owner_id) and (context or {}).get("user") == _owner_id
        preflight = {}
        skill_contents = []
        try:
            preflight = await self._preflight(text or "", speaker_is_owner=speaker_is_owner)
            selected_skill_names = list(preflight.get("skills") or [])
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
                    logger.info(f"[Skills] Injecting {len(skill_contents)} skill(s): {selected_skill_names}")
        except Exception as e:
            logger.warning(f"[Skills] Skill selection error (continuing without skills): {e}")

        # ── PreFlightのtierヒントで昇格（降格はしない: キーワード判定を下限とする） ──
        tier_order = {"light": 0, "medium": 1, "heavy": 2}
        hint = (preflight or {}).get("tier_hint") or ""
        if hint == "heavy" and tier_order.get(route["tier"], 1) < 2:
            logger.info("[Router] preflight hint → upgrading tier to heavy")
            route["tier"] = "heavy"
            route["model"] = self.model_heavy

        # ── 能力マップ判定: 「できない/条件付き」依頼は正直＋代替案をコードで保証 ──
        cap_status = (preflight or {}).get("capability") or "ok"
        cap_id = (preflight or {}).get("capability_id") or ""
        cap_injection = ""
        if cap_status in ("blocked", "partial") and cap_id:
            cap_injection = self._get_capability_map().injection_note(cap_id)
            if cap_injection:
                logger.info(f"[Capability] {cap_status}: {cap_id}")
                if cap_status == "blocked" and route["tier"] == "heavy":
                    # できない依頼にheavyランは不要（代替案提示はmediumで十分）
                    route["tier"] = "medium"
                    route["model"] = self.model
                    logger.info("[Capability] blocked → tier downgraded to medium")

        system_prompt = self._get_system_prompt(
            memory_context,
            skill_contents=skill_contents or None,
            channel_id=channel,
            thread_ts=thread_ts,
        )
        if cap_injection:
            system_prompt += cap_injection

        # ── 中期コンテキスト: スレッド内の発言を取得して注入 ──
        # 2026-07-16: セッションresume時はSDK側が過去ターンを記憶しているため、
        # 前回ターン以降の新規発言だけを注入する（同じ発言の二重注入＝判断ブレの防止）
        thread_context = ""
        _tk = f"{channel}:{thread_ts}" if thread_ts else None
        _since_ts = None
        if _tk and _tk in self._thread_sessions:
            _since_ts = (self._thread_sessions.get(_tk) or {}).get("last_ts")
        if thread_ts and context and context.get("slack_client"):
            thread_context = await self._fetch_thread_messages(
                context["slack_client"], channel, thread_ts, since_ts=_since_ts
            )

        # ── 会話目的追跡: 長いスレッドでは目的をsystem promptに注入 ──
        if thread_context and thread_context.count("\n") > 8:
            # 10行以上のスレッド → 会話目的をsystem promptの末尾に追加
            system_prompt += (
                "\n\n## 今回のタスク指針（最重要 — 迷ったらここに立ち返る）\n"
                "このスレッドは長くなっています。*直近のメッセージに書かれている依頼に集中*してください。\n"
                "途中の脱線・解決済みの話題・過去の方針変更に引きずられないこと。\n"
                "最新のユーザー発言が今回のタスクです。\n"
                "ただし最新発言が「1」「それ」「さっきの」等の指示語・番号でスレッド内（特に直前のエージェント自身の発言の"
                "番号付きリスト・選択肢）を参照している場合は、その参照先を必ず上の会話履歴から特定して具体的に答えること。"
                "『どれですか』『内容を共有してください』と聞き返さない。"
            )

        user_prompt = self._build_user_prompt(text, files, context, thread_context=thread_context)

        # Heavy タスクではツール実行を強制するリマインダーを追加
        if route["tier"] == "heavy":
            user_prompt += "\n\n[重要: テキストで計画を説明するのではなく、必要なツール（gsk_web_search, start_presentation 等）を即座に呼び出してください。]"

        if progress_callback:
            tier_emoji = {"light": "⚡", "medium": "💭", "heavy": "🧠"}.get(route["tier"], "💭")
            # 意図エコー: 「エージェントがどう理解したか」を最初に宣言する（気の利いた挙動の核）
            echo = (preflight or {}).get("intent_echo") or ""
            if echo:
                await progress_callback(f"{tier_emoji} {echo}")
            else:
                await progress_callback(f"{tier_emoji} エージェントが確認しています…")

        # ── 短期コンテキスト: スレッドtsをセッションIDとして管理 ──
        thread_key = f"{channel}:{thread_ts}" if thread_ts else None
        session_id = None
        resume_session = None
        if thread_key and thread_key in self._thread_sessions:
            # 既存セッションを再開
            # 2026-07-16: tier引き継ぎ（ダウングレード禁止）は廃止 — tierは毎ターン判定する。
            # Sonnet 5デフォルト化で軽tierの品質リスクが消え、継承による永久heavy化
            # （quota浪費＋グローバルセマフォの長時間占有）の方が害が大きいため。
            session_info = self._thread_sessions.get(thread_key) or {}
            resume_session = session_info.get("session_id")
            logger.info(f"[Context] Resuming session {resume_session[:12]}... for thread {thread_key}")
        elif thread_key:
            # 新しいセッションを開始
            # ⚠️ UUIDv5 (決定的) だと エージェント再起動後に同じIDが生成され、SDK内部のstaleな
            # セッションstateと衝突する（"already in use"エラー + 古い文脈のresume）
            # → UUIDv4 (ランダム) で毎回ユニークなIDを生成する
            # in-process の継続は _thread_sessions dict で管理するので問題ない
            session_id = str(uuid.uuid4())
            logger.info(f"[Context] New session {session_id} for thread {thread_key}")

        # tier引き継ぎ後の最終モデルを使用（selected_modelはルーティング時点の値なので再取得）
        final_model = route["model"]

        # stderrでセッション衝突を検出するためのフラグ
        self._last_stderr_session_conflict = False

        # ── SDK同時実行制限: Max課金のレート制限対策 ──
        # 別のリクエストを処理中の場合、ユーザーに待機メッセージを表示
        if self._sdk_semaphore.locked():
            active_user = self._active_sdk_user or "別のユーザー"
            logger.info(f"[SDK] Concurrency wait: {active_user} の処理が進行中、待機開始")
            if progress_callback:
                await progress_callback(
                    f"⏳ 別の処理を進めているところです。終わり次第すぐ取りかかりますね"
                )

        async with self._sdk_semaphore:
            # 現在処理中のユーザーを記録
            user_name = (context or {}).get("user_name", "")
            self._active_sdk_user = user_name or channel
            try:
                try:
                    response_text = await self._call_via_sdk(
                        system_prompt, user_prompt, progress_callback,
                        model_override=final_model, tier=route["tier"],
                        session_id=session_id, resume_session=resume_session,
                        thread_key=thread_key, requester_id=(context or {}).get("user", ""), source_channel=channel, source_thread=thread_ts,
                    )
                except Exception as e:
                    error_msg = str(e)
                    # セッションID衝突エラー → resumeで再試行 → ダメなら新規IDで再試行
                    is_session_conflict = (
                        "already in use" in error_msg
                        or getattr(self, "_last_stderr_session_conflict", False)
                        or ("exit code 1" in error_msg and session_id and not resume_session)
                    )
                    if is_session_conflict and (resume_session or session_id):
                        conflict_id = resume_session or session_id
                        logger.warning(f"Session conflict detected, trying resume then fresh: {error_msg[:200]}")

                        # 1) まず既存セッションのresumeを試す（再起動後のUUID v5衝突対策）
                        try:
                            logger.info(f"[SDK] Retry#1: resume existing session {conflict_id[:12]}...")
                            response_text = await self._call_via_sdk(
                                system_prompt, user_prompt, progress_callback,
                                model_override=final_model, tier=route["tier"],
                                session_id=None, resume_session=conflict_id,
                                thread_key=thread_key, requester_id=(context or {}).get("user", ""), source_channel=channel, source_thread=thread_ts,
                            )
                            resume_session = conflict_id
                            session_id = None
                        except Exception as e_resume:
                            # 2) resumeも失敗 → 完全新規セッション
                            logger.warning(f"[SDK] Resume failed, retry#2 with fresh ID: {e_resume}")
                            if thread_key:
                                self._thread_sessions.delete(thread_key)
                            # 新規セッションはSDK側の記憶がゼロなので、スレッド全文で
                            # user_prompt を再構築する（差分注入のままだと文脈が欠落する）
                            try:
                                if thread_ts and context and context.get("slack_client"):
                                    _full_ctx = await self._fetch_thread_messages(
                                        context["slack_client"], channel, thread_ts
                                    )
                                    if _full_ctx:
                                        user_prompt = self._build_user_prompt(
                                            text, files, context, thread_context=_full_ctx
                                        )
                            except Exception as _rebuild_err:
                                logger.warning(f"[SDK] fresh-context rebuild failed: {_rebuild_err}")
                            fresh_id = str(uuid.uuid4())
                            try:
                                response_text = await self._call_via_sdk(
                                    system_prompt, user_prompt, progress_callback,
                                    model_override=final_model, tier=route["tier"],
                                    session_id=fresh_id, resume_session=None,
                                    thread_key=thread_key, requester_id=(context or {}).get("user", ""), source_channel=channel, source_thread=thread_ts,
                                )
                                session_id = fresh_id
                                resume_session = None
                            except Exception as e2:
                                logger.error(f"All retries failed: {e2}", exc_info=True)
                                # 生の例外文字列 (パス/内部API名を含み得る) は Slack に出さない。詳細は log のみ。
                                response_text = "☁️ ごめんなさい、処理中にエラーが発生しました。もう一度お試しいただけますか？"
                    else:
                        logger.error(f"Claude Code call failed: {e}", exc_info=True)
                        # 生の例外文字列は Slack に出さない。詳細は log のみ。
                        response_text = "☁️ ごめんなさい、処理中にエラーが発生しました。もう一度お試しいただけますか？"
            finally:
                self._active_sdk_user = None

        # セッションIDとtierを記録（初回: session_id, 継続: tierを更新）
        # ⚠️ ここで例外が発生すると、生成済みの response_text がユーザーに届かないので
        # try/exceptで保護する（応答の返却を最優先）
        try:
            if thread_key:
                # last_ts: このターンで処理済みの時点（次ターンのスレッド履歴差分注入に使う）
                _now_ts = time.time()
                if session_id:
                    self._thread_sessions.set(thread_key, {"session_id": session_id, "tier": route["tier"], "last_ts": _now_ts})
                elif resume_session:
                    # resumeで成功したケース
                    # - 通常の継続: 既にthread_keyが存在するので tier/last_ts のみ更新
                    # - リトライ経由でresumeに切り替わった新規セッション: thread_keyは未登録 → 新規作成
                    if thread_key in self._thread_sessions:
                        self._thread_sessions.update_partial(thread_key, {"tier": route["tier"], "last_ts": _now_ts})
                    else:
                        self._thread_sessions.set(thread_key, {
                            "session_id": resume_session,
                            "tier": route["tier"],
                            "last_ts": _now_ts,
                        })
        except Exception as bookkeep_err:
            logger.warning(f"Session bookkeeping error (response still returned): {bookkeep_err}")

        # SDKツール結果から抽出されたファイルパスを取得
        extracted_files = getattr(self, "_last_files", [])
        # 2026-05-29 hotfix: ホワイトリスト外（.md/.txt/.json/.html等）を念のため除外
        if extracted_files:
            before = len(extracted_files)
            extracted_files = [f for f in extracted_files if is_attachment_allowed(f)]
            dropped = before - len(extracted_files)
            if dropped:
                logger.info(f"[SDK] Dropped {dropped} non-deliverable file(s) by whitelist")
            if extracted_files:
                logger.info(f"[SDK] Returning {len(extracted_files)} file(s): {extracted_files}")

        # 2026-05-29 hotfix: 最終応答先頭の英語メタ独白をフェイルセーフ除去
        response_text = strip_leading_english_meta(response_text)

        # ── 接地チェック（層3・コード検証）: 数値主張×データツール台帳の突合 ──
        # Stage 0=ログのみ。AGENT_GROUNDING_ENFORCE=1 で未接地の数値に注記をコードが付ける
        try:
            from utils.grounding import apply_grounding_check
            response_text = apply_grounding_check(
                response_text, getattr(self, "_last_tool_ledger", set())
            )
        except Exception:
            logger.exception("[Grounding] check failed (fail-open)")

        # ── 能力マップ候補の記録: 未登録なのに「できませんでした」で終わった依頼 ──
        try:
            import re as _re
            if cap_status == "ok" and _re.search(r"できません|できなかった|対応できない", response_text or ""):
                self._record_capability_candidate(text, response_text)
        except Exception:
            pass

        return {"text": response_text, "files": extracted_files, "thread_ts": thread_ts}


    def _build_user_prompt(self, text, files=None, context=None, thread_context: str = "") -> str:
        parts = []

        # ── 中期コンテキスト: スレッド内の発言（他メンバー含む） ──
        if thread_context:
            parts.append(f"## このスレッドの会話（最新の依頼は末尾です）\n{thread_context}\n---")

        # ── 発話者と適用トーンを明示（敬語/タメ語の誤爆を防ぐ・最重要） ──
        # 話者IDをモデルに推測させず、コード側で決め打ちして注入する。
        # タメ語が許されるのはオーナー（AGENT_OWNER_SLACK_ID）のみ。未設定なら全員に敬語。
        speaker = (context or {}).get("user") if context else None
        owner_id = os.getenv("AGENT_OWNER_SLACK_ID", "")
        if speaker:
            if owner_id and speaker == owner_id:
                parts.append(
                    f"［発話者: <@{speaker}>（オーナー）／"
                    f"トーン: フランクなタメ口・タメ語でOK］"
                )
            else:
                parts.append(
                    f"［発話者: <@{speaker}>（オーナー以外）／トーン: *親しみのある砕けた敬語*で答えること。"
                    f"距離は近いが、語尾は必ず敬語（です・ます・ですね・しますね 等）。"
                    f"タメ語・タメ口（〜だよ／〜だね／〜して／〜しとく／〜っしょ 等）は*絶対に使わない*。"
                    f"これはこのスレッドにオーナーがいても変わらない（オーナー本人に返す時だけタメ語）。］"
                )

        parts.append(text or "（メッセージ内容なし）")

        if files:
            text_files = [f for f in files if f.get("type") == "text" and f.get("text_content")]
            image_files = [f for f in files if f.get("type") == "image"]
            for f in text_files:
                content = f["text_content"]
                fname = f.get("filename", "file")
                if len(content) > 2000:
                    content = content[:2000] + f"\n...(省略、全{len(f['text_content'])}文字)"
                parts.append(f"\n--- 添付: {fname} ---\n{content}")
            if image_files:
                # 画像はワークスペースに保存済み → Read tool で読ませるためパスを明示
                # PTY 経由では multimodal block を直接渡せないため、Read tool 経路を「最優先・先頭タスク」として
                # 強く指示する。曖昧な書き方だと skip されて文字情報だけで答えるバグが頻出した（2026-06-11 v18 強化）。
                image_lines = [
                    "\n--- 添付画像（**必ず以下を最初にやってください**） ---",
                    "**返信を書き始める前に、以下のすべての画像を Read tool で読み込んで内容を確認してください。これは必須ステップで、スキップ厳禁です。**",
                ]
                for i, img in enumerate(image_files, 1):
                    fname = img.get("filename", "image")
                    path = img.get("workspace_path", "")
                    if path:
                        image_lines.append(f"{i}. Read tool で `{path}` を開く（{fname}, {img.get('mimetype', '')}）")
                    else:
                        image_lines.append(f"{i}. {fname}（保存失敗、ダウンロード元: {img.get('url', '')} — 取得できなければユーザーに再送依頼）")
                image_lines.append(
                    "\n画像を Read で読んだ後、見えた内容（配色・レイアウト・写っている物体・テキスト・UI 要素・モーション・人物の表情等）を踏まえた上で返信してください。"
                    "「画像が見えない」「画像ありがとう」だけの返信は禁止。"
                    "画像が複数あれば全部読んでから統合的に判断すること。"
                )
                parts.append("\n".join(image_lines))
        return "\n".join(parts)

    # ── 中期コンテキスト: スレッド全発言取得 ──

    async def _resolve_name(self, slack_client, user_id: str) -> str:
        """Slack user_id を実名(display_name優先, なければreal_name)に解決。キャッシュ付き。
        スレッド履歴で発言者を生ID <@U..> のまま LLM に渡すと名前を取り違えるため、ここで実名化する。"""
        if not user_id:
            return ""
        if user_id in self._name_cache:
            return self._name_cache[user_id]
        name = ""
        try:
            if slack_client:
                r = await slack_client.users_info(user=user_id)
                prof = (r.get("user") or {}).get("profile", {}) or {}
                name = (prof.get("display_name") or "").strip() or (prof.get("real_name") or "").strip()
        except Exception as e:
            logger.debug(f"[name] resolve failed {user_id}: {e}")
        name = name or user_id
        self._name_cache[user_id] = name
        return name

    async def _resolve_mentions(self, slack_client, text: str) -> str:
        """本文中の <@U..> メンションを実名(@名前)に置換し、LLM が人物を取り違えないようにする。"""
        if not text or "<@" not in text:
            return text
        import re as _re
        for uid in set(_re.findall(r"<@(U[A-Z0-9]+)>", text)):
            nm = await self._resolve_name(slack_client, uid)
            text = text.replace(f"<@{uid}>", f"@{nm}")
        return text

    async def _save_thread_images(self, msg: dict) -> List[str]:
        """スレッド内発言の添付画像をワークスペースへ保存し、ローカルパスを返す。

        Slack の url_private は Bearer 認証が必要なので落としてから渡す。
        同じファイルは一度だけ落とす（同一スレッドで毎ターン取得するのを防ぐ）。
        """
        files = msg.get("files") or []
        if not files:
            return []
        import os as _os
        token = _os.getenv("SLACK_BOT_TOKEN", "")
        if not token:
            return []
        # ワークスペース直下に置くと、PTYエンジンが「生成物」と誤認して
        # Slackへ再アップロードしてしまう（2026-09-24 オーナー指摘: 元スレの写真が
        # 回答と無関係に4枚貼られた）。読むためだけの画像は外に置く。
        ws = _os.path.expanduser("~/エージェント/memory_data/thread_images")
        try:
            _os.makedirs(ws, exist_ok=True)
        except Exception:
            return []
        if not hasattr(self, "_thread_image_cache"):
            self._thread_image_cache = {}
        out: List[str] = []
        MAX_PER_MSG = 4
        MAX_BYTES = 12 * 1024 * 1024
        for f in files[:MAX_PER_MSG]:
            if not (f.get("mimetype") or "").startswith("image/"):
                continue
            fid = f.get("id") or f.get("url_private", "")
            if not fid or not f.get("url_private"):
                continue
            # 保存済みでも Slack 上のサイズと違えば途中で切れたもの。使わずに取り直す
            # （2026-10-08: 修正前に保存した灰色の画像を使い回していた）
            want = f.get("size") or 0

            def _ok(pth):
                return _os.path.exists(pth) and (not want or _os.path.getsize(pth) == want)

            cached = self._thread_image_cache.get(fid)
            if cached and _ok(cached):
                out.append(cached)
                continue
            safe = _os.path.basename(f.get("name") or "image.png").replace(" ", "_")
            path = _os.path.join(ws, f"thread_{fid}_{safe}")
            if _ok(path):
                self._thread_image_cache[fid] = path
                out.append(path)
                continue
            try:
                import aiohttp
                async with aiohttp.ClientSession() as sess:
                    async with sess.get(f["url_private"],
                                        headers={"Authorization": f"Bearer {token}"}) as r:
                        if r.status != 200:
                            logger.warning(f"[ThreadImage] HTTP {r.status} for {safe}")
                            continue
                        # content.read(n) は「いま届いている分」しか返さないので、
                        # 大きい写真が途中で切れて下半分が灰色になっていた（2026-10-08）。
                        # EOF まで読み切る。
                        buf = bytearray()
                        async for chunk in r.content.iter_chunked(256 * 1024):
                            buf.extend(chunk)
                            if len(buf) > MAX_BYTES:
                                break
                        data = bytes(buf)
                        expected = r.content_length
                if len(data) > MAX_BYTES:
                    logger.warning(f"[ThreadImage] too large, skipped: {safe}")
                    continue
                if expected and len(data) != expected:
                    logger.warning(f"[ThreadImage] incomplete {safe}: {len(data)}/{expected} bytes")
                    continue
                with open(path, "wb") as fp:
                    fp.write(data)
                self._thread_image_cache[fid] = path
                out.append(path)
                logger.info(f"[ThreadImage] saved {safe} -> {path} ({len(data)} bytes)")
            except Exception as e:
                logger.warning(f"[ThreadImage] download failed for {safe}: {e}")
        return out

    async def _fetch_thread_messages(self, slack_client, channel: str, thread_ts: str,
                                     since_ts: Optional[float] = None) -> str:
        """Slack APIでスレッド内の発言を取得し、テキストとして返す

        エージェント自身の発言も含めるが、SDKセッション継続で重複する可能性があるため
        「参考情報」として注入。SDKセッションが持つ短期記憶と補完し合う。
        since_ts が指定された場合、それ以前の発言はスキップする
        （セッションresume時の二重注入防止。SDK側が過去ターンを記憶している）。
        """
        try:
            result = await slack_client.conversations_replies(
                channel=channel, ts=thread_ts, limit=50
            )
            messages = result.get("messages", [])
            if len(messages) <= 1:
                # 親メッセージのみ（返信なし）= スレッド会話がまだない
                return ""

            full_lines = []
            display_lines = []
            bot_user_id = os.getenv("SLACK_BOT_USER_ID", "")
            _thread_images: List[str] = []
            for msg in messages:
                user_id = msg.get("user", "")
                text = msg.get("text", "").strip()
                # 2026-09-24: 添付画像もスレッド文脈として渡す。
                # 以前は text だけを見ていたため、「画像について解説して」と頼まれても
                # 本文しか届かず「画像が読み込めていません」で行き詰まっていた
                # 画像のみの発言も拾う。
                _msg_imgs = await self._save_thread_images(msg)
                if _msg_imgs:
                    _thread_images.extend(_msg_imgs)
                if not text and not _msg_imgs:
                    continue
                # resume時: 前回ターンまでに処理済みの発言はスキップ（二重注入防止）
                if since_ts:
                    try:
                        if float(msg.get("ts", 0)) <= float(since_ts):
                            continue
                    except (TypeError, ValueError):
                        pass

                # ユーザー名の解決（生ID <@U..> ではなく実名でラベル付け = 取り違え防止）
                if user_id == bot_user_id or msg.get("bot_id"):
                    name = "エージェント"
                else:
                    name = await self._resolve_name(slack_client, user_id)
                # 本文中の <@U..> メンションも実名化
                text = await self._resolve_mentions(slack_client, text)

                if _msg_imgs:
                    _tag = " ".join(f"[添付画像: {p}]" for p in _msg_imgs)
                    text = (text + " " if text else "") + _tag
                full_line = f"{name}: {text}"
                full_lines.append(full_line)

                # 表示用テキストの打ち切りは緩和（1000→3000字）。
                # 番号付きリスト・選択肢が末尾で切れて「1」「2番」等の参照解決を壊さないため。
                if len(text) > 3000:
                    text = text[:3000] + "…（一部省略）"
                display_lines.append(f"{name}: {text}")

            if not display_lines:
                return ""

            if _thread_images:
                display_lines.append(
                    "（履歴内の [添付画像: パス] は Read ツールで実際に開いて中身を見られる。"
                    "画像について聞かれたら必ず Read してから答えること。"
                    "「画像が届いていない」と言って終わらせない）"
                )

            # 短〜中スレッドは全文保持
            if len(display_lines) <= 20:
                return "\n".join(display_lines)

            # 長スレッド: 2026-07-16 ハーネス改善で LLM圧縮を廃止（Sonnet 5 の広い
            # コンテキストで不要になった。毎回のSonnet呼び出し＝quota/レイテンシの削減）。
            # 直近12件は全文、それ以前は各発言の先頭600字まで（決定論・番号リストは概ね保持）
            older = [
                (l[:600] + "…（省略）") if len(l) > 600 else l
                for l in display_lines[:-12]
            ]
            recent = display_lines[-12:]
            return (
                "【前半の経緯（各発言は先頭600字まで）】\n" + "\n".join(older)
                + "\n\n【直近の会話（全文）】\n" + "\n".join(recent)
            )

        except Exception as e:
            logger.warning(f"Failed to fetch thread messages: {e}")
            return ""

    # ── thinking進捗表示（コードブロック形式） ──

    _THINKING_UPDATE_INTERVAL = 0.8  # Slack更新間隔（秒）— レート制限回避のため少し長め
    _THINKING_MAX_DISPLAY = 1200  # コードブロック内の最大表示文字数

    @staticmethod
    def _build_thinking_message(header: str, thinking_text: str) -> str:
        """ヘッダーの下にthinking内容をコードブロックで表示（Slack mrkdwn形式）

        表示例:
            🧠 考えています…

            ```
            ユーザーは最新のCLIツール比較を求めている…
            主要なツールを整理しよう…
            ```
        """
        # 2026-05-29 hotfix: AGENT_SHOW_REASONING=true の時のみ thinking 内容を出す
        if not show_reasoning():
            return header
        if not thinking_text.strip():
            return header
        display = thinking_text.strip()
        lines = display.split("\n")
        # 末尾から最大文字数分を取得（最新の思考を見せる）
        taken = []
        total = 0
        for line in reversed(lines):
            if total + len(line) > ClaudeEngine._THINKING_MAX_DISPLAY and taken:
                break
            taken.insert(0, line)
            total += len(line) + 1
        trimmed = "\n".join(taken)
        if len(display) > len(trimmed):
            trimmed = "…\n" + trimmed
        return f"{header}\n\n```{trimmed}```"

    @staticmethod
    def _thinking_done_header(header: str) -> str:
        """thinking完了時にヘッダーを変換"""
        for before, after in [
            ("考えています", "考えがまとまりました✨"),
            ("分析しています", "分析できました✨"),
            ("整理しています", "整理できました✨"),
            ("確認しています", "確認できました✨"),
        ]:
            if before in header:
                return header.replace(before, after)
        return header

    # ステップ単位タイムアウト（秒）: 新しいSDKメッセージが来ない時間の上限
    _STEP_TIMEOUT = {
        "light": 120,      # Haiku: 2分（MCP初期化に時間がかかる場合がある）
        "medium": 240,     # Sonnet: 4分（MCP初期化+思考時間）
        "heavy": 1200,     # Opus+MCP: 20分（リッチモードプレゼン生成は15分超え可能）
    }

    @staticmethod
    def _build_subagents() -> dict:
        """P2-1 (2026-05-29): 並列化可能なタスクを subagent に隔離する。

        subagent は main agent から独立した context window を持つので:
        - 大量検索/Deep research の結果が main の context を汚さない
        - 画像生成のような長尺ツールが main を block しない
        - subagent 内では当該タスクへの attention を維持できる

        main agent は AI が判断して subagent を呼ぶ。subagent description で
        「どんな時に呼ぶか」を明示しておく。
        """
        try:
            from claude_agent_sdk.types import AgentDefinition
        except Exception:
            return {}

        return {
            "research_agent": AgentDefinition(
                description=(
                    "Web検索とDeep researchに特化したsubagent。"
                    "複数ソース横断調査、競合分析、最新情報収集など、"
                    "大量のテキスト処理を main agent の context から隔離したい時に呼ぶ。"
                ),
                prompt=(
                    "あなたはリサーチ専門のsubagentです。"
                    "Web検索ツールを使って正確な情報を集め、"
                    "簡潔な要約（5-10行）でmain agentに返してください。"
                    "ソースURLは必ず含めてください。長い引用は不要。"
                ),
                tools=["WebSearch", "WebFetch"],
                effort="medium",
                maxTurns=10,
            ),
            "image_agent": AgentDefinition(
                description=(
                    "画像生成・編集に特化したsubagent。"
                    "OpenAI GPT Image 2 や gsk_image を使った画像生成、"
                    "背景除去、スタイル変換など、画像処理を main agent から隔離したい時に呼ぶ。"
                ),
                prompt=(
                    "あなたは画像生成専門のsubagentです。"
                    "プロンプト最適化を行い、画像生成ツールで成果物を作って"
                    "ファイルパスを main agent に返してください。"
                    "余計な解説は不要、ファイルパスと一言だけ。"
                ),
                tools=["Bash", "Read"],
                effort="low",
                maxTurns=8,
            ),
        }

    async def _call_via_sdk(self, system_prompt, user_prompt, progress_callback=None,
                           model_override=None, tier="medium",
                           session_id=None, resume_session=None,
                           thread_key=None, requester_id="", source_channel="", source_thread="") -> str:
        # ツール結果から抽出されたファイルパスを保持
        self._last_files: list = []
        # 接地レジャー: このラン中に実際に呼ばれたツール名の台帳（数値主張との突合に使う）
        self._last_tool_ledger: set = set()
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage

        mcp_config = None
        if self.mcp_config_path and os.path.exists(self.mcp_config_path):
            mcp_config = self.mcp_config_path

        selected_model = model_override or self.model
        step_timeout = self._STEP_TIMEOUT.get(tier, 120)
        # プロンプトサイズ可視化（Prompt too long診断用）
        logger.info(
            f"[SDK/PromptSize] system={len(system_prompt):,}chars user={len(user_prompt):,}chars "
            f"tier={tier} model={selected_model}"
        )

        # SDK stderr をログに転送（MCP起動エラー等を検出）
        def _sdk_stderr_handler(line: str):
            line = line.strip()
            if line:
                logger.info(f"[SDK/stderr] {line[:500]}")
                if "already in use" in line:
                    self._last_stderr_session_conflict = True

        # ── Extended thinking 設定 ──
        # P1-1 (2026-05-29): tier 別に thinking と effort を分離
        # - light: thinking なし + effort=low（simple lookup、TTFT 重視、cost 60%減）
        # - medium: adaptive thinking + effort=medium（バランス型）
        # - heavy: adaptive thinking + effort=high（複雑なマルチステップ）
        thinking_config = None
        effort_level = None
        if tier == "light":
            effort_level = "low"
        elif tier == "medium":
            effort_level = "medium"
            try:
                from claude_agent_sdk.types import ThinkingConfigAdaptive
                thinking_config = ThinkingConfigAdaptive(type="adaptive")
            except Exception as _thk_err:
                logger.debug(f"ThinkingConfig not available: {_thk_err}")
        elif tier == "heavy":
            effort_level = "high"
            try:
                from claude_agent_sdk.types import ThinkingConfigAdaptive
                thinking_config = ThinkingConfigAdaptive(type="adaptive")
            except Exception as _thk_err:
                logger.debug(f"ThinkingConfig not available: {_thk_err}")
        logger.info(f"[SDK/effort] tier={tier} effort={effort_level} thinking={'adaptive' if thinking_config else 'off'}")

        options_kwargs = dict(
            model=selected_model,
            cli_path=os.getenv("CLAUDE_CLI_PATH") or None,
            mcp_servers=mcp_config or {},
            env={"AGENT_REQUEST_USER": requester_id, "AGENT_REQUEST_CHANNEL": source_channel, "AGENT_REQUEST_THREAD": source_thread or "", "AGENT_REQUIRE_CONTEXT": "1"},
            system_prompt=system_prompt,
            permission_mode="bypassPermissions",
            # max_turns: heavy=50 (深掘り5ステップ + 多数のツール呼び出しを許容)
            # medium/light=20 (通常の1〜2ツール呼び出し + 応答生成)
            max_turns=50 if tier == "heavy" else 20,
            stderr=_sdk_stderr_handler,
            # ── 部分メッセージストリーミング有効化 ──
            # StreamEventでtext_delta/thinking_deltaが順次届く
            # → 進捗を細かくSlackに反映できる
            include_partial_messages=True,
            thinking=thinking_config,
            # ── strict-mcp-config: claude.ai web経由のcloud MCP（Higgsfield, Motion等11個）
            # を完全に無効化し、.mcp.jsonの7サーバーのみを使用する。これを付けないと
            # cloud MCPsのtool定義がプロンプトに混入してcontext overflow（Prompt too long）。
            extra_args={"strict-mcp-config": None},
            # P1-1: effort routing
            effort=effort_level,
            max_buffer_size=64 * 1024 * 1024,
            # P2-1: subagent 定義（main agent が必要時に呼ぶ、並列化と context 隔離）
            agents=self._build_subagents(),
        )
        options = ClaudeAgentOptions(**options_kwargs)

        # ── 短期コンテキスト: セッション継続 ──
        if resume_session:
            options.resume = resume_session
            logger.info(f"[SDK] Resuming session: {resume_session[:20]}...")
        elif session_id:
            options.session_id = session_id
            logger.info(f"[SDK] New session: {session_id}")

        logger.info(f"[SDK] Starting: model={selected_model}, mcp_config={mcp_config}, tier={tier}, step_timeout={step_timeout}s")
        client = ClaudeSDKClient(options=options)
        # 停止ボタン用: thread_key で稼働中の client を登録（finally で削除）
        if thread_key:
            self._active_sdk_clients[thread_key] = client
            logger.info(f"[SDK/stop] registered active client for {thread_key}")
        await client.connect(user_prompt)

        full_response = ""
        current_thinking = ""
        # ── ストリーミング用のバッファ（StreamEvent経由で積み上げる） ──
        streaming_text = ""    # text_delta を累積（アシスタントの応答生成中の途中文字列）
        streaming_thinking = ""  # thinking_delta を累積（推論中の途中文字列）
        last_update = time.time()
        start_time = time.time()
        last_message_time = time.time()  # ステップ単位タイムアウト用
        thinking_shown = False
        # Opus系モデルの場合はthinking表示を有効化
        is_heavy = any(selected_model.startswith(m) for m in ("claude-opus",))
        status_header = "🧠 考えています…" if is_heavy else "💭 考えています…"

        # ── 処理中の経過時間表示用 ──
        last_active_tool = ""  # 現在実行中のツール名
        _HEARTBEAT_INTERVAL = 5  # 秒ごとに経過表示を更新（短めに設定してレスポンシブに）
        _heartbeat_stop = False

        # ── バックグラウンドでハートビートを送るタスク ──
        async def _heartbeat_loop():
            nonlocal last_update
            while not _heartbeat_stop:
                await asyncio.sleep(_HEARTBEAT_INTERVAL)
                if _heartbeat_stop:
                    break
                if progress_callback:
                    now = time.time()
                    # メッセージ処理で最近更新された場合はスキップ
                    if (now - last_update) < (_HEARTBEAT_INTERVAL - 1):
                        continue
                    elapsed = now - start_time
                    elapsed_min = int(elapsed // 60)
                    elapsed_sec = int(elapsed % 60)
                    if last_active_tool:
                        tool_status = self._get_tool_status(last_active_tool)
                        hb_msg = f"{tool_status}（{elapsed_min}分{elapsed_sec}秒経過）"
                    else:
                        hb_msg = f"{status_header}（{elapsed_min}分{elapsed_sec}秒経過）"
                    try:
                        await progress_callback(hb_msg)
                        last_update = now
                    except Exception:
                        pass

        heartbeat_task = asyncio.create_task(_heartbeat_loop())

        timeout_reason = ""
        # 全体タイムアウト（暴走防止の最終ガード。従来は存在せず無制限だった）
        _total_timeout = {"light": 300, "medium": 900, "heavy": 2400}.get(tier, 900)
        try:
            msg_count = 0
            # 2026-07-16 修正: 従来の step timeout は「次のメッセージを受信した後」に
            # 判定していたため、SDKが無応答のまま停止すると永久にブロックしていた。
            # anext() 自体を wait_for で包むことで、無応答でも必ず抜ける。
            _msg_iter = client.receive_messages().__aiter__()
            while True:
                try:
                    message = await asyncio.wait_for(
                        _msg_iter.__anext__(), timeout=step_timeout
                    )
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    timeout_reason = f"{int(step_timeout)}秒間応答が止まった"
                    logger.warning(f"SDK step timed out: no message for {step_timeout}s (tier={tier})")
                    break
                msg_count += 1
                now = time.time()
                elapsed = now - start_time

                cname = message.__class__.__name__
                step_elapsed = now - last_message_time
                logger.info(f"[SDK] msg#{msg_count} type={cname} elapsed={elapsed:.1f}s step={step_elapsed:.1f}s")
                last_message_time = now  # ステップタイマーリセット

                # 全体タイムアウト
                if elapsed > _total_timeout:
                    timeout_reason = f"全体の処理時間が上限（{int(_total_timeout // 60)}分）に達した"
                    logger.warning(f"SDK total timeout: {elapsed:.0f}s > {_total_timeout}s (tier={tier})")
                    break

                # ── RateLimitEvent: レート制限検知 → ユーザーに通知 ──
                if cname == "RateLimitEvent":
                    logger.warning(f"[SDK] Rate limit hit at {elapsed:.0f}s — SDK will auto-retry")
                    if progress_callback:
                        try:
                            await progress_callback("⏳ APIのレート制限に当たったので少し待っています…自動リトライ中")
                        except Exception:
                            pass
                    continue

                # ── StreamEvent: 部分メッセージ・デルタ更新 ──
                # include_partial_messages=True 有効時に届く
                # Anthropic APIのraw stream event (content_block_delta等) が入っている
                # → text_delta / thinking_delta を抽出してSlack進捗に即時反映
                if cname == "StreamEvent":
                    raw_event = getattr(message, "event", {}) or {}
                    ev_type = raw_event.get("type", "")

                    # content_block_delta: ブロック内のデルタ（text / thinking / tool_input）
                    if ev_type == "content_block_delta":
                        delta = raw_event.get("delta", {}) or {}
                        delta_type = delta.get("type", "")
                        if delta_type == "thinking_delta":
                            chunk = delta.get("thinking", "")
                            if chunk:
                                streaming_thinking += chunk
                        elif delta_type == "text_delta":
                            chunk = delta.get("text", "")
                            if chunk:
                                streaming_text += chunk

                        # 0.8秒ごとにSlack更新（レート制限回避）
                        if progress_callback and (now - last_update) >= self._THINKING_UPDATE_INTERVAL:
                            # 優先順位: 思考中 > 回答生成中
                            if streaming_thinking:
                                display = streaming_thinking
                                header = status_header
                            elif streaming_text and "<agent_clarify" not in streaming_text:
                                # clarify envelope生成中はJSON断片を進捗に漏らさない
                                display = streaming_text
                                header = "✍️ 回答を生成しています…"
                            else:
                                display = ""
                                header = status_header
                            if display:
                                msg = self._build_thinking_message(header, display)
                                try:
                                    await progress_callback(msg)
                                    last_update = now
                                    thinking_shown = True
                                except Exception:
                                    pass
                    elif ev_type == "content_block_stop":
                        # ブロック終了 → streaming バッファをcurrent_thinkingに合流
                        if streaming_thinking:
                            current_thinking += streaming_thinking
                            streaming_thinking = ""
                        # text deltaは後続のassistantメッセージブロックで改めて検出されるのでクリアのみ
                        streaming_text = ""
                    continue

                # ── SystemMessage: タスク進捗イベント ──
                if cname in ("TaskStartedMessage", "TaskProgressMessage", "TaskNotificationMessage"):
                    if progress_callback:
                        desc = getattr(message, "description", "")
                        summary = getattr(message, "summary", "")
                        status = getattr(message, "status", "")
                        last_tool = getattr(message, "last_tool_name", "")

                        if cname == "TaskStartedMessage":
                            task_msg = f"📋 タスク開始: {desc[:100]}" if desc else "📋 タスクを実行中…"
                        elif cname == "TaskProgressMessage":
                            if last_tool:
                                tool_status = self._get_tool_status(last_tool)
                                last_active_tool = last_tool
                                task_msg = tool_status
                            elif desc:
                                task_msg = f"⏳ {desc[:100]}"
                            else:
                                task_msg = f"{status_header}（処理中…）"
                        else:  # TaskNotificationMessage
                            if status == "completed":
                                task_msg = f"✅ タスク完了: {summary[:100]}" if summary else "✅ タスク完了"
                            elif status == "failed":
                                task_msg = f"❌ タスク失敗: {summary[:100]}" if summary else "❌ タスク失敗"
                            else:
                                task_msg = f"⚠️ タスク停止: {summary[:100]}" if summary else "⚠️ タスク停止"

                        await progress_callback(task_msg)
                        last_update = time.time()
                    continue

                # ── UserMessage = ツール結果（SDK内部）→ 進捗更新＋ファイルパス抽出 ──
                if cname == "UserMessage":
                    # ToolResultBlockからファイルパスを抽出
                    if hasattr(message, "content") and message.content:
                        for blk in message.content:
                            blk_type = type(blk).__name__
                            raw = ""
                            # content属性を深堀り（SDK ToolResultBlockは content がリスト of TextBlock等）
                            blk_content = getattr(blk, "content", None)
                            if blk_content:
                                if isinstance(blk_content, str):
                                    raw = blk_content
                                elif isinstance(blk_content, list):
                                    for sub in blk_content:
                                        if isinstance(sub, str):
                                            raw += sub
                                        elif hasattr(sub, "text"):
                                            raw += (sub.text or "")
                                        else:
                                            # 未知の型 → str()で強制変換
                                            raw += str(sub)
                                else:
                                    # dict, object等 → str()で強制変換
                                    raw = str(blk_content)
                            # text/result属性もフォールバックでチェック
                            if not raw:
                                for attr_name in ("text", "result", "output"):
                                    val = getattr(blk, attr_name, None)
                                    if val and isinstance(val, str):
                                        raw += val
                            if raw:
                                logger.info(f"[SDK] ToolResult raw ({blk_type}, {len(raw)} chars): {raw[:300]}")
                                self._extract_files_from_tool_result(raw)
                            else:
                                # 最終手段: str(blk)で全体をダンプして確認
                                blk_str = str(blk)[:500]
                                logger.info(f"[SDK] ToolResult block ({blk_type}) no text extracted. str(blk)={blk_str}")
                                if "/tmp/" in blk_str or ".pptx" in blk_str:
                                    self._extract_files_from_tool_result(blk_str)
                    if last_active_tool and progress_callback:
                        await progress_callback(f"✅ {self._get_tool_done_label(last_active_tool)} → 次のステップへ…")
                        last_update = now
                    last_active_tool = ""

                if hasattr(message, "content") and message.content:
                    block_types = [getattr(b, "type", type(b).__name__) for b in message.content]
                    logger.info(f"[SDK] msg#{msg_count} blocks: {block_types}")
                    for block in message.content:
                        block_type = getattr(block, "type", "")

                        # ── thinkingブロック検出・進捗表示 ──
                        if block_type == "thinking":
                            thinking_text = getattr(block, "thinking", "")
                            if thinking_text and progress_callback:
                                current_thinking += thinking_text
                                if now - last_update >= self._THINKING_UPDATE_INTERVAL:
                                    msg = self._build_thinking_message(status_header, current_thinking)
                                    await progress_callback(msg)
                                    last_update = now
                                    thinking_shown = True

                        # ── テキストブロック（最終回答） ──
                        elif hasattr(block, "text") and block.text:
                            text_content = block.text
                            logger.info(f"[SDK] TextBlock: {text_content[:120]}...")

                            # コンテキストコンパクションのサマリーを除外
                            # SDK内部でセッション再開時にサマリーがTextBlockとして出る
                            _compaction_markers = [
                                "This session is being continued",
                                "Summary:",
                                "summary below covers",
                                "Continue the conversation from where it left off",
                                "Resume directly",
                                "Optional Next Step:",
                                "If you need specific details from before compaction",
                            ]
                            if any(marker in text_content[:200] for marker in _compaction_markers):
                                logger.info(f"[SDK] Skipping compaction summary TextBlock ({len(text_content)} chars)")
                                # サマリー内に実際の回答が続く場合、最後の回答部分だけ抽出
                                # "Pick up the last task" 以降に実際の回答がある場合
                                import re as _re
                                # サマリー末尾のJSON読み込み指示の後に続く実回答を探す
                                resume_patterns = [
                                    r'(?:Pick up|Resume|Continue).*?\n(.+)',
                                    r'\.jsonl\n+(.+)',
                                ]
                                for pat in resume_patterns:
                                    m = _re.search(pat, text_content, _re.DOTALL)
                                    if m and len(m.group(1).strip()) > 20:
                                        text_content = m.group(1).strip()
                                        logger.info(f"[SDK] Extracted actual response from compaction block ({len(text_content)} chars)")
                                        break
                                else:
                                    continue  # サマリーのみ → スキップ

                            # thinking完了 → 完了ヘッダーに更新
                            if current_thinking and thinking_shown and progress_callback:
                                done_header = self._thinking_done_header(status_header)
                                msg = self._build_thinking_message(done_header, current_thinking)
                                await progress_callback(msg)
                                current_thinking = ""
                                thinking_shown = False
                            full_response += text_content

                        # ── ツール使用ブロック（開始） ──
                        elif block_type == "tool_use" or (hasattr(block, "name") and hasattr(block, "input") and not hasattr(block, "text")):
                            tool_name = getattr(block, "name", "tool")
                            tool_input = getattr(block, "input", {})
                            logger.info(f"[SDK] ToolUse: {tool_name} input_keys={list(tool_input.keys()) if isinstance(tool_input, dict) else '?'}")
                            self._last_tool_ledger.add(str(tool_name))  # 接地レジャー
                            last_active_tool = tool_name
                            if progress_callback:
                                await progress_callback(self._get_tool_status(tool_name))
                                last_update = now

                        # ── ツール結果ブロック（完了） ──
                        elif block_type == "tool_result" or (hasattr(block, "is_error") and not hasattr(block, "text")):
                            is_error = getattr(block, "is_error", False)
                            if progress_callback and last_active_tool:
                                if is_error:
                                    await progress_callback(f"⚠️ {last_active_tool} でエラー発生、リトライ中…")
                                else:
                                    await progress_callback(f"✅ {self._get_tool_done_label(last_active_tool)} → 次のステップへ…")
                                last_update = now
                            last_active_tool = ""

                if cname == "ResultMessage":
                    max_turns = 50 if tier == "heavy" else 20
                    # ResultMessageに正確なturn countとstop_reasonがあるのでそれを使う
                    actual_turns = getattr(message, "num_turns", 0)
                    stop_reason = getattr(message, "stop_reason", None) or ""
                    logger.info(
                        f"[SDK] ResultMessage: num_turns={actual_turns}, "
                        f"stop_reason={stop_reason!r}, response_len={len(full_response)}"
                    )
                    # 本当にmax_turns到達した場合のみ警告を出す
                    # （stop_reason が 'max_turns' または 'end_turn'でない場合）
                    hit_max_turns = (
                        "max_turn" in stop_reason.lower()
                        or (actual_turns >= max_turns and len(full_response) < 30)
                    )
                    if hit_max_turns:
                        logger.warning(f"[SDK] Hit max_turns={max_turns} (actual={actual_turns}, stop_reason={stop_reason})")
                        full_response += (
                            f"\n\n⚠️ 処理ステップが上限（{max_turns}ターン）に達したため、ここで一区切りしました。"
                            f"このスレッドで「続きやって」と言ってもらえれば、ここまでの文脈を引き継いで再開します"
                        )
                    logger.info(f"[SDK] ResultMessage received. total_msgs={msg_count}, response_len={len(full_response)}")
                    # P0-1: usage / cache メトリクスを可視化（cache hit rate 計測用）
                    try:
                        usage_dict = getattr(message, "usage", None) or {}
                        model_usage = getattr(message, "model_usage", None) or {}
                        cost = getattr(message, "total_cost_usd", None)
                        if usage_dict:
                            cache_read = usage_dict.get("cache_read_input_tokens", 0)
                            cache_create = usage_dict.get("cache_creation_input_tokens", 0)
                            input_t = usage_dict.get("input_tokens", 0)
                            output_t = usage_dict.get("output_tokens", 0)
                            total_input = input_t + cache_read + cache_create
                            hit_rate = (cache_read / total_input * 100) if total_input > 0 else 0
                            logger.info(
                                f"[SDK/usage] input={input_t} output={output_t} "
                                f"cache_read={cache_read} cache_create={cache_create} "
                                f"hit_rate={hit_rate:.1f}% cost=${cost or 0:.4f}"
                            )
                        if model_usage:
                            logger.info(f"[SDK/model_usage] {model_usage}")
                    except Exception as _usage_err:
                        logger.debug(f"usage logging skipped: {_usage_err}")
                    break
        except Exception as e:
            logger.error(f"Agent SDK query failed: {e}", exc_info=True)
            if full_response:
                full_response += "(Error: " + str(e)[:200] + ")"
            else:
                raise
        finally:
            # ハートビート停止
            _heartbeat_stop = True
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except (asyncio.CancelledError, Exception):
                pass
            # thinking内容が残っていれば最終表示
            if current_thinking and thinking_shown and progress_callback:
                done_header = self._thinking_done_header(status_header)
                msg = self._build_thinking_message(done_header, current_thinking)
                try:
                    await progress_callback(msg)
                except Exception:
                    pass
            # 停止ボタン用: thread_key の登録を解除（中断 or 正常終了どちらでも）
            if thread_key:
                self._active_sdk_clients.pop(thread_key, None)
            try:
                await client.disconnect()
            except Exception:
                pass
        # ── 失敗ナラティブ: 中断しても「どこまで終わったか」と「再開方法」を必ず語る ──
        # （2026-07-16 ハーネス改善: 定型エラー文で時間を溶かした後に何も残さないのが
        #   一番信頼を削る、というレビュー指摘への対応）
        if timeout_reason:
            partial = full_response.strip()
            if partial:
                full_response = (
                    partial
                    + f"\n\n⏰ ごめんなさい、ここで処理が中断しました（{timeout_reason}ため）☁️ "
                    + "上までは完了しています。このスレッドで「続きやって」と言えば続きから再開します。"
                )
            else:
                full_response = (
                    f"⏰ ごめんなさい、処理が途中で止まってしまいました（{timeout_reason}ため）☁️\n"
                    "このスレッドで「続きやって」と言ってもらえれば、ここまでの文脈を引き継いで再開します。"
                )
        # 2026-05-29 hotfix: 最終応答先頭の英語メタ独白をフェイルセーフ除去
        return strip_leading_english_meta(full_response.strip()) or "（応答を生成できませんでした）"

    def _extract_files_from_tool_result(self, raw_text: str):
        """ツール結果テキストからファイルパスを抽出してself._last_filesに追加"""
        import re as _re
        # JSON内の "file": "/path/to/file.ext" パターン
        try:
            data = json.loads(raw_text)
            if isinstance(data, dict):
                for key in ("file", "file_path", "path", "pptx_path", "output_path"):
                    val = data.get(key, "")
                    if val and isinstance(val, str) and os.path.isfile(val):
                        if val not in self._last_files:
                            self._last_files.append(val)
                            logger.info(f"[SDK] Extracted file from tool result: {val}")
        except (json.JSONDecodeError, ValueError):
            pass
        # フォールバック: 正規表現でファイルパスを抽出
        # 日本語ファイル名（【】や全角文字）にも対応するため、空白・引用符・改行・バッククォート以外を許容
        for match in _re.finditer(r'(/[^\s\'"<>`\n]+?\.(pptx|pdf|xlsx|docx|png|jpg|jpeg|csv|mp3|mp4|wav|m4a|txt|zip))', raw_text):
            fpath = match.group(1).strip().rstrip('.,;:)】」』')
            if os.path.isfile(fpath) and fpath not in self._last_files:
                self._last_files.append(fpath)
                logger.info(f"[SDK] Extracted file from tool result (regex): {fpath}")

    async def _call_via_cli(self, system_prompt, user_prompt, progress_callback=None) -> str:
        """CLI subprocess フォールバック"""
        if progress_callback:
            await progress_callback("🧠 考えています…")
        claude_bin = _get_claude_binary()
        cmd = [claude_bin, "-p", "--output-format", "json", "--max-turns", "10"]
        if self.model:
            cmd.extend(["--model", self.model])
        if self.mcp_config_path and os.path.exists(self.mcp_config_path):
            cmd.extend(["--mcp-config", self.mcp_config_path])
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(system_prompt)
            sys_file = f.name
        try:
            cmd.extend(["--system-prompt-file", sys_file])
            cmd.append(user_prompt)
            # NVMパスを環境変数に追加
            env = {**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL": "1"}
            nvm_bin = os.path.expanduser("~/.nvm/versions/node/v22.22.0/bin")
            if os.path.isdir(nvm_bin) and nvm_bin not in env.get("PATH", ""):
                env["PATH"] = nvm_bin + ":" + env.get("PATH", "")
            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=300)
            except asyncio.TimeoutError:
                logger.error("CLI timeout (300s) — killing subprocess to avoid zombie")
                try:
                    process.kill()
                    await process.wait()
                except Exception:
                    pass
                return "☁️ 処理がタイムアウトしました。もう一度お試しください。"
            if process.returncode != 0:
                err = stderr.decode("utf-8", errors="replace")[:500]
                logger.error(f"CLI failed (rc={process.returncode}): {err}")
                return f"☁️ 処理エラー: {err[:300]}"
            output = stdout.decode("utf-8", errors="replace")
            try:
                result = json.loads(output)
                if isinstance(result, dict):
                    return result.get("result", result.get("text", output))
                return str(result)
            except json.JSONDecodeError:
                return output.strip()
        finally:
            try:
                os.unlink(sys_file)
            except OSError:
                pass

    def _get_tool_status(self, tool_name: str) -> str:
        status_map = {
            "gsheets": "📊 スプレッドシートを確認中…",
            "perplexity": "🔍 Web検索しています…",
            "gsk_web_search": "🔍 Web検索しています…",
            "gsk_deep_research": "🔬 Deep Research実行中…（数分かかります）",
            "gemini": "🎨 画像を生成中…わくわく！",
            "weather": "🌤️ 天気情報を取得中…",
            "gsk_slides": "📊 スライドを作成中…（数分かかります）",
            "gsk_docs": "📝 ドキュメントを作成中…",
            "gsk_sheets": "📊 スプレッドシートを作成中…",
            "gsk_image": "🎨 画像を生成中…",
            "gsk_video": "🎬 動画を生成中…",
            "gsk_audio": "🔊 音声を生成中…",
            "gsk_crawl": "🌐 Webページを取得中…",
            "gsk_summarize": "📋 要約しています…",
            "gsk_cross_check": "✅ ファクトチェック中…",
        }
        tn = tool_name.lower()
        for key, msg in status_map.items():
            if key in tn:
                return msg
        if "search" in tn or "web" in tn:
            return "🔍 検索しています…"
        return f"🔧 {tool_name} を実行中…"

    def _get_tool_done_label(self, tool_name: str) -> str:
        """ツール完了時の短いラベルを返す"""
        done_map = {
            "gsk_web_search": "Web検索完了",
            "gsk_deep_research": "Deep Research完了",
            "gsk_image": "画像生成完了",
            "gsk_slides": "スライド生成完了",
            "gsk_docs": "ドキュメント生成完了",
            "gsk_sheets": "スプレッドシート生成完了",
            "gsk_crawl": "Webページ取得完了",
            "gsk_summarize": "要約完了",
            "gsk_cross_check": "ファクトチェック完了",
            "gsheets": "スプレッドシート確認完了",
            "perplexity": "Web検索完了",
            "weather": "天気情報取得完了",
        }
        tn = tool_name.lower()
        for key, label in done_map.items():
            if key in tn:
                return label
        return f"{tool_name} 完了"

    def clear_conversation(self, channel, thread_ts=None):
        """セッションキャッシュをクリア（互換性維持）。

        2026-05-29: 文脈リセット機能から呼ばれる。次の対話で新 session_id 発行。
        """
        thread_key = f"{channel}:{thread_ts}" if thread_ts else channel
        self._thread_sessions.delete(thread_key)
        logger.info(f"[Reset] cleared session for {thread_key}")

    async def cancel_thread(self, thread_key: str) -> bool:
        """停止ボタン: thread_key で稼働中の SDK Client を disconnect で中断。

        2026-05-29: ESC 相当の停止機能。
        - 戻り値 True: 中断成功（稼働 client があった）
        - 戻り値 False: そもそも稼働中の client がなかった
        - 部分出力は破棄される設計（中途半端な状態を残さない方が安全）
        """
        client = self._active_sdk_clients.get(thread_key)
        if client is None:
            logger.info(f"[Stop] no active client for {thread_key}")
            return False
        try:
            await client.disconnect()
            logger.info(f"[Stop] disconnected client for {thread_key}")
            return True
        except Exception as e:
            logger.warning(f"[Stop] disconnect failed for {thread_key}: {e}")
            return False
        finally:
            self._active_sdk_clients.pop(thread_key, None)



def _find_claude_binary() -> str:
    """利用可能なclaude CLIバイナリのパスを検索して返す"""
    import shutil

    # 0. 明示指定（CLAUDE_CLI_PATH）。SDK 同梱の CLI が古くて新モデルに対応しない時に、
    #    ネイティブ版(~/.local/bin/claude、自動更新あり)を指す。認証は .env の CLAUDE_CODE_OAUTH_TOKEN で通る
    explicit = os.getenv("CLAUDE_CLI_PATH", "")
    if explicit and os.path.isfile(explicit) and os.access(explicit, os.X_OK):
        logger.info(f"[ShimCLI] Using CLAUDE_CLI_PATH: {explicit}")
        return explicit

    # 1. Agent SDKバンドルCLI（最優先: 確実にAuth済み）
    try:
        import claude_agent_sdk
        sdk_dir = os.path.dirname(os.path.abspath(claude_agent_sdk.__file__))
        bundled = os.path.join(sdk_dir, "_bundled", "claude")
        if os.path.isfile(bundled) and os.access(bundled, os.X_OK):
            logger.info(f"[ShimCLI] Using bundled CLI: {bundled}")
            return bundled
    except ImportError:
        pass

    # 2. NVMパス配下のclaude（Maxサブスク）
    nvm_claude = os.path.expanduser("~/.nvm/versions/node/v22.22.0/bin/claude")
    if os.path.isfile(nvm_claude) and os.access(nvm_claude, os.X_OK):
        logger.info(f"[ShimCLI] Using NVM CLI: {nvm_claude}")
        return nvm_claude

    # 3. NVMの他のバージョンも探す
    nvm_dir = os.path.expanduser("~/.nvm/versions/node")
    if os.path.isdir(nvm_dir):
        for ver in sorted(os.listdir(nvm_dir), reverse=True):
            candidate = os.path.join(nvm_dir, ver, "bin", "claude")
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                logger.info(f"[ShimCLI] Using NVM CLI (auto-detected): {candidate}")
                return candidate

    # 4. PATHから検索（フォールバック）
    found = shutil.which("claude")
    if found:
        logger.info(f"[ShimCLI] Using PATH CLI: {found}")
        return found

    logger.error("[ShimCLI] claude binary NOT FOUND anywhere!")
    return "claude"  # 最後の手段（FileNotFoundErrorになるが呼び出し側でキャッチ）


# モジュール起動時に一度だけ検索してキャッシュ
_CLAUDE_BINARY: Optional[str] = None

def _get_claude_binary() -> str:
    global _CLAUDE_BINARY
    if _CLAUDE_BINARY is None:
        _CLAUDE_BINARY = _find_claude_binary()
    return _CLAUDE_BINARY


class _CLIClientShim:
    """ai_engine.client 互換シム（軽量タスク用CLI呼び出し）"""
    def __init__(self, engine):
        self._engine = engine
        self.messages = _MessagesShim(engine)


class _AsyncCLIClientShim:
    """ai_engine.async_client 互換シム"""
    def __init__(self, engine):
        self._engine = engine
        self.messages = _AsyncMessagesShim(engine)


class _MessagesShim:
    """anthropic.Anthropic().messages.create() 互換"""
    def __init__(self, engine):
        self._engine = engine

    def create(self, **kwargs):
        prompt = ""
        messages = kwargs.get("messages", [])
        system = kwargs.get("system", "")
        for msg in messages:
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    prompt += content + "\n"
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            prompt += block.get("text", "") + "\n"
        if not prompt.strip():
            prompt = "（入力なし）"
        model = kwargs.get("model", "claude-haiku-4-5-20251001")
        # モデルに応じてタイムアウトを動的に設定
        # 公式Xスケジューラー等の長いプロンプトでSonnetが120秒でタイムアウトしていたため引き上げ
        if "opus" in model:
            cli_timeout = 300
        elif "sonnet" in model:
            cli_timeout = 300
        else:
            cli_timeout = 120
        claude_bin = _get_claude_binary()
        cmd = [claude_bin, "-p", "--model", model, "--output-format", "json"]
        import tempfile
        sys_file = None
        if system:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
                f.write(system if isinstance(system, str) else str(system))
                sys_file = f.name
            cmd.extend(["--system-prompt-file", sys_file])
        cmd.append(prompt.strip())
        try:
            # NVMパスを環境変数に追加（子プロセスでも使えるように）
            env = {**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL": "1"}
            nvm_bin = os.path.expanduser("~/.nvm/versions/node/v22.22.0/bin")
            if os.path.isdir(nvm_bin) and nvm_bin not in env.get("PATH", ""):
                env["PATH"] = nvm_bin + ":" + env.get("PATH", "")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=cli_timeout, env=env)
            if result.returncode != 0:
                stderr = result.stderr.strip()
                logger.error(f"[ShimCLI] CLI failed (rc={result.returncode}): {stderr[:300]}")
                text = f"(CLI error: {stderr[:200]})" if stderr else "(CLI returned empty)"
            else:
                text = result.stdout.strip()
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    text = parsed.get("result", text)
            except json.JSONDecodeError:
                pass
        except FileNotFoundError:
            logger.error(f"[ShimCLI] claude binary not found at: {claude_bin}")
            text = "(claude CLI not found)"
        except subprocess.TimeoutExpired:
            logger.error(f"[ShimCLI] CLI timed out after {cli_timeout}s (model={model})")
            text = "(CLI timeout)"
        except Exception as e:
            logger.error(f"[ShimCLI] Unexpected error: {e}", exc_info=True)
            text = f"Error: {e}"
        finally:
            if sys_file:
                try:
                    os.unlink(sys_file)
                except OSError:
                    pass
        return _FakeResponse(text)


class _AsyncMessagesShim:
    """非同期版 messages.create() 互換"""
    def __init__(self, engine):
        self._engine = engine

    async def create(self, **kwargs):
        loop = asyncio.get_event_loop()
        shim = _MessagesShim(self._engine)
        return await loop.run_in_executor(None, lambda: shim.create(**kwargs))


class _FakeResponse:
    """anthropic APIレスポンス最小互換"""
    def __init__(self, text):
        self.content = [_FakeTextBlock(text)]
        self.stop_reason = "end_turn"
        self.model = "claude-code"
        self.usage = _FakeUsage()


class _FakeTextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeUsage:
    def __init__(self):
        self.input_tokens = 0
        self.output_tokens = 0


# ============================================================
# SDK query() ベースのワンショットLLM呼び出し（CLI shim代替）
# ============================================================

async def sdk_query(
    prompt: str,
    model: str = models.LIGHT,
    system_prompt: str = "",
    max_turns: int = 1,
    effort: str = "",
) -> str:
    """SDK query()経由の非同期LLM呼び出し。CLI shimのsubprocess.runを置き換える。

    メリット:
    - subprocess起動オーバーヘッドなし
    - タイムアウトはSDK内部で管理（72分タイムアウト問題を解消）
    - ネイティブ非同期（run_in_executor不要）
    - ANTHROPIC_API_KEY不要（Max月額、CLI経由で動作）
    """
    try:
        from claude_agent_sdk import query as _query, ClaudeAgentOptions
        from claude_agent_sdk.types import ResultMessage, AssistantMessage, TextBlock

        options = ClaudeAgentOptions(
            model=model,
            system_prompt=system_prompt if system_prompt else None,
            max_turns=max_turns,
            permission_mode="bypassPermissions",
            effort=effort or None,
            # CLAUDE_CLI_PATH が設定されていれば同梱 CLI ではなくそれを使う(新モデル対応のため)
            cli_path=os.getenv("CLAUDE_CLI_PATH") or None,
            # cloud MCPの混入を防ぐ（context overflow対策）
            extra_args={"strict-mcp-config": None},
        )

        result_text = ""
        async for message in _query(prompt=prompt, options=options):
            if isinstance(message, ResultMessage):
                result_text = message.result or ""
                break
            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        result_text += block.text

        logger.info(f"[sdk_query] model={model} prompt_len={len(prompt)} result_len={len(result_text)}")
        return result_text.strip()

    except Exception as e:
        logger.error(f"[sdk_query] failed (model={model}): {e}", exc_info=True)
        # フォールバック: CLI shim
        logger.info("[sdk_query] falling back to CLI shim")
        try:
            loop = asyncio.get_event_loop()
            shim = _MessagesShim(None)
            response = await loop.run_in_executor(
                None,
                lambda: shim.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    system=system_prompt,
                ),
            )
            return response.content[0].text.strip()
        except Exception as fallback_err:
            logger.error(f"[sdk_query] CLI shim fallback also failed: {fallback_err}")
            return f"(error: {e})"


async def sdk_query_as_response(
    prompt: str,
    model: str = models.LIGHT,
    system_prompt: str = "",
    **kwargs,
) -> "_FakeResponse":
    """sdk_query() の結果を _FakeResponse でラップ（既存 .content[0].text 互換）"""
    text = await sdk_query(prompt, model, system_prompt, **kwargs)
    return _FakeResponse(text)
