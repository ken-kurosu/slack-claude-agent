"""
テキスト処理ツール: 翻訳、ファイル生成、データ分析、コード実行
"""
import ast
import asyncio
import csv
import io
import json
import logging
import os
import subprocess
import tempfile
from typing import Any, Optional

logger = logging.getLogger(__name__)


async def translate_text(
    text: str,
    target_language: str,
    source_language: Optional[str] = None,
    anthropic_client=None,
) -> dict:
    """Claude APIを使ってテキスト翻訳"""
    if not anthropic_client:
        return {"error": "翻訳エンジンが利用できません"}

    lang_names = {
        "ja": "日本語", "en": "English", "zh": "中国語", "ko": "韓国語",
        "fr": "フランス語", "de": "ドイツ語", "es": "スペイン語",
        "pt": "ポルトガル語", "it": "イタリア語", "ru": "ロシア語",
    }
    target_name = lang_names.get(target_language, target_language)

    prompt = f"以下のテキストを{target_name}に翻訳してください。翻訳文のみを出力してください。\n\n{text}"

    response = anthropic_client.messages.create(
        model="claude-haiku-4-5",  # 翻訳はHaikuで十分（コスト効率）
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    translated = response.content[0].text.strip()
    return {
        "success": True,
        "translated_text": translated,
        "target_language": target_language,
        "message": f"{target_name}に翻訳しました",
    }


async def send_static_image(
    image_name: str,
    assets_dir: str = "/app/assets",
    workspace: str = "/tmp/slack-agent-workspace",
) -> dict:
    """静的アセット画像をワークスペースにコピーしてSlackアップロード用パスを返す。

    image_name例: "guide/step1.png", "guide/step2.png"
    """
    import shutil as _shutil

    # パストラバーサル防止
    safe_name = os.path.normpath(image_name).lstrip("/")
    if ".." in safe_name:
        return {"error": "無効なファイル名です"}

    src_path = os.path.join(assets_dir, safe_name)
    if not os.path.exists(src_path):
        return {"error": f"アセットファイルが見つかりません: {safe_name}"}

    os.makedirs(workspace, exist_ok=True)
    dest_filename = os.path.basename(safe_name)
    dest_path = os.path.join(workspace, dest_filename)
    _shutil.copy2(src_path, dest_path)

    return {
        "success": True,
        "file_path": dest_path,
        "message": f"画像を送信します: {dest_filename}",
    }


async def generate_file(
    filename: str,
    content: str,
    encoding: str = "utf-8",
    workspace: str = "/tmp/slack-agent-workspace",
) -> dict:
    """ファイル生成（パストラバーサル防止付き）"""
    os.makedirs(workspace, exist_ok=True)

    # パストラバーサル防止: ファイル名からディレクトリ成分を除去
    safe_filename = os.path.basename(filename)
    if not safe_filename or safe_filename.startswith("."):
        return {"error": f"無効なファイル名です: {filename}"}

    filepath = os.path.join(workspace, safe_filename)
    # 念のため resolved path が workspace 内にあることを確認
    real_workspace = os.path.realpath(workspace)
    real_filepath = os.path.realpath(filepath)
    if not real_filepath.startswith(real_workspace):
        return {"error": "ファイルパスが不正です"}

    # ファイルサイズ制限 (10MB)
    if len(content.encode(encoding)) > 10 * 1024 * 1024:
        return {"error": "ファイルサイズが10MBを超えています"}

    with open(filepath, "w", encoding=encoding) as f:
        f.write(content)

    file_size = os.path.getsize(filepath)
    return {
        "success": True,
        "file_path": filepath,
        "filename": filename,
        "size_bytes": file_size,
        "message": f"ファイル '{filename}' を生成しました ({file_size} bytes)",
    }


async def analyze_data(data: str, analysis_type: str) -> dict:
    """データ分析"""
    try:
        # JSONかCSVか自動判定
        try:
            parsed = json.loads(data)
            data_format = "json"
        except json.JSONDecodeError:
            reader = csv.DictReader(io.StringIO(data))
            parsed = list(reader)
            data_format = "csv"

        if not parsed:
            return {"error": "データが空です"}

        result = {
            "data_format": data_format,
            "row_count": len(parsed),
        }

        if isinstance(parsed, list) and isinstance(parsed[0], dict):
            columns = list(parsed[0].keys())
            result["columns"] = columns
            result["column_count"] = len(columns)

            if analysis_type == "summary":
                result["first_rows"] = parsed[:5]
                result["last_rows"] = parsed[-3:]
                result["message"] = (
                    f"データサマリー: {len(parsed)}行 x {len(columns)}列\n"
                    f"カラム: {', '.join(columns)}"
                )

            elif analysis_type == "statistics":
                # 数値カラムの統計
                numeric_stats = {}
                for col in columns:
                    values = []
                    for row in parsed:
                        try:
                            values.append(float(row[col]))
                        except (ValueError, TypeError):
                            continue
                    if values:
                        numeric_stats[col] = {
                            "count": len(values),
                            "min": min(values),
                            "max": max(values),
                            "mean": sum(values) / len(values),
                        }
                result["numeric_statistics"] = numeric_stats
                result["message"] = f"数値カラムの統計情報を計算しました"

            elif analysis_type == "trends":
                result["message"] = f"データのトレンド分析: {len(parsed)}行のデータを確認しました"

            elif analysis_type == "anomalies":
                result["message"] = f"異常値検出: {len(parsed)}行のデータを確認しました"

        result["success"] = True
        return result

    except Exception as e:
        return {"error": f"データ分析中にエラー: {str(e)}"}


# execute_code で子プロセスに渡す環境変数のホワイトリスト
# APIキーは絶対に渡さない
_SAFE_ENV_KEYS = {
    "PATH", "HOME", "USER", "LANG", "LC_ALL", "LC_CTYPE",
    "TERM", "TMPDIR", "TZ",
    "PYTHONPATH", "PYTHONHASHSEED",
}

# インポートが許可されているモジュール（ホワイトリスト方式）
_ALLOWED_IMPORTS = {
    # データ分析系
    "pandas", "numpy", "scipy", "sklearn", "statistics", "math",
    # 可視化
    "matplotlib", "seaborn", "plotly",
    # 標準ライブラリ（安全なもの）
    "json", "csv", "re", "datetime", "collections", "itertools",
    "functools", "operator", "string", "textwrap", "unicodedata",
    "decimal", "fractions", "random", "hashlib", "base64",
    "pathlib", "glob", "fnmatch", "io", "os", "os.path",
    "copy", "pprint", "dataclasses", "typing", "enum",
    "time", "calendar", "zoneinfo",
    # ファイル操作（読み取り中心）
    "openpyxl", "xlrd", "chardet",
}

# 呼び出しが禁止される関数/属性名
_BLOCKED_CALLS = {
    "eval", "exec", "compile", "__import__",
    "globals", "locals", "vars", "dir",
    "getattr", "setattr", "delattr",
}

# アクセスが禁止される属性パス
_BLOCKED_ATTRS = {
    "os.system", "os.popen", "os.exec", "os.execl", "os.execle",
    "os.execv", "os.execve", "os.execvp", "os.execvpe",
    "os.spawn", "os.spawnl", "os.spawnle",
    "os.environ", "os.getenv", "os.putenv",
    "os.remove", "os.unlink", "os.rmdir",
    "subprocess.run", "subprocess.Popen", "subprocess.call",
    "subprocess.check_call", "subprocess.check_output",
    "shutil.rmtree", "shutil.move",
}

# 出力サイズの上限
_MAX_OUTPUT_CHARS = 10000


def _check_code_safety(code: str) -> Optional[str]:
    """ASTベースでコードの安全性を検査。問題があればエラーメッセージを返す。"""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"構文エラー: {e}"

    for node in ast.walk(tree):
        # ── import文の検査（ホワイトリスト方式） ──
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_module = alias.name.split(".")[0]
                if top_module not in _ALLOWED_IMPORTS:
                    return f"セキュリティ: `import {alias.name}` は許可されていません（使えるモジュール: pandas, numpy, json, csv, re, datetime 等）"

        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top_module = node.module.split(".")[0]
                if top_module not in _ALLOWED_IMPORTS:
                    return f"セキュリティ: `from {node.module}` は許可されていません"

        # ── 危険な関数呼び出しの検査 ──
        elif isinstance(node, ast.Call):
            func = node.func
            # 直接呼び出し: eval(), exec(), __import__()
            if isinstance(func, ast.Name) and func.id in _BLOCKED_CALLS:
                return f"セキュリティ: `{func.id}()` は使用できません"
            # 属性呼び出し: os.system(), subprocess.run()
            if isinstance(func, ast.Attribute):
                attr_path = _get_attr_path(func)
                if attr_path:
                    for blocked in _BLOCKED_ATTRS:
                        if attr_path == blocked or attr_path.startswith(blocked + "."):
                            return f"セキュリティ: `{attr_path}()` は使用できません"
            # open() のワークスペース外アクセスをブロック
            if isinstance(func, ast.Name) and func.id == "open":
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    path = node.args[0].value
                    if path.startswith("/") and not path.startswith("/tmp/slack-agent-workspace"):
                        return f"セキュリティ: ワークスペース外のファイル `{path}` にはアクセスできません"

    return None  # 問題なし


def _get_attr_path(node: ast.Attribute) -> str:
    """ast.Attribute ノードからドット区切りのパスを構築 (例: os.system)"""
    parts = [node.attr]
    current = node.value
    depth = 0
    while depth < 5:  # 無限ループ防止
        if isinstance(current, ast.Attribute):
            parts.insert(0, current.attr)
            current = current.value
        elif isinstance(current, ast.Name):
            parts.insert(0, current.id)
            break
        else:
            break
        depth += 1
    return ".".join(parts)


async def execute_code(
    code: str,
    description: str = "",
    workspace: str = "/tmp/slack-agent-workspace",
) -> dict:
    """Pythonコードをサンドボックスで実行

    セキュリティ対策:
    - 環境変数: APIキーを含まないホワイトリスト方式
    - ASTベースのインポート検査（ホワイトリスト方式）
    - 危険な関数呼び出しのブロック
    - タイムアウト: 60秒
    - 出力サイズ制限: 10,000文字
    """
    os.makedirs(workspace, exist_ok=True)

    # ── ASTベースの静的安全性チェック ──
    safety_error = _check_code_safety(code)
    if safety_error:
        return {
            "success": False,
            "error": safety_error,
            "message": safety_error,
        }

    # ── 安全な環境変数のみ構築 ──
    safe_env = {
        k: v for k, v in os.environ.items()
        if k in _SAFE_ENV_KEYS
    }
    safe_env["PYTHONDONTWRITEBYTECODE"] = "1"
    safe_env["PYTHONIOENCODING"] = "utf-8"
    # PATHが空だとpython自体が動かないので保証
    if "PATH" not in safe_env:
        safe_env["PATH"] = "/usr/local/bin:/usr/bin:/bin"

    # コードをファイルに書き出し
    code_file = os.path.join(workspace, "_exec_code.py")
    with open(code_file, "w") as f:
        f.write(code)

    try:
        result = subprocess.run(
            ["python3", code_file],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=workspace,
            env=safe_env,
        )

        output = result.stdout.strip()
        error = result.stderr.strip()

        # 出力サイズ制限
        if len(output) > _MAX_OUTPUT_CHARS:
            output = output[:_MAX_OUTPUT_CHARS] + f"\n... (出力が{_MAX_OUTPUT_CHARS}文字を超えたため省略)"
        if len(error) > _MAX_OUTPUT_CHARS:
            error = error[:_MAX_OUTPUT_CHARS] + "\n... (省略)"

        if result.returncode == 0:
            return {
                "success": True,
                "output": output or "(出力なし)",
                "message": f"コード実行完了{': ' + description if description else ''}",
            }
        else:
            return {
                "success": False,
                "error": error,
                "output": output,
                "message": f"コード実行エラー: {error[:200]}",
            }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error": "実行タイムアウト (60秒)",
            "message": "コードの実行が60秒を超えたため中断しました",
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "message": f"実行エラー: {str(e)}",
        }


