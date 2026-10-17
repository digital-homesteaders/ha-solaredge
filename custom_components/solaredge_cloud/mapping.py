"""From SolarEdge answers to entity values.

Port of the device logic in drivers/*/device.js of com.solaredge v2.0.0 for
Homey, Copyright 2026 Robin de Gruijter, GPL-3.0. Pure functions: every value
comes from the answers passed in. Power is in W, energy in kWh.

Changes from 2.0.0: nothing here makes a device unavailable. An inverter that
is not producing (night, idle) reports 0 W, and the EV charger has no
"discharged" counter (it was always 0).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .timing import is_asleep

# energyOverview timePeriod -> key (energy in Wh)
ENERGY_PERIODS = {
    "LIFE_TIME": "energy_lifetime",
    "LAST_YEAR": "energy_year",
    "LAST_MONTH": "energy_month",
    "LAST_DAY": "energy_today",
}

# Lower-case inverterStatus values; only 'production' was seen live, the others
# are assumed after the SunSpec states. 'sleeping' is set from is_asleep.
INVERTER_STATUSES = [
    "production",
    "sleeping",
    "starting",
    "throttled",
    "shutting_down",
    "fault",
    "standby",
    "off",
]

BATTERY_STATES = ["charging", "discharging", "idle"]

# EV connectionStatus (lower case in the powerflow, upper case in getSiteDevices)
# -> [charging state, charging]
EV_CONNECTION = {
    "connected": ("plugged_in", False),
    "charging": ("plugged_in_charging", True),
    "disconnected": ("plugged_out", False),
}
EV_CHARGING_STATES = ["plugged_in", "plugged_in_charging", "plugged_out"]
EV_MODES = {"AUTO": "auto", "MANUAL": "manual"}
# excessPV: -1 solar surplus charging on, -2 off.
EXCESS_PV = {"-1": True, "-2": False}

# Load controller state -> activationState body.
HLC_STATES: dict[str, dict[str, Any]] = {
    "on": {"mode": "MANUAL", "level": 100},
    "off": {"mode": "MANUAL", "level": 0},
    "auto": {"mode": "AUTO", "level": None, "duration": None},
}


def finite(value: Any) -> float | None:
    """The value when it is a finite number (not a bool), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def watts(kw: Any) -> int | None:
    """kW to whole W."""
    value = finite(kw)
    return None if value is None else round(value * 1000)


def kwh(wh: Any) -> float | None:
    """Wh to kWh, Wh resolution."""
    value = finite(wh)
    return None if value is None else round(value / 1000, 3)


def _sum(items: Iterable[Mapping[str, Any]], key: str) -> float | None:
    """The sum of key over items, or None when one has no number."""
    values = [finite(item.get(key)) for item in items]
    if not values or any(value is None for value in values):
        return None
    return sum(values)  # type: ignore[arg-type]


# ---------------------------------------------------------------- site


def all_site_devices(response: Any) -> list[dict[str, Any]]:
    """Every device of a getSiteDevices/getSiteBatteries answer, once per reporterId.

    Devices are in `devices` and in every list under `devicesByType`; a real EV
    charger answer has it only in devicesByType.EV_CHARGER.
    """
    if not isinstance(response, Mapping):
        return []
    found: list[Any] = list(response.get("devices") or [])
    for group in (response.get("devicesByType") or {}).values():
        found.extend(group or [])
    seen: set[str] = set()
    result = []
    for device in found:
        if not isinstance(device, Mapping):
            continue
        ident = device.get("reporterId") or (device.get("info") or {}).get("serialNumber")
        if ident is not None:
            if str(ident) in seen:
                continue
            seen.add(str(ident))
        result.append(dict(device))
    return result


def device_in(response: Any, reporter_id: str) -> dict[str, Any] | None:
    """The device with reporter_id in a getSiteDevices answer."""
    for device in all_site_devices(response):
        if str(device.get("reporterId")) == str(reporter_id):
            return device
    return None


def equipment_serials(equipment: Any) -> list[str]:
    """The serials of the site's inverters, from getSiteEquipment."""
    if not isinstance(equipment, Mapping):
        return []
    details = (equipment.get("inverters") or {}).get("details") or []
    return [str(serial) for detail in details for serial in detail.get("serialNumbers") or []]


