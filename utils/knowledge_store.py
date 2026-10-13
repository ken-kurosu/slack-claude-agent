"""Markdownファイルベースのナレッジストア

会話の中で学んだこと（チーム知識、プロジェクト決定事項、ユーザーの好み等）を
構造化されたMarkdownファイルに自動蓄積し、次の会話で参照できるようにする。

ディレクトリ構造:
  memory_data/knowledge/
  ├── README.md          ← インデックス（自動生成）
  ├── team/              ← チーム・メンバー情報
  │   ├── members.md     ← メンバーの役割・担当・好み
  │   └── decisions.md   ← 重要な決定事項
  ├── projects/          ← プロジェクト別の知識
  │   └── {project_name}.md
  ├── skills/            ← エージェントが学んだスキル・ノウハウ
  │   └── {topic}.md
  └── daily/             ← 日次で蓄積される学び
      └── {YYYY-MM-DD}.md
"""

import logging
import os
import re
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 各Markdownファイルの最大行数（超えたらアーカイブ）
MAX_LINES_PER_FILE = 100
# dailyファイルの保持日数
DAILY_RETENTION_DAYS = 90


class KnowledgeStore:
    """Markdownファイルベースのナレッジストア"""

    def __init__(self, base_dir: str = "memory_data/knowledge"):
        # 絶対パスに正規化
        if not os.path.isabs(base_dir):
            # エージェントプロジェクトルート基準
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            base_dir = os.path.join(project_root, base_dir)
        self.base_dir = base_dir
        self._lock = threading.Lock()
        self._ensure_dirs()
        logger.info(f"KnowledgeStore initialized: {self.base_dir}")

    def _ensure_dirs(self):
        """必要なディレクトリ構造を作成"""
        dirs = [
            self.base_dir,
            os.path.join(self.base_dir, "team"),
            os.path.join(self.base_dir, "projects"),
            os.path.join(self.base_dir, "skills"),
            os.path.join(self.base_dir, "daily"),
        ]
        for d in dirs:
            os.makedirs(d, exist_ok=True)

    # ──────────────────────────────────────────────
    # 書き込み
    # ──────────────────────────────────────────────

    def add_learning(self, category: str, content: str, source: str = "") -> bool:
        """学びを追加（daily/{today}.md に追記）

        Args:
            category: 分類（tech/business/team/process 等）
            content: 学びの内容
            source: 情報源（会話、ユーザー名、チャンネル等）

        Returns:
            追加成功ならTrue
        """
        if not content or not content.strip():
            return False

        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M")
        file_path = os.path.join(self.base_dir, "daily", f"{date_str}.md")

        # 重複チェック
        if self._is_duplicate(file_path, content):
            logger.debug(f"重複スキップ: {content[:50]}...")
            return False

        # エントリ作成
        entry = f"\n## {time_str} - [{category}] {content[:60]}\n"
        entry += f"{content}\n"
        if source:
            entry += f"\n> 出典: {source}\n"

        with self._lock:
            self._append_to_file(file_path, entry, header=f"# {date_str} の学び\n")
            self._maybe_archive(file_path)

        logger.info(f"Learning added: [{category}] {content[:50]}...")
        return True

    def add_team_info(self, content: str, member_name: str = "") -> bool:
        """チーム情報を team/members.md に追記・更新

        Args:
            content: チーム・メンバーに関する情報
            member_name: 特定メンバー名（あれば既存エントリを更新）
        """
        if not content or not content.strip():
            return False

        file_path = os.path.join(self.base_dir, "team", "members.md")
        now = datetime.now().strftime("%Y-%m-%d %H:%M")

        # メンバー名が指定されていれば、既存のセクションを探して更新
        if member_name:
            updated = self._update_member_section(file_path, member_name, content, now)
            if updated:
                return True

        # 新規追記
        entry = f"\n## {member_name or '情報'} ({now})\n{content}\n"

        with self._lock:
            self._append_to_file(file_path, entry, header="# チームメンバー情報\n")
            self._maybe_archive(file_path)

        logger.info(f"Team info added: {content[:50]}...")
        return True

    def add_decision(self, content: str, project: str = "") -> bool:
        """決定事項を記録

        Args:
            content: 決定事項の内容
            project: 関連プロジェクト名（あればプロジェクトファイルにも記録）
        """
        if not content or not content.strip():
            return False

        file_path = os.path.join(self.base_dir, "team", "decisions.md")
        now = datetime.now().strftime("%Y-%m-%d %H:%M")

        # 重複チェック
        if self._is_duplicate(file_path, content):
            return False

        entry = f"\n## {now} - {content[:60]}\n{content}\n"
        if project:
            entry += f"\n> プロジェクト: {project}\n"

        with self._lock:
            self._append_to_file(file_path, entry, header="# 重要な決定事項\n")
            self._maybe_archive(file_path)

        # プロジェクト固有のファイルにも記録
        if project:
            self.add_project_knowledge(project, f"【決定】{content}")

        logger.info(f"Decision recorded: {content[:50]}...")
        return True

    def add_project_knowledge(self, project: str, content: str) -> bool:
        """プロジェクト固有の知識を追記

        Args:
            project: プロジェクト名（ファイル名に使用）
            content: 知識の内容
        """
        if not content or not content.strip() or not project:
            return False

        # プロジェクト名をファイル名に安全に変換
        safe_name = self._sanitize_filename(project)
        file_path = os.path.join(self.base_dir, "projects", f"{safe_name}.md")
        now = datetime.now().strftime("%Y-%m-%d %H:%M")

        # 重複チェック
        if self._is_duplicate(file_path, content):
            return False

        entry = f"\n## {now}\n{content}\n"

        with self._lock:
            self._append_to_file(file_path, entry, header=f"# {project}\n")
            self._maybe_archive(file_path)

        logger.info(f"Project knowledge added ({project}): {content[:50]}...")
        return True

    def add_skill_knowledge(self, topic: str, content: str) -> bool:
        """エージェントが学んだスキル・ノウハウを追記

        Args:
            topic: トピック名（ファイル名に使用）
            content: ノウハウ内容
        """
        if not content or not content.strip() or not topic:
            return False

        safe_name = self._sanitize_filename(topic)
        file_path = os.path.join(self.base_dir, "skills", f"{safe_name}.md")
        now = datetime.now().strftime("%Y-%m-%d %H:%M")

        # 重複チェック
        if self._is_duplicate(file_path, content):
            return False

        entry = f"\n## {now}\n{content}\n"

        with self._lock:
            self._append_to_file(file_path, entry, header=f"# {topic}\n")
            self._maybe_archive(file_path)

        logger.info(f"Skill knowledge added ({topic}): {content[:50]}...")
        return True

    # ──────────────────────────────────────────────
    # 検索・読み出し
    # ──────────────────────────────────────────────

    def search(self, query: str, max_results: int = 5) -> List[Dict]:
        """全ファイルを横断検索

        Args:
            query: 検索クエリ（スペース区切りでAND検索）
            max_results: 最大結果数

        Returns:
            [{"file": str, "section": str, "content": str, "score": float}, ...]
        """
        if not query or not query.strip():
            return []

        # 日本語対応 bigram トークン化（旧 split() による AND 検索は日本語が
        # 1 キーワード化して完全一致以外ヒットしなかった）。
        from utils import text_match
        q_tokens = text_match.tokenize(query)
        if not q_tokens:
            return []
        min_overlap = max(1, len(q_tokens) // 3)  # クエリ語の約3割以上を含むセクション

        results: List[Dict] = []

        for root, _dirs, files in os.walk(self.base_dir):
            for fname in files:
                if not fname.endswith(".md"):
                    continue
                file_path = os.path.join(root, fname)
                try:
                    sections = self._parse_sections(file_path)
                    for section_title, section_content in sections:
                        sec_tokens = text_match.tokenize(section_title + " " + section_content)
                        overlap = len(q_tokens & sec_tokens)
                        if overlap < min_overlap:
                            continue
                        rel_path = os.path.relpath(file_path, self.base_dir)
                        results.append({
                            "file": rel_path,
                            "section": section_title,
                            "content": section_content.strip()[:300],
                            "score": overlap,
                        })
                except Exception as e:
                    logger.debug(f"Search error in {file_path}: {e}")

        # スコア降順でソート
        results.sort(key=lambda x: -x["score"])
        return results[:max_results]

    def get_recent_learnings(self, days: int = 7) -> str:
        """直近N日間の学びをまとめて返す

        Args:
            days: 取得日数

        Returns:
            Markdown形式のまとめ文字列
        """
        daily_dir = os.path.join(self.base_dir, "daily")
        if not os.path.isdir(daily_dir):
            return ""

        today = datetime.now()
        entries = []

        for i in range(days):
            date = today - timedelta(days=i)
            date_str = date.strftime("%Y-%m-%d")
            file_path = os.path.join(daily_dir, f"{date_str}.md")
            if os.path.exists(file_path):
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        content = f.read().strip()
                    if content:
                        entries.append(f"### {date_str}\n{content}")
                except Exception as e:
                    logger.debug(f"Failed to read {file_path}: {e}")

        if not entries:
            return ""

        return "# 直近の学び\n\n" + "\n\n---\n\n".join(entries)

    def get_context_for_prompt(self, channel: str = "", user: str = "",
                                query: str = "") -> str:
        """エージェントのsystem promptに注入するコンテキストを生成

        直近の学び + 関連するプロジェクト知識 + チーム情報をコンパクトにまとめる。

        Args:
            channel: Slackチャンネル名（あればチャンネル関連情報を優先）
            user: ユーザーID（あればユーザー関連情報を優先）
            query: 会話内容のクエリ（関連性の高い知識を検索）

        Returns:
            system promptに注入するMarkdown文字列（空文字なら注入不要）
        """
        parts = []

        # 1. チーム情報（常に注入、ただし最大500文字）
        team_members = self._read_file_safe(
            os.path.join(self.base_dir, "team", "members.md")
        )
        if team_members:
            # 先頭の見出しを除去して本文のみ
            cleaned = self._strip_main_header(team_members)
            if cleaned:
                parts.append(f"### チームメンバー\n{cleaned[:500]}")

        # 2. 直近の決定事項（最新5件）
        decisions = self._read_file_safe(
            os.path.join(self.base_dir, "team", "decisions.md")
        )
        if decisions:
            recent_decisions = self._extract_recent_sections(decisions, max_sections=5)
            if recent_decisions:
                parts.append(f"### 最近の決定事項\n{recent_decisions}")

        # 3. クエリに関連する知識
        if query:
            search_results = self.search(query, max_results=3)
            if search_results:
                knowledge_lines = []
                for r in search_results:
                    knowledge_lines.append(f"- [{r['file']}] {r['content'][:150]}")
                parts.append(
                    f"### 関連するナレッジ\n" + "\n".join(knowledge_lines)
                )

        # 4. 直近3日間の学び（要約）
        recent = self._get_daily_summaries(days=3)
        if recent:
            parts.append(f"### 直近の学び\n{recent}")

        if not parts:
            return ""

        return "## 蓄積されたナレッジ（Markdownストア）\n\n" + "\n\n".join(parts)

    def generate_index(self):
        """README.mdにインデックスを自動生成"""
        lines = ["# ナレッジストア インデックス\n"]
        lines.append(f"自動生成: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")

        for subdir in ["team", "projects", "skills", "daily"]:
            dir_path = os.path.join(self.base_dir, subdir)
            if not os.path.isdir(dir_path):
                continue

            files = sorted([
                f for f in os.listdir(dir_path)
                if f.endswith(".md") and not f.startswith(".")
            ])
            if not files:
                continue

            lines.append(f"\n## {subdir}/\n")
            for f in files:
                file_path = os.path.join(dir_path, f)
                size = os.path.getsize(file_path)
                # セクション数をカウント
                try:
                    with open(file_path, "r", encoding="utf-8") as fh:
                        content = fh.read()
                    section_count = content.count("\n## ")
                except Exception:
                    section_count = 0
                lines.append(f"- `{f}` ({size}B, {section_count}セクション)")

        readme_path = os.path.join(self.base_dir, "README.md")
        with self._lock:
            with open(readme_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")

        logger.info("Knowledge index regenerated")

    # ──────────────────────────────────────────────
    # 内部ヘルパー
    # ──────────────────────────────────────────────

    def _append_to_file(self, file_path: str, entry: str, header: str = ""):
        """ファイルに追記（なければヘッダー付きで新規作成）"""
        if not os.path.exists(file_path):
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(header)

        with open(file_path, "a", encoding="utf-8") as f:
            f.write(entry)

    def _is_duplicate(self, file_path: str, content: str, threshold: float = 0.7) -> bool:
        """既存ファイル内に類似コンテンツがあるか判定（簡易文字列マッチ）"""
        if not os.path.exists(file_path):
            return False

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                existing = f.read()
        except Exception:
            return False

        existing_lower = existing.lower()
        # 短い内容を正規化して完全一致チェック
        content_normalized = content.strip().lower()
        if len(content_normalized) < 20:
            return content_normalized in existing_lower

        # まず全文中にコンテンツがそのまま含まれているかチェック
        if content_normalized in existing_lower:
            return True

        # 長い内容はキーワードベースで類似度チェック
        content_words = set(content_normalized.split())
        if not content_words:
            return False

        # 既存の各セクションと比較
        sections = self._parse_sections(file_path)
        for _, section_content in sections:
            section_lower = section_content.lower()
            # セクション内にコンテンツがそのまま含まれているかチェック
            if content_normalized in section_lower:
                return True
            section_words = set(section_lower.split())
            if not section_words:
                continue
            overlap = len(content_words & section_words)
            # 小さい方のセットに対する割合で判定（追加側が部分一致でも検出）
            min_len = min(len(content_words), len(section_words))
            if min_len > 0 and overlap / min_len >= threshold:
                return True

        return False

    def _parse_sections(self, file_path: str) -> List[Tuple[str, str]]:
        """ファイルを ## 見出しでセクション分割

        Returns:
            [(title, content), ...]
        """
        if not os.path.exists(file_path):
            return []

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                text = f.read()
        except Exception:
            return []

        # ## で分割
        parts = re.split(r'\n(?=## )', text)
        sections = []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            # 最初の行を見出しとして取得
            lines = part.split("\n", 1)
            title = lines[0].lstrip("#").strip()
            content = lines[1].strip() if len(lines) > 1 else ""
            sections.append((title, content))

        return sections

    def _maybe_archive(self, file_path: str):
        """ファイルが最大行数を超えていたらアーカイブ"""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception:
            return

        if len(lines) <= MAX_LINES_PER_FILE:
            return

        # 前半をアーカイブ、後半を残す
        split_point = len(lines) // 2
        archive_path = file_path.replace(".md", f"_archive_{datetime.now().strftime('%Y%m%d')}.md")

        # アーカイブ先が既に存在する場合は追記
        mode = "a" if os.path.exists(archive_path) else "w"
        with open(archive_path, mode, encoding="utf-8") as f:
            f.writelines(lines[:split_point])

        # 元ファイルは後半のみ残す（先頭ヘッダーを保持）
        header_lines = []
        for line in lines:
            if line.startswith("# ") and not line.startswith("## "):
                header_lines.append(line)
                break

        with open(file_path, "w", encoding="utf-8") as f:
            f.writelines(header_lines + lines[split_point:])

        logger.info(f"Archived {split_point} lines from {file_path} to {archive_path}")

    def _update_member_section(self, file_path: str, member_name: str,
                                content: str, timestamp: str) -> bool:
        """members.mdの特定メンバーセクションを更新"""
        if not os.path.exists(file_path):
            return False

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                text = f.read()
        except Exception:
            return False

        # メンバー名のセクションを探す
        pattern = rf'(## {re.escape(member_name)}[^\n]*\n)([\s\S]*?)(?=\n## |\Z)'
        match = re.search(pattern, text)
        if not match:
            return False

        # 既存セクション内容に追記
        existing_content = match.group(2).rstrip()
        new_section = f"{match.group(1)}{existing_content}\n- ({timestamp}) {content}\n"

        with self._lock:
            new_text = text[:match.start()] + new_section + text[match.end():]
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(new_text)

        logger.info(f"Updated member section: {member_name}")
        return True

    def _sanitize_filename(self, name: str) -> str:
        """ファイル名に安全な文字列に変換"""
        # 日本語はそのまま保持、ファイル名に使えない文字のみ除去
        safe = re.sub(r'[/\\:*?"<>|]', '_', name.strip())
        safe = re.sub(r'\s+', '_', safe)
        # 先頭・末尾のアンダースコアを除去
        safe = safe.strip('_')
        return safe[:50] if safe else "unnamed"

    def _read_file_safe(self, file_path: str) -> str:
        """ファイルを安全に読み込み（存在しなければ空文字）"""
        if not os.path.exists(file_path):
            return ""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            logger.debug(f"Failed to read {file_path}: {e}")
            return ""

    def _strip_main_header(self, text: str) -> str:
        """# で始まるメインヘッダー行を除去"""
        lines = text.split("\n")
        result = []
        for line in lines:
            if line.startswith("# ") and not line.startswith("## "):
                continue
            result.append(line)
        return "\n".join(result).strip()

    def _extract_recent_sections(self, text: str, max_sections: int = 5) -> str:
        """テキストから最新N個のセクションを抽出"""
        sections = re.split(r'\n(?=## )', text)
        # 最初の要素はヘッダーの可能性があるのでスキップ
        content_sections = [s.strip() for s in sections if s.strip().startswith("## ")]
        # 最新（末尾）からmax_sections個
        recent = content_sections[-max_sections:]
        return "\n\n".join(recent)

    def _get_daily_summaries(self, days: int = 3) -> str:
        """直近N日間のdailyファイルから要約を生成"""
        daily_dir = os.path.join(self.base_dir, "daily")
        if not os.path.isdir(daily_dir):
            return ""

        today = datetime.now()
        summaries = []

        for i in range(days):
            date = today - timedelta(days=i)
            date_str = date.strftime("%Y-%m-%d")
            file_path = os.path.join(daily_dir, f"{date_str}.md")
            if not os.path.exists(file_path):
                continue

            try:
                sections = self._parse_sections(file_path)
                if sections:
                    # 各セクションのタイトルのみをリスト表示
                    items = [f"- {title}" for title, _ in sections
                             if title and not title.startswith(date_str)]
                    if items:
                        summaries.append(f"**{date_str}**: " + " / ".join(
                            [t.lstrip("- ") for t in items[:5]]
                        ))
            except Exception:
                pass

        return "\n".join(summaries) if summaries else ""

    def cleanup_old_daily(self):
        """古いdailyファイルを削除（DAILY_RETENTION_DAYS超過分）"""
        daily_dir = os.path.join(self.base_dir, "daily")
        if not os.path.isdir(daily_dir):
            return

        cutoff = datetime.now() - timedelta(days=DAILY_RETENTION_DAYS)
        removed = 0

        for fname in os.listdir(daily_dir):
            if not fname.endswith(".md"):
                continue
            # ファイル名から日付を抽出
            match = re.match(r'(\d{4}-\d{2}-\d{2})\.md', fname)
            if not match:
                continue
            try:
                file_date = datetime.strptime(match.group(1), "%Y-%m-%d")
                if file_date < cutoff:
                    os.remove(os.path.join(daily_dir, fname))
                    removed += 1
            except ValueError:
                pass

        if removed:
            logger.info(f"Cleaned up {removed} old daily files")