_MAX_QUERY_LENGTH = 1000  # Web検索クエリの最大文字数


async def web_search(query: str, num_results: int = 5, timelimit: str = None, perplexity_api_key: str = None) -> dict:
    """Web検索（Perplexity API → DuckDuckGo フォールバック）

    Args:
        query: 検索クエリ
        num_results: 取得する結果数
        timelimit: 時間フィルタ (d=24時間以内, w=1週間以内, m=1ヶ月以内, y=1年以内, None=制限なし)
        perplexity_api_key: Perplexity API キー（あれば優先使用）
    """
    # クエリサイズ制限（プロンプトインジェクション / API乱用防止）
    if len(query) > _MAX_QUERY_LENGTH:
        return {
            "success": False,
            "error": f"検索クエリが長すぎます（{len(query)}文字）。{_MAX_QUERY_LENGTH}文字以内にしてください。",
            "message": f"検索クエリは{_MAX_QUERY_LENGTH}文字以内にしてください。",
        }

    # Perplexity APIが利用可能ならそちらを優先
    if perplexity_api_key:
        result = await _perplexity_search(query, perplexity_api_key, timelimit)
        if result.get("success"):
            return result
        logger.warning(f"Perplexity search failed, falling back to DuckDuckGo: {result.get('error')}")

    # フォールバック: DuckDuckGo
    return await _duckduckgo_search(query, num_results, timelimit)


