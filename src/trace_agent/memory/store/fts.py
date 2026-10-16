"""FTS5 document and MATCH-expression construction.

SQLite's ``unicode61`` tokenizer splits on non-alphanumeric characters and
treats a run of Chinese as one token, so indexing raw text would reproduce
exactly the bug P0 fixed in :func:`trace_agent.memory.text.tokenize`: only a
byte-identical sentence could ever match. Both sides therefore go through the
same bigram expansion, and this module is the only place that knows how the
index and the query have to line up.
"""

from __future__ import annotations

from trace_agent.memory.text import tokenize


def fts_body(text: str) -> str:
    """Space-joined, de-duplicated tokens for one indexed FTS column.

    Bigram expansion roughly doubles the token count for Chinese text, so
    duplicates are dropped before indexing. That does not change which documents
    match - FTS5 only needs a term to be present - and bm25 is used here to
    order a candidate pool, not as the final score. It is also the mitigation
    section 13 asks for against bigram index bloat.
    """
    return " ".join(dict.fromkeys(tokenize(text)))


def fts_match(text: str) -> str | None:
    """Build an OR'd MATCH expression, or ``None`` when there are no tokens.

    ``MATCH`` ANDs its terms implicitly, so ``'白屏 耗时'`` would demand both and
    return nothing for a memory mentioning only one. Explicit ``OR`` makes the
    pool "shares at least one token with the query", which is exactly the set
    that can score above zero and therefore above the relevance floor - the
    property ``RecallService`` relies on to skip the full scan.

    Every term is quoted so no token can be reinterpreted as FTS5 syntax. Tokens
    from :func:`tokenize` are alphanumeric or CJK bigrams and cannot contain a
    quote, so the quoting is defensive rather than load-bearing.
    """
    tokens = dict.fromkeys(tokenize(text))
    if not tokens:
        return None
    return " OR ".join(f'"{token}"' for token in tokens)