def discover(
    powerflow: Any, site_devices: Any
) -> dict[str, Any]:
    """What a site has, from its powerflow and devices list.

    The inverter is always there: at night the powerflow reports production as
    inactive, which is no reason to leave it out (2.0.0 pairing did).
    """
    powerflow = powerflow if isinstance(powerflow, Mapping) else {}
    grid = powerflow.get("grid") or {}
    storage = powerflow.get("storage") or {}
    ev_ids = {
        str(item.get("reporterId"))
        for item in ((site_devices or {}).get("devicesByType") or {}).get("EV_CHARGER") or []
        if isinstance(item, Mapping) and item.get("reporterId") is not None
    }
    evs: dict[str, str] = {}
    hlcs: dict[str, str] = {}
    for device in all_site_devices(site_devices):
        rid = device.get("reporterId")
        if rid is None:
            continue
        rid = str(rid)
        if rid in ev_ids or str(device.get("type") or "").startswith("EV_CHARGER"):
            evs[rid] = device.get("name") or "EV charger"
        elif device.get("type") == "ON_OFF":
            hlcs[rid] = device.get("name") or "Load controller"
    return {
        "meter": grid.get("isActive") is True,
        "battery": storage.get("isActive") is True
        or finite(storage.get("chargeLevel")) is not None,
        "ev": evs,
        "hlc": hlcs,
    }


def site_values(
    communication: Any, alerts: Any, asleep: bool
) -> dict[str, Any]:
    """Connectivity (not offline while the inverters sleep) and open alerts."""
    values: dict[str, Any] = {}
    status = (communication or {}).get("connectivityStatus") if isinstance(communication, Mapping) else None
    if isinstance(status, str):
        values["connectivity"] = status == "ONLINE" or asleep
    count = finite(alerts.get("totalAlertsCount")) if isinstance(alerts, Mapping) else None
    if count is not None:
        values["alerts"] = int(count)
        values["problem"] = count > 0
    return values


# ---------------------------------------------------------------- inverter


def inverter_power(powerflow: Any) -> dict[str, Any]:
    """Solar power; 0 W when not producing (never unavailable at night)."""
    if not isinstance(powerflow, Mapping):
        return {}
    production = powerflow.get("solarProduction") or {}
    return {"power": watts(production.get("currentPower")) or 0}


def inverter_energy(overview: Any) -> dict[str, Any]:
    """Lifetime, year, month and day production; a period without a number is left out."""
    values: dict[str, Any] = {}
    items = (overview or {}).get("energyProducedOverviewList") if isinstance(overview, Mapping) else None
    if isinstance(items, Mapping):
        items = list(items.values())
    for item in items or []:
        key = ENERGY_PERIODS.get(item.get("timePeriod"))
        value = kwh(item.get("energy"))
        if key and value is not None:
            values[key] = value
    return values


def inverter_live(information: Any) -> list[dict[str, Any]]:
    """serialToLiveData values of getInvertersInformation."""
    if not isinstance(information, Mapping):
        return []
    return [dict(item) for item in (information.get("serialToLiveData") or {}).values() if isinstance(item, Mapping)]


def inverter_details(
    information: Any, now_ms: int, cadence_ms: int | None = None
) -> dict[str, Any]:
    """Voltage per phase (averaged over the site's inverters) and status."""
    if not isinstance(information, Mapping):
        return {}
    values: dict[str, Any] = {}
    phases = [
        line
        for lines in (information.get("serialToPhaseMeasurements") or {}).values()
        for line in lines or []
        if isinstance(line, Mapping)
    ]
    for phase in (1, 2, 3):
        volts = [
            v
            for line in phases
            if line.get("lineNumber_3ph") == phase and (v := finite(line.get("v_ac_V"))) is not None
        ]
        if volts:
            values[f"voltage_l{phase}"] = round(sum(volts) / len(volts), 1)
    live = inverter_live(information)
    reported = [item["inverterStatus"] for item in live if isinstance(item.get("inverterStatus"), str)]
    if is_asleep(live, now_ms, cadence_ms):
        values["status"] = "sleeping"
    elif reported:
        status = next((s for s in reported if s.lower() == "fault"), reported[0]).lower()
        values["status"] = status if status in INVERTER_STATUSES else None
    return values


