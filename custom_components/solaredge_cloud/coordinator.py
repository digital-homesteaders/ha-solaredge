"""One coordinator per mySolarEdge account.

Port of app.js and the polling in lib/common_device.js of com.solaredge v2.0.0
for Homey, Copyright 2026 Robin de Gruijter, GPL-3.0.

Every poll reads the real-time powerflow once per site (and the devices list
once per site with EV chargers or load controllers), for all devices. An energy
tick every 5 minutes, placed just after the inverters upload (timing.py), also
reads the energy totals, inverter details, batteries and the meter's yearly
totals; every third energy tick also reads alerts, connectivity, optimizer
temperatures and the EV charging history.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import mapping
from .api import (
    SolarEdgeAuthError,
    SolarEdgeCloudClient,
    SolarEdgeConnectionError,
    SolarEdgeError,
    SolarEdgeRateLimitError,
)
from .const import (
    BACKOFF_MAX,
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    FAIL_LIMIT,
    KIND_BATTERY,
    KIND_EV,
    KIND_HLC,
    KIND_INVERTER,
    KIND_METER,
    KIND_SITE,
    SLOW_EVERY,
    STORAGE_VERSION,
)
from .meter_totals import async_meter_totals
from .timing import EnergyTiming, delay_to_next_tick, parse_time_ms

_LOGGER = logging.getLogger(__name__)

type SolarEdgeConfigEntry = ConfigEntry[SolarEdgeCoordinator]


def async_create_session(hass: HomeAssistant, *, auto_cleanup: bool = True) -> aiohttp.ClientSession:
    """A session of its own per account.

    HA's shared session keeps cookies for everyone; this one keeps none and the
    client sends its own session cookies, so two accounts never mix. Created
    during entry setup, HA detaches it when the entry unloads.
    """
    return async_create_clientsession(
        hass, auto_cleanup=auto_cleanup, cookie_jar=aiohttp.DummyCookieJar()
    )


@dataclass
class SiteInfo:
    """A site and what it has."""

    site_id: str
    name: str
    timezone: str | None = None
    install_year: int | None = None
    peak_power: float | None = None
    has_meter: bool = False
    has_battery: bool = False
    inverter_serials: list[str] = field(default_factory=list)
    ev_chargers: dict[str, str] = field(default_factory=dict)
    load_controllers: dict[str, str] = field(default_factory=dict)

    @property
    def inverter_only(self) -> bool:
        """Only an inverter: nothing to read while the sun is down."""
        return not (self.has_meter or self.has_battery or self.ev_chargers or self.load_controllers)

    def device_keys(self) -> list[str]:
        """The keys of the site's devices, the site itself first."""
        keys = [device_key(self.site_id, KIND_SITE), device_key(self.site_id, KIND_INVERTER)]
        if self.has_meter:
            keys.append(device_key(self.site_id, KIND_METER))
        if self.has_battery:
            keys.append(device_key(self.site_id, KIND_BATTERY))
        keys += [device_key(self.site_id, KIND_EV, rid) for rid in self.ev_chargers]
        keys += [device_key(self.site_id, KIND_HLC, rid) for rid in self.load_controllers]
        return keys


def device_key(site_id: str, kind: str, reporter_id: str | None = None) -> str:
    """site:kind[:reporterId]."""
    return f"{site_id}:{kind}" + (f":{reporter_id}" if reporter_id else "")


def _year_in(when: datetime, tz: str | None) -> int:
    return when.astimezone(_zone(tz)).year


def _zone(tz: str | None) -> Any:
    if tz:
        try:
            return ZoneInfo(tz)
        except (KeyError, ValueError):
            pass
    return dt_util.get_default_time_zone()


class SolarEdgeCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Polls one account; data is { device key: { value key: value } }."""

    config_entry: SolarEdgeConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: SolarEdgeConfigEntry,
        client: SolarEdgeCloudClient,
    ) -> None:
        """Set up the account's polling."""
        self.poll_interval = int(entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL))
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=self.poll_interval),
        )
        self.client = client
        self.sites: dict[str, SiteInfo] = {}
        # Site id -> the site's device registry id.
        self.site_device_ids: dict[str, str] = {}
        self.timing = EnergyTiming()
        self.consecutive_failures = 0
        self.outages = 0
        self._energy_due = True
        self._energy_count = 0
        self._unsub_energy: CALLBACK_TYPE | None = None
        self._solar_idle: dict[str, bool] = {}
        self._meter_tasks: dict[str, asyncio.Task[None]] = {}
        # The latest yearly totals per site, merged into every poll's values.
        self._meter_values: dict[str, dict[str, Any]] = {}
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self._stored: dict[str, Any] = {}
        self._was_available = True
        # The time source; tests replace it.
        self.now_ms = lambda: int(time.time() * 1000)

    # ------------------------------------------------------------ setup

    async def _async_setup(self) -> None:
        """Find the sites and their devices (once per load)."""
        self._stored = await self._store.async_load() or {}
        self._stored.setdefault("years", {})
        self._stored.setdefault("counters", {})
        self._stored.setdefault("units", {})
        self._stored.setdefault("seen", {})
        try:
            sites = await self.client.get_sites()
            for site in sites:
                info = await self._discover_site(site)
                self.sites[info.site_id] = info
        except SolarEdgeAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except SolarEdgeError as err:
            raise UpdateFailed(f"Could not read the SolarEdge sites: {err}") from err
        self._store.async_delay_save(lambda: self._stored, 1)

    async def _discover_site(self, site: dict[str, Any]) -> SiteInfo:
        site_id = str(site["id"])
        tz = site.get("timezone")
        installed = parse_time_ms(site.get("installationDate"))
        if installed is None and isinstance(site.get("installationDate"), str):
            installed = parse_time_ms(site["installationDate"][:10] + "T00:00:00+00:00")
        info = SiteInfo(
            site_id=site_id,
            name=str(site.get("name") or f"Site {site_id}"),
            timezone=tz,
            install_year=None
            if installed is None
            else _year_in(datetime.fromtimestamp(installed / 1000, tz=dt_util.UTC), tz),
            peak_power=mapping.finite(site.get("peakPower")),
        )
        powerflow: Any
        devices: Any
        equipment: Any
        powerflow, devices, equipment = await asyncio.gather(
            self.client.get_site_powerflow(site_id),
            self.client.get_site_devices(site_id),
            self.client.get_site_equipment(site_id),
            return_exceptions=True,
        )
        for result in (powerflow, devices, equipment):
            if isinstance(result, SolarEdgeAuthError):
                raise result
        if isinstance(powerflow, BaseException):
            raise powerflow
        found = mapping.discover(
            powerflow, None if isinstance(devices, BaseException) else devices
        )
        # Once seen, a device stays: a battery or meter that is inactive at the
        # next restart (night, empty) keeps its entities.
        seen = self._stored["seen"].setdefault(site_id, {"meter": False, "battery": False, "ev": {}, "hlc": {}})
        seen["meter"] = seen["meter"] or found["meter"]
        seen["battery"] = seen["battery"] or found["battery"]
        seen["ev"].update(found["ev"])
        seen["hlc"].update(found["hlc"])
        info.has_meter = seen["meter"]
        info.has_battery = seen["battery"]
        info.ev_chargers = dict(seen["ev"])
        info.load_controllers = dict(seen["hlc"])
        if not isinstance(equipment, BaseException):
            info.inverter_serials = mapping.equipment_serials(equipment)
        return info

    @callback
    def async_start_energy_ticks(self) -> None:
        """Schedule the energy ticks; call once the entry is set up."""
        self._schedule_energy_tick()

    async def async_shutdown(self) -> None:
        """Stop the energy ticks and yearly-totals runs, and save the store (on unload)."""
        if self._unsub_energy:
            self._unsub_energy()
            self._unsub_energy = None
        for task in self._meter_tasks.values():
            task.cancel()
        if self._stored:
            await self._store.async_save(self._stored)
        await super().async_shutdown()

    @callback
    def _schedule_energy_tick(self) -> None:
        if self._unsub_energy:
            self._unsub_energy()
        delay = delay_to_next_tick(self.now_ms(), self.timing.phase_ms) / 1000
        self._unsub_energy = async_call_later(self.hass, delay, self._on_energy_tick)

    @callback
    def _on_energy_tick(self, _now: datetime) -> None:
        self._unsub_energy = None
        self._schedule_energy_tick()
        self._energy_due = True
        self.timing.judge_pending = True
        # While backing off or waiting for new credentials the regular poll
        # picks up the pending energy reading later.
        if self.outages or isinstance(self.last_exception, ConfigEntryAuthFailed):
            return
        self.config_entry.async_create_background_task(
            self.hass, self.async_refresh(), f"{DOMAIN} energy tick"
        )

    @callback
    def async_set_poll_interval(self, seconds: int) -> None:
        """A changed poll interval from the options flow."""
        self.poll_interval = seconds
        if not self.outages:
            self.update_interval = timedelta(seconds=seconds)
            self._schedule_refresh()

    # ------------------------------------------------------------ availability

    @property
    def available(self) -> bool:
        """Entities go unavailable only after FAIL_LIMIT failed polls in a row."""
        return self.data is not None and self.consecutive_failures < FAIL_LIMIT

    @callback
    def _async_refresh_finished(self) -> None:
        # The base class tells listeners only about the first failure in a row;
        # they also need to hear when FAIL_LIMIT is reached.
        if self.available != self._was_available:
            self._was_available = self.available
            if self.last_update_success is False:
                self.async_update_listeners()

    # ------------------------------------------------------------ polling

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        try:
            data = await self._tick()
        except SolarEdgeAuthError as err:
            self.consecutive_failures += 1
            raise ConfigEntryAuthFailed("SolarEdge rejected the login") from err
        except SolarEdgeConnectionError as err:
            self.consecutive_failures += 1
            delay = self._note_outage()
            raise UpdateFailed(f"{err}; next poll in {delay} s") from err
        except SolarEdgeError as err:
            self.consecutive_failures += 1
            raise UpdateFailed(str(err)) from err
        self.consecutive_failures = 0
        if self.outages:
            self.outages = 0
            self.update_interval = timedelta(seconds=self.poll_interval)
        return data

    def _note_outage(self) -> int:
        """Back off: twice the poll interval, doubling per outage, up to BACKOFF_MAX."""
        self.outages += 1
        delay = min(self.poll_interval * 2 ** min(self.outages, 16), BACKOFF_MAX)
        self.update_interval = timedelta(seconds=delay)
        return delay

    async def _tick(self) -> dict[str, dict[str, Any]]:
        """One poll of the account."""
        data = {key: dict(values) for key, values in (self.data or {}).items()}
        energy = self._energy_due
        slow = False
        if energy:
            self._energy_count += 1
            slow = self._energy_count % SLOW_EVERY == 1

        await self._live(data, energy)
        if energy:
            # Pending until here: a failed live part keeps it for the next poll.
            self._energy_due = False
            for site in self.sites.values():
                await self._energy(site, data, slow)
        # The yearly totals run outside the poll and may finish during it.
        for sid, values in self._meter_values.items():
            data.setdefault(device_key(sid, KIND_METER), {}).update(values)
        self._store.async_delay_save(lambda: self._stored, 10)
        return data

    async def _live(self, data: dict[str, dict[str, Any]], energy: bool) -> None:
        """The powerflow (and devices list) of every site, all at once."""
        requests: list[tuple[SiteInfo, str, Any]] = []
        for site in self.sites.values():
            # Only an inverter and the sun is down: the energy tick is enough.
            if not (site.inverter_only and self._solar_idle.get(site.site_id) and not energy):
                requests.append((site, "powerflow", self.client.get_site_powerflow(site.site_id)))
            if site.ev_chargers or site.load_controllers:
                requests.append((site, "devices", self.client.get_site_devices(site.site_id)))
        if not requests:
            return
        results = await asyncio.gather(*(request for _, _, request in requests), return_exceptions=True)
        errors = [result for result in results if isinstance(result, BaseException)]
        for error in errors:
            if isinstance(error, (SolarEdgeAuthError, SolarEdgeRateLimitError)):
                raise error
            if not isinstance(error, SolarEdgeError):
                raise error
        if len(errors) == len(results):
            raise errors[0]
        for error in errors:
            _LOGGER.debug("Part of a SolarEdge poll failed: %s", error)

        by_site: dict[str, dict[str, Any]] = {}
        for (site, kind, _), result in zip(requests, results, strict=True):
            if not isinstance(result, BaseException):
                by_site.setdefault(site.site_id, {})[kind] = result
        for site in self.sites.values():
            answers = by_site.get(site.site_id, {})
            if powerflow := answers.get("powerflow"):
                production = powerflow.get("solarProduction") or {}
                self._solar_idle[site.site_id] = not production.get("isActive") or not production.get("currentPower")
                self._merge(data, site, KIND_INVERTER, mapping.inverter_power(powerflow))
                if site.has_meter:
                    self._merge(data, site, KIND_METER, mapping.meter_power(powerflow))
                if site.has_battery:
                    self._merge(data, site, KIND_BATTERY, mapping.battery_power(powerflow))
            devices = answers.get("devices")
            if powerflow or devices:
                for rid in site.ev_chargers:
                    self._merge(data, site, KIND_EV, mapping.ev_values(powerflow, devices, rid), rid)
            if devices:
                for rid in site.load_controllers:
                    self._merge(data, site, KIND_HLC, mapping.hlc_values(devices, rid), rid)

    async def _optional(self, request: Any) -> Any:
        """An energy-tick reading: a failure leaves the values as they are.

        A rejected login and a rate limit still end the poll.
        """
        try:
            return await request
        except (SolarEdgeAuthError, SolarEdgeRateLimitError):
            raise
        except SolarEdgeError as err:
            _LOGGER.debug("SolarEdge reading failed: %s", err)
            return None

    async def _energy(self, site: SiteInfo, data: dict[str, dict[str, Any]], slow: bool) -> None:
        """The energy tick's readings of one site."""
        sid = site.site_id
        now_ms = self.now_ms()
        if not site.inverter_serials:
            equipment = await self._optional(self.client.get_site_equipment(sid))
            site.inverter_serials = mapping.equipment_serials(equipment)
        serials = site.inverter_serials

        overview, information = await asyncio.gather(
            self._optional(self.client.get_site_energy_overview(sid)),
            self._optional(self.client.get_inverters_information(serials)) if serials else _none(),
        )
        self._merge(data, site, KIND_INVERTER, mapping.inverter_energy(overview))
        live = mapping.inverter_live(information)
        if information is not None:
            for serial, item in (information.get("serialToLiveData") or {}).items():
                at = parse_time_ms(item.get("lastMeasurement"))
                if at is not None and self.timing.note_snapshot(str(serial), at, now_ms):
                    _LOGGER.debug("Energy tick moved to %.0f s after each 5-minute mark", self.timing.phase_ms / 1000)
                    self._schedule_energy_tick()
            self._merge(data, site, KIND_INVERTER, mapping.inverter_details(information, now_ms, self.timing.cadence_ms))

        if site.has_battery:
            batteries = mapping.batteries_of(await self._optional(self.client.get_site_batteries(sid)))
            if batteries:
                self._merge(data, site, KIND_BATTERY, self._battery_energy(sid, batteries))

        if site.has_meter:
            task = self._meter_tasks.get(sid)
            if task is None or task.done():
                self._meter_tasks[sid] = self.config_entry.async_create_background_task(
                    self.hass, self._meter_totals(site), f"{DOMAIN} meter totals {sid}"
                )

        if not slow:
            return
        asleep = mapping.is_asleep(live, now_ms, self.timing.cadence_ms) if live else False
        communication, alerts = await asyncio.gather(
            self._optional(self.client.get_site_communication(sid)),
            self._optional(self.client.get_site_alerts(sid)),
        )
        self._merge(data, site, KIND_SITE, mapping.site_values(communication, alerts, asleep))
        if serials:
            today = datetime.fromtimestamp(now_ms / 1000, tz=_zone(site.timezone)).date().isoformat()
            by_inverter = await self._optional(self.client.get_site_energy_by_inverter(sid, today, serials))
            if by_inverter is not None:
                # None before sunrise: cleared rather than yesterday's.
                self._merge(
                    data, site, KIND_INVERTER,
                    {"optimizer_temperature_max": mapping.max_optimizer_temperature(by_inverter)},
                    keep_none=True,
                )
        for rid in site.ev_chargers:
            history = await self._optional(self.client.get_appliance_sessions_history(sid, rid))
            charged = mapping.ev_charged_wh(history)
            if charged is not None:
                # Old sessions leave the rolling year: kept rising.
                total = self._monotonic(f"{sid}:ev:{rid}:charged", charged / 1000)
                self._merge(data, site, KIND_EV, {"energy_charged": total}, rid)

    def _battery_energy(self, site_id: str, batteries: list[dict[str, Any]]) -> dict[str, Any]:
        # The unit is kept from the last answer with a nameplate: a guess that
        # flipped between answers would change the counters 1000-fold.
        unit = mapping.battery_unit(batteries)
        if unit is not None:
            self._stored["units"][site_id] = unit
        to_kwh = self._stored["units"].get(site_id, 0.001)
        values = mapping.battery_energy(batteries, to_kwh)
        for key in ("energy_charged", "energy_discharged"):
            if key in values:
                values[key] = self._monotonic(f"{site_id}:battery:{key}", values[key])
        return values

    async def _meter_totals(self, site: SiteInfo) -> None:
        """The meter's yearly totals, outside the poll; one run per site at a time."""
        sid = site.site_id
        now = dt_util.utcnow()
        cache = self._stored["years"].setdefault(sid, {})
        try:
            totals = await async_meter_totals(
                self.client,
                sid,
                current_year=_year_in(now, site.timezone),
                install_year=site.install_year,
                cache=cache,
                now_ms=self.now_ms(),
            )
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - any failure aborts the run
            _LOGGER.debug("Meter totals for site %s not updated: %s", sid, err)
            return
        finally:
            self._store.async_delay_save(lambda: self._stored, 10)
        values: dict[str, Any] = {}
        for key in ("imported", "exported"):
            # Lifetime counters never fall: a short answer is a hiccup, not a reset.
            values[f"energy_{key}"] = self._never_less(f"{sid}:meter:{key}", totals[key] / 1000)
            values[f"energy_{key}_year"] = round(totals[f"{key}_year"] / 1000, 3)
        self._meter_values[sid] = values
        if self.data is not None:
            self._merge(self.data, site, KIND_METER, values)
            self.async_update_listeners()

    # ------------------------------------------------------------ values

    def _merge(
        self,
        data: dict[str, dict[str, Any]],
        site: SiteInfo,
        kind: str,
        values: dict[str, Any],
        reporter_id: str | None = None,
        keep_none: bool = False,
    ) -> None:
        target = data.setdefault(device_key(site.site_id, kind, reporter_id), {})
        for key, value in values.items():
            if value is not None or keep_none:
                target[key] = value
            elif key == "status":
                target[key] = None

    def _never_less(self, name: str, reading: float) -> float:
        counters = self._stored["counters"]
        last = counters.get(name, {}).get("last")
        if isinstance(last, (int, float)) and reading < last:
            _LOGGER.debug("%s fell from %s to %s; kept", name, last, reading)
            return last
        counters[name] = {"last": round(reading, 3)}
        return round(reading, 3)

    def _monotonic(self, name: str, reading: float) -> float:
        """A counter that never falls, from one that can (a reset, a rolling window).

        A fall adds the difference to an offset. A single low reading is held
        and only counted when the next one is low too: a battery or session
        missing from one answer would otherwise be counted twice.
        """
        counter = self._stored["counters"].setdefault(name, {})
        last = counter.get("last")
        offset = counter.get("offset", 0.0)
        if isinstance(last, (int, float)) and reading < last:
            if not counter.get("falling"):
                counter["falling"] = True
                return round(last + offset, 3)
            offset += last - reading
            counter["offset"] = offset
            _LOGGER.debug("%s fell from %s to %s; offset now %s", name, last, reading, offset)
        counter["falling"] = False
        counter["last"] = reading
        return round(reading + offset, 3)

    # ------------------------------------------------------------ commands

    async def async_set_activation(self, site_id: str, reporter_id: str, state: dict[str, Any]) -> None:
        """Switch an EV charger or load controller, then poll."""
        try:
            await self.client.set_activation_state(site_id, reporter_id, state)
        except SolarEdgeAuthError as err:
            self.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError("SolarEdge rejected the login") from err
        except SolarEdgeError as err:
            raise HomeAssistantError(f"SolarEdge did not accept the command: {err}") from err
        await self.async_request_refresh()

    def diagnostics(self) -> dict[str, Any]:
        """State for the diagnostics download (no credentials, no serials)."""
        return {
            "poll_interval": self.poll_interval,
            "update_interval": self.update_interval.total_seconds() if self.update_interval else None,
            "consecutive_failures": self.consecutive_failures,
            "outages": self.outages,
            "logged_in": self.client.logged_in,
            "login_blocked_for": self.client.login_blocked_for,
            "energy_timing": self.timing.as_dict(),
            "sites": {
                sid: {
                    "timezone": site.timezone,
                    "install_year": site.install_year,
                    "peak_power": site.peak_power,
                    "has_meter": site.has_meter,
                    "has_battery": site.has_battery,
                    "inverters": len(site.inverter_serials),
                    "ev_chargers": len(site.ev_chargers),
                    "load_controllers": len(site.load_controllers),
                    "meter_years": self._stored.get("years", {}).get(sid, {}),
                }
                for sid, site in self.sites.items()
            },
            "values": self.data,
        }


async def _none() -> None:
    return None
