"""Terminal text helpers shared by the CLI surfaces.

Both ``memory`` and ``eval`` print tables whose headers are Chinese and whose
cells are numbers. ``str.format``'s width counts code points, so a CJK label
padded with it lands two columns left of the numbers it is supposed to sit
above. These helpers count terminal columns instead, which is the only way to
keep mixed-width tables aligned.
"""

from __future__ import annotations

import unicodedata


def display_width(text: str) -> int:
    """Columns ``text`` occupies in a terminal, counting CJK as two."""
    return sum(
        2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text
    )


def pad(text: str, width: int) -> str:
    """Left-justify ``text`` to a terminal display width."""
    return text + " " * max(0, width - display_width(text))


def rpad(text: str, width: int) -> str:
    """Right-justify ``text`` to a terminal display width."""
    return " " * max(0, width - display_width(text)) + text
