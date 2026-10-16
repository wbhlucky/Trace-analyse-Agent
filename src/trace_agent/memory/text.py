from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
_CJK_RUN_RE = re.compile(r"[\u3400-\u9fff]+")
_WHITESPACE_RE = re.compile(r"\s+")
# Punctuation is dropped before fingerprinting a topic: "主线程同步 IO" and
# "主线程同步 IO。" are the same claim, and a fingerprint that disagreed would
# let two runs record the same conclusion as two topics and never compare them.
_PUNCTUATION_RE = re.compile(r"[^\w\u3400-\u9fff]+", re.UNICODE)


def cjk_bigrams(run: str) -> list[str]:
    """Split one contiguous CJK run into overlapping bigrams.

    Chinese has no whitespace word boundary, and symptom terms are usually two
    characters ("白屏", "冷启", "卡顿"). Keeping a whole run as a single token
    made recall depend on byte-identical sentences: "冷启动 白屏" never matched
    a memory storing "冷启动白屏". Bigrams make both index and query side align
    on the same unit, which is the same trick FTS5 needs for CJK.
    """
    if len(run) <= 1:
        return [run] if run else []
    return [run[index : index + 2] for index in range(len(run) - 1)]


def tokenize(text: str) -> list[str]:
    """Lowercase tokenizer robust to CJK and arbitrary payloads.

    ASCII words are kept whole; CJK runs are expanded into overlapping bigrams.
    Indexing and querying must share this function so both sides produce the
    same units. It is a deterministic baseline, not a replacement for an
    embedding store.
    """
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(text):
        token = match.group(0).lower()
        if token:
            tokens.append(token)
    for run in cjk_runs(text):
        tokens.extend(cjk_bigrams(run.lower()))
    return tokens


def cjk_runs(text: str) -> list[str]:
    """Contiguous CJK spans in ``text``, in order.

    Public because the embedder needs character-level features over the same
    spans the tokenizer uses. A second regex in a second module is how two
    definitions of "what counts as Chinese" end up disagreeing.
    """
    return _CJK_RUN_RE.findall(text or "")


def fingerprint(text: str) -> str:
    """Stable identity of a claim, ignoring case, whitespace and punctuation.

    Used to decide whether two memories are *about the same thing*, which is a
    different question from whether they *say the same thing*. Two records that
    share a fingerprint but differ in content are candidates for a conflict;
    two that share a fingerprint and nearly share content are duplicates.
    """
    collapsed = _WHITESPACE_RE.sub(" ", text or "").strip().lower()
    return _PUNCTUATION_RE.sub("", collapsed)


def similarity(left: str, right: str) -> float:
    """Jaccard similarity over the shared tokenizer, in ``[0, 1]``.

    Symmetric and cheap, which is what a pairwise sweep over one scope needs.
    It is a lexical measure: P3 replaces the call site with cosine similarity
    over embeddings, and the thresholds move with it (0.92 lexical is roughly
    0.98 semantic for paraphrases).
    """
    left_tokens = set(tokenize(left))
    right_tokens = set(tokenize(right))
    if not left_tokens or not right_tokens:
        return 0.0
    union = left_tokens | right_tokens
    return len(left_tokens & right_tokens) / len(union)


def overlap_coefficient(left: str, right: str) -> float:
    """Intersection over the *smaller* token set, in ``[0, 1]``.

    Jaccard punishes length: one record explaining a root cause in a sentence and
    another explaining the same root cause in a paragraph can share every
    substantive word and still score 0.4. Dividing by the smaller set asks the
    question the conflict sweep actually needs - "is the shorter claim covered by
    the longer one?" - which is what separates a paraphrase from two records
    describing different mechanisms.
    """
    left_tokens = set(tokenize(left))
    right_tokens = set(tokenize(right))
    if not left_tokens or not right_tokens:
        return 0.0
    smaller = min(len(left_tokens), len(right_tokens))
    return len(left_tokens & right_tokens) / smaller


# Verdict vocabulary, not general sentiment. Each entry is a word a diagnostic
# report uses to *accept or reject a cause*, which is why "never" and "not" are
# absent: they describe symptoms ("no frame was ever produced") far more often
# than they refute a conclusion, and counting them turned ordinary findings into
# one-sided polarity.
_NEGATION_WORDS = frozenset({"unlikely", "excluded", "unrelated", "disproved"})
_NEGATION_PHRASES = (
    "ruled out",
    "rules out",
    "rule out",
    "not the cause",
    "not the root cause",
    "no evidence",
    "cannot explain",
    "can be excluded",
)
_NEGATION_CJK = (
    "排除",
    "不是根因",
    "并非根因",
    "与本次无关",
    "无关",
    "不成立",
    "予以否定",
    "否定",
    "无异常",
    "未发现异常",
    "不足以解释",
)
_ASSERTION_WORDS = frozenset({"confirmed", "indicates", "blames"})
_ASSERTION_PHRASES = (
    "root cause",
    "caused by",
    "due to",
    "points to",
    "responsible for",
)
_ASSERTION_CJK = (
    "根因",
    "根本原因",
    "确认为",
    "确认是",
    "证实",
    "指向",
    "定位为",
    "判定为",
    "原因是",
    "导致本次",
)


def polarity_signals(text: str) -> tuple[bool, bool]:
    """Whether ``text`` rejects a cause, and whether it asserts one.

    Both can be true - "主线程阻塞可排除，根因是 GPU 崩溃" rejects one cause and
    asserts another in the same sentence - which is exactly why the caller asks
    for a *pure* side before reading a pair as a disagreement.
    """
    flat = _WHITESPACE_RE.sub(" ", (text or "").lower())
    tokens = set(tokenize(text or ""))
    rejects = (
        any(word in tokens for word in _NEGATION_WORDS)
        or any(phrase in flat for phrase in _NEGATION_PHRASES)
        or any(phrase in flat for phrase in _NEGATION_CJK)
    )
    asserts = (
        any(word in tokens for word in _ASSERTION_WORDS)
        or any(phrase in flat for phrase in _ASSERTION_PHRASES)
        or any(phrase in flat for phrase in _ASSERTION_CJK)
    )
    return rejects, asserts


def polarity_conflict(left: str, right: str) -> bool:
    """Whether two claims take opposite sides on the same question.

    Deliberately narrow: one record must *only* reject and the other must *only*
    assert. A hedge on either side ("可能是 X，暂不能排除 Y") reads as no signal
    rather than a guess, because the cost of a false refutation is a halved
    confidence and eventually an archived memory, while the cost of a missed one
    is a pair that stays linked as corroboration until better evidence arrives.
    """
    left_rejects, left_asserts = polarity_signals(left)
    right_rejects, right_asserts = polarity_signals(right)
    left_reject_only = left_rejects and not left_asserts
    left_assert_only = left_asserts and not left_rejects
    right_reject_only = right_rejects and not right_asserts
    right_assert_only = right_asserts and not right_rejects
    return (left_reject_only and right_assert_only) or (
        left_assert_only and right_reject_only
    )
