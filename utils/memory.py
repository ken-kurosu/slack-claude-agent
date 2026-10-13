"""
記憶・学習システム: 会話コンテキストを蓄積してエージェントを賢くする

3層構造:
1. 短期記憶 (Short-term): チャンネル/スレッド単位の会話履歴 (AIEngine側で管理)
2. ワーキングメモリ (Working): チャンネル単位のトピック・文脈要約
3. 長期記憶 (Long-term): ユーザー特性、チーム知識、過去の成功パターン
"""
import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from config import models

logger = logging.getLogger(__name__)

# 永続化ファイル
DEFAULT_MEMORY_PATH = "/tmp/slack-agent-workspace/memory.json"


class Memory:
    """エージェントの記憶を管理"""

    def __init__(self, persist_path: str = DEFAULT_MEMORY_PATH):
        self.persist_path = persist_path

        # ── ワーキングメモリ: チャンネルごとの直近コンテキスト ──
        # {channel_id: {"topics": [...], "recent_summary": str, "updated_at": float}}
        self.channel_context: Dict[str, dict] = {}

        # ── 長期記憶: ユーザー情報 ──
        # {user_id: {"name": str, "skills": [...], "preferences": {...}, "interaction_count": int}}
        self.user_profiles: Dict[str, dict] = {}

        # ── 長期記憶: チーム知識 ──
        # [{"fact": str, "source": str, "confidence": float, "created_at": float}]
        self.team_knowledge: List[dict] = []

        # ── 長期記憶: 介入パターン学習 ──
        # [{"message_type": str, "action_taken": str, "was_helpful": bool, "context": str}]
        self.intervention_patterns: List[dict] = []

        # ── 長期記憶: タスク成功パターン ──
        # [{"task_type": str, "approach": str, "tools_used": [...], "success": bool}]
        self.task_patterns: List[dict] = []

        # ── 長期記憶: 満足度フィードバック ──
        # [{"score": int(1-5), "user": str, "channel": str, "thread_ts": str, "created_at": float}]
        self.feedback_scores: List[dict] = []

        # ── 会話ログ（タイムスタンプ付き） ──
        # エージェントとの対話を時系列で記録。「おとといの話」「先週のあの件」に対応
        self.conversation_log: List[dict] = []

        # ── プロジェクト単位の長期記憶 ──
        # {project_name: {"decisions": [...], "open_questions": [...], "key_context": [...], ...}}
        self.project_memory: Dict[str, dict] = {}

        # ── チャンネル横断ナレッジインデックス ──
        # [{"text": str, "channel": str, "user": str, "timestamp": float,
        #   "keywords": [...], "importance": float}]
        self.knowledge_index: List[dict] = []

        # ── ユーザー指示メモ（CLAUDE.md相当） ──
        # ユーザーが「覚えて」「メモして」で保存する永続的な指示・情報
        # [{"text": str, "user": str, "category": str, "created_at": float, "active": bool}]
        self.user_notes: List[dict] = []

        # ── 会話フィードバック履歴 ──
        # [{"user": str, "channel": str, "score": int, "text_snippet": str,
        #   "response_snippet": str, "timestamp": float, "feedback_type": str}]
        self.conversation_feedback: List[dict] = []

        # ── 会話参加者追跡 ──
        # {channel:thread_ts -> {"participants": {user_id: {"last_msg": float, "msg_count": int}},
        #                         "mention_graph": [(from_user, to_user, timestamp), ...],
        #                         "initiator": user_id}}
        self.thread_participants: Dict[str, dict] = {}

        # ── ユーザー名キャッシュ（Slack user ID → 表示名） ──
        # {user_id: {"display_name": str, "real_name": str, "resolved_at": float}}
        self.user_name_cache: Dict[str, dict] = {}

        # ── Markdownベースのナレッジストア（追加レイヤー） ──
        # 記憶ストアはデータフォルダと同じ階層の knowledge/ に一本化。
        # これで save_knowledge 等の書き込みが knowledge_organizer/find_relevant_nodes と
        # 同じトピックツリーに入り、書き込みと読み出し経路が一致する。
        from utils.knowledge_store import KnowledgeStore
        knowledge_dir = os.path.join(os.path.dirname(os.path.dirname(persist_path)), "knowledge")
        self.knowledge_store = KnowledgeStore(base_dir=knowledge_dir)

        self._load()

    # ──────────────────────────────────────────────
    # ワーキングメモリ (チャンネルコンテキスト)
    # ──────────────────────────────────────────────
    def update_channel_context(self, channel: str, message: str, user: str):
        """チャンネルの会話コンテキストを更新"""
        if channel not in self.channel_context:
            self.channel_context[channel] = {
                "topics": [],
                "recent_messages": [],
                "active_users": {},
                "updated_at": time.time(),
            }

        ctx = self.channel_context[channel]

        # 直近メッセージを保持（最大30件）
        ctx["recent_messages"].append({
            "user": user,
            "text": message[:300],  # 省メモリのため切り詰め
            "ts": time.time(),
        })
        if len(ctx["recent_messages"]) > 30:
            ctx["recent_messages"] = ctx["recent_messages"][-30:]

        # アクティブユーザーの発言回数
        ctx["active_users"][user] = ctx["active_users"].get(user, 0) + 1
        ctx["updated_at"] = time.time()

    def get_channel_summary(self, channel: str) -> str:
        """チャンネルの直近コンテキストをテキストで返す"""
        ctx = self.channel_context.get(channel)
        if not ctx or not ctx.get("recent_messages"):
            return ""

        recent = ctx["recent_messages"][-10:]  # 直近10件
        lines = [f"- {self.get_cached_user_name(m['user'])}: {m['text'][:100]}" for m in recent]

        active = sorted(ctx["active_users"].items(), key=lambda x: -x[1])[:5]
        active_str = ", ".join(f"{self.get_cached_user_name(u)}({c}回)" for u, c in active)

        return (
            f"## このチャンネルの直近の文脈\n"
            f"アクティブユーザー: {active_str}\n"
            f"直近の会話:\n" + "\n".join(lines)
        )

    # ──────────────────────────────────────────────
    # ユーザー名解決
    # ──────────────────────────────────────────────
    async def resolve_user_name(self, user_id: str, slack_client=None) -> str:
        """Slack user IDを表示名に解決する（キャッシュ付き）"""
        # キャッシュチェック（24時間有効）
        cached = self.user_name_cache.get(user_id)
        if cached and (time.time() - cached.get("resolved_at", 0)) < 86400:
            return cached.get("display_name") or cached.get("real_name") or user_id

        if not slack_client:
            return user_id

        try:
            result = await slack_client.users_info(user=user_id)
            user_info = result.get("user", {})
            profile = user_info.get("profile", {})

            display_name = profile.get("display_name", "").strip()
            real_name = profile.get("real_name", "").strip()
            # display_name優先、なければreal_name
            resolved_name = display_name or real_name or user_id

            self.user_name_cache[user_id] = {
                "display_name": display_name,
                "real_name": real_name,
                "resolved_at": time.time(),
            }

            # user_profilesにも反映
            prof = self.get_user_profile(user_id)
            if not prof.get("name") or prof["name"] == user_id:
                prof["name"] = resolved_name

            logger.info(f"Resolved user {user_id} -> {resolved_name}")
            return resolved_name
        except Exception as e:
            logger.debug(f"Failed to resolve user name for {user_id}: {e}")
            return user_id

    def get_cached_user_name(self, user_id: str) -> str:
        """キャッシュ済みのユーザー名を取得（同期版、API呼び出しなし）"""
        cached = self.user_name_cache.get(user_id)
        if cached:
            return cached.get("display_name") or cached.get("real_name") or user_id
        # user_profilesのnameもフォールバック
        prof = self.user_profiles.get(user_id, {})
        if prof.get("name"):
            return prof["name"]
        return user_id

    # ──────────────────────────────────────────────
    # ユーザープロファイル
    # ──────────────────────────────────────────────
    def get_user_profile(self, user_id: str) -> dict:
        """ユーザープロファイルを取得"""
        if user_id not in self.user_profiles:
            self.user_profiles[user_id] = {
                "name": "",
                "skills": [],
                "preferences": {},
                "frequent_requests": [],
                "interaction_count": 0,
                "last_interaction": 0,
            }
        return self.user_profiles[user_id]

    def record_interaction(self, user_id: str, request_type: str, name: str = ""):
        """ユーザーとのやりとりを記録"""
        profile = self.get_user_profile(user_id)
        profile["interaction_count"] += 1
        profile["last_interaction"] = time.time()
        if name and not profile["name"]:
            profile["name"] = name

        # 頻出リクエストタイプを記録
        freq = profile["frequent_requests"]
        freq.append(request_type)
        if len(freq) > 50:
            profile["frequent_requests"] = freq[-50:]

    def update_user_preference(self, user_id: str, key: str, value):
        """ユーザーの好みを記録（言語、呼び方、etc）"""
        profile = self.get_user_profile(user_id)
        profile["preferences"][key] = value

    def get_user_context(self, user_id: str) -> str:
        """AIに渡すユーザーコンテキスト文字列を生成（パーソナライズ対応）"""
        profile = self.user_profiles.get(user_id)
        # プロフィール未作成でもキャッシュされた名前があれば返す
        cached_name = self.get_cached_user_name(user_id)
        if (not profile or profile["interaction_count"] == 0) and cached_name == user_id:
            return ""

        parts = []
        # 名前: キャッシュ名 > プロフィール名
        display_name = cached_name if cached_name != user_id else (profile or {}).get("name", "")
        if display_name:
            parts.append(f"名前: {display_name}（この名前で呼んでください）")

        # 役割・専門度
        role = profile.get("role", "")
        expertise = profile.get("expertise_level", "")
        if role or expertise:
            parts.append(f"役割: {role or '不明'} / レベル: {expertise or '不明'}")

        parts.append(f"過去の対話回数: {profile['interaction_count']}")

        if profile.get("preferences"):
            prefs = ", ".join(f"{k}={v}" for k, v in profile["preferences"].items())
            parts.append(f"好み: {prefs}")

        if profile.get("frequent_requests"):
            from collections import Counter
            top = Counter(profile["frequent_requests"]).most_common(3)
            top_str = ", ".join(f"{t}({c}回)" for t, c in top)
            parts.append(f"よく依頼するタスク: {top_str}")

        # 自動学習パターン
        auto = profile.get("auto_learned", {})
        if auto:
            hints = []
            if auto.get("prefers_detailed", 0) > 3:
                hints.append("詳細な回答を好む")
            if auto.get("prefers_code_examples", 0) > 3:
                hints.append("コード例があると喜ぶ")
            if auto.get("prefers_brief", 0) > 3:
                hints.append("簡潔な回答を好む")
            if hints:
                parts.append(f"学習済みの傾向: {', '.join(hints)}")

        # 満足度
        avg_sat = profile.get("avg_satisfaction")
        if avg_sat:
            parts.append(f"平均満足度: {avg_sat:.1f}/5")

        # パーソナライズ指示
        style = profile.get("preferred_style", "")
        if style:
            style_map = {
                "detailed": "→ 技術的に正確で詳しい回答を心がけてください",
                "brief": "→ 簡潔で要点を絞った回答を心がけてください",
                "structured": "→ 見出しやリスト形式で整理された回答を心がけてください",
            }
            instruction = style_map.get(style, "")
            if instruction:
                parts.append(instruction)

        # トーン指示
        tone = profile.get("preferred_tone", "")
        if tone == "formal":
            parts.append("→ 丁寧語・敬語で回答してください（このユーザーはフォーマルな文体を好みます）")
        elif tone == "casual":
            # タメ語はオーナー（AGENT_OWNER_SLACK_ID）のみ。他ユーザーには敬語指示に切り替える。
            _owner = os.getenv("AGENT_OWNER_SLACK_ID", "")
            if _owner and user_id == _owner:
                parts.append("→ カジュアルで親しみやすい文体で回答してください（「〜だよ」「〜だね」等OK）")
            else:
                parts.append("→ 親しみのある敬語で回答してください（タメ語は使わない）")

        # メッセージ長さの傾向に合わせる
        avg_msg = auto.get("avg_msg_length", 0)
        if avg_msg > 0 and avg_msg < 50:
            parts.append("→ このユーザーは短文派です。回答も簡潔にしてください")
        elif avg_msg > 200:
            parts.append("→ このユーザーは詳細に説明するタイプです。充分な情報量で回答してください")

        # 絵文字の使い方
        total = auto.get("total_analyzed", 0)
        emoji_ct = auto.get("emoji_usage_count", 0)
        if total > 5:
            emoji_rate = emoji_ct / total
            if emoji_rate > 0.4:
                parts.append("→ 絵文字をよく使うユーザーです。回答にも適度に絵文字を含めてOK")
            elif emoji_rate < 0.1:
                parts.append("→ 絵文字をほぼ使わないユーザーです。回答も絵文字控えめに")

        return "\n".join(parts)

    def auto_learn_user_style(self, user: str, text: str, response: str, score: Optional[int] = None):
        """会話パターンからユーザースタイルを包括的に自動学習"""
        profile = self.get_user_profile(user)
        auto = profile.setdefault("auto_learned", {})

        # ── ユーザーのメッセージ長さを追跡 ──
        msg_lengths = auto.setdefault("msg_lengths", [])
        msg_lengths.append(len(text))
        if len(msg_lengths) > 50:
            auto["msg_lengths"] = msg_lengths[-50:]
        avg_len = sum(auto["msg_lengths"]) / len(auto["msg_lengths"])
        auto["avg_msg_length"] = avg_len

        # ── 文体分析 ──
        # 絵文字使用率
        import re as _re
        emoji_count = len(_re.findall(r'[\U0001F300-\U0001F9FF\u2600-\u26FF\u2700-\u27BF]', text))
        emoji_uses = auto.get("emoji_usage_count", 0)
        total_msgs = auto.get("total_analyzed", 0)
        auto["emoji_usage_count"] = emoji_uses + (1 if emoji_count > 0 else 0)
        auto["total_analyzed"] = total_msgs + 1

        # 敬語 vs カジュアル
        formal_markers = ["ございます", "いただ", "存じ", "です。", "ます。", "でしょうか", "お願いいたし"]
        casual_markers = ["〜", "w", "笑", "だよ", "だね", "じゃん", "っす", "してー", "して！", "やって"]
        formal_count = sum(1 for m in formal_markers if m in text)
        casual_count = sum(1 for m in casual_markers if m in text)
        auto["formal_score"] = auto.get("formal_score", 0) + formal_count
        auto["casual_score"] = auto.get("casual_score", 0) + casual_count

        # 疑問文率（質問が多いか指示が多いか）
        if text.rstrip().endswith("？") or text.rstrip().endswith("?"):
            auto["question_count"] = auto.get("question_count", 0) + 1
        else:
            auto["command_count"] = auto.get("command_count", 0) + 1

        # リクエストタイプの推定
        if any(w in text for w in ["コード", "実装", "バグ", "エラー", "関数"]):
            auto["prefers_code_examples"] = auto.get("prefers_code_examples", 0) + 1
        if any(w in text for w in ["要約", "まとめ", "サマリー", "簡単に", "手短に", "一言で"]):
            auto["prefers_brief"] = auto.get("prefers_brief", 0) + 1
        if any(w in text for w in ["詳しく", "詳細", "ステップバイステップ", "具体的に"]):
            auto["prefers_detailed"] = auto.get("prefers_detailed", 0) + 1

        # 高スコア時のパターン記録
        if score and score >= 4:
            if len(response) > 500:
                auto["prefers_detailed"] = auto.get("prefers_detailed", 0) + 1
            if "```" in response:
                auto["prefers_code_examples"] = auto.get("prefers_code_examples", 0) + 1
            if len(response) < 200:
                auto["prefers_brief"] = auto.get("prefers_brief", 0) + 1

        # ── 自動スタイル推定 ──
        detailed_score = auto.get("prefers_detailed", 0) + auto.get("prefers_code_examples", 0)
        brief_score = auto.get("prefers_brief", 0)
        if detailed_score > brief_score + 3:
            profile["preferred_style"] = "detailed"
        elif brief_score > detailed_score + 3:
            profile["preferred_style"] = "brief"

        # ── トーン推定 ──
        f = auto.get("formal_score", 0)
        c = auto.get("casual_score", 0)
        if f > c + 5:
            profile["preferred_tone"] = "formal"
        elif c > f + 5:
            profile["preferred_tone"] = "casual"
        else:
            profile["preferred_tone"] = "neutral"

        # 満足度の移動平均
        if score:
            old_avg = profile.get("avg_satisfaction", 3.0)
            profile["avg_satisfaction"] = old_avg * 0.8 + score * 0.2

    # ──────────────────────────────────────────────
    # 会話ログ（タイムスタンプ付き長期記録）
    # ──────────────────────────────────────────────
    def log_conversation(
        self, user: str, channel: str, text: str,
        response_summary: str = "", thread_ts: str = "", topics: Optional[List[str]] = None
    ):
        """エージェントとの対話を時系列で記録"""
        now = time.time()
        self.conversation_log.append({
            "user": user,
            "channel": channel,
            "text": text[:1000],
            "response_summary": response_summary[:1000],
            "timestamp": now,
            "date": datetime.fromtimestamp(now).strftime("%Y-%m-%d"),
            "weekday": datetime.fromtimestamp(now).strftime("%A"),
            "topics": topics or [],
            "thread_ts": thread_ts,
        })
        # 最大500件保持
        if len(self.conversation_log) > 500:
            self.conversation_log = self.conversation_log[-500:]

    def search_conversation_log(
        self, query: str = "", user: str = "", days_ago: Optional[int] = None, limit: int = 10
    ) -> List[dict]:
        """会話ログを検索。時間範囲・ユーザー・キーワードで絞り込み"""
        results = self.conversation_log

        # 時間範囲フィルタ
        if days_ago is not None:
            cutoff = time.time() - (days_ago * 86400)
            results = [r for r in results if r["timestamp"] >= cutoff]

        # ユーザーフィルタ
        if user:
            results = [r for r in results if r["user"] == user]

        # キーワードフィルタ（日本語対応 bigram。旧 split() は日本語を引けなかった）
        if query:
            from utils import text_match
            q_tokens = text_match.tokenize(query)
            if q_tokens:
                scored = []
                for r in results:
                    searchable = f"{r['text']} {r['response_summary']} {' '.join(r.get('topics', []))}"
                    overlap = len(q_tokens & text_match.tokenize(searchable))
                    if overlap > 0:
                        scored.append((overlap, r))
                scored.sort(key=lambda x: -x[0])
                results = [item[1] for item in scored]

        return results[-limit:] if not query else results[:limit]

    def get_conversation_history_context(self, user: str, query: str = "") -> str:
        """AIに渡す過去の会話履歴コンテキストを生成（ユーザー別・要約拡大版）

        長期コンテキスト: このユーザーとの過去のやり取りを500文字まで表示。
        別ユーザーの関連会話はキーワード一致時のみ低優先で付加。
        """
        # 直近14日間のこのユーザーとの対話を取得（拡大: 7日→14日）
        recent = self.search_conversation_log(user=user, days_ago=14, limit=15)
        # クエリがあればそれでも検索（全期間・ユーザー問わず）
        if query:
            keyword_results = self.search_conversation_log(query=query, limit=5)
            # 重複排除してマージ
            seen_ts = {r["timestamp"] for r in recent}
            for r in keyword_results:
                if r["timestamp"] not in seen_ts:
                    recent.append(r)

        if not recent:
            return ""

        # 時系列でソート
        recent.sort(key=lambda x: x["timestamp"])

        lines = []
        for r in recent:
            dt = datetime.fromtimestamp(r["timestamp"])
            relative = self._relative_time(r["timestamp"])
            date_str = dt.strftime("%m/%d %H:%M")
            topics_str = f" [{', '.join(r['topics'])}]" if r.get("topics") else ""
            summary = r.get("response_summary", "")
            # 表示ボリューム拡大: 100→500文字
            user_text = r['text'][:500]
            resp_text = summary[:500] if summary else ""
            is_same_user = r.get("user") == user
            prefix = "★" if is_same_user else "◇"  # ★=本人, ◇=他ユーザーの関連
            entry = f"- {prefix} {date_str}（{relative}）{topics_str}: {user_text}"
            if resp_text:
                entry += f"\n  → {resp_text}"
            lines.append(entry)

        return "## 過去の会話履歴（★=あなた、◇=関連する他の会話）\n" + "\n".join(lines)

    @staticmethod
    def _relative_time(timestamp: float) -> str:
        """タイムスタンプを「2日前」「先週」などの相対表現に変換"""
        now = time.time()
        diff = now - timestamp
        if diff < 3600:
            return f"{int(diff / 60)}分前"
        elif diff < 86400:
            return f"{int(diff / 3600)}時間前"
        elif diff < 86400 * 2:
            return "昨日"
        elif diff < 86400 * 3:
            return "おととい"
        elif diff < 86400 * 7:
            return f"{int(diff / 86400)}日前"
        elif diff < 86400 * 14:
            return "先週"
        elif diff < 86400 * 30:
            return f"{int(diff / (86400 * 7))}週間前"
        else:
            return f"{int(diff / (86400 * 30))}ヶ月前"

    # ──────────────────────────────────────────────
    # チーム知識
    # ──────────────────────────────────────────────
    def add_team_knowledge(self, fact: str, source: str = "", confidence: float = 0.8):
        """チームの知識を追加"""
        # 重複チェック（類似度は簡易的にexact match）
        for existing in self.team_knowledge:
            if existing["fact"] == fact:
                existing["confidence"] = max(existing["confidence"], confidence)
                return

        self.team_knowledge.append({
            "fact": fact,
            "source": source,
            "confidence": confidence,
            "created_at": time.time(),
        })

        # 最大200件
        if len(self.team_knowledge) > 200:
            # 古い低信頼度のものから削除
            self.team_knowledge.sort(key=lambda x: (x["confidence"], x["created_at"]))
            self.team_knowledge = self.team_knowledge[-200:]

    def get_relevant_knowledge(self, query: str, limit: int = 5) -> List[dict]:
        """クエリに関連する知識を返す（日本語対応 bigram マッチ）"""
        from utils import text_match
        q_tokens = text_match.tokenize(query)
        if not q_tokens:
            return []
        scored = []
        for k in self.team_knowledge:
            # 単語の重なりでスコアリング（旧 split() は日本語が引けなかった）
            overlap = len(q_tokens & text_match.tokenize(k["fact"]))
            if overlap > 0:
                scored.append((overlap * k["confidence"], k))

        scored.sort(key=lambda x: -x[0])
        return [item[1] for item in scored[:limit]]

    def get_knowledge_context(self, query: str) -> str:
        """AIに渡す関連知識文字列を生成"""
        relevant = self.get_relevant_knowledge(query)
        if not relevant:
            return ""
        lines = [f"- {k['fact']}" for k in relevant]
        return "## 蓄積された関連知識\n" + "\n".join(lines)

    # ──────────────────────────────────────────────
    # 介入パターン学習
    # ──────────────────────────────────────────────
    def record_intervention(
        self, message_summary: str, action: str, was_helpful: Optional[bool] = None, skill: str = ""
    ):
        """介入判断の結果を記録"""
        self.intervention_patterns.append({
            "message_summary": message_summary[:200],
            "action_taken": action,
            "skill": skill,
            "was_helpful": was_helpful,
            "feedback": None,  # good/bad フィードバック
            "feedback_user": None,
            "score": None,  # 後からfeedbackで紐付け
            "created_at": time.time(),
        })
        if len(self.intervention_patterns) > 200:
            self.intervention_patterns = self.intervention_patterns[-200:]

    def record_intervention_feedback(
        self, message_summary: str, skill: str, feedback: str, user: str
    ):
        """介入フィードバック（good/bad）を記録し、直近の同スキル介入に紐付け"""
        # 直近の同スキル介入パターンを探して紐付け
        for pattern in reversed(self.intervention_patterns):
            if pattern.get("skill") == skill and pattern.get("feedback") is None:
                pattern["feedback"] = feedback
                pattern["feedback_user"] = user
                pattern["was_helpful"] = feedback == "good"
                logger.info(f"Intervention feedback recorded: skill={skill}, feedback={feedback}")
                break
        else:
            # マッチしない場合は新規エントリとして記録
            self.intervention_patterns.append({
                "message_summary": message_summary[:200],
                "action_taken": "feedback_only",
                "skill": skill,
                "was_helpful": feedback == "good",
                "feedback": feedback,
                "feedback_user": user,
                "score": None,
                "created_at": time.time(),
            })

    def mark_intervention_helpful(self, helpful: bool):
        """直近の介入が役に立ったかフィードバック（テキストベース）"""
        if self.intervention_patterns:
            self.intervention_patterns[-1]["was_helpful"] = helpful

    def record_feedback(self, score: int, user: str, channel: str, thread_ts: str):
        """5段階満足度スコアを記録し、直近の介入パターンにも紐付ける"""
        self.feedback_scores.append({
            "score": score,
            "user": user,
            "channel": channel,
            "thread_ts": thread_ts,
            "created_at": time.time(),
        })
        if len(self.feedback_scores) > 500:
            self.feedback_scores = self.feedback_scores[-500:]

        # 直近の介入パターンにスコアを紐付け
        if self.intervention_patterns:
            latest = self.intervention_patterns[-1]
            latest["score"] = score
            latest["was_helpful"] = score >= 4  # 4以上を「役に立った」とみなす

    def get_intervention_stats(self) -> dict:
        """介入パターンの統計を返す"""
        if not self.intervention_patterns:
            return {"total": 0}

        total = len(self.intervention_patterns)
        actions = defaultdict(int)
        scored = [p for p in self.intervention_patterns if p.get("score") is not None]
        scores = [p["score"] for p in scored]

        for p in self.intervention_patterns:
            actions[p["action_taken"]] += 1

        return {
            "total": total,
            "actions": dict(actions),
            "scored_count": len(scored),
            "avg_score": sum(scores) / len(scores) if scores else 0,
            "high_satisfaction": sum(1 for s in scores if s >= 4),
            "low_satisfaction": sum(1 for s in scores if s <= 2),
        }

    def get_feedback_summary(self) -> dict:
        """フィードバック全体の統計"""
        if not self.feedback_scores:
            return {"total": 0, "avg": 0}
        scores = [f["score"] for f in self.feedback_scores]
        recent = [f["score"] for f in self.feedback_scores[-20:]]
        return {
            "total": len(scores),
            "avg": sum(scores) / len(scores),
            "recent_avg": sum(recent) / len(recent) if recent else 0,
            "trend": "improving" if len(recent) >= 5 and sum(recent[-5:]) / 5 > sum(recent[:5]) / max(len(recent[:5]), 1) else "stable",
        }

    def get_intervention_context(self) -> str:
        """介入判断に役立つ過去のパターン文字列（スコアベース）"""
        # スコア付きの介入を抽出
        with_score = [
            p for p in self.intervention_patterns if p.get("score") is not None
        ]
        # スコアなしだがwas_helpfulがあるものも
        with_feedback = with_score or [
            p for p in self.intervention_patterns if p.get("was_helpful") is not None
        ]
        if not with_feedback:
            return ""

        # 直近10件
        recent = with_feedback[-10:]
        lines = []
        for p in recent:
            if p.get("score") is not None:
                stars = "⭐" * p["score"] + "☆" * (5 - p["score"])
                lines.append(f"- [{stars}] {p['action_taken']}: {p['message_summary'][:80]}")
            else:
                result = "👍" if p["was_helpful"] else "👎"
                lines.append(f"- [{result}] {p['action_taken']}: {p['message_summary'][:80]}")

        stats = self.get_intervention_stats()
        avg = f"{stats['avg_score']:.1f}/5" if stats["scored_count"] > 0 else "N/A"

        # 高評価/低評価パターンの傾向を抽出
        high_rated = [p for p in with_score if p["score"] >= 4]
        low_rated = [p for p in with_score if p["score"] <= 2]
        hints = []
        if high_rated:
            high_actions = [p["action_taken"] for p in high_rated[-5:]]
            hints.append(f"高評価: {', '.join(set(high_actions))} が喜ばれる傾向")
        if low_rated:
            low_actions = [p["action_taken"] for p in low_rated[-5:]]
            hints.append(f"低評価: {', '.join(set(low_actions))} は控えめに")

        # スキルごとのフィードバック統計
        skill_stats = defaultdict(lambda: {"good": 0, "bad": 0, "approved": 0, "rejected": 0})
        for p in self.intervention_patterns:
            sk = p.get("skill", "")
            if not sk:
                continue
            if p.get("feedback") == "good":
                skill_stats[sk]["good"] += 1
            elif p.get("feedback") == "bad":
                skill_stats[sk]["bad"] += 1
            if p.get("action_taken") == "approved":
                skill_stats[sk]["approved"] += 1
            elif p.get("action_taken") == "rejected":
                skill_stats[sk]["rejected"] += 1

        if skill_stats:
            hints.append("スキル別フィードバック:")
            for sk, st in skill_stats.items():
                total = st["good"] + st["bad"]
                accept_total = st["approved"] + st["rejected"]
                if total > 0:
                    good_rate = st["good"] / total * 100
                    hints.append(f"  {sk}: 👍{st['good']} / 👎{st['bad']} (好評率{good_rate:.0f}%)")
                if accept_total > 0:
                    accept_rate = st["approved"] / accept_total * 100
                    hints.append(f"  {sk}: 承認{st['approved']} / 却下{st['rejected']} (承認率{accept_rate:.0f}%)")

        result = f"## 過去の介入パターン（平均スコア: {avg}）\n" + "\n".join(lines)
        if hints:
            result += "\n\n### 学習した傾向\n" + "\n".join(f"- {h}" for h in hints)
        return result

    # ──────────────────────────────────────────────
    # タスクパターン
    # ──────────────────────────────────────────────
    def record_task(self, task_type: str, tools_used: List[str], success: bool):
        """タスク実行パターンを記録"""
        self.task_patterns.append({
            "task_type": task_type,
            "tools_used": tools_used,
            "success": success,
            "created_at": time.time(),
        })
        if len(self.task_patterns) > 200:
            self.task_patterns = self.task_patterns[-200:]

    # ──────────────────────────────────────────────
    # プロジェクト単位の長期記憶
    # ──────────────────────────────────────────────
    def update_project(self, project_name: str, update_type: str, content: str, user: str = ""):
        """プロジェクトメモリを更新"""
        if project_name not in self.project_memory:
            self.project_memory[project_name] = {
                "name": project_name,
                "description": "",
                "created_at": time.time(),
                "last_updated": time.time(),
                "decisions": [],
                "open_questions": [],
                "key_context": [],
                "participants": [],
                "status": "active",
            }

        proj = self.project_memory[project_name]
        proj["last_updated"] = time.time()

        if user and user not in proj["participants"]:
            proj["participants"].append(user)

        if update_type == "decision":
            proj["decisions"].append({
                "text": content[:300],
                "date": datetime.fromtimestamp(time.time()).strftime("%Y-%m-%d"),
                "by": user,
            })
            if len(proj["decisions"]) > 30:
                proj["decisions"] = proj["decisions"][-30:]
        elif update_type == "context":
            proj["key_context"].append(content[:500])
            if len(proj["key_context"]) > 20:
                proj["key_context"] = proj["key_context"][-20:]
        elif update_type == "question":
            proj["open_questions"].append(content[:300])
            if len(proj["open_questions"]) > 15:
                proj["open_questions"] = proj["open_questions"][-15:]
        elif update_type == "description":
            proj["description"] = content[:500]
        elif update_type == "status":
            proj["status"] = content

    def get_project_context(self, query: str) -> str:
        """クエリに関連するプロジェクトの文脈を返す"""
        if not self.project_memory:
            return ""

        relevant = []
        query_lower = query.lower()
        for name, proj in self.project_memory.items():
            score = 0
            if name.lower() in query_lower or query_lower in name.lower():
                score += 10
            for ctx in proj.get("key_context", []):
                if any(w in ctx.lower() for w in query_lower.split() if len(w) > 1):
                    score += 2
            for d in proj.get("decisions", []):
                if any(w in d["text"].lower() for w in query_lower.split() if len(w) > 1):
                    score += 1
            if score > 0:
                relevant.append((score, name, proj))

        if not relevant:
            return ""

        relevant.sort(key=lambda x: x[0], reverse=True)
        parts = []
        for _, name, proj in relevant[:3]:
            lines = [f"### プロジェクト: {name}"]
            if proj.get("description"):
                lines.append(f"概要: {proj['description'][:200]}")
            if proj.get("decisions"):
                recent_d = proj["decisions"][-3:]
                lines.append("決定事項: " + "; ".join(d["text"][:80] for d in recent_d))
            if proj.get("open_questions"):
                lines.append("未解決: " + "; ".join(proj["open_questions"][-3:]))
            if proj.get("key_context"):
                lines.append("コンテキスト: " + "; ".join(c[:80] for c in proj["key_context"][-3:]))
            parts.append("\n".join(lines))

        return "## 関連プロジェクトの記憶\n" + "\n\n".join(parts)

    def list_projects(self) -> List[dict]:
        """全プロジェクト一覧"""
        return [
            {"name": name, "status": p.get("status", "active"),
             "last_updated": p.get("last_updated", 0),
             "description": p.get("description", ""),
             "participants": p.get("participants", [])}
            for name, p in self.project_memory.items()
        ]

    # ──────────────────────────────────────────────
    # チャンネル横断ナレッジインデックス
    # ──────────────────────────────────────────────
    def index_message(self, channel: str, user: str, text: str, importance: float = 0.5):
        """メッセージをナレッジインデックスに追加"""
        if not text or len(text) < 10:
            return

        # 簡易キーワード抽出（2文字以上の単語）
        words = set(text.replace("。", " ").replace("、", " ").split())
        keywords = [w for w in words if len(w) >= 2][:10]

        self.knowledge_index.append({
            "text": text[:500],
            "channel": channel,
            "user": user,
            "timestamp": time.time(),
            "keywords": keywords,
            "importance": importance,
        })

        # 最大2000件に制限
        if len(self.knowledge_index) > 2000:
            # 古くて重要度の低いものから削除
            self.knowledge_index.sort(
                key=lambda x: x["importance"] * 0.3 + (x["timestamp"] / time.time()) * 0.7
            )
            self.knowledge_index = self.knowledge_index[-2000:]

    def cross_channel_search(self, query: str, channel_filter: str = "", limit: int = 10) -> List[dict]:
        """チャンネル横断でキーワードマッチ検索（日本語対応 bigram）"""
        from utils import text_match as tmatch
        q_tokens = tmatch.tokenize(query)
        if not q_tokens:
            return []
        scored = []
        for entry in self.knowledge_index:
            if channel_filter and entry["channel"] != channel_filter:
                continue
            kw_overlap = len(q_tokens & tmatch.tokenize(" ".join(entry.get("keywords", []))))
            txt_overlap = len(q_tokens & tmatch.tokenize(entry.get("text", "")))
            overlap = kw_overlap * 2 + txt_overlap
            # キーワード一致ゼロの無関係エントリは除外
            # (旧実装は importance 0.6/0.7 単独で閾値 0.5 を超え、無関係でも通過していた)
            if overlap == 0:
                continue
            score = overlap + entry.get("importance", 0.5)
            scored.append((score, entry))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in scored[:limit]]

    def get_cross_channel_context(self, query: str) -> str:
        """AIに渡すチャンネル横断ナレッジ"""
        results = self.cross_channel_search(query, limit=5)
        if not results:
            return ""
        lines = []
        for r in results:
            dt = datetime.fromtimestamp(r["timestamp"]).strftime("%m/%d")
            lines.append(f"- [{dt} #{r['channel'][:8]}] {r['text'][:150]}")
        return "## チーム全体のナレッジ（チャンネル横断）\n" + "\n".join(lines)

    # ──────────────────────────────────────────────
    # チーム傾向分析
    # ──────────────────────────────────────────────
    def analyze_team_trends(self, days: int = 7) -> dict:
        """チーム全体の傾向を分析"""
        cutoff = time.time() - (days * 86400)

        channel_activity = defaultdict(int)
        user_activity = defaultdict(int)
        sentiment = {"positive": 0, "negative": 0, "neutral": 0}

        for ch_id, ctx in self.channel_context.items():
            for msg in ctx.get("recent_messages", []):
                if msg.get("ts", 0) < cutoff:
                    continue
                channel_activity[ch_id] += 1
                user_activity[msg.get("user", "?")] += 1
                text = msg.get("text", "").lower()
                if any(w in text for w in ["困", "問題", "バグ", "エラー", "遅れ", "ブロック", "障害"]):
                    sentiment["negative"] += 1
                elif any(w in text for w in ["完了", "リリース", "成功", "いいね", "素晴らしい", "ありがとう"]):
                    sentiment["positive"] += 1
                else:
                    sentiment["neutral"] += 1

        # 会話ログからトピック抽出
        recent_logs = [l for l in self.conversation_log if l["timestamp"] > cutoff]
        topic_freq = defaultdict(int)
        for log in recent_logs:
            for t in log.get("topics", []):
                topic_freq[t] += 1

        return {
            "period_days": days,
            "total_messages": sum(channel_activity.values()),
            "active_channels": len(channel_activity),
            "active_users": len(user_activity),
            "most_active_channels": sorted(channel_activity.items(), key=lambda x: x[1], reverse=True)[:5],
            "most_active_users": sorted(user_activity.items(), key=lambda x: x[1], reverse=True)[:5],
            "sentiment": sentiment,
            "top_topics": sorted(topic_freq.items(), key=lambda x: x[1], reverse=True)[:10],
            "agent_interactions": len(recent_logs),
        }

    # ──────────────────────────────────────────────
    # ユーザー指示メモ（CLAUDE.md相当）
    # ──────────────────────────────────────────────
    def add_note(self, text: str, user: str = "", category: str = "general") -> dict:
        """ユーザーの指示・情報をメモとして永続保存"""
        note = {
            "id": len(self.user_notes) + 1,
            "text": text[:1000],
            "user": user,
            "category": category,
            "created_at": time.time(),
            "created_date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "active": True,
        }
        self.user_notes.append(note)
        self.save()
        return {"success": True, "id": note["id"], "message": f"メモ #{note['id']} を保存しました。"}

    def remove_note(self, note_id: int) -> dict:
        """メモを無効化（論理削除）"""
        for note in self.user_notes:
            if note["id"] == note_id:
                note["active"] = False
                self.save()
                return {"success": True, "message": f"メモ #{note_id} を削除しました。"}
        return {"success": False, "error": f"メモ #{note_id} が見つかりません"}

    def list_notes(self, user: str = "", category: str = "", active_only: bool = True) -> List[dict]:
        """メモ一覧を取得"""
        results = self.user_notes
        if active_only:
            results = [n for n in results if n.get("active", True)]
        if user:
            results = [n for n in results if n["user"] == user]
        if category:
            results = [n for n in results if n["category"] == category]
        return results

    def get_notes_context(self, user: str = "") -> str:
        """AIに渡すメモコンテキスト。
        本人のメモ + オーナー（AGENT_OWNER_SLACK_ID）のメモ + 旧データ(user不明) + category=='global' のみ。
        (旧実装は全ユーザーの全メモを全会話に注入し、A さんの「覚えて」が
         B さんの応答を歪めていた)
        """
        _OWNER = os.getenv("AGENT_OWNER_SLACK_ID", "") or "\0"  # 未設定なら誰にも一致させない
        active = [
            n for n in self.user_notes
            if n.get("active", True) and (
                not user                                # 後方互換: user 未指定なら全件
                or n.get("user", "") in (user, _OWNER)
                or not n.get("user")                    # 旧データ(user不明)は全体扱い
                or n.get("category") == "global"
            )
        ]
        if not active:
            return ""

        lines = []
        for n in active:
            cat = f"[{n['category']}] " if n["category"] != "general" else ""
            lines.append(f"- {cat}{n['text']}")

        return "## ユーザーからの永続メモ（常に参照してください）\n" + "\n".join(lines)

    def search_notes(self, query: str) -> List[dict]:
        """メモをキーワード検索"""
        query_lower = query.lower()
        return [
            n for n in self.user_notes
            if n.get("active", True) and query_lower in n["text"].lower()
        ]

    # ──────────────────────────────────────────────
    # 会話参加者追跡
    # ──────────────────────────────────────────────
    def track_participant(self, channel: str, thread_ts: str, user: str,
                          text: str = "", mentioned_users: List[str] = None):
        """スレッドの会話参加者を追跡し、メンショングラフを構築"""
        key = f"{channel}:{thread_ts}"
        if key not in self.thread_participants:
            self.thread_participants[key] = {
                "participants": {},
                "mention_graph": [],
                "initiator": user,
                "channel": channel,
                "started_at": time.time(),
            }

        thread_data = self.thread_participants[key]
        now = time.time()

        # 参加者を記録
        if user not in thread_data["participants"]:
            thread_data["participants"][user] = {"last_msg": now, "msg_count": 0}
        thread_data["participants"][user]["last_msg"] = now
        thread_data["participants"][user]["msg_count"] += 1

        # メンショングラフ（誰が誰に話しかけたか）
        if mentioned_users:
            for mentioned in mentioned_users:
                if mentioned != user:  # 自分自身へのメンションは除外
                    thread_data["mention_graph"].append((user, mentioned, now))

        # 古いスレッドデータを自動クリーンアップ（24時間以上前）
        self._cleanup_old_threads()

    def get_thread_context(self, channel: str, thread_ts: str) -> str:
        """スレッドの参加者コンテキストを取得"""
        key = f"{channel}:{thread_ts}"
        thread_data = self.thread_participants.get(key)
        if not thread_data:
            return ""

        parts = []
        participants = thread_data.get("participants", {})
        if participants:
            user_list = ", ".join(
                f"{self.get_cached_user_name(uid)}(<@{uid}>)"
                for uid in participants.keys()
            )
            parts.append(f"このスレッドの参加者: {user_list}")

        initiator = thread_data.get("initiator", "")
        if initiator:
            initiator_name = self.get_cached_user_name(initiator)
            parts.append(f"会話の開始者: {initiator_name}(<@{initiator}>)")

        # メンショングラフから「誰が誰に話しかけたか」を要約
        graph = thread_data.get("mention_graph", [])
        if graph:
            # 最新5件のメンション関係をまとめる
            recent = graph[-5:]
            relations = []
            for from_u, to_u, _ in recent:
                relations.append(f"<@{from_u}> → <@{to_u}>")
            parts.append(f"メンション関係: {', '.join(relations)}")

        return "\n".join(parts) if parts else ""

    def get_active_conversations(self, channel: str) -> List[dict]:
        """チャンネル内のアクティブな会話を取得（1時間以内）"""
        cutoff = time.time() - 3600
        active = []
        for key, data in self.thread_participants.items():
            if data.get("channel") != channel:
                continue
            # 参加者の最終メッセージが1時間以内
            any_active = any(
                p["last_msg"] > cutoff
                for p in data["participants"].values()
            )
            if any_active:
                active.append({
                    "thread_key": key,
                    "initiator": data.get("initiator", ""),
                    "participants": list(data["participants"].keys()),
                    "msg_count": sum(p["msg_count"] for p in data["participants"].values()),
                })
        return active

    def _cleanup_old_threads(self):
        """24時間以上前のスレッドデータをクリーンアップ"""
        cutoff = time.time() - 86400
        expired = [
            key for key, data in self.thread_participants.items()
            if data.get("started_at", 0) < cutoff
        ]
        for key in expired:
            del self.thread_participants[key]

    # ──────────────────────────────────────────────
    # 会話フィードバック記録
    # ──────────────────────────────────────────────
    def record_conversation_feedback(
        self, user: str, channel: str, score: int,
        text_snippet: str = "", response_snippet: str = "",
        feedback_type: str = "reaction",
    ):
        """会話ごとのフィードバックを記録"""
        self.conversation_feedback.append({
            "user": user,
            "channel": channel,
            "score": score,
            "text_snippet": text_snippet[:200],
            "response_snippet": response_snippet[:200],
            "timestamp": time.time(),
            "feedback_type": feedback_type,
        })
        if len(self.conversation_feedback) > 500:
            self.conversation_feedback = self.conversation_feedback[-500:]

        # ユーザープロフィールにも反映
        if score:
            profile = self.get_user_profile(user)
            old_avg = profile.get("avg_satisfaction", 3.0)
            profile["avg_satisfaction"] = old_avg * 0.8 + score * 0.2
        self.save()

    def should_ask_feedback(self, user: str, channel: str) -> bool:
        """フィードバックを聞くべきかどうか判定（確率ベース + クールダウン）"""
        import random
        # 5回に1回の確率（自己改善ループに必要なデータ量確保のため）
        if random.random() > (1 / 5):
            return False

        # 同じユーザーに直近1時間以内に聞いていたらスキップ
        cutoff = time.time() - 3600
        recent = [
            f for f in self.conversation_feedback
            if f["user"] == user and f["timestamp"] > cutoff
        ]
        if recent:
            return False

        return True

    # ──────────────────────────────────────────────
    # 週次開発者レポート生成
    # ──────────────────────────────────────────────
    def generate_weekly_report(self, days: int = 7) -> str:
        """開発者向け週次フィードバックレポートを生成"""
        cutoff = time.time() - (days * 86400)

        # フィードバック統計
        recent_fb = [f for f in self.conversation_feedback if f["timestamp"] > cutoff]
        fb_by_user = defaultdict(list)
        for f in recent_fb:
            fb_by_user[f["user"]].append(f)

        # 会話ログ統計
        recent_logs = [l for l in self.conversation_log if l["timestamp"] > cutoff]
        log_by_user = defaultdict(int)
        for l in recent_logs:
            log_by_user[l["user"]] += 1

        # 介入統計
        recent_interventions = [
            p for p in self.intervention_patterns if p.get("created_at", 0) > cutoff
        ]

        lines = [f"📊 *エージェント 週次レポート* ({days}日間)\n"]

        # ── 利用状況 ──
        lines.append("*📈 利用状況*")
        lines.append(f"• 総対話数: {len(recent_logs)}回")
        lines.append(f"• アクティブユーザー: {len(log_by_user)}人")
        if log_by_user:
            top_users = sorted(log_by_user.items(), key=lambda x: -x[1])[:5]
            for uid, cnt in top_users:
                name = self.user_profiles.get(uid, {}).get("name", uid)
                lines.append(f"  - <@{uid}> ({name}): {cnt}回")
        lines.append("")

        # ── フィードバックサマリー ──
        lines.append("*⭐ フィードバック*")
        if recent_fb:
            scores = [f["score"] for f in recent_fb if f.get("score")]
            if scores:
                avg = sum(scores) / len(scores)
                lines.append(f"• 平均スコア: {avg:.1f}/5 ({len(scores)}件)")
                good = len([s for s in scores if s >= 4])
                bad = len([s for s in scores if s <= 2])
                lines.append(f"• 高評価: {good}件 / 低評価: {bad}件")

            # ユーザー別満足度
            lines.append("• ユーザー別:")
            for uid, fbs in fb_by_user.items():
                user_scores = [f["score"] for f in fbs if f.get("score")]
                if user_scores:
                    u_avg = sum(user_scores) / len(user_scores)
                    name = self.user_profiles.get(uid, {}).get("name", uid)
                    lines.append(f"  - <@{uid}> ({name}): {u_avg:.1f}/5 ({len(user_scores)}件)")

            # 高評価の会話例
            good_fbs = [f for f in recent_fb if f.get("score", 0) >= 4]
            if good_fbs:
                lines.append("\n• 😊 高評価の会話例:")
                for f in good_fbs[-3:]:
                    lines.append(f"  - 「{f['text_snippet'][:60]}」→ score={f['score']}")

            # 低評価の会話例
            bad_fbs = [f for f in recent_fb if f.get("score", 0) <= 2]
            if bad_fbs:
                lines.append("\n• 😞 低評価の会話例:")
                for f in bad_fbs[-3:]:
                    lines.append(f"  - 「{f['text_snippet'][:60]}」→ score={f['score']}")
        else:
            lines.append("• フィードバックなし")
        lines.append("")

        # ── 介入統計 ──
        if recent_interventions:
            lines.append("*🤖 自発介入*")
            lines.append(f"• 介入回数: {len(recent_interventions)}回")
            helpful = len([i for i in recent_interventions if i.get("was_helpful")])
            unhelpful = len([i for i in recent_interventions if i.get("was_helpful") is False])
            lines.append(f"• 好評: {helpful}回 / 不評: {unhelpful}回")
            lines.append("")

        # ── ユーザースタイル学習状況 ──
        lines.append("*🧠 学習状況*")
        lines.append(f"• チーム知識: {len(self.team_knowledge)}件")
        lines.append(f"• ユーザープロフィール: {len(self.user_profiles)}人")
        for uid, prof in self.user_profiles.items():
            auto = prof.get("auto_learned", {})
            tone = prof.get("preferred_tone", "?")
            style = prof.get("preferred_style", "?")
            avg_len = auto.get("avg_msg_length", 0)
            name = prof.get("name", uid)
            if prof.get("interaction_count", 0) > 3:
                lines.append(f"  - <@{uid}> ({name}): tone={tone}, style={style}, avg_len={avg_len:.0f}")
        lines.append("")

        # ── トピック傾向 ──
        topic_freq = defaultdict(int)
        for log in recent_logs:
            for t in log.get("topics", []):
                topic_freq[t] += 1
        if topic_freq:
            lines.append("*📝 よく依頼されたトピック*")
            top_topics = sorted(topic_freq.items(), key=lambda x: -x[1])[:8]
            for t, c in top_topics:
                lines.append(f"  - {t}: {c}回")

        return "\n".join(lines)

    # ──────────────────────────────────────────────
    # Markdownナレッジストア連携
    # ──────────────────────────────────────────────
    def record_learning(self, category: str, content: str, source: str = "") -> bool:
        """学びをMarkdownナレッジストアに記録

        Args:
            category: 分類（tech/business/team/process 等）
            content: 学びの内容
            source: 情報源

        Returns:
            追加成功ならTrue
        """
        return self.knowledge_store.add_learning(category, content, source)

    def get_md_knowledge_context(self, query: str = "", channel: str = "",
                                  user: str = "") -> str:
        """Markdownナレッジストアからsystem prompt注入用コンテキストを取得

        Args:
            query: 会話内容（関連知識の検索に使用）
            channel: チャンネルID
            user: ユーザーID

        Returns:
            system promptに注入するMarkdown文字列
        """
        return self.knowledge_store.get_context_for_prompt(
            channel=channel, user=user, query=query
        )

    # ──────────────────────────────────────────────
    # 永続化
    # ──────────────────────────────────────────────
    def save(self):
        """メモリをJSONファイルに保存"""
        try:
            os.makedirs(os.path.dirname(self.persist_path), exist_ok=True)
            data = {
                "channel_context": self.channel_context,
                "user_profiles": self.user_profiles,
                "team_knowledge": self.team_knowledge,
                "intervention_patterns": self.intervention_patterns,
                "task_patterns": self.task_patterns,
                "feedback_scores": self.feedback_scores,
                "conversation_log": self.conversation_log,
                "project_memory": self.project_memory,
                "knowledge_index": self.knowledge_index,
                "user_notes": self.user_notes,
                "conversation_feedback": self.conversation_feedback,
                "user_name_cache": self.user_name_cache,
            }
            tmp_path = self.persist_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            # 直前の正常ファイルを .bak に退避してからアトミック置換（SIGKILL時の全損防止）
            if os.path.exists(self.persist_path):
                try:
                    os.replace(self.persist_path, self.persist_path + ".bak")
                except OSError:
                    pass
            os.replace(tmp_path, self.persist_path)
            logger.debug("Memory saved successfully")
        except Exception as e:
            logger.error(f"Failed to save memory: {e}")

    def _load(self):
        """永続化されたメモリを復元"""
        if not os.path.exists(self.persist_path):
            return
        def _apply(data: dict):
            self.channel_context = data.get("channel_context", {})
            self.user_profiles = data.get("user_profiles", {})
            self.team_knowledge = data.get("team_knowledge", [])
            self.intervention_patterns = data.get("intervention_patterns", [])
            self.task_patterns = data.get("task_patterns", [])
            self.feedback_scores = data.get("feedback_scores", [])
            self.conversation_log = data.get("conversation_log", [])
            self.project_memory = data.get("project_memory", {})
            self.knowledge_index = data.get("knowledge_index", [])
            self.user_notes = data.get("user_notes", [])
            self.conversation_feedback = data.get("conversation_feedback", [])
            self.user_name_cache = data.get("user_name_cache", {})

        def _read(path: str) -> dict:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)

        # 本体→.bak の順に読む。両方駄目なら破損本体を退避して空起動（次回saveの空上書き全損を防ぐ）。
        try:
            _apply(_read(self.persist_path))
        except Exception as e:
            logger.error(f"Failed to load memory ({self.persist_path}): {e}")
            bak = self.persist_path + ".bak"
            recovered = False
            if os.path.exists(bak):
                try:
                    _apply(_read(bak))
                    logger.warning(f"Recovered memory from backup: {bak}")
                    recovered = True
                except Exception as e2:
                    logger.error(f"Backup load also failed ({bak}): {e2}")
            if not recovered:
                try:
                    import time as _t
                    os.replace(self.persist_path, f"{self.persist_path}.corrupt.{int(_t.time())}")
                    logger.error("Quarantined corrupt memory.json; starting with empty memory")
                except OSError:
                    pass
                return
        logger.info(
            f"Memory loaded: {len(self.user_profiles)} users, "
            f"{len(self.team_knowledge)} knowledge items, "
            f"{len(self.intervention_patterns)} intervention patterns, "
            f"{len(self.conversation_log)} conversation logs, "
            f"{len(self.user_name_cache)} cached user names"
        )


