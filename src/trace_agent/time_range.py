from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True, slots=True)
class ParsedTimeRange:
    start_ns: int
    end_ns: int

    @property
    def duration_ms(self) -> float:
        return (self.end_ns - self.start_ns) / 1_000_000.0


_TIME_RANGE = re.compile(
    r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(ns|us|µs|ms|s)\s*"
    r"(?:-|~|～|\.\.|,)\s*"
    r"([0-9]+(?:\.[0-9]+)?)\s*(ns|us|µs|ms|s)\s*$",
    re.IGNORECASE,
)
_UNIT_NS = {
    "ns": Decimal(1),
    "us": Decimal(1_000),
    "µs": Decimal(1_000),
    "ms": Decimal(1_000_000),
    "s": Decimal(1_000_000_000),
}


def parse_time_range(value: str | None) -> ParsedTimeRange | None:
    if value is None or not value.strip():
        return None
    match = _TIME_RANGE.fullmatch(value)
    if match is None:
        raise ValueError(
            "time_range 格式无效；请使用带单位的 "
            "<start>-<end>，例如 788297524337656ns-788298373582031ns"
        )
    try:
        start_value = Decimal(match.group(1)) * _UNIT_NS[match.group(2).lower()]
        end_value = Decimal(match.group(3)) * _UNIT_NS[match.group(4).lower()]
    except (InvalidOperation, KeyError) as exc:
        raise ValueError("time_range 数值或单位无效") from exc
    if start_value != start_value.to_integral_value() or (
        end_value != end_value.to_integral_value()
    ):
        raise ValueError("time_range 无法精确换算为整数纳秒")
    start_ns = int(start_value)
    end_ns = int(end_value)
    if start_ns < 0 or end_ns <= start_ns:
        raise ValueError("time_range 结束时间必须晚于开始时间")
    return ParsedTimeRange(start_ns=start_ns, end_ns=end_ns)
