"""Mapping of the captured answers (gruijter/test fixtures) to entity values."""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.solaredge_cloud import mapping
from custom_components.solaredge_cloud.timing import parse_time_ms

from .conftest import load_fixture

CAPTURE = load_fixture("synthetic-site.json")
EV = load_fixture("ev-charger-devices.json")["responses"]
NOW_MS = CAPTURE["capturedAt"]


def result(method: str, **match: Any) -> Any:
    for call in CAPTURE["calls"]:
        if call["method"] == method and all(call.get("args", {}).get(k) == v for k, v in match.items()):
            return call["result"]
    raise KeyError(method)


def test_discover_synthetic_site() -> None:
    found = mapping.discover(result("getSitePowerflow"), result("getSiteDevices"))
    assert found["meter"] and found["battery"]
    assert found["ev"] == {"1000001479067354": "EV Charger with Boost"}
    assert found["hlc"] == {"102": "Boiler"}
    assert mapping.equipment_serials(result("getSiteEquipment")) == ["7E000000-00"]


def test_discover_keba_and_load_controllers() -> None:
    found = mapping.discover({}, EV["keba_charging"])
    assert list(found["ev"].values()) == ["EV Charger 99203855"]
    assert sorted(found["hlc"].values()) == ["Entfeuchter", "Wärme-Pumpe"]
    assert not found["meter"] and not found["battery"]


def test_inverter() -> None:
    assert mapping.inverter_power(result("getSitePowerflow")) == {"power": 5600}
    assert mapping.inverter_energy(result("getSiteEnergyOverview")) == {
        "energy_lifetime": 29338.528,
        "energy_year": 4345.507,
        "energy_month": 176.703,
        "energy_today": 7.131,
    }
    details = mapping.inverter_details(result("getInvertersInformation"), NOW_MS)
    assert details["status"] == "production"
    assert details["voltage_l1"] == 234.8 and details["voltage_l2"] == 232.2 and details["voltage_l3"] == 234.2
    assert mapping.max_optimizer_temperature(result("getSiteEnergyByInverter")) == 38


@pytest.mark.parametrize(
    "production",
    [
        {"isActive": False, "currentPower": None, "isProducing": False},
        {"isActive": False, "currentPower": 0},
        {"isActive": True, "currentPower": 0.0},
        {},
    ],
)
def test_inverter_at_night_reports_zero(production: dict[str, Any]) -> None:
    assert mapping.inverter_power({"solarProduction": production}) == {"power": 0}


def test_inverter_asleep_status() -> None:
    information = result("getInvertersInformation")
    live = next(iter(information["serialToLiveData"].values()))
    live["pAc_W"] = 0
    last = parse_time_ms(live["lastMeasurement"])
    assert mapping.inverter_details(information, last + 9 * 60_000)["status"] == "production"
    assert mapping.inverter_details(information, last + 10 * 60_000)["status"] == "sleeping"
    # The connectivity alarm stays clear while asleep.
    offline = {"connectivityStatus": "OFFLINE"}
    assert mapping.site_values(offline, None, asleep=True) == {"connectivity": True}
    assert mapping.site_values(offline, None, asleep=False) == {"connectivity": False}


def test_fahrenheit() -> None:
    by_inverter = {"inverters": [{"optimizers": [
        {"temperature": {"temperature": 104, "temperatureUnit": "FAHRENHEIT"}},
        {"temperature": {"temperature": 35, "temperatureUnit": "CELSIUS"}},
    ]}]}
    assert mapping.max_optimizer_temperature(by_inverter) == 40


def test_meter() -> None:
    assert mapping.meter_power(result("getSitePowerflow")) == {"grid_power": -1200, "load_power": 800, "solar_power": 5600}
    assert mapping.meter_power({"grid": {"isActive": True, "currentPower": None}}) == {"grid_power": 0}
    assert mapping.year_totals(result("getSiteMeasurements", endDate="2025-12-31")) == {"imported": 1800000, "exported": 3100000}
    assert mapping.year_totals({"summary": None}) is None
    assert mapping.year_totals({"summary": {"import": None, "export": None}}) == {"imported": 0, "exported": 0, "unknown": True}


def test_site_alerts() -> None:
    assert mapping.site_values(result("getSiteCommunication"), result("getSiteAlerts"), False) == {
        "connectivity": True, "alerts": 1, "problem": True,
    }


def test_battery() -> None:
    assert mapping.battery_power(result("getSitePowerflow")) == {"power": 500, "soc": 62, "state": "charging"}
    discharging = {"storage": {"isActive": True, "currentPower": 1.5, "status": "Discharging", "chargeLevel": 40}}
    assert mapping.battery_power(discharging) == {"power": -1500, "soc": 40, "state": "discharging"}
    batteries = mapping.batteries_of(result("getSiteBatteries"))
    unit = mapping.battery_unit(batteries)
    assert unit == 0.001
    values = mapping.battery_energy(batteries, unit)
    # SolarEdge's charge/discharge counters are not used (see the real answer below).
    assert set(values) == {"residual_energy", "state_of_health"}
    assert values["residual_energy"] == pytest.approx(6.2)
    assert values["state_of_health"] == 95


