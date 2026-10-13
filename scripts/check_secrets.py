#!/usr/bin/env python3
"""秘密情報の混入検査（コミット前フック・CI から呼ぶ。gitleaks が無い環境でも動く最低限の検査）

  python scripts/check_secrets.py            — git で管理しているファイル全部を検査
  python scripts/check_secrets.py --staged   — コミットしようとしている変更だけを検査

見つかったら exit 1 で止める。誤検知は該当行に `# secret-scan: ok` と書けば通る（理由も書く）。
"""
import re
import subprocess
import sys

PATTERNS = [
    ("Slack トークン", re.compile(r"xox[abposr]-[0-9A-Za-z-]{10,}")),
    ("Slack App トークン", re.compile(r"xapp-\d-[0-9A-Za-z-]{10,}")),
    ("Slack Webhook", re.compile(r"hooks\.slack\.com/services/T[0-9A-Z]+/B[0-9A-Z]+/[0-9A-Za-z]+")),
    ("Anthropic / Claude トークン", re.compile(r"sk-ant-[a-z0-9]{2,}-[0-9A-Za-z_-]{20,}")),
    ("OpenAI キー", re.compile(r"sk-(proj-)?[0-9A-Za-z]{32,}")),
    ("GitHub トークン", re.compile(r"(ghp|gho|ghu|ghs|ghr)_[0-9A-Za-z]{30,}|github_pat_[0-9A-Za-z_]{40,}")),
    ("AWS アクセスキー", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("Google API キー", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("Notion トークン", re.compile(r"(secret|ntn)_[0-9A-Za-z]{40,}")),
    ("秘密鍵", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("サービスアカウント鍵", re.compile(r"\"private_key\"\s*:\s*\"-----BEGIN")),
    ("代入された秘密値", re.compile(r"(?i)\b[A-Z0-9_]*(TOKEN|SECRET|PASSWORD|API_KEY)\s*=\s*['\"]?[0-9A-Za-z_\-/+=]{20,}")),
]
ALLOW = "secret-scan: ok"
SKIP_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".pdf", ".woff2", ".ttf", ".ico")


def _git(*args) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def targets(staged: bool):
    if staged:
        diff = _git("diff", "--cached", "-U0", "--no-color")
        cur = None
        for line in diff.splitlines():
            if line.startswith("+++ b/"):
                cur = line[6:]
            elif line.startswith("+") and not line.startswith("+++") and cur:
                yield cur, line[1:]
        return
    for path in _git("ls-files").splitlines():
        if path.endswith(SKIP_SUFFIX):
            continue
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    yield path, line.rstrip("\n")
        except (FileNotFoundError, IsADirectoryError):
            continue


def main() -> int:
    staged = "--staged" in sys.argv
    found = []
    for path, line in targets(staged):
        if ALLOW in line or path.endswith(SKIP_SUFFIX) or path == "scripts/check_secrets.py":
            continue
        for label, rx in PATTERNS:
            if rx.search(line):
                found.append((path, label, line.strip()[:60]))
    # .env 等の秘密ファイル自体が管理対象に入っていないか
    tracked = _git("ls-files").splitlines()
    for p in tracked:
        name = p.rsplit("/", 1)[-1]
        if (name == ".env" or (name.startswith(".env.") and name != ".env.example")
                or name == ".mcp.json" or p.startswith("credentials/") or p.startswith("memory_data/")):
            found.append((p, "管理してはいけないファイル", ""))
    if not found:
        print("secret-scan: 0 件")
        return 0
    print("⛔ 秘密情報の可能性があるものが見つかりました（コミットを止めます）:")
    for path, label, snippet in found:
        masked = re.sub(r"[0-9A-Za-z]{8,}", lambda m: m.group(0)[:4] + "…", snippet)
        print(f"  {path}: {label}  {masked}")
    print("誤検知なら、その行に `# secret-scan: ok（理由）` を書いてください。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
