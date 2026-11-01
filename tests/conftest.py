"""Fixtures: a replay client fed from the captured answers in tests/fixtures."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

USERNAME = "someone@example.com"
PASSWORD = "secret-password"

# The synthetic site's capture moment: 2026-10-15 11:00 UTC.
CAPTURED_AT = "2026-10-15T11:00:00+00:00"


def load_fixture(name: str) -> Any:
    """A JSON file from tests/fixtures."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/ in every test."""


class ReplayClient:
    """Answers like SolarEdgeCloudClient, from the synthetic site (test/fixtures of 2.0.0).

    `fail` maps a method name to an exception to raise instead; `answers`
    overrides a method's result. Every call is recorded.
    """

    def __init__(self, *_: Any, **__: Any) -> None:
        capture = load_fixture("synthetic-site.json")
        self.site = capture["site"]
        self.results: dict[str, list[dict[str, Any]]] = {}
        for call in capture["calls"]:
            self.results.setdefault(call["method"], []).append(call)
        self.fail: dict[str, BaseException] = {}
        self.answers: dict[str, Any] = {}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.logged_in = True
        self.login_blocked_for = 0.0
        self.refresh_token: str | None = None
        self.on_refresh_token: Any = None

    def set_refresh_token(self, token: str | None) -> None:
        self.refresh_token = token

    def _answer(self, method: str, *args: Any, match: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, args))
        if method in self.fail:
            raise self.fail[method]
        if method in self.answers:
            answer = self.answers[method]
            return copy.deepcopy(answer(*args) if callable(answer) else answer)
        for call in self.results.get(method, []):
            if not match or all(call.get("args", {}).get(k) == v for k, v in match.items()):
                return copy.deepcopy(call.get("result"))
        raise AssertionError(f"not captured: {method} {args}")

    def count(self, method: str) -> int:
        """How often a method was called."""
        return sum(1 for name, _ in self.calls if name == method)

    async def login(self) -> None:
        self.calls.append(("login", ()))
        if "login" in self.fail:
            raise self.fail["login"]

    async def get_sites(self) -> list[dict[str, Any]]:
        return list(self._answer("getSites").values())

    async def get_site_powerflow(self, site_id: str) -> Any:
        return self._answer("getSitePowerflow", site_id)

    async def get_site_devices(self, site_id: str) -> Any:
        return self._answer("getSiteDevices", site_id)

    async def get_site_equipment(self, site_id: str) -> Any:
        return self._answer("getSiteEquipment", site_id)

    async def get_site_energy_overview(self, site_id: str) -> Any:
        return self._answer("getSiteEnergyOverview", site_id)

    async def get_inverters_information(self, serials: list[str]) -> Any:
        return self._answer("getInvertersInformation", serials)

    async def get_site_batteries(self, site_id: str) -> Any:
        return self._answer("getSiteBatteries", site_id)

    async def get_site_battery_mode(self, site_id: str) -> Any:
        return self._answer("getSiteBatteryMode", site_id)

    async def get_site_measurements(self, site_id: str, end_date: str) -> Any:
        return self._answer("getSiteMeasurements", site_id, end_date, match={"endDate": end_date})

    async def get_site_communication(self, site_id: str) -> Any:
        return self._answer("getSiteCommunication", site_id)

    async def get_site_alerts(self, site_id: str) -> Any:
        return self._answer("getSiteAlerts", site_id)

    async def get_site_energy_by_inverter(self, site_id: str, date: str, serials: list[str]) -> Any:
        return self._answer("getSiteEnergyByInverter", site_id, date, serials)

    async def get_appliance_sessions_history(self, site_id: str, reporter_id: str) -> Any:
        return self._answer("getSiteDeviceApplianceSessionsHistory", site_id, reporter_id)

    async def set_activation_state(self, site_id: str, reporter_id: str, state: dict[str, Any]) -> Any:
        self.calls.append(("setActivationState", (site_id, reporter_id, state)))
        if "setActivationState" in self.fail:
            raise self.fail["setActivationState"]
        return {"status": "PASSED"}


@pytest.fixture
def replay() -> ReplayClient:
    """The client every config entry set up in the test gets."""
    client = ReplayClient()
    with patch("custom_components.solaredge_cloud.SolarEdgeCloudClient", return_value=client):
        yield client


@pytest.fixture
def entry_data() -> dict[str, str]:
    """A config entry's data."""
    return {"username": USERNAME, "password": PASSWORD}