def _celsius(reading: Any) -> float | None:
    if not isinstance(reading, Mapping):
        return None
    value = finite(reading.get("temperature"))
    if value is None:
        return None
    if str(reading.get("temperatureUnit") or "CELSIUS").upper() == "FAHRENHEIT":
        return round((value - 32) * 5 / 9, 1)
    return value


def max_optimizer_temperature(by_inverter: Any) -> float | None:
    """The hottest optimizer of the day in °C; None before sunrise."""
    if not isinstance(by_inverter, Mapping):
        return None
    temperatures = [
        t
        for inverter in by_inverter.get("inverters") or []
        for optimizer in inverter.get("optimizers") or []
        if (t := _celsius(optimizer.get("temperature"))) is not None
    ]
    return max(temperatures) if temperatures else None


# ---------------------------------------------------------------- meter


def meter_power(powerflow: Any) -> dict[str, Any]:
    """Grid power (import positive, export negative), load and solar power."""
    if not isinstance(powerflow, Mapping):
        return {}
    values: dict[str, Any] = {}
    grid = powerflow.get("grid") or {}
    power = watts(grid.get("currentPower"))
    if power is not None:
        if str(grid.get("status")).lower() == "export":
            power = -power
        values["grid_power"] = power
    elif "currentPower" in grid and grid.get("isActive"):
        values["grid_power"] = 0
    if "consumption" in powerflow:
        values["load_power"] = watts((powerflow["consumption"] or {}).get("currentPower")) or 0
    if "solarProduction" in powerflow:
        values["solar_power"] = watts((powerflow["solarProduction"] or {}).get("currentPower")) or 0
    return values


def year_totals(result: Any) -> dict[str, Any] | None:
    """A year's import/export in Wh, or None when it has no data (summary: null).

    Without a grid meter in that year import/export are null: counted as 0 and
    marked unknown (it can also be a hiccup, so it is asked again later).
    """
    summary = result.get("summary") if isinstance(result, Mapping) else None
    if not isinstance(summary, Mapping):
        return None
    imported = finite(summary.get("import"))
    exported = finite(summary.get("export"))
    totals: dict[str, Any] = {"imported": imported or 0, "exported": exported or 0}
    if imported is None or exported is None:
        totals["unknown"] = True
    return totals


# ---------------------------------------------------------------- battery


def battery_power(powerflow: Any) -> dict[str, Any]:
    """Battery power (charging positive), charge level and state."""
    if not isinstance(powerflow, Mapping) or not isinstance(powerflow.get("storage"), Mapping):
        return {}
    storage = powerflow["storage"]
    values: dict[str, Any] = {}
    status = storage.get("status")
    status = status.lower() if isinstance(status, str) else None
    power = watts(storage.get("currentPower")) or 0
    values["power"] = -power if status == "discharging" else power
    level = finite(storage.get("chargeLevel"))
    if level is not None:
        values["soc"] = level
    if status in BATTERY_STATES:
        values["state"] = status
    return values


def battery_unit(batteries: Sequence[Mapping[str, Any]]) -> float | None:
    """kWh per unit of the battery energies: kWh below a nameplate of 100, else Wh."""
    name_plate = _sum(batteries, "namePlateEnergy")
    if name_plate is None or name_plate <= 0:
        return None
    return 1.0 if name_plate < 100 else 0.001


def batteries_of(response: Any) -> list[dict[str, Any]]:
    """The batteries of a getSiteBatteries answer."""
    if not isinstance(response, Mapping):
        return []
    return [dict(b) for b in ((response.get("devicesByType") or {}).get("BATTERY") or []) if isinstance(b, Mapping)]


def battery_energy(batteries: Sequence[Mapping[str, Any]], to_kwh: float) -> dict[str, Any]:
    """Energies (kWh, before the never-falling correction) and health of the batteries.

    Not seen live: the fields come from nerd2/solaredgego via 2.0.0.
    """
    def energy(key: str) -> float | None:
        value = _sum(batteries, key)
        return None if value is None else value * to_kwh

    name_plate = _sum(batteries, "namePlateEnergy")
    full_pack = energy("fullPackEnergy")
    values: dict[str, Any] = {
        "energy_charged": energy("chargeEnergy"),
        "energy_discharged": energy("dischargeEnergy"),
        "residual_energy": energy("remainingEnergy"),
        "state_of_health": None,
    }
    if full_pack is not None and name_plate:
        values["state_of_health"] = min(100, round(full_pack / (name_plate * to_kwh) * 100))
    return {key: value for key, value in values.items() if value is not None}