def test_battery_in_kwh() -> None:
    batteries = [{"chargeEnergy": 1234.5, "dischargeEnergy": 1000, "remainingEnergy": 6.2,
                  "fullPackEnergy": 9.2, "namePlateEnergy": 9.7}]
    unit = mapping.battery_unit(batteries)
    assert unit == 1.0
    values = mapping.battery_energy(batteries, unit)
    assert values["residual_energy"] == 6.2 and values["state_of_health"] == 95


# state -> charging state, charging, charge mode, solar charging (gruijter test/devices.js)
EV_EXPECTED = {
    "charging": ("plugged_in_charging", True, "manual", False),
    "pluggedin_not_charging": ("plugged_in", False, "manual", False),
    "pluggedin_pending_schedule": ("plugged_in", False, "auto", False),
    "pluggedin_pending_solar": ("plugged_in", False, "auto", True),
    "unplugged_not_charging": ("plugged_out", False, "manual", False),
    "unplugged_not_charging_excesspv_and_schedule_disabled": ("plugged_out", False, "manual", False),
    "unplugged_not_charging_excesspv_disabled_but_schedule_enabled_M-F1000-1200_SASU0900-2000": ("plugged_out", False, "auto", False),
    "unplugged_not_charging_excesspv_enabled_schedule_disabled": ("plugged_out", False, "auto", True),
    "keba_charging": ("plugged_in_charging", True, "auto", True),
}


@pytest.mark.parametrize("state", list(EV_EXPECTED))
def test_ev_charger_states(state: str) -> None:
    answer = EV[state]
    charger = answer["devicesByType"]["EV_CHARGER"][0]
    values = mapping.ev_values(None, answer, str(charger["reporterId"]))
    got = (values.get("charging_state"), values.get("charging"), values.get("charge_mode"), values.get("solar_charging"))
    assert got == EV_EXPECTED[state]
    # CURRENT_POWER_WH is W; 0 while charging (KEBA) is no reading; not charging is 0 W.
    assert values.get("power") == {"charging": 9509, "keba_charging": None}.get(state, 0)
    assert values["session_energy"] == round(charger["sessionEnergy"] / 1000, 3)
    assert values["session_duration"] == round(charger["sessionDuration"] / 60)
    assert values.get("vehicle") == (charger.get("applianceData") or {}).get("alias")
    # No always-zero "discharged" value.
    assert not any("discharged" in key for key in values)


def test_ev_powerflow_only() -> None:
    values = mapping.ev_values({"evCharger": {"isActive": True, "connectionStatus": "charging", "currentPower": 3.1}}, None, "1")
    assert values == {"charging_state": "plugged_in_charging", "charging": True, "power": 3100}


def test_ev_history() -> None:
    assert mapping.ev_charged_wh(result("getSiteDeviceApplianceSessionsHistory")) == 127456
    assert mapping.ev_charged_wh({"applianceDetails": [{}]}) == 0
    assert mapping.ev_charged_wh({}) is None


def test_load_controllers() -> None:
    answer = EV["keba_charging"]
    by_name = {device["name"]: str(device["reporterId"]) for device in answer["devices"]}
    pump = mapping.hlc_values(answer, by_name["Wärme-Pumpe"])
    dryer = mapping.hlc_values(answer, by_name["Entfeuchter"])
    assert (pump["state"], pump["power"], pump["power_measured"]) == ("on", 1500, False)
    assert (dryer["state"], dryer["power"]) == ("auto", 0)
    boiler = mapping.hlc_values(result("getSiteDevices"), "102")
    assert (boiler["state"], boiler["power"]) == ("on", 2000)
    assert mapping.hlc_values(answer, "unknown") == {}


def test_real_two_battery_answer() -> None:
    """Two 9.7 kWh SolarEdge Home Batteries at 95 % (tests/fixtures/batteries-two.json)."""
    answer = load_fixture("batteries-two.json")["response"]
    batteries = mapping.batteries_of(answer)
    assert len(batteries) == 2
    unit = mapping.battery_unit(batteries)
    assert unit == 0.001
    values = mapping.battery_energy(batteries, unit)
    # The same as SolarEdge's own storageRemainingEnergy, and 95 % of the full pack.
    assert values["residual_energy"] == pytest.approx(answer["storageInfo"]["storageRemainingEnergy"] / 1000)
    assert values["residual_energy"] / (answer["storageInfo"]["storageFullPackEnergy"] / 1000) == pytest.approx(0.952, abs=0.001)
    assert values["state_of_health"] == 99
    # Its counters are 0: nothing to take from them.
    assert all(b["chargeEnergy"] == 0 and b["dischargeEnergy"] == 0 for b in batteries)
    # The inverter (POLESTAR) is no battery.
    assert mapping.discover({"storage": {"isActive": True, "chargeLevel": 95}}, None)["battery"]


@pytest.mark.parametrize(
    ("previous", "current", "charged", "discharged"),
    [
        (1000, 1000, 1.0, 0.0),
        (0, 2000, 1.0, 0.0),
        (-1000, -3000, 0.0, 2.0),
        (0, 0, 0.0, 0.0),
        # A sign change splits at zero: 1 kW to -1 kW is half an hour each way.
        (1000, -1000, 0.25, 0.25),
        (3000, -1000, 1.125, 0.125),
    ],
)
def test_battery_flow(previous: float, current: float, charged: float, discharged: float) -> None:
    got = mapping.battery_flow_kwh(previous, current, 3600)
    assert got == (pytest.approx(charged), pytest.approx(discharged))
