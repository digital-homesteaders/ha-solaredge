"""Setup, polling, backoff, night behaviour and diagnostics against the replay client."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.solaredge_cloud.api import (
    SolarEdgeApiError,
    SolarEdgeAuthError,
    SolarEdgeConnectionError,
    SolarEdgeLoginRefusedError,
    SolarEdgeRateLimitError,
)
from custom_components.solaredge_cloud.const import BACKOFF_MAX, CONF_POLL_INTERVAL, DOMAIN
from custom_components.solaredge_cloud.diagnostics import async_get_config_entry_diagnostics

from .conftest import CAPTURED_AT, PASSWORD, USERNAME, ReplayClient, load_fixture

SITE = "9999999"
EV = "1000001479067354"


@pytest.fixture(autouse=True)
def frozen(freezer: FrozenDateTimeFactory) -> FrozenDateTimeFactory:
    """Time stands at the capture."""
    freezer.move_to(CAPTURED_AT)
    return freezer


@pytest.fixture(autouse=True)
def no_year_pause():
    """No pause between yearly requests."""
    with patch("custom_components.solaredge_cloud.meter_totals.YEAR_FETCH_DELAY", 0):
        yield


async def setup(hass: HomeAssistant, entry_data: dict[str, str], options: dict[str, Any] | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=USERNAME, title="Synthetic site", data=entry_data, options=options or {})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


def entity_id(hass: HomeAssistant, platform: str, unique_id: str) -> str:
    found = er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)
    assert found, unique_id
    return found


def state(hass: HomeAssistant, platform: str, unique_id: str) -> str:
    return hass.states.get(entity_id(hass, platform, unique_id)).state


async def advance(hass: HomeAssistant, frozen: FrozenDateTimeFactory, seconds: float, step: float = 10) -> None:
    """Let time pass in small steps, firing the timers due at each."""
    while seconds > 0:
        frozen.tick(timedelta(seconds=min(step, seconds)))
        seconds -= step
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)


# Setup is at 11:00:00; the energy ticks follow the inverter's uploads at :28
# past each 5 minutes, 20 s later: 11:00:48, 11:05:48, ... A refresh resets the
# 60 s poll timer, so the plain polls after setup are at 11:01:48, 11:02:48, ...


async def test_setup_creates_devices_and_values(hass: HomeAssistant, replay: ReplayClient, entry_data) -> None:
    entry = await setup(hass, entry_data)
    assert entry.state is ConfigEntryState.LOADED

    devices = dr.async_get(hass)
    site = devices.async_get_device_by_identifier((DOMAIN, SITE), entry.entry_id)
    assert site and site.name == "Synthetic site"
    for ident in (f"{SITE}_inverter", f"{SITE}_meter", f"{SITE}_battery", f"{SITE}_ev_{EV}", f"{SITE}_hlc_102"):
        device = devices.async_get_device_by_identifier((DOMAIN, ident), entry.entry_id)
        assert device, ident
        assert device.via_device_id == site.id

    # The synthetic site's expected values (gruijter test/responses.js).
    expected = {
        ("sensor", f"{SITE}_inverter_power"): "5600",
        ("sensor", f"{SITE}_inverter_energy_lifetime"): "29338.528",
        ("sensor", f"{SITE}_inverter_energy_today"): "7.131",
        ("sensor", f"{SITE}_inverter_status"): "production",
        ("sensor", f"{SITE}_inverter_optimizer_temperature_max"): "38",
        ("sensor", f"{SITE}_meter_grid_power"): "-1200",
        ("sensor", f"{SITE}_meter_load_power"): "800",
        ("sensor", f"{SITE}_meter_solar_power"): "5600",
        ("sensor", f"{SITE}_meter_energy_imported"): "4400.0",
        ("sensor", f"{SITE}_meter_energy_exported"): "8300.0",
        ("sensor", f"{SITE}_battery_power"): "500",
        ("sensor", f"{SITE}_battery_soc"): "62",
        ("sensor", f"{SITE}_battery_state"): "charging",
        # Integrated from the battery power: nothing yet after one sample.
        ("sensor", f"{SITE}_battery_energy_charged"): "0.0",
        ("sensor", f"{SITE}_battery_energy_discharged"): "0.0",
        ("sensor", f"{SITE}_battery_residual_energy"): "6.2",
        ("sensor", f"{SITE}_battery_state_of_health"): "95",
        ("sensor", f"{SITE}_ev_{EV}_power"): "9509",
        ("sensor", f"{SITE}_ev_{EV}_charging_state"): "plugged_in_charging",
        ("sensor", f"{SITE}_ev_{EV}_energy_charged"): "127.456",
        ("sensor", f"{SITE}_ev_{EV}_session_energy"): "0.093",
        ("sensor", f"{SITE}_ev_{EV}_charge_mode"): "manual",
        ("sensor", f"{SITE}_ev_{EV}_vehicle"): "Ioniq",
        ("switch", f"{SITE}_ev_{EV}_charging"): "on",
        ("binary_sensor", f"{SITE}_ev_{EV}_solar_charging"): "off",
        ("select", f"{SITE}_hlc_102_state"): "on",
        ("sensor", f"{SITE}_hlc_102_power"): "2000",
        ("binary_sensor", f"{SITE}_site_connectivity"): "on",
        ("binary_sensor", f"{SITE}_site_problem"): "on",
    }
    got = {key: state(hass, *key) for key in expected}
    assert got == expected

    # Energy dashboard: lifetime counters are total_increasing energy in kWh.
    attrs = hass.states.get(entity_id(hass, "sensor", f"{SITE}_meter_energy_imported")).attributes
    assert attrs["state_class"] == "total_increasing" and attrs["device_class"] == "energy"
    assert attrs["unit_of_measurement"] == "kWh"
    attrs = hass.states.get(entity_id(hass, "sensor", f"{SITE}_inverter_power")).attributes
    assert attrs["state_class"] == "measurement" and attrs["device_class"] == "power"
    # No always-zero EV "discharged".
    assert not er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{SITE}_ev_{EV}_energy_discharged")


async def test_requests_per_tick(hass: HomeAssistant, replay: ReplayClient, frozen, entry_data) -> None:
    entry = await setup(hass, entry_data)
    assert entry.runtime_data.timing.phase_ms == 48_000
    await advance(hass, frozen, 50)
    replay.calls.clear()
    await advance(hass, frozen, 60)
    # A plain poll: one powerflow for all five devices, plus one devices list for
    # the EV charger and the load controller.
    assert sorted(name for name, _ in replay.calls) == ["getSiteDevices", "getSitePowerflow"]

    # Ten minutes: two energy ticks and about one powerflow a minute.
    replay.calls.clear()
    await advance(hass, frozen, 600)
    assert replay.count("getSiteEnergyOverview") == 2
    assert replay.count("getInvertersInformation") == 2
    assert replay.count("getSiteBatteries") == 2
    assert 10 <= replay.count("getSitePowerflow") <= 11
    # The current year's meter totals on each energy tick; past years from the cache.
    assert [args[1] for name, args in replay.calls if name == "getSiteMeasurements"] == ["2026-12-31"] * 2
    # The slow readings go with every third energy tick: setup (1st), then the
    # 4th at 11:10:48.
    assert replay.count("getSiteDeviceApplianceSessionsHistory") == 1
    assert replay.count("getSiteAlerts") == 1
    assert replay.count("getSiteEnergyByInverter") == 1
    await advance(hass, frozen, 300)
    assert replay.count("getSiteDeviceApplianceSessionsHistory") == 1


async def test_default_and_custom_poll_interval(hass: HomeAssistant, replay: ReplayClient, entry_data) -> None:
    entry = await setup(hass, entry_data)
    assert entry.runtime_data.update_interval == timedelta(seconds=60)
    hass.config_entries.async_update_entry(entry, options={CONF_POLL_INTERVAL: 120})
    await hass.async_block_till_done()
    assert entry.runtime_data.update_interval == timedelta(seconds=120)


async def test_inverter_stays_available_at_night(hass: HomeAssistant, replay: ReplayClient, frozen, entry_data) -> None:
    await setup(hass, entry_data)
    replay.answers["getSitePowerflow"] = {
        "grid": {"isActive": True, "currentPower": 0.3, "status": "import"},
        "consumption": {"isActive": True, "currentPower": 0.3},
        "solarProduction": {"isActive": False, "currentPower": None, "isProducing": False},
        "storage": {"isActive": False, "currentPower": None, "chargeLevel": 15, "status": "idle"},
    }
    information = replay.results["getInvertersInformation"][0]["result"]
    live = next(iter(information["serialToLiveData"].values()))
    live["pAc_W"] = 0
    live["lastMeasurement"] = "2026-10-15T10:40:28Z"
    replay.answers["getSiteCommunication"] = {"connectivityStatus": "OFFLINE"}
    await advance(hass, frozen, 120)
    assert state(hass, "sensor", f"{SITE}_inverter_power") == "0"
    assert state(hass, "sensor", f"{SITE}_meter_solar_power") == "0"
    assert state(hass, "sensor", f"{SITE}_battery_power") == "0"
    assert state(hass, "sensor", f"{SITE}_battery_soc") == "15"
    # The inverter sleeps: status says so, and the site is not reported offline.
    await advance(hass, frozen, 600)
    assert state(hass, "sensor", f"{SITE}_inverter_status") == "sleeping"
    assert state(hass, "binary_sensor", f"{SITE}_site_connectivity") == "on"
    assert state(hass, "sensor", f"{SITE}_inverter_power") == "0"


async def test_inverter_only_site_rests_at_night(hass: HomeAssistant, replay: ReplayClient, frozen, entry_data) -> None:
    replay.answers["getSitePowerflow"] = {"solarProduction": {"isActive": False, "currentPower": None}, "grid": {"isActive": False}}
    replay.answers["getSiteDevices"] = {"devices": []}
    await setup(hass, entry_data)
    await advance(hass, frozen, 50)
    replay.calls.clear()
    # Plain polls skip the powerflow; the energy tick still reads it.
    await advance(hass, frozen, 240)
    assert replay.count("getSitePowerflow") == 0
    await advance(hass, frozen, 60)
    assert replay.count("getSitePowerflow") == 1
    assert state(hass, "sensor", f"{SITE}_inverter_power") == "0"


async def test_backoff_after_outage(hass: HomeAssistant, replay: ReplayClient, frozen, entry_data) -> None:
    entry = await setup(hass, entry_data)
    coordinator = entry.runtime_data
    power = entity_id(hass, "sensor", f"{SITE}_inverter_power")
    replay.fail["getSitePowerflow"] = SolarEdgeConnectionError("down")
    replay.fail["getSiteDevices"] = SolarEdgeConnectionError("down")

    await advance(hass, frozen, 50)  # the energy tick at :48 fails
    assert coordinator.update_interval == timedelta(seconds=120)
    # One failed poll: still available, last value kept.
    assert hass.states.get(power).state == "5600"

    replay.calls.clear()
    await advance(hass, frozen, 100)
    # Backing off: no request until 120 s after the failure.
    assert replay.calls == []
    await advance(hass, frozen, 20)
    assert replay.count("getSitePowerflow") == 1
    assert coordinator.update_interval == timedelta(seconds=240)
    assert hass.states.get(power).state == "5600"

    replay.calls.clear()
    await advance(hass, frozen, 240)
    # Third failure in a row: now unavailable. The energy tick in between was
    # skipped, not sent.
    assert replay.count("getSitePowerflow") == 1
    assert coordinator.consecutive_failures == 3
    assert hass.states.get(power).state == STATE_UNAVAILABLE
    assert coordinator.update_interval == timedelta(seconds=480)

    await advance(hass, frozen, 480 + 960 + BACKOFF_MAX, step=60)
    assert coordinator.update_interval == timedelta(seconds=BACKOFF_MAX)

    replay.fail.clear()
    await advance(hass, frozen, BACKOFF_MAX, step=60)
    assert coordinator.update_interval == timedelta(seconds=60)
    assert coordinator.consecutive_failures == 0
    assert hass.states.get(power).state == "5600"


async def test_backoff_on_rate_limit_even_if_part_succeeds(hass: HomeAssistant, replay: ReplayClient, frozen, entry_data) -> None:
    entry = await setup(hass, entry_data)
    replay.fail["getSiteDevices"] = SolarEdgeRateLimitError("429", status=429)
    await advance(hass, frozen, 50)
    assert entry.runtime_data.update_interval == timedelta(seconds=120)


async def test_partial_failure_is_not_an_outage(hass: HomeAssistant, replay: ReplayClient, frozen, entry_data) -> None:
    entry = await setup(hass, entry_data)
    replay.fail["getSiteDevices"] = SolarEdgeApiError("x", status=404)
    await advance(hass, frozen, 120)
    assert entry.runtime_data.update_interval == timedelta(seconds=60)
    assert entry.runtime_data.consecutive_failures == 0


async def test_rejected_login_starts_reauth(hass: HomeAssistant, replay: ReplayClient, frozen, entry_data) -> None:
    entry = await setup(hass, entry_data)
    replay.fail["getSitePowerflow"] = SolarEdgeAuthError("rejected")
    replay.fail["getSiteDevices"] = SolarEdgeAuthError("rejected")
    await advance(hass, frozen, 50)
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]
    # No more polls or energy ticks: no retries with the rejected password.
    replay.calls.clear()
    await advance(hass, frozen, 900, step=30)
    assert replay.calls == []
    assert entry.state is ConfigEntryState.LOADED


async def test_rejected_login_at_setup(hass: HomeAssistant, replay: ReplayClient, entry_data) -> None:
    replay.fail["getSites"] = SolarEdgeAuthError("rejected")
    entry = await setup(hass, entry_data)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert [f["context"]["source"] for f in hass.config_entries.flow.async_progress_by_handler(DOMAIN)] == ["reauth"]


async def test_outage_at_setup_retries(hass: HomeAssistant, replay: ReplayClient, entry_data) -> None:
    replay.fail["getSites"] = SolarEdgeConnectionError("down")
    entry = await setup(hass, entry_data)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_commands(hass: HomeAssistant, replay: ReplayClient, entry_data) -> None:
    await setup(hass, entry_data)
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": entity_id(hass, "switch", f"{SITE}_ev_{EV}_charging")}, blocking=True
    )
    await hass.services.async_call(
        "button", "press", {"entity_id": entity_id(hass, "button", f"{SITE}_ev_{EV}_set_automatic")}, blocking=True
    )
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": entity_id(hass, "select", f"{SITE}_hlc_102_state"), "option": "auto"}, blocking=True,
    )
    sent = [args for name, args in replay.calls if name == "setActivationState"]
    assert sent == [
        (SITE, EV, {"mode": "MANUAL", "level": 0, "duration": None}),
        (SITE, EV, {"mode": "AUTO"}),
        (SITE, "102", {"mode": "AUTO", "level": None, "duration": None}),
    ]


async def test_meter_totals_survive_restart_from_cache(hass: HomeAssistant, replay: ReplayClient, entry_data) -> None:
    entry = await setup(hass, entry_data)
    asked = [args[1] for name, args in replay.calls if name == "getSiteMeasurements"]
    assert sorted(asked) == ["2024-12-31", "2025-12-31", "2026-12-31"]
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    asked = [args[1] for name, args in replay.calls if name == "getSiteMeasurements"]
    # Past years from the store; only the current year again.
    assert sorted(asked) == ["2024-12-31", "2025-12-31", "2026-12-31", "2026-12-31"]
    assert state(hass, "sensor", f"{SITE}_meter_energy_imported") == "4400.0"


async def test_diagnostics_redact_credentials(hass: HomeAssistant, replay: ReplayClient, entry_data) -> None:
    entry = await setup(hass, entry_data)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    text = str(diagnostics)
    assert USERNAME not in text and PASSWORD not in text
    assert diagnostics["entry"]["data"] == {"username": "**REDACTED**", "password": "**REDACTED**"}
    assert "7E000000-00" not in text
    assert diagnostics["coordinator"]["poll_interval"] == 60


async def test_diagnostics_have_raw_answers(hass: HomeAssistant, replay: ReplayClient, entry_data) -> None:
    entry = await setup(hass, entry_data)
    raw = (await async_get_config_entry_diagnostics(hass, entry))["raw"]
    site = raw[SITE]
    assert {"powerflow", "devices", "batteries", "inverters"} <= set(site)
    assert "answer" in site["powerflow"]
    replay.fail["getSiteBatteries"] = SolarEdgeConnectionError("down")
    raw = (await async_get_config_entry_diagnostics(hass, entry))["raw"]
    assert raw[SITE]["batteries"] == {"error": "down"}
    assert "answer" in raw[SITE]["devices"]


async def test_failing_request_warned_once(hass: HomeAssistant, replay: ReplayClient, frozen, entry_data, caplog) -> None:
    await setup(hass, entry_data)
    replay.fail["getSiteDevices"] = SolarEdgeApiError("x", status=404)
    await advance(hass, frozen, 180)
    await advance(hass, frozen, 180)
    assert caplog.text.count("devices for site") == 1
    assert "failed" in caplog.text
    del replay.fail["getSiteDevices"]
    await advance(hass, frozen, 120)
    assert "devices for site" in caplog.text and "works again" in caplog.text


async def test_stale_answer_warned_once(hass: HomeAssistant, replay: ReplayClient, frozen, entry_data, caplog) -> None:
    await setup(hass, entry_data)
    flow = load_fixture("synthetic-site.json")
    answer = next(c["result"] for c in flow["calls"] if c["method"] == "getSitePowerflow")
    replay.answers["getSitePowerflow"] = {**answer, "isRealTime": False}
    await advance(hass, frozen, 120)
    await advance(hass, frozen, 120)
    assert caplog.text.count("looks stale: isRealTime false") == 1
    replay.answers["getSitePowerflow"] = answer
    await advance(hass, frozen, 120)
    assert "powerflow for site" in caplog.text and "is live again" in caplog.text


async def test_stale_not_warned_while_idle(hass: HomeAssistant, replay: ReplayClient, frozen, entry_data, caplog) -> None:
    await setup(hass, entry_data)
    answer = next(c["result"] for c in load_fixture("synthetic-site.json")["calls"] if c["method"] == "getSitePowerflow")
    night = {**answer, "isRealTime": False, "isCommunicating": False, "solarProduction": {"isActive": False, "currentPower": 0}}
    replay.answers["getSitePowerflow"] = night
    # The fixture's devices list is older than its frozen clock; make it current.
    devices = next(c["result"] for c in load_fixture("synthetic-site.json")["calls"] if c["method"] == "getSiteDevices")
    replay.answers["getSiteDevices"] = lambda *_: {**devices, "fieldLastUpdateTS": int(dt_util.utcnow().timestamp() * 1000)}
    await advance(hass, frozen, 240)
    assert "looks stale" not in caplog.text


async def test_all_requests_failing_still_noted(hass: HomeAssistant, replay: ReplayClient, frozen, entry_data, caplog) -> None:
    await setup(hass, entry_data)
    replay.fail["getSitePowerflow"] = SolarEdgeConnectionError("down")
    replay.fail["getSiteDevices"] = SolarEdgeConnectionError("down")
    await advance(hass, frozen, 50)
    assert "powerflow for site" in caplog.text and "failed: down" in caplog.text


async def test_raw_answers_not_asked_while_paused(hass: HomeAssistant, replay: ReplayClient, frozen, entry_data) -> None:
    entry = await setup(hass, entry_data)
    replay.fail["getSitePowerflow"] = SolarEdgeConnectionError("down")
    replay.fail["getSiteDevices"] = SolarEdgeConnectionError("down")
    await advance(hass, frozen, 50)
    assert entry.runtime_data.outages
    replay.calls.clear()
    raw = (await async_get_config_entry_diagnostics(hass, entry))["raw"]
    assert raw == {"skipped": "polling is paused; no requests sent"}
    assert replay.calls == []


async def test_credentials_not_logged(hass: HomeAssistant, replay: ReplayClient, frozen, entry_data, caplog) -> None:
    caplog.set_level("DEBUG")
    await setup(hass, entry_data)
    replay.fail["getSitePowerflow"] = SolarEdgeAuthError("rejected")
    await advance(hass, frozen, 60)
    assert USERNAME not in caplog.text and PASSWORD not in caplog.text


async def test_unload(hass: HomeAssistant, replay: ReplayClient, entry_data) -> None:
    entry = await setup(hass, entry_data)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_failing_site_does_not_block_the_others(hass: HomeAssistant, replay: ReplayClient, entry_data) -> None:
    sites = replay.results["getSites"][0]["result"]
    replay.answers["getSites"] = {**sites, "42": {"id": 42, "name": "Old site", "timezone": "UTC"}}
    good = replay.results["getSitePowerflow"][0]["result"]

    def powerflow(site_id: str) -> Any:
        if site_id == "42":
            raise SolarEdgeApiError("Unknown site id", status=404)
        return good

    replay.answers["getSitePowerflow"] = powerflow
    entry = await setup(hass, entry_data)
    assert entry.state is ConfigEntryState.LOADED
    assert list(entry.runtime_data.sites) == [SITE]
    assert state(hass, "sensor", f"{SITE}_inverter_power") == "5600"


async def test_setup_retries_when_no_site_answers(hass: HomeAssistant, replay: ReplayClient, entry_data) -> None:
    replay.fail["getSitePowerflow"] = SolarEdgeConnectionError("down")
    entry = await setup(hass, entry_data)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_startup_does_not_ask_twice(hass: HomeAssistant, replay: ReplayClient, entry_data) -> None:
    await setup(hass, entry_data)
    # Discovery's answers feed the first poll.
    assert replay.count("getSitePowerflow") == 1
    assert replay.count("getSiteDevices") == 1
    assert replay.count("getSiteEquipment") == 1


async def test_year_totals_survive_an_empty_answer(hass: HomeAssistant, replay: ReplayClient, frozen, entry_data) -> None:
    await setup(hass, entry_data)
    assert state(hass, "sensor", f"{SITE}_meter_energy_imported_year") == "1400.0"
    # Past years are cached, so only this year is asked: and answers no data.
    replay.answers["getSiteMeasurements"] = {"summary": None}
    await advance(hass, frozen, 50)
    assert replay.count("getSiteMeasurements") == 4
    assert state(hass, "sensor", f"{SITE}_meter_energy_imported_year") == "1400.0"
    assert state(hass, "sensor", f"{SITE}_meter_energy_exported_year") == "2700.0"
    assert state(hass, "sensor", f"{SITE}_meter_energy_imported") == "4400.0"


async def test_new_poll_interval_does_not_resume_after_rejected_login(
    hass: HomeAssistant, replay: ReplayClient, frozen, entry_data
) -> None:
    entry = await setup(hass, entry_data)
    replay.fail["getSitePowerflow"] = SolarEdgeAuthError("rejected")
    replay.fail["getSiteDevices"] = SolarEdgeAuthError("rejected")
    await advance(hass, frozen, 50)
    hass.config_entries.async_update_entry(entry, options={CONF_POLL_INTERVAL: 30})
    await hass.async_block_till_done()
    replay.calls.clear()
    await advance(hass, frozen, 600, step=30)
    assert replay.calls == []


async def test_device_list_only_while_an_ev_charges(hass: HomeAssistant, replay: ReplayClient, frozen, entry_data) -> None:
    unplugged = load_fixture("ev-charger-devices.json")["responses"]["unplugged_not_charging"]
    flow = replay.results["getSitePowerflow"][0]["result"]
    flow["evCharger"] = {"isActive": True, "connectionStatus": "disconnected", "currentPower": None}
    replay.answers["getSitePowerflow"] = flow
    replay.answers["getSiteDevices"] = unplugged
    entry = await setup(hass, entry_data)
    assert not entry.runtime_data.sites[SITE].load_controllers
    await advance(hass, frozen, 50)
    replay.calls.clear()
    # Plain polls with the charger unplugged: the powerflow only.
    await advance(hass, frozen, 180)
    assert replay.count("getSitePowerflow") == 3
    assert replay.count("getSiteDevices") == 0
    assert state(hass, "sensor", f"{SITE}_ev_{EV}_charging_state") == "plugged_out"
    # The powerflow says it charges: from the next poll on, the devices list too.
    flow["evCharger"] = {"isActive": True, "connectionStatus": "charging", "currentPower": 7.4}
    replay.answers["getSiteDevices"] = load_fixture("ev-charger-devices.json")["responses"]["charging"]
    await advance(hass, frozen, 60)
    assert state(hass, "switch", f"{SITE}_ev_{EV}_charging") == "on"
    assert state(hass, "sensor", f"{SITE}_ev_{EV}_power") == "7400"
    await advance(hass, frozen, 60)
    assert replay.count("getSiteDevices") == 1
    assert state(hass, "sensor", f"{SITE}_ev_{EV}_power") == "9509"


async def test_empty_equipment_is_asked_hourly(hass: HomeAssistant, replay: ReplayClient, frozen, entry_data) -> None:
    replay.answers["getSiteEquipment"] = {"inverters": None}
    await setup(hass, entry_data)
    await advance(hass, frozen, 1800, step=60)
    assert replay.count("getSiteEquipment") == 1
    await advance(hass, frozen, 1900, step=60)
    assert replay.count("getSiteEquipment") == 2


async def test_store_written_only_on_change(hass: HomeAssistant, replay: ReplayClient, frozen, entry_data) -> None:
    flow = replay.results["getSitePowerflow"][0]["result"]
    flow["storage"] = {**flow["storage"], "currentPower": 0, "status": "idle"}
    entry = await setup(hass, entry_data)
    with patch.object(entry.runtime_data._store, "async_delay_save") as save:
        # Ten minutes of polls and two energy ticks with nothing new.
        await advance(hass, frozen, 600, step=30)
        assert save.call_count == 0
        # The battery charges: its integrated energy is saved on the next
        # energy tick, not on every poll.
        flow["storage"] = {**flow["storage"], "currentPower": 2.0, "status": "charging"}
        await advance(hass, frozen, 300, step=30)
        assert save.call_count == 1


async def test_battery_energy_is_integrated_from_power(hass: HomeAssistant, replay: ReplayClient, frozen, entry_data) -> None:
    # The synthetic battery charges at 500 W.
    await setup(hass, entry_data)
    await advance(hass, frozen, 3600, step=30)
    charged = float(state(hass, "sensor", f"{SITE}_battery_energy_charged"))
    # 500 W for (almost) an hour: the last poll may be up to a minute short.
    assert 0.48 <= charged <= 0.5
    assert state(hass, "sensor", f"{SITE}_battery_energy_discharged") == "0.0"
    flow = replay.results["getSitePowerflow"][0]["result"]
    flow["storage"] = {**flow["storage"], "currentPower": 1.5, "status": "discharging"}
    await advance(hass, frozen, 1800, step=30)
    discharged = float(state(hass, "sensor", f"{SITE}_battery_energy_discharged"))
    assert 0.7 <= discharged <= 0.76
    # Charged did not fall or move back.
    assert float(state(hass, "sensor", f"{SITE}_battery_energy_charged")) >= charged
    # The totals survive a reload (store).
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert float(state(hass, "sensor", f"{SITE}_battery_energy_discharged")) == discharged


async def test_battery_energy_not_guessed_across_an_outage(hass: HomeAssistant, replay: ReplayClient, frozen, entry_data) -> None:
    entry = await setup(hass, entry_data)
    replay.fail["getSitePowerflow"] = SolarEdgeConnectionError("down")
    replay.fail["getSiteDevices"] = SolarEdgeConnectionError("down")
    await advance(hass, frozen, 3 * 3600, step=120)
    replay.fail.clear()
    await advance(hass, frozen, BACKOFF_MAX, step=60)
    assert entry.runtime_data.outages == 0
    # Three hours unseen are not counted as 500 W charging.
    assert float(state(hass, "sensor", f"{SITE}_battery_energy_charged")) < 0.3


async def test_refresh_token_kept_across_restarts(hass: HomeAssistant, replay: ReplayClient, entry_data) -> None:
    entry = await setup(hass, entry_data)
    assert replay.refresh_token is None
    # The client got a refresh token at login.
    replay.on_refresh_token("RT9")
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert replay.refresh_token == "RT9"
    # Dropped (no longer accepted): not handed back after the next restart.
    replay.on_refresh_token(None)
    replay.refresh_token = None
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert replay.refresh_token is None
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert "RT9" not in str(diagnostics)


async def test_refresh_token_of_another_account_is_not_used(hass: HomeAssistant, replay: ReplayClient, entry_data) -> None:
    entry = await setup(hass, entry_data)
    replay.on_refresh_token("RT9")
    hass.config_entries.async_update_entry(entry, data={**entry.data, "username": "other@example.com"})
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    replay.refresh_token = None
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert replay.refresh_token is None


async def test_refused_login_is_no_reauth_and_no_outage(hass: HomeAssistant, replay: ReplayClient, frozen, entry_data) -> None:
    entry = await setup(hass, entry_data)
    replay.fail["getSitePowerflow"] = SolarEdgeLoginRefusedError("second factor")
    replay.fail["getSiteDevices"] = SolarEdgeLoginRefusedError("second factor")
    await advance(hass, frozen, 50)
    # Not the password: no reauth. Not an outage: no backoff. (The client holds
    # its logins back by itself.)
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []
    assert entry.runtime_data.update_interval == timedelta(seconds=60)
    assert entry.runtime_data.consecutive_failures == 1
    assert state(hass, "sensor", f"{SITE}_inverter_power") == "5600"