# ---------------------------------------------------------------- EV charger


def ev_values(powerflow: Any, site_devices: Any, reporter_id: str) -> dict[str, Any]:
    """Power, charging state and session values of one EV charger.

    The powerflow's evCharger is one per site: the charger's own entry in
    getSiteDevices comes first. Its CURRENT_POWER_WH is W despite its name, and
    present only while charging; 0 while charging is no reading.
    """
    flow = powerflow.get("evCharger") if isinstance(powerflow, Mapping) else None
    flow = flow if isinstance(flow, Mapping) else None
    charger = device_in(site_devices, reporter_id)
    values: dict[str, Any] = {}

    connection = (charger or {}).get("connectionStatus") or (flow or {}).get("connectionStatus")
    if connection:
        state = EV_CONNECTION.get(str(connection).lower())
        if state:
            values["charging_state"], values["charging"] = state

    power: int | None = None
    if charger:
        sub = next(
            (s for s in charger.get("chargerStatusSubTitle") or [] if s.get("subStatusTitle") == "CURRENT_POWER_WH"),
            None,
        )
        status = str(charger.get("connectionStatus") or "").upper()
        reading = finite((sub or {}).get("numericValue"))
        power = None if reading is None else round(reading)
        if status == "CHARGING" and power == 0:
            power = None
        if power is None and status and status != "CHARGING":
            power = 0
        energy = kwh(charger.get("sessionEnergy"))
        if energy is not None:
            values["session_energy"] = energy
        duration = finite(charger.get("sessionDuration"))
        if duration is not None:
            values["session_duration"] = round(duration / 60)
        if charger.get("activationMode") in EV_MODES:
            values["charge_mode"] = EV_MODES[charger["activationMode"]]
        solar = EXCESS_PV.get(str(charger.get("excessPV")))
        if solar is not None:
            values["solar_charging"] = solar
        alias = (charger.get("applianceData") or {}).get("alias")
        if isinstance(alias, str):
            values["vehicle"] = alias
    if power is None and flow is not None:
        power = 0 if flow.get("currentPower") is None else watts(flow.get("currentPower"))
    if power is not None:
        values["power"] = power
    return values


def ev_charged_wh(history: Any) -> float | None:
    """Wh charged over the sessions of the last year; None without a history."""
    if not isinstance(history, Mapping) or not isinstance(history.get("applianceDetails"), list):
        return None
    total = 0.0
    for details in history["applianceDetails"]:
        for statistic in (details or {}).get("statistics") or []:
            if statistic.get("subjectEnum") == "CONSUMED_ENERGY":
                total += finite(statistic.get("totalValue")) or 0
                break
    return total


# ---------------------------------------------------------------- load controller


def hlc_values(site_devices: Any, reporter_id: str) -> dict[str, Any]:
    """State (on/off/auto) and power of a home load controller.

    Power is the device's meter, else rated power x level / 100 (an estimate,
    as the SolarEdge app shows it for relays without a meter).
    """
    device = device_in(site_devices, reporter_id)
    if device is None:
        return {}
    values: dict[str, Any] = {}
    status = device.get("status") or {}
    level = finite(status.get("level"))
    mode = device.get("activationMode")
    if mode == "AUTO":
        values["state"] = "auto"
    elif mode == "MANUAL" and level == 0:
        values["state"] = "off"
    elif mode == "MANUAL" and level == 100:
        values["state"] = "on"
    measured = finite(status.get("activePowerMeter"))
    rated = finite((device.get("config") or {}).get("ratedPower"))
    if measured is not None:
        values["power"] = round(measured)
    elif rated is not None and level is not None:
        values["power"] = round(rated * level / 100)
    if "power" in values:
        values["power_measured"] = measured is not None
    values["active"] = device.get("deviceStatus") == "ACTIVE"
    return values
