"""Energy tick timing and inverter sleep.

Port of lib/solarEdgeTiming.js and lib/inverterSleep.js from com.solaredge
v2.0.0 for Homey, Copyright 2026 Robin de Gruijter, GPL-3.0.

The energy tick goes right after SolarEdge has new inverter data. Measured on a
real site (2026-10-15): an inverter's lastMeasurement moves once per exactly
300 s, always at the same second, and is served within ~5 s; the energy
overview changes at the same moments. All times here are epoch milliseconds.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

PERIOD_MS = 5 * 60 * 1000

MARGIN_MS = 20 * 1000
MARGIN_STEP_MS = 15 * 1000
MARGIN_DECAY_MS = 5 * 1000
MAX_MARGIN_MS = 2 * 60 * 1000

# Snapshots of one account further apart than this fall back to the period boundary.
MAX_SPREAD_MS = 60 * 1000

# A measurement this long past its upload interval is the last one before the
# inverter stopped uploading for the night.
SILENT_MS = PERIOD_MS
# Silent longer than this is not a night.
MAX_SLEEP_MS = 24 * 60 * 60 * 1000


def learn_cadence(cadence_ms: int | None, gap_ms: int) -> int:
    """The site's upload interval after a new snapshot: the shortest seen, at least the period."""
    return max(PERIOD_MS, gap_ms if cadence_ms is None else min(cadence_ms, gap_ms))


def judges_tick(
    previous_ms: int, at_ms: int, now_ms: int, cadence_ms: int = PERIOD_MS
) -> bool:
    """Whether a tick that found snapshot at_ms says anything about the margin.

    A new snapshot always does; the old one again only when a new one was due
    by now. A snapshot older than one period past its due time is not current
    (the inverter sleeps): it does not judge a tick.
    """
    age = now_ms - at_ms
    if age >= cadence_ms + PERIOD_MS:
        return False
    return at_ms > previous_ms or age >= cadence_ms


def tick_phase(
    snapshot_ms: Iterable[int | None],
    margin_ms: int = MARGIN_MS,
    period_ms: int = PERIOD_MS,
) -> int:
    """The tick's position after the period boundary, from the snapshot moments.

    Returns 0 when unknown or when the inverters are too far apart.
    """
    phases = sorted(t % period_ms for t in snapshot_ms if isinstance(t, int))
    if not phases:
        return 0
    # Spread on a circle: the smallest arc that holds them all.
    widest_gap = phases[0] + period_ms - phases[-1]
    last = phases[-1]
    for before, after in zip(phases, phases[1:], strict=False):
        if after - before > widest_gap:
            widest_gap = after - before
            last = before
    if period_ms - widest_gap > MAX_SPREAD_MS:
        return 0
    return (last + margin_ms) % period_ms


def adapt_margin(margin_ms: int, got_new: bool) -> int:
    """The margin after a tick: shorter when it found new data, longer when not."""
    if got_new:
        return max(MARGIN_MS, margin_ms - MARGIN_DECAY_MS)
    return min(MAX_MARGIN_MS, margin_ms + MARGIN_STEP_MS)


def delay_to_next_tick(now_ms: int, phase_ms: int, period_ms: int = PERIOD_MS) -> int:
    """Milliseconds until the next moment phase_ms after a period boundary, at least 1 s away."""
    nxt = ((now_ms - phase_ms) // period_ms) * period_ms + phase_ms + period_ms
    return nxt - now_ms + period_ms if nxt - now_ms < 1000 else nxt - now_ms


def parse_time_ms(value: Any) -> int | None:
    """Epoch ms of an ISO 8601 time such as 2026-10-15T11:50:28Z; None when not one."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return int(parsed.timestamp() * 1000)


def is_asleep(
    live: Iterable[Mapping[str, Any]],
    now_ms: int,
    cadence_ms: int | None = None,
) -> bool:
    """Whether the inverters stopped uploading after their output fell to 0 W.

    Asleep for the night, not cut off while producing: an inverter whose last
    status was fault is not asleep.
    """
    items = list(live)
    if not items:
        return False
    cadence = cadence_ms or PERIOD_MS
    for item in items:
        at = parse_time_ms(item.get("lastMeasurement"))
        if at is None:
            return False
        silent = now_ms - at
        if (
            item.get("pAc_W") != 0
            or str(item.get("inverterStatus")).lower() == "fault"
            or not cadence + SILENT_MS <= silent < MAX_SLEEP_MS
        ):
            return False
    return True


class EnergyTiming:
    """Where in the 5-minute period an account's energy tick goes (app.js noteSnapshot)."""

    def __init__(self) -> None:
        """Start at the period boundary."""
        self.phase_ms = 0
        self.margin_ms = MARGIN_MS
        self.cadence_ms: int | None = None
        self.snapshots: dict[str, int] = {}
        self.judge_pending = False

    def note_snapshot(self, serial: str, at_ms: int, now_ms: int) -> bool:
        """Record an inverter's latest measurement; True when the tick moved."""
        previous = self.snapshots.get(serial)
        self.snapshots[serial] = at_ms
        if previous and at_ms > previous:
            self.cadence_ms = learn_cadence(self.cadence_ms, at_ms - previous)
        if (
            self.judge_pending
            and previous
            and judges_tick(previous, at_ms, now_ms, self.cadence_ms or PERIOD_MS)
        ):
            self.judge_pending = False
            self.margin_ms = adapt_margin(self.margin_ms, at_ms > previous)
        phase = tick_phase(self.snapshots.values(), self.margin_ms)
        if phase == self.phase_ms:
            return False
        self.phase_ms = phase
        return True

    def as_dict(self) -> dict[str, Any]:
        """For diagnostics (serials left out)."""
        return {
            "phase_s": self.phase_ms / 1000,
            "margin_s": self.margin_ms / 1000,
            "cadence_s": None if self.cadence_ms is None else self.cadence_ms / 1000,
            "inverters": len(self.snapshots),
        }
