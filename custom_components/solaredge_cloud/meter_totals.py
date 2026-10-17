"""Lifetime grid import/export as the sum of the yearly totals.

Port of updateTotals in drivers/meter/device.js of com.solaredge v2.0.0 for
Homey, Copyright 2026 Robin de Gruijter, GPL-3.0, with these changes:

- The walk back through past years has a hard floor: the site's install year,
  else YEARS_BACK years ago, and never before FIRST_YEAR. (1.8.1 could walk
  back forever during an outage.)
- Only "no data" ends the walk. Any other error aborts the whole run: nothing
  is summed, the error goes to the caller, and the next energy tick tries
  again.
- Finished years are cached; only the current year (and last year, until it
  has settled) are asked again.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, MutableMapping
from datetime import UTC, datetime
from typing import Any

from .api import SolarEdgeCloudClient, is_no_data
from .const import FIRST_YEAR, YEAR_FETCH_DELAY, YEARS_BACK
from .mapping import year_totals

_LOGGER = logging.getLogger(__name__)

# A past year is final this long after it ended: then it has ended in every
# timezone and its last data is in.
YEAR_SETTLE_MS = 2 * 24 * 60 * 60 * 1000
# A year without data, or without import/export, is asked again after this.
RECHECK_MS = 24 * 60 * 60 * 1000


class MeterTotalsError(Exception):
    """The yearly totals are inconsistent (no data for a year after one with data)."""


def year_floor(current_year: int, install_year: int | None) -> int:
    """The oldest year to ask for."""
    floor = install_year if install_year is not None else current_year - YEARS_BACK
    return max(FIRST_YEAR, min(floor, current_year))


def _is_totals(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("imported"), (int, float))
        and isinstance(value.get("exported"), (int, float))
    )


def _settled(year: int, now_ms: int) -> bool:
    end = int(datetime(year + 1, 1, 1, tzinfo=UTC).timestamp() * 1000)
    return now_ms >= end + YEAR_SETTLE_MS


async def _fetch(client: SolarEdgeCloudClient, site_id: str, year: int) -> dict[str, Any] | None:
    """A year's totals; None when it has no data."""
    try:
        result = await client.get_site_measurements(site_id, f"{year}-12-31")
    except Exception as err:
        if is_no_data(err):
            return None
        raise
    return year_totals(result)


async def async_meter_totals(
    client: SolarEdgeCloudClient,
    site_id: str,
    *,
    current_year: int,
    install_year: int | None,
    cache: MutableMapping[str, Any],
    now_ms: int,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, Any]:
    """Lifetime and this year's import/export in Wh.

    `cache` holds the past years by year string: { imported, exported } (plus
    unknown, checked_at), or { missing: True, checked_at }. Only settled years
    go into it. Raises on any error that is not "no data"; the cache then keeps
    what was already final.
    """
    current = await _fetch(client, site_id, current_year)
    if current is None:
        # The site has no data yet this year (new, or the meter is newer).
        current = {"imported": 0, "exported": 0, "unknown": True}

    floor = year_floor(current_year, install_year)
    imported = current["imported"]
    exported = current["exported"]
    fetched = 0
    for year in range(current_year - 1, floor - 1, -1):
        key = str(year)
        stored: dict[str, Any] = cache.get(key) or {}
        recheck = now_ms - stored.get("checked_at", 0) >= RECHECK_MS
        if _is_totals(stored) and not (stored.get("unknown") and recheck):
            totals = stored
        elif stored.get("missing") and not recheck:
            break
        else:
            if fetched:
                await sleep(YEAR_FETCH_DELAY)
            fetched += 1
            fetched_totals = await _fetch(client, site_id, year)
            if fetched_totals is None:
                # With data for the year before, "no data" cannot be the start of the site.
                if _is_totals(cache.get(str(year - 1))):
                    raise MeterTotalsError(f"No data for {year}, but there is for {year - 1}")
                _LOGGER.debug("Site %s: no meter data before %d", site_id, year + 1)
                if _settled(year, now_ms):
                    cache[key] = {"missing": True, "checked_at": now_ms}
                break
            totals = fetched_totals
            if _settled(year, now_ms):
                cache[key] = {**totals, "checked_at": now_ms} if totals.get("unknown") else totals
        imported += totals["imported"]
        exported += totals["exported"]

    return {
        "imported": imported,
        "exported": exported,
        "imported_year": current["imported"],
        "exported_year": current["exported"],
        "current_unknown": bool(current.get("unknown")),
    }