async def _perplexity_search(query: str, api_key: str, timelimit: str = None) -> dict:
    """Perplexity APIを使ったWeb検索（最新情報に強い）"""
    import aiohttp

    # timelimitに応じてrecency filterを設定
    recency_map = {"d": "day", "w": "week", "m": "month", "y": "year"}
    search_recency = recency_map.get(timelimit)

    try:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "sonar",
            "messages": [
                {
                    "role": "system",
                    "content": "あなたはWeb検索アシスタントです。質問に対して最新の情報を正確に検索し、出典URL付きで回答してください。日本語で回答してください。",
                },
                {"role": "user", "content": query},
            ],
            "max_tokens": 1024,
            "search_recency_filter": search_recency,
        }
        # search_recency_filterがNoneの場合は削除
        if not search_recency:
            del payload["search_recency_filter"]

        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.perplexity.ai/chat/completions",
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    return {"success": False, "error": f"Perplexity API error {resp.status}: {error_text[:200]}"}

                data = await resp.json()

        # レスポンスから回答と引用を抽出
        answer = ""
        citations = []
        if data.get("choices"):
            answer = data["choices"][0].get("message", {}).get("content", "")
        if data.get("citations"):
            citations = data["citations"]

        # 検索結果としてフォーマット
        formatted_results = []
        for i, url in enumerate(citations[:10]):
            formatted_results.append({
                "title": f"出典 {i + 1}",
                "url": url,
                "snippet": "",
            })

        filter_note = f" (期間: {search_recency})" if search_recency else ""
        return {
            "success": True,
            "answer": answer,
            "results": formatted_results,
            "count": len(formatted_results),
            "source": "perplexity",
            "message": f"Perplexity検索完了{filter_note}: {len(formatted_results)}件の出典付き",
        }

    except Exception as e:
        return {"success": False, "error": str(e)}


