"""日本語対応のテキストマッチング（外部依存なし）。

旧来の `query.split()`（whitespace 分割）は、日本語が分かち書きされないため
文がまるごと 1 トークンになり、完全一致以外ヒットしなかった。
ここでは文字 bigram + ASCII 単語でトークン化し、重なりベースでスコアリングする。
SudachiPy 等の形態素解析を入れずに「関連知識がほぼ引けない」状態を解消するのが目的。
"""
import re

_ASCII_WORD = re.compile(r"[a-z0-9]+")
# 日本語文字（ひらがな・カタカナ・長音・漢字・々）の連続
_JP_RUN = re.compile(r"[ぁ-んァ-ヴーｦ-ﾟ一-鿿々]+")


def tokenize(text: str) -> set:
    """テキストを検索用トークン集合に変換。

    - ASCII 単語（小文字化、2 文字以上）
    - 日本語は連続文字列ごとに bigram（連続 2 文字）。1 文字語はそのまま採用。
    """
    if not text:
        return set()
    tokens = set()
    lowered = text.lower()
    for m in _ASCII_WORD.findall(lowered):
        if len(m) >= 2:
            tokens.add(m)
    for run in _JP_RUN.findall(text):
        if len(run) == 1:
            tokens.add(run)
        else:
            for i in range(len(run) - 1):
                tokens.add(run[i:i + 2])
    return tokens


def score(query: str, text: str) -> float:
    """query と text の関連度（0.0〜1.0）。query トークンのうち text に含まれる割合。"""
    q = tokenize(query)
    if not q:
        return 0.0
    d = tokenize(text)
    if not d:
        return 0.0
    return len(q & d) / len(q)


def matches(query: str, text: str, threshold: float = 0.3) -> bool:
    """query が text に関連すると見なせるか（しきい値以上のスコア）。"""
    return score(query, text) >= threshold


def overlap_count(query: str, text: str) -> int:
    """query と text の共有トークン数（重み付けに使う）。"""
    return len(tokenize(query) & tokenize(text))
