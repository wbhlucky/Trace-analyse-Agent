"""Domain concept lexicon for the offline embedder.

A hashed bag-of-n-grams cannot know that "blank screen" and "白屏" are the same
symptom, because they share no characters. A real embedding model would; this
project has no model dependency and recall must work offline, so the offline
recipe carries a small closed lexicon instead: trace-diagnosis symptoms are a
short, knowable vocabulary, and mapping every surface form to one concept token
gives the local embedder the one thing n-grams structurally cannot provide -
cross-lingual and synonym matching.

This is a stopgap with a defined successor, not a competing design. Configuring
``TRACE_AGENT_MEMORY_EMBEDDING=http`` swaps in a real model and the lexicon stops
being consulted; nothing else changes, because the concept tokens are just
features in the same vector.

Matching rules exist to keep the lexicon from firing on accidents:

* CJK forms match as substrings of a flattened (lowercased, punctuation-free)
  text, which is how Chinese has to be matched at all.
* Single ASCII words match whole tokens from :func:`trace_agent.memory.text.tokenize`,
  never substrings: ``gc`` is a concept, and ``logcat`` contains the letters
  ``gc``. Substring matching there would tag half the corpus with garbage
  collection.
* Multi-word ASCII forms match flattened substrings, since they are long enough
  (``blankscreen``) that accidental containment is not a practical risk.
"""

from __future__ import annotations

import re

from trace_agent.memory.text import tokenize

_FLATTEN_RE = re.compile(r"[^\w\u3400-\u9fff]+", re.UNICODE)
_ASCII_RE = re.compile(r"^[a-z0-9 _+.:/-]+$")
# Multi-word ASCII forms shorter than this are skipped: short phrases are where
# accidental containment lives, and every phrase in the lexicon is longer.
_MIN_PHRASE_LEN = 6

# concept -> surface forms. Keep forms lowercase; matching lowercases the input.
CONCEPTS: dict[str, tuple[str, ...]] = {
    "white_screen": (
        "白屏",
        "空白页",
        "空白屏幕",
        "页面空白",
        "首屏空白",
        "黑屏",
        "blank screen",
        "white screen",
        "blank page",
        "empty page",
    ),
    "cold_start": (
        "冷启动",
        "冷启",
        "首次启动",
        "冷起动",
        "cold start",
        "coldstart",
        "cold launch",
    ),
    "launch": ("启动", "拉起", "launch", "startup", "start up", "boot"),
    "jank": (
        "卡顿",
        "掉帧",
        "卡帧",
        "丢帧",
        "不流畅",
        "帧率",
        "jank",
        "frame drop",
        "fps",
        "stutter",
    ),
    "anr": ("anr", "无响应", "卡死", "假死", "not responding", "application not responding"),
    "crash": ("崩溃", "闪退", "异常退出", "crash", "tombstone", "fatal"),
    "main_thread": ("主线程", "ui线程", "ui 线程", "main thread", "ui thread", "mainthread"),
    "sync_io": (
        "同步io",
        "同步 io",
        "sync io",
        "disk io",
        "文件读写",
        "读文件",
        "file read",
    ),
    "memory_leak": (
        "内存泄漏",
        "内存泄露",
        "内存溢出",
        "memory leak",
        "oom",
        "out of memory",
    ),
    "latency": (
        "耗时",
        "延迟",
        "超时",
        "慢",
        "latency",
        "slow",
        "timeout",
        "duration",
    ),
    "gc": ("gc", "垃圾回收", "garbage collection"),
    "inflate": ("布局加载", "加载布局", "视图加载", "inflate", "inflation", "layout"),
    "network": ("网络请求", "网络", "接口", "network", "http", "request", "socket"),
    "database": ("数据库", "慢查询", "database", "sqlite", "query", "sql"),
    "cpu": ("cpu占用", "cpu飙升", "cpu", "高负载", "busy loop"),
    "classload": ("类加载", "加载类", "classload", "class load", "dex", "dexopt"),
    "render": ("渲染", "绘制", "render", "draw", "vsync", "choreographer"),
    "scroll": ("滑动", "滚动", "scroll", "fling", "recycler"),
    "process": ("进程", "子进程", "zygote", "process", "fork"),
    "thermal": ("发热", "温度", "thermal", "overheat"),
    "power": ("耗电", "功耗", "power", "battery", "wakeup"),
}


def flatten(text: str) -> str:
    """Lowercase, collapse away whitespace and punctuation.

    The same normalisation :func:`trace_agent.memory.text.fingerprint` applies to
    a topic, reused here so "同步 IO" and "同步IO" are one surface form rather
    than two lexicon entries an operator has to remember to keep in sync.
    """
    return _FLATTEN_RE.sub("", (text or "").lower())


def _matches(form: str, tokens: set[str], flat: str) -> bool:
    if not _ASCII_RE.match(form):
        return form in flat
    if " " not in form:
        return form in tokens
    collapsed = form.replace(" ", "")
    return len(collapsed) >= _MIN_PHRASE_LEN and collapsed in flat


def concepts_in(text: str) -> list[str]:
    """Concept tokens present in ``text``, in lexicon order.

    Ordered rather than set-ordered so two runs over the same text produce the
    same feature sequence, which matters because the caller weights features and
    a nondeterministic order would make the vector - and therefore recall order
    - depend on hash seeding.
    """
    if not text:
        return []
    tokens = set(tokenize(text))
    flat = flatten(text)
    return [
        concept
        for concept, forms in CONCEPTS.items()
        if any(_matches(form, tokens, flat) for form in forms)
    ]


__all__ = ["CONCEPTS", "concepts_in", "flatten"]
