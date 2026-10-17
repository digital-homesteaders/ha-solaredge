"""Energy tick timing and inverter sleep (port of gruijter test/timing.js)."""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.solaredge_cloud.timing import (
    MARGIN_MS,
    PERIOD_MS,
    EnergyTiming,
    adapt_margin,
    delay_to_next_tick,
    is_asleep,
    parse_time_ms,
    tick_phase,
)


def ms(*args: int) -> int:
    return int(datetime(*args, tzinfo=UTC).timestamp() * 1000)


# Measured moments xx:x0:28 / xx:x5:28 (site 1665942).
AT28 = ms(2026, 10, 15, 12, 40, 28)


def test_functions() -> None:
    assert tick_phase([AT28]) == 28000 + MARGIN_MS
    assert tick_phase([]) == 0
    # Inverters far apart: back to the period boundary.
    assert tick_phase([AT28, AT28 + 150_000]) == 0
    assert adapt_margin(MARGIN_MS, False) == MARGIN_MS + 15000
    assert adapt_margin(MARGIN_MS + 15000, True) == MARGIN_MS + 10000
    now = ms(2026, 10, 15, 12, 41, 0)
    assert delay_to_next_tick(now, 48000) == (5 * 60 + 48 - 60) * 1000
    # At least a second away.
    assert delay_to_next_tick(ms(2026, 10, 15, 12, 40, 47) + 500, 48000) == 500 + PERIOD_MS


def test_margin_follows_the_uploads() -> None:
    timing = EnergyTiming()
    now = ms(2026, 10, 15, 12, 41, 0)
    assert timing.note_snapshot("INV1", AT28, now)
    assert timing.phase_ms == 28000 + MARGIN_MS
    # Right after the tick the same measurement says nothing: the next is not due yet.
    timing.judge_pending = True
    timing.note_snapshot("INV1", AT28, now)
    assert timing.margin_ms == MARGIN_MS
    # A period later it found the same one: too early; the margin grows, the tick moves later.
    timing.note_snapshot("INV1", AT28, AT28 + PERIOD_MS + MARGIN_MS)
    assert timing.margin_ms == MARGIN_MS + 15000
    assert timing.phase_ms == 28000 + MARGIN_MS + 15000
    # The next finds a new one: the margin eases back.
    timing.judge_pending = True
    timing.note_snapshot("INV1", AT28 + PERIOD_MS, AT28 + PERIOD_MS + 40000)
    assert timing.margin_ms == MARGIN_MS + 10000
    # A sleeping inverter (old measurement) does not judge the tick.
    timing.judge_pending = True
    timing.note_snapshot("INV1", AT28 + PERIOD_MS, AT28 + 10 * PERIOD_MS)
    assert timing.margin_ms == MARGIN_MS + 10000 and timing.judge_pending


def test_fifteen_minute_site() -> None:
    timing = EnergyTiming()
    timing.note_snapshot("INV2", AT28, AT28 + MARGIN_MS)
    timing.judge_pending = True
    timing.note_snapshot("INV2", AT28 + 3 * PERIOD_MS, AT28 + 3 * PERIOD_MS + MARGIN_MS)
    assert timing.cadence_ms == 3 * PERIOD_MS
    for period in (4, 5):
        timing.judge_pending = True
        timing.note_snapshot("INV2", AT28 + 3 * PERIOD_MS, AT28 + period * PERIOD_MS + MARGIN_MS)
    assert timing.margin_ms == MARGIN_MS
    # The upload that was due is late: that does judge.
    timing.judge_pending = True
    timing.note_snapshot("INV2", AT28 + 3 * PERIOD_MS, AT28 + 6 * PERIOD_MS + MARGIN_MS)
    assert timing.margin_ms == MARGIN_MS + 15000


def test_asleep() -> None:
    last = parse_time_ms("2026-10-15T18:55:28Z")
    night = [{"lastMeasurement": "2026-10-15T18:55:28Z", "pAc_W": 0}]

    def minutes(count: int) -> int:
        return last + count * 60_000

    assert not is_asleep(night, minutes(9))
    assert is_asleep(night, minutes(10))
    assert not is_asleep(night, minutes(14), 3 * PERIOD_MS)
    assert is_asleep(night, minutes(20), 3 * PERIOD_MS)
    assert not is_asleep([{**night[0], "pAc_W": 1040}], minutes(60))
    assert not is_asleep([{**night[0], "inverterStatus": "FAULT"}], minutes(60))
    assert is_asleep([{**night[0], "inverterStatus": "PRODUCTION"}], minutes(60))
    # Silent for more than a day is not a night.
    assert not is_asleep(night, minutes(25 * 60))
    assert not is_asleep([], minutes(60))
    assert not is_asleep([{"pAc_W": 0}], minutes(60))
