"""
スキルシステム: 再利用可能な手順/テンプレートをファイルベースで管理

Claude CodeのSKILL.md に相当する仕組み。
スキルフォルダ内の .md ファイルを読み込み、特定タスク時にAIに注入する。

構造:
  skills/
    ├── weekly_report/
    │   └── SKILL.md     # スキル定義（手順、テンプレート、注意点）
    ├── meeting_notes/
    │   └── SKILL.md
    └── ...
"""
import json
import logging
import os
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_SKILLS_DIR = "/tmp/slack-agent-workspace/skills"


class SkillManager:
    """スキルの登録・検索・読み込みを管理"""

    def __init__(self, skills_dir: str = DEFAULT_SKILLS_DIR, flat_skill_dirs: Optional[List[str]] = None):
        self.skills_dir = skills_dir
        os.makedirs(skills_dir, exist_ok=True)
        # フラット.mdファイルを読み込む追加ディレクトリ（skills_content/等）
        self._flat_skill_dirs: List[str] = flat_skill_dirs or []
        # スキルメタデータのキャッシュ
        self._cache: Dict[str, dict] = {}
        self._load_all()

    def _load_all(self):
        """全スキルをスキャンしてメタデータをキャッシュ

        2つの構造に対応:
        1. サブディレクトリ形式: skills/name/SKILL.md
        2. フラットファイル形式: skills_content/name.md（YAMLフロントマター付き）
        """
        self._cache.clear()

        # --- 1. サブディレクトリ形式 ---
        if os.path.isdir(self.skills_dir):
            for name in os.listdir(self.skills_dir):
                skill_path = os.path.join(self.skills_dir, name)
                if not os.path.isdir(skill_path):
                    continue
                skill_file = os.path.join(skill_path, "SKILL.md")
                if not os.path.exists(skill_file):
                    continue
                self._load_one_skill(skill_file, name)

        # --- 2. フラットファイル形式 + サブディレクトリ形式（skills_content/ ディレクトリ） ---
        for flat_dir in self._flat_skill_dirs:
            if not os.path.isdir(flat_dir):
                continue
            for entry in os.listdir(flat_dir):
                path = os.path.join(flat_dir, entry)
                # (a) フラット .md ファイル
                if entry.endswith(".md") and os.path.isfile(path):
                    default_name = entry[:-3]  # ".md" を除去
                    if default_name not in self._cache:
                        self._load_one_skill(path, default_name)
                # (b) サブディレクトリ形式 例: my-skill/SKILL.md
                #     "_" 始まり (_archive 等) は読み込まない（アーカイブ退避先）
                elif os.path.isdir(path) and not entry.startswith("_"):
                    sub_skill = os.path.join(path, "SKILL.md")
                    if os.path.isfile(sub_skill) and entry not in self._cache:
                        self._load_one_skill(sub_skill, entry)

        logger.info(f"Total skills loaded: {len(self._cache)}")

    def _load_one_skill(self, skill_file: str, default_name: str):
        """1つのスキルファイルを読み込んでキャッシュに追加"""
        try:
            with open(skill_file, "r", encoding="utf-8") as f:
                content = f.read()
            meta = self._parse_metadata(content, default_name)
            meta["path"] = skill_file
            meta["content"] = content
            # YAMLフロントマターのname値があればそちらを使う
            cache_key = meta.get("name", default_name).replace(" ", "_").lower()
            if not cache_key:
                cache_key = default_name
            self._cache[cache_key] = meta
            logger.info(f"Skill loaded: {cache_key} — {meta.get('description', '')[:60]}")
        except Exception as e:
            logger.error(f"Failed to load skill '{default_name}': {e}")

    def _parse_metadata(self, content: str, fallback_name: str) -> dict:
        """SKILL.mdのフロントマター(YAML --- ブロック)やヘッダーからメタデータを抽出"""
        meta = {
            "name": fallback_name,
            "description": "",
            "triggers": [],
            "tags": [],
            "capabilities": [],
            "limitations": [],
            "always_inject": False,
        }

        lines = content.split("\n")

        # --- YAMLフロントマター解析 ---
        if lines and lines[0].strip() == "---":
            frontmatter_end = None
            for i, line in enumerate(lines[1:], 1):
                if line.strip() == "---":
                    frontmatter_end = i
                    break
            if frontmatter_end:
                # YAML フロントマター解析（外部依存なし）。
                # インライン形式 ("triggers: a, b" / "triggers: [a, b]") と
                # ブロックリスト形式 ("triggers:\n  - a\n  - b") の両方に対応する。
                # 旧パーサはブロックリストを読めず、triggers が空になるスキルが多数あった。
                list_keys = {"triggers", "tags", "capabilities", "limitations"}
                current_list_key = None
                name_from_fm = False  # frontmatter で name が明示されたか
                for line in lines[1:frontmatter_end]:
                    line_stripped = line.strip()
                    if not line_stripped or line_stripped.startswith("#"):
                        continue
                    # ブロックリスト項目: "  - 値"
                    if line_stripped.startswith("- ") or line_stripped == "-":
                        if current_list_key:
                            item = line_stripped[1:].strip().strip('"').strip("'")
                            if item:
                                meta[current_list_key].append(item)
                        continue
                    if ":" not in line_stripped:
                        continue
                    key, val = line_stripped.split(":", 1)
                    key = key.strip().lower()
                    val = val.strip()
                    current_list_key = None  # 新しいキーに入ったらリスト追跡をリセット
                    if key in list_keys:
                        if val:
                            if val.startswith("[") and val.endswith("]"):
                                val = val[1:-1]
                            meta[key] = [t.strip().strip('"').strip("'") for t in val.split(",") if t.strip()]
                        else:
                            # 値が空 = 次行以降のブロックリストを拾う
                            current_list_key = key
                            meta[key] = []
                    elif key == "name" and val:
                        meta["name"] = val
                        name_from_fm = True
                    elif key == "description" and val:
                        meta["description"] = val
                    elif key == "always_inject":
                        meta["always_inject"] = val.lower() in ("true", "1", "yes")
                # frontmatter に name が無い時だけ、本文先頭の # 見出しを名前に採用する。
                # (旧実装は name==ファイル名の時に見出しで上書きしてしまい、cache_key が
                #  見出し文字列に化けて参照不能になっていた)
                if not name_from_fm:
                    for line in lines[frontmatter_end+1:frontmatter_end+5]:
                        if line.strip().startswith("# "):
                            meta["name"] = line.strip()[2:].strip()
                            break
                return meta

        # --- フロントマターなし: 旧形式（先頭20行のキーワード解析） ---
        for line in lines[:20]:
            line_stripped = line.strip()
            if line_stripped.startswith("# "):
                meta["name"] = line_stripped[2:].strip()
            elif line_stripped.lower().startswith("description:"):
                meta["description"] = line_stripped.split(":", 1)[1].strip()
            elif line_stripped.lower().startswith("triggers:"):
                triggers_str = line_stripped.split(":", 1)[1].strip()
                meta["triggers"] = [t.strip() for t in triggers_str.split(",") if t.strip()]
            elif line_stripped.lower().startswith("tags:"):
                tags_str = line_stripped.split(":", 1)[1].strip()
                meta["tags"] = [t.strip() for t in tags_str.split(",") if t.strip()]

        return meta

    def reload(self):
        """スキルキャッシュをリロード（外部からスキル追加/更新された場合に呼ぶ）"""
        logger.info("Reloading skills...")
        self._load_all()

    def list_skills(self) -> List[dict]:
        """登録されている全スキルの一覧"""
        return [
            {
                "name": name,
                "description": meta.get("description", ""),
                "triggers": meta.get("triggers", []),
                "tags": meta.get("tags", []),
                "capabilities": meta.get("capabilities", []),
                "limitations": meta.get("limitations", []),
            }
            for name, meta in self._cache.items()
        ]

    def get_skill(self, name: str) -> Optional[dict]:
        """スキル名で取得"""
        return self._cache.get(name)

    def get_skill_content(self, name: str) -> str:
        """スキルのSKILL.md全文を返す"""
        skill = self._cache.get(name)
        if not skill:
            return ""
        return skill.get("content", "")

    def get_always_inject_names(self) -> List[str]:
        """always_inject: true のスキルの cache_key 一覧。
        LLM 選定に関わらず常時注入すべきスキル（例: 最新の料金表のような、常に最新が必要な社内情報）。
        """
        return [k for k, m in self._cache.items() if m.get("always_inject")]

    def find_matching_skills(self, query: str) -> List[dict]:
        """ユーザーの依頼テキストからマッチするスキルを検索"""
        query_lower = query.lower()
        results = []

        for name, meta in self._cache.items():
            score = 0

            # トリガーキーワードとのマッチ
            for trigger in meta.get("triggers", []):
                if trigger.lower() in query_lower:
                    score += 10

            # スキル名とのマッチ
            if name.lower() in query_lower:
                score += 5

            # タグとのマッチ
            for tag in meta.get("tags", []):
                if tag.lower() in query_lower:
                    score += 3

            # 説明文との単語マッチ
            desc = meta.get("description", "").lower()
            query_words = set(query_lower.split())
            desc_words = set(desc.split())
            overlap = len(query_words & desc_words)
            score += overlap

            if score > 0:
                results.append({"name": name, "score": score, **meta})

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:3]  # 上位3件

    def create_skill(self, name: str, content: str, description: str = "",
                     triggers: Optional[List[str]] = None, tags: Optional[List[str]] = None) -> dict:
        """新しいスキルを作成"""
        safe_name = name.replace(" ", "_").replace("/", "_").lower()
        skill_dir = os.path.join(self.skills_dir, safe_name)
        os.makedirs(skill_dir, exist_ok=True)

        # SKILL.md を生成
        header_parts = [f"# {name}"]
        if description:
            header_parts.append(f"Description: {description}")
        if triggers:
            header_parts.append(f"Triggers: {', '.join(triggers)}")
        if tags:
            header_parts.append(f"Tags: {', '.join(tags)}")
        header_parts.append("")  # 空行

        full_content = "\n".join(header_parts) + "\n" + content

        skill_file = os.path.join(skill_dir, "SKILL.md")
        with open(skill_file, "w", encoding="utf-8") as f:
            f.write(full_content)

        # キャッシュ更新
        self._load_all()

        return {
            "success": True,
            "name": safe_name,
            "path": skill_file,
            "message": f"スキル「{name}」を作成しました。",
        }

    def update_skill(self, name: str, content: str) -> dict:
        """既存スキルのSKILL.mdを更新"""
        if name not in self._cache:
            return {"success": False, "error": f"スキル「{name}」が見つかりません"}

        skill_file = self._cache[name]["path"]
        with open(skill_file, "w", encoding="utf-8") as f:
            f.write(content)

        self._load_all()
        return {"success": True, "message": f"スキル「{name}」を更新しました。"}

    def delete_skill(self, name: str) -> dict:
        """スキルを削除"""
        if name not in self._cache:
            return {"success": False, "error": f"スキル「{name}」が見つかりません"}

        import shutil
        skill_dir = os.path.join(self.skills_dir, name)
        shutil.rmtree(skill_dir, ignore_errors=True)

        self._load_all()
        return {"success": True, "message": f"スキル「{name}」を削除しました。"}