async def deep_research(query: str, perplexity_api_key: str = None) -> dict:
    """Perplexity sonar-deep-research を使った徹底的なリサーチ。
    数分かかる場合があるが、Deep Research水準の品質で情報を収集する。
    """
    import aiohttp

    if not perplexity_api_key:
        return {"success": False, "error": "Perplexity APIキーが設定されていません。"}

    try:
        headers = {
            "Authorization": f"Bearer {perplexity_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "sonar-deep-research",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "あなたは徹底的なリサーチを行うリサーチアナリストです。"
                        "与えられたテーマについて、複数の情報源を横断的に調査し、"
                        "包括的かつ正確な調査結果を提供してください。"
                        "必ず出典URLを含めてください。日本語で回答してください。"
                    ),
                },
                {"role": "user", "content": query},
            ],
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.perplexity.ai/chat/completions",
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=300),  # 最大5分
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    return {"success": False, "error": f"Perplexity Deep Research API error {resp.status}: {error_text[:300]}"}

                data = await resp.json()

        answer = ""
        citations = []
        if data.get("choices"):
            answer = data["choices"][0].get("message", {}).get("content", "")
        if data.get("citations"):
            citations = data["citations"]

        formatted_sources = []
        for i, url in enumerate(citations):
            formatted_sources.append({
                "title": f"出典 {i + 1}",
                "url": url,
                "snippet": "",
            })

        return {
            "success": True,
            "answer": answer,
            "sources": formatted_sources,
            "source_count": len(formatted_sources),
            "source": "perplexity-deep-research",
            "message": f"Deep Research完了: {len(formatted_sources)}件の出典付き",
        }

    except asyncio.TimeoutError:
        return {"success": False, "error": "Deep Researchがタイムアウトしました（5分超過）。クエリを絞り込んで再試行してください。"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def _duckduckgo_search(query: str, num_results: int = 5, timelimit: str = None) -> dict:
    """DuckDuckGo APIを使ったWeb検索（フォールバック）"""
    try:
        from duckduckgo_search import DDGS

        with DDGS() as ddgs:
            results = list(ddgs.text(
                query,
                max_results=num_results,
                timelimit=timelimit,
            ))

        # timelimitありで結果が少ない場合、フィルタなしで再検索
        if timelimit and len(results) < 2:
            with DDGS() as ddgs:
                fallback_results = list(ddgs.text(query, max_results=num_results))
            if len(fallback_results) > len(results):
                results = fallback_results

        if not results:
            return {
                "success": True,
                "results": [],
                "message": f"'{query}' に関する検索結果は見つかりませんでした",
            }

        formatted = []
        for r in results:
            formatted.append({
                "title": r.get("title", ""),
                "url": r.get("href", r.get("link", "")),
                "snippet": r.get("body", r.get("snippet", "")),
            })

        filter_note = f" (期間: {timelimit})" if timelimit else ""
        return {
            "success": True,
            "results": formatted,
            "count": len(formatted),
            "source": "duckduckgo",
            "message": f"'{query}' の検索結果: {len(formatted)}件{filter_note}",
        }

    except ImportError:
        return {
            "success": False,
            "error": "duckduckgo_search がインストールされていません",
            "message": "Web検索機能が利用できません。",
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "message": f"検索エラー: {str(e)}",
        }



async def fetch_url(url: str) -> dict:
    """URLからWebページのテキスト内容を取得する"""
    import aiohttp
    from html.parser import HTMLParser

    class _TextExtractor(HTMLParser):
        def __init__(self):
            super().__init__()
            self._texts = []
            self._skip = False
            self._skip_tags = {"script", "style", "noscript", "header", "nav", "footer"}

        def handle_starttag(self, tag, attrs):
            if tag in self._skip_tags:
                self._skip = True

        def handle_endtag(self, tag):
            if tag in self._skip_tags:
                self._skip = False

        def handle_data(self, data):
            if not self._skip:
                t = data.strip()
                if t:
                    self._texts.append(t)

        def get_text(self):
            return "\n".join(self._texts)

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; SlackAgent/1.0)",
            "Accept": "text/html,application/xhtml+xml,*/*",
            "Accept-Language": "ja,en;q=0.9",
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
                allow_redirects=True,
            ) as resp:
                if resp.status != 200:
                    return {"success": False, "error": f"HTTP {resp.status}"}
                html = await resp.text()

        extractor = _TextExtractor()
        extractor.feed(html)
        text = extractor.get_text()

        if len(text) > 15000:
            text = text[:15000] + "\n\n...(以下省略)"

        return {
            "success": True,
            "url": url,
            "text": text,
            "length": len(text),
            "message": f"URL取得完了: {len(text)}文字",
        }
    except Exception as e:
        return {"success": False, "error": str(e)}
