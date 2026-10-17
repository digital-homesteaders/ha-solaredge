"""End conditions of the meter's yearly-totals loop."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from custom_components.solaredge_cloud.api import SolarEdgeApiError, SolarEdgeConnectionError
from custom_components.solaredge_cloud.const import YEARS_BACK
from custom_components.solaredge_cloud.meter_totals import (
    MeterTotalsError,
    async_meter_totals,
    year_floor,
)

NOW_MS = int(datetime(2026, 10, 15, 11, tzinfo=UTC).timestamp() * 1000)
JAN_1_MS = int(datetime(2026, 1, 1, 12, tzinfo=UTC).timestamp() * 1000)


class Years:
    """get_site_measurements by year: totals in kWh, 'none' (summary null), or an exception."""

    def __init__(self, years: dict[int, Any]) -> None:
        self.years = years
        self.asked: list[int] = []

    async def get_site_measurements(self, site_id: str, end_date: str) -> Any:
        year = int(end_date[:4])
        self.asked.append(year)
        answer = self.years.get(year, SolarEdgeApiError("x", status=400, code="INVALID_ARGUMENTS"))
        if isinstance(answer, BaseException):
            raise answer
        if answer == "none":
            return {"summary": None}
        return {"summary": {"import": answer[0] * 1000, "export": answer[1] * 1000}}


async def no_sleep(_: float) -> None:
    return None


async def run(client: Years, cache: dict[str, Any], *, install_year: int | None, now_ms: int = NOW_MS) -> dict[str, Any]:
    return await async_meter_totals(
        client, "1", current_year=datetime.fromtimestamp(now_ms / 1000, UTC).year,
        install_year=install_year, cache=cache, now_ms=now_ms, sleep=no_sleep,
    )


async def test_stops_at_install_year() -> None:
    client = Years({2026: (1400, 2700), 2025: (1800, 3100), 2024: (1200, 2500), 2023: (999, 999)})
    cache: dict[str, Any] = {}
    totals = await run(client, cache, install_year=2024)
    assert totals["imported"] == 4_400_000 and totals["exported"] == 8_300_000
    assert totals["imported_year"] == 1_400_000
    assert client.asked == [2026, 2025, 2024]
    assert set(cache) == {"2025", "2024"}


async def test_finished_years_come_from_the_cache() -> None:
    client = Years({2026: (1400, 2700), 2025: (1800, 3100), 2024: (1200, 2500)})
    cache: dict[str, Any] = {}
    await run(client, cache, install_year=2024)
    client.asked.clear()
    totals = await run(client, cache, install_year=2024)
    # Only the current year again.
    assert client.asked == [2026]
    assert totals["imported"] == 4_400_000


async def test_stops_at_no_data_and_remembers() -> None:
    client = Years({2026: (1, 1), 2025: (2, 2), 2024: "none"})
    cache: dict[str, Any] = {}
    totals = await run(client, cache, install_year=None)
    assert client.asked == [2026, 2025, 2024]
    assert totals["imported"] == 3000
    assert cache["2024"]["missing"] is True
    client.asked.clear()
    await run(client, cache, install_year=None)
    assert client.asked == [2026]
    # Asked again a day later, in case that answer was a hiccup.
    client.asked.clear()
    await run(client, cache, install_year=None, now_ms=NOW_MS + 25 * 3600 * 1000)
    assert client.asked == [2026, 2024]


async def test_ten_year_floor_without_install_year() -> None:
    # Every year has data: the walk still stops YEARS_BACK years ago.
    client = Years({year: (1, 1) for year in range(1990, 2027)})
    totals = await run(client, {}, install_year=None)
    assert client.asked == list(range(2026, 2026 - YEARS_BACK - 1, -1))
    assert totals["imported"] == (YEARS_BACK + 1) * 1000
    assert year_floor(2026, None) == 2016
    assert year_floor(2026, 1999) == 2006
    assert year_floor(2026, 2030) == 2026


@pytest.mark.parametrize(
    "error",
    [
        SolarEdgeConnectionError("down"),
        SolarEdgeApiError("x", status=500, code="GENERAL_ERROR"),
        SolarEdgeApiError("unauthorized", status=401, code="UNAUTHORIZED"),
    ],
)
async def test_any_other_error_aborts_the_run(error: Exception) -> None:
    client = Years({2026: (1, 1), 2025: (2, 2), 2024: error, 2023: (3, 3)})
    cache: dict[str, Any] = {}
    with pytest.raises(type(error)):
        await run(client, cache, install_year=2020)
    # It did not walk past the failing year, and stored nothing for it.
    assert client.asked == [2026, 2025, 2024]
    assert "2024" not in cache and "2023" not in cache
    # The year that was final stays cached.
    assert cache["2025"] == {"imported": 2000, "exported": 2000}


async def test_outage_on_the_current_year_aborts_before_the_walk() -> None:
    client = Years({2026: SolarEdgeConnectionError("down")})
    with pytest.raises(SolarEdgeConnectionError):
        await run(client, {}, install_year=2020)
    assert client.asked == [2026]


async def test_no_data_after_data_is_an_error() -> None:
    client = Years({2026: (1, 1), 2025: "none"})
    cache: dict[str, Any] = {"2024": {"imported": 5, "exported": 5}}
    with pytest.raises(MeterTotalsError):
        await run(client, cache, install_year=2020)


async def test_last_year_is_cached_only_once_settled() -> None:
    client = Years({2026: (1, 1), 2025: (2, 2), 2024: (3, 3)})
    cache: dict[str, Any] = {}
    await run(client, cache, install_year=2024, now_ms=JAN_1_MS)
    assert "2025" not in cache and "2024" in cache


async def test_unknown_years_are_asked_again_daily() -> None:
    client = Years({2026: (1, 1), 2025: (2, 2)})
    client.years[2025] = (2, 2)
    cache: dict[str, Any] = {"2025": {"imported": 0, "exported": 0, "unknown": True, "checked_at": NOW_MS - 1000}}
    await run(client, cache, install_year=2025)
    assert client.asked == [2026]
    await run(client, cache, install_year=2025, now_ms=NOW_MS + 25 * 3600 * 1000)
    assert client.asked == [2026, 2026, 2025]
    assert cache["2025"] == {"imported": 2000, "exported": 2000}
