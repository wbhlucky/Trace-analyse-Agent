from __future__ import annotations

import pytest

from trace_agent.time_range import parse_time_range


def test_parse_time_range_preserves_exact_nanoseconds() -> None:
    parsed = parse_time_range(
        "788297524337656ns-788298373582031ns"
    )

    assert parsed is not None
    assert parsed.start_ns == 788_297_524_337_656
    assert parsed.end_ns == 788_298_373_582_031
    assert parsed.duration_ms == pytest.approx(849.244375)


def test_parse_time_range_accepts_seconds() -> None:
    parsed = parse_time_range("788297.524337656s ~ 788298.373582031s")

    assert parsed is not None
    assert parsed.start_ns == 788_297_524_337_656
    assert parsed.end_ns == 788_298_373_582_031


def test_parse_time_range_rejects_unitless_or_reversed_values() -> None:
    with pytest.raises(ValueError, match="格式无效"):
        parse_time_range("100-200")
    with pytest.raises(ValueError, match="结束时间"):
        parse_time_range("200ns-100ns")