class MemoryExtractor:
    """会話から知識や好みを自動抽出するヘルパー"""

    def __init__(self, anthropic_client, memory: Memory):
        self.client = anthropic_client
        self.memory = memory

    async def extract_from_conversation(
        self, messages: List[dict], channel: str, user: str
    ):
        """会話の完了後に知識・好みを非同期で抽出"""
        if len(messages) < 4:  # 短い会話はスキップ
            return

        # 会話テキストを構築
        conv_text = []
        for m in messages[-10:]:  # 直近10ターン
            role = m.get("role", "?")
            content = m.get("content", "")
            if isinstance(content, list):
                text_parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
                content = " ".join(text_parts)
            if isinstance(content, str):
                conv_text.append(f"{role}: {content[:200]}")

        if not conv_text:
            return

        prompt = f"""以下のSlack会話から、チームの知識として蓄積すべき情報を抽出してください。

## 会話
{chr(10).join(conv_text)}

## 抽出対象
1. facts: チームや業務に関する事実（プロジェクト名、担当者、ルール等）
2. user_prefs: このユーザーの好みや傾向（言語、呼び方、作業スタイル等）
3. task_type: このタスクの分類（翻訳、画像編集、データ分析 等）

JSONのみで回答:
{{"facts": ["事実1", "事実2"], "user_prefs": {{"key": "value"}}, "task_type": "分類"}}

抽出できるものがなければ空にしてください。"""

        try:
            response = self.client.messages.create(
                model=models.MEDIUM,
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}],
            )
            import re
            text = response.content[0].text.strip()
            match = re.search(r'\{[\s\S]+\}', text)
            if not match:
                return

            extracted = json.loads(match.group())

            # 知識を追加
            for fact in extracted.get("facts", []):
                if fact and len(fact) > 5:
                    self.memory.add_team_knowledge(fact, source=f"channel:{channel}")

            # ユーザー好みを更新
            for key, value in extracted.get("user_prefs", {}).items():
                if key and value:
                    self.memory.update_user_preference(user, key, value)

            # タスクタイプを記録
            task_type = extracted.get("task_type", "")
            if task_type:
                self.memory.record_interaction(user, task_type)

            self.memory.save()

        except Exception as e:
            logger.debug(f"Knowledge extraction failed (non-critical): {e}")
