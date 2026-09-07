from __future__ import annotations

import re
from collections import Counter

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def tokenize(text: str) -> list[str]:
    """Lowercase ASCII tokenizer robust to CJK and arbitrary payloads.

    CJK text has no obvious naive word boundary, so it is retained as
    contiguous runs of CJK codepoints. The tokenizer is intentionally small:
    it is a deterministic baseline, not a replacement for an embedding store.
    """
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(text):
        token = match.group(0).lower()
        if token:
            tokens.append(token)
    cjk_runs = re.findall(r"[\u3400-\u9fff]+", text)
    tokens.extend(run.lower() for run in cjk_runs)
    return tokens


def _keyword_score(query: str, text: str) -> float:
    query_tokens = tokenize(query)
    if not query_tokens:
        return 0.0
    text_tokens = Counter(tokenize(text))
    total = len(set(query_tokens))
    matched = 0
    for token in set(query_tokens):
        if token in text_tokens:
            matched += 1
    return matched / total


def _phrase_bonus(query: str, text: str) -> float:
    lowered = text.lower()
    query_lowered = query.lower().strip()
    if not query_lowered:
        return 0.0
    return 0.25 if query_lowered in lowered else 0.0
