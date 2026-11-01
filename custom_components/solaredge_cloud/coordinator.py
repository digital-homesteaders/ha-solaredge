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
import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME
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
    RESERVE_RECHECK_S,
    SLOW_EVERY,
    STORAGE_VERSION,
)
from .meter_totals import async_meter_totals
from .timing import EnergyTiming, delay_to_next_tick, parse_time_ms

_LOGGER = logging.getLogger(__name__)

# A site whose equipment list has no inverters is asked again after this long.
EQUIPMENT_RECHECK_MS = 60 * 60 * 1000
# Battery power samples further apart than this (an outage) are not
# integrated: the energy in between is unknown.
MAX_SAMPLE_GAP_MS = 10 * 60 * 1000

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
        # Site id -> the pending re-read of the batteries after a backup reserve was set.
        self._unsub_reserve: dict[str, CALLBACK_TYPE] = {}
        self._solar_idle: dict[str, bool] = {}
        # Requests that are failing or look stale, until they are fine again.
        self._failing: set[str] = set()
        self._stale: set[str] = set()
        self._meter_tasks: dict[str, asyncio.Task[None]] = {}
        # The latest yearly totals per site, merged into every poll's values.
        self._meter_values: dict[str, dict[str, Any]] = {}
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self._stored: dict[str, Any] = {}
        # Whether _stored changed since the last save was scheduled.
        self._dirty = False
        # Discovery answers per site, used by the first poll instead of asking again.
        self._seed: dict[str, dict[str, Any]] = {}
        # Site id -> when an empty equipment list was last asked for (epoch ms).
        self._equipment_checked: dict[str, int] = {}
        # Site id -> the last battery power sample (epoch ms, W).
        self._battery_sample: dict[str, tuple[int, float]] = {}
        # Whether the integrated battery energy grew since the last save.
        self._flow_changed = False
        self._was_available = True
        # Which account a stored refresh token belongs to (the e-mail itself is not stored).
        self._user_key = hashlib.sha256(str(entry.data.get(CONF_USERNAME, "")).lower().encode()).hexdigest()
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
        # A refresh token from the last run renews the access token without the password.
        auth = self._stored.get("auth") or {}
        if auth.get("user") == self._user_key:
            self.client.set_refresh_token(auth.get("refresh_token"))
        self.client.on_refresh_token = self._keep_refresh_token
        try:
            sites = await self.client.get_sites()
        except SolarEdgeAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except SolarEdgeError as err:
            raise UpdateFailed(f"Could not read the SolarEdge sites: {err}") from err
        results = await asyncio.gather(
            *(self._discover_site(site) for site in sites), return_exceptions=True
        )
        errors: list[BaseException] = []
        for site, result in zip(sites, results, strict=True):
            if isinstance(result, SolarEdgeAuthError):
                raise ConfigEntryAuthFailed(str(result)) from result
            if isinstance(result, SiteInfo):
                self.sites[result.site_id] = result
                continue
            if not isinstance(result, SolarEdgeError):
                raise result
            # A failing site does not hold back the others (2.0.0 pairing did the
            # same). One seen before keeps its devices; polls retry it.
            info = self._site_from_store(site)
            if info is not None:
                _LOGGER.warning("SolarEdge site %s not reachable at setup (%s); using what was found before", info.site_id, result)
                self.sites[info.site_id] = info
            else:
                _LOGGER.warning("SolarEdge site %s left out: %s", site.get("id"), result)
                errors.append(result)
        if not self.sites and errors:
            raise UpdateFailed(f"Could not read any SolarEdge site: {errors[0]}") from errors[0]
        self._dirty = True
        self._save()

    def _keep_refresh_token(self, token: str | None) -> None:
        """Keep the account's refresh token in the store (dropped: None)."""
        if token:
            self._stored["auth"] = {"user": self._user_key, "refresh_token": token}
        else:
            self._stored.pop("auth", None)
        self._dirty = True
        self._save()

    def _site_info(self, site: dict[str, Any]) -> SiteInfo:
        """A site from the getSites answer, without its devices."""
        site_id = str(site["id"])
        tz = site.get("timezone")
        installed = parse_time_ms(site.get("installationDate"))
        if installed is None and isinstance(site.get("installationDate"), str):
            installed = parse_time_ms(site["installationDate"][:10] + "T00:00:00+00:00")
        return SiteInfo(
            site_id=site_id,
            name=str(site.get("name") or f"Site {site_id}"),
            timezone=tz,
            install_year=None
            if installed is None
            else _year_in(datetime.fromtimestamp(installed / 1000, tz=dt_util.UTC), tz),
            peak_power=mapping.finite(site.get("peakPower")),
        )

    def _site_from_store(self, site: dict[str, Any]) -> SiteInfo | None:
        """A site with the devices found at an earlier setup; None if never seen."""
        if str(site["id"]) not in self._stored["seen"]:
            return None
        info = self._site_info(site)
        self._apply_seen(info, {"meter": False, "battery": False, "ev": {}, "hlc": {}})
        return info

    def _apply_seen(self, info: SiteInfo, found: dict[str, Any]) -> None:
        """Once seen, a device stays: a battery or meter that is inactive at the
        next restart (night, empty) keeps its entities."""
        seen = self._stored["seen"].setdefault(info.site_id, {"meter": False, "battery": False, "ev": {}, "hlc": {}})
        seen["meter"] = seen["meter"] or found["meter"]
        seen["battery"] = seen["battery"] or found["battery"]
        seen["ev"].update(found["ev"])
        seen["hlc"].update(found["hlc"])
        info.has_meter = seen["meter"]
        info.has_battery = seen["battery"]
        info.ev_chargers = dict(seen["ev"])
        info.load_controllers = dict(seen["hlc"])

    async def _discover_site(self, site: dict[str, Any]) -> SiteInfo:
        """A site and its devices, from its powerflow, devices list and equipment."""
        info = self._site_info(site)
        site_id = info.site_id
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
        devices = None if isinstance(devices, BaseException) else devices
        self._apply_seen(info, mapping.discover(powerflow, devices))
        if not isinstance(equipment, BaseException):
            info.inverter_serials = mapping.equipment_serials(equipment)
            self._equipment_checked[site_id] = self.now_ms()
        self._seed[site_id] = {"powerflow": powerflow, "devices": devices}
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
        for unsub in self._unsub_reserve.values():
            unsub()
        self._unsub_reserve.clear()
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
        if self.outages or self.auth_failed:
            return
        self.config_entry.async_create_background_task(
            self.hass, self.async_refresh(), f"{DOMAIN} energy tick"
        )

    @property
    def auth_failed(self) -> bool:
        """Whether polling stopped for a rejected login (until reauth reloads the entry)."""
        return isinstance(self.last_exception, ConfigEntryAuthFailed)

    @callback
    def async_set_poll_interval(self, seconds: int) -> None:
        """A changed poll interval from the options flow."""
        self.poll_interval = seconds
        if self.outages:
            return
        self.update_interval = timedelta(seconds=seconds)
        # Not after a rejected login: that would try the rejected password again.
        if not self.auth_failed:
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
            await asyncio.gather(*(self._energy(site, data, slow) for site in self.sites.values()))
            if self._flow_changed:
                self._flow_changed = False
                self._dirty = True
        # The yearly totals run outside the poll and may finish during it.
        for sid, values in self._meter_values.items():
            data.setdefault(device_key(sid, KIND_METER), {}).update(values)
        self._save()
        return data

    def _save(self) -> None:
        """Schedule a store write, only when something in it changed."""
        if self._dirty:
            self._dirty = False
            self._store.async_delay_save(lambda: self._stored, 10)

    async def _live(self, data: dict[str, dict[str, Any]], energy: bool) -> None:
        """The powerflow (and devices list) of every site, all at once."""
        requests: list[tuple[SiteInfo, str, Any]] = []
        seeded: dict[str, dict[str, Any]] = {}
        for site in self.sites.values():
            # The first poll right after setup reuses the discovery answers.
            if seed := self._seed.pop(site.site_id, None):
                seeded[site.site_id] = {kind: answer for kind, answer in seed.items() if answer is not None}
                continue
            # Only an inverter and the sun is down: the energy tick is enough.
            if not (site.inverter_only and self._solar_idle.get(site.site_id) and not energy):
                requests.append((site, "powerflow", self.client.get_site_powerflow(site.site_id)))
            if self._devices_due(site, data, energy):
                requests.append((site, "devices", self.client.get_site_devices(site.site_id)))
        results = await asyncio.gather(*(request for _, _, request in requests), return_exceptions=True)
        errors = [result for result in results if isinstance(result, BaseException)]
        for error in errors:
            if isinstance(error, (SolarEdgeAuthError, SolarEdgeRateLimitError)):
                raise error
            if not isinstance(error, SolarEdgeError):
                raise error
        by_site: dict[str, dict[str, Any]] = seeded
        for (site, kind, _), result in zip(requests, results, strict=True):
            what = f"{kind} for site {site.site_id}"
            if isinstance(result, BaseException):
                self._note_failure(what, result)
                continue
            self._note_failure(what, None)
            by_site.setdefault(site.site_id, {})[kind] = result
        if errors and len(errors) == len(results):
            raise errors[0]
        for site_id, answers in by_site.items():
            for kind, answer in answers.items():
                self._note_stale(f"{kind} for site {site_id}", self._stale_reason(self.sites[site_id], kind, answer, data))
        for site in self.sites.values():
            answers = by_site.get(site.site_id, {})
            if powerflow := answers.get("powerflow"):
                production = powerflow.get("solarProduction") or {}
                self._solar_idle[site.site_id] = not production.get("isActive") or not production.get("currentPower")
                self._merge(data, site, KIND_INVERTER, mapping.inverter_power(powerflow))
                if site.has_meter:
                    self._merge(data, site, KIND_METER, mapping.meter_power(powerflow))
                if site.has_battery:
                    battery = mapping.battery_power(powerflow)
                    if "power" in battery:
                        battery.update(self._battery_flow(site.site_id, battery["power"]))
                    self._merge(data, site, KIND_BATTERY, battery)
            devices = answers.get("devices")
            if powerflow or devices:
                # Without a fresh devices list the powerflow's charger status
                # counts; the session values stay from the last list.
                for rid in site.ev_chargers:
                    self._merge(data, site, KIND_EV, mapping.ev_values(powerflow, devices, rid), rid)
            if devices:
                for rid in site.load_controllers:
                    self._merge(data, site, KIND_HLC, mapping.hlc_values(devices, rid), rid)

    def _devices_due(self, site: SiteInfo, data: dict[str, dict[str, Any]], energy: bool) -> bool:
        """Whether this poll needs the site's devices list.

        Load controllers need it every poll, and so do several EV chargers on
        one site: the powerflow's evCharger is one per site, so only the list
        tells them apart. One EV charger only while it charges (for its own
        power and session); otherwise the powerflow has its status, and the
        list is read on the energy tick.
        """
        if site.load_controllers or len(site.ev_chargers) > 1:
            return True
        if not site.ev_chargers:
            return False
        if energy:
            return True
        return any(
            data.get(device_key(site.site_id, KIND_EV, rid), {}).get("charging") is not False
            for rid in site.ev_chargers
        )

    def _note_failure(self, what: str, error: BaseException | None) -> None:
        """Warn once when `what` starts failing, and say once when it works again.

        One failing request doesn't fail the poll, so nothing else would show it.
        """
        if self._flag(self._failing, what, error is not None):
            _LOGGER.warning("SolarEdge %s failed: %s (logged again only once it works)", what, error)
        elif error is None and self._unflag(self._failing, what):
            _LOGGER.info("SolarEdge %s works again", what)

    def _note_stale(self, what: str, why: str | None) -> None:
        """Warn once when an answer looks like old data, and say once when it is live again."""
        if self._flag(self._stale, what, why is not None):
            _LOGGER.warning("SolarEdge %s looks stale: %s", what, why)
        elif why is None and self._unflag(self._stale, what):
            _LOGGER.info("SolarEdge %s is live again", what)

    @staticmethod
    def _flag(flags: set[str], what: str, bad: bool) -> bool:
        """True when `what` just went bad (not already flagged)."""
        if bad and what not in flags:
            flags.add(what)
            return True
        return False

    @staticmethod
    def _unflag(flags: set[str], what: str) -> bool:
        """True when `what` was flagged and is fine again."""
        if what in flags:
            flags.discard(what)
            return True
        return False

    def _stale_reason(self, site: SiteInfo, kind: str, answer: Any, data: dict[str, dict[str, Any]]) -> str | None:
        """Whether an answer looks old, not counting what is normal at night or while idle.

        An inverter reports not-real-time or not-communicating when the sun is
        down, and an idle charger's devices list doesn't change.
        """
        if kind == "powerflow" and isinstance(answer, dict):
            production = answer.get("solarProduction") or {}
            if not production.get("isActive") or not production.get("currentPower"):
                return None
        return mapping.stale_reason(kind, answer, self.now_ms(), check_age=self._ev_charging(site, data))

    @staticmethod
    def _ev_charging(site: SiteInfo, data: dict[str, dict[str, Any]]) -> bool:
        return any(
            data.get(device_key(site.site_id, KIND_EV, rid), {}).get("charging") is True for rid in site.ev_chargers
        )

    async def _optional(self, what: str, request: Any) -> Any:
        """An energy-tick reading: a failure leaves the values as they are.

        A rejected login and a rate limit still end the poll.
        """
        try:
            answer = await request
        except (SolarEdgeAuthError, SolarEdgeRateLimitError):
            raise
        except SolarEdgeError as err:
            self._note_failure(what, err)
            return None
        self._note_failure(what, None)
        return answer

    async def _energy(self, site: SiteInfo, data: dict[str, dict[str, Any]], slow: bool) -> None:
        """The energy tick's readings of one site."""
        sid = site.site_id
        now_ms = self.now_ms()
        if not site.inverter_serials and now_ms - self._equipment_checked.get(sid, 0) >= EQUIPMENT_RECHECK_MS:
            # A site without inverters in its equipment list is asked again hourly, not every tick.
            self._equipment_checked[sid] = now_ms
            equipment = await self._optional(f"equipment for site {sid}", self.client.get_site_equipment(sid))
            site.inverter_serials = mapping.equipment_serials(equipment)
        serials = site.inverter_serials

        overview, information, battery_answer = await asyncio.gather(
            self._optional(f"energy overview for site {sid}", self.client.get_site_energy_overview(sid)),
            self._optional(f"inverters for site {sid}", self.client.get_inverters_information(serials)) if serials else _none(),
            self._optional(f"batteries for site {sid}", self.client.get_site_batteries(sid)) if site.has_battery else _none(),
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

        if batteries := mapping.batteries_of(battery_answer):
            self._merge(data, site, KIND_BATTERY, self._battery_energy(sid, batteries))
        self._merge(data, site, KIND_BATTERY, mapping.battery_settings(battery_answer))

        if site.has_meter:
            task = self._meter_tasks.get(sid)
            if task is None or task.done():
                self._meter_tasks[sid] = self.config_entry.async_create_background_task(
                    self.hass, self._meter_totals(site), f"{DOMAIN} meter totals {sid}"
                )

        if not slow:
            return
        asleep = mapping.is_asleep(live, now_ms, self.timing.cadence_ms) if live else False
        today = datetime.fromtimestamp(now_ms / 1000, tz=_zone(site.timezone)).date().isoformat()
        rids = list(site.ev_chargers)
        communication, alerts, by_inverter, mode, *histories = await asyncio.gather(
            self._optional(f"communication for site {sid}", self.client.get_site_communication(sid)),
            self._optional(f"alerts for site {sid}", self.client.get_site_alerts(sid)),
            self._optional(f"energy by inverter for site {sid}", self.client.get_site_energy_by_inverter(sid, today, serials)) if serials else _none(),
            # The mode only changes when someone changes it: the slow tick is enough.
            self._optional(f"battery mode for site {sid}", self.client.get_site_battery_mode(sid)) if site.has_battery else _none(),
            *(self._optional(f"charging history of {rid} for site {sid}", self.client.get_appliance_sessions_history(sid, rid)) for rid in rids),
        )
        self._merge(data, site, KIND_SITE, mapping.site_values(communication, alerts, asleep))
        if by_inverter is not None:
            # None before sunrise: cleared rather than yesterday's (CLEARABLE).
            self._merge(
                data, site, KIND_INVERTER,
                {"optimizer_temperature_max": mapping.max_optimizer_temperature(by_inverter)},
            )
        self._merge(data, site, KIND_BATTERY, mapping.battery_mode(mode))
        for rid, history in zip(rids, histories, strict=True):
            charged = mapping.ev_charged_wh(history)
            if charged is not None:
                # Old sessions leave the rolling year: kept rising.
                total = self._monotonic(f"{sid}:ev:{rid}:charged", charged / 1000)
                self._merge(data, site, KIND_EV, {"energy_charged": total}, rid)

    def _battery_flow(self, site_id: str, power: float) -> dict[str, float]:
        """Charged and discharged kWh so far, integrated from the power samples.

        Kept in the store, which is written on energy ticks (and on unload), not
        on every poll.
        """
        now = self.now_ms()
        flow = self._stored.setdefault("battery_flow", {}).setdefault(site_id, {"charged": 0.0, "discharged": 0.0})
        last = self._battery_sample.get(site_id)
        self._battery_sample[site_id] = (now, power)
        gap = max(MAX_SAMPLE_GAP_MS, 3 * self.poll_interval * 1000)
        if last is not None and 0 < now - last[0] <= gap:
            charged, discharged = mapping.battery_flow_kwh(last[1], power, (now - last[0]) / 1000)
            if charged or discharged:
                flow["charged"] += charged
                flow["discharged"] += discharged
                self._flow_changed = True
        return {"energy_charged": round(flow["charged"], 3), "energy_discharged": round(flow["discharged"], 3)}

    def _battery_energy(self, site_id: str, batteries: list[dict[str, Any]]) -> dict[str, Any]:
        # The unit is kept from the last answer with a nameplate: a guess that
        # flipped between answers would change the counters 1000-fold.
        unit = mapping.battery_unit(batteries)
        if unit is not None and self._stored["units"].get(site_id) != unit:
            self._stored["units"][site_id] = unit
            self._dirty = True
        to_kwh = self._stored["units"].get(site_id, 0.001)
        return mapping.battery_energy(batteries, to_kwh)

    async def _meter_totals(self, site: SiteInfo) -> None:
        """The meter's yearly totals, outside the poll; one run per site at a time."""
        sid = site.site_id
        now = dt_util.utcnow()
        cache = self._stored["years"].setdefault(sid, {})
        before = dict(cache)
        current_year = _year_in(now, site.timezone)
        try:
            totals = await async_meter_totals(
                self.client,
                sid,
                current_year=current_year,
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
            if cache != before:
                self._dirty = True
            self._save()
        values: dict[str, Any] = {}
        for key in ("imported", "exported"):
            # Neither falls: a short or empty answer is a hiccup, not a reset
            # (total_increasing would count the way back up twice). The year's
            # counter starts again with each new year.
            values[f"energy_{key}"] = self._never_less(f"{sid}:meter:{key}", totals[key] / 1000)
            values[f"energy_{key}_year"] = self._never_less(
                f"{sid}:meter:{key}_year", totals[f"{key}_year"] / 1000, period=current_year
            )
        self._save()
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
    ) -> None:
        """Merge new values; None keeps the old value unless the key is CLEARABLE."""
        target = data.setdefault(device_key(site.site_id, kind, reporter_id), {})
        for key, value in values.items():
            if value is not None or key in mapping.CLEARABLE:
                target[key] = value

    def _never_less(self, name: str, reading: float, period: int | None = None) -> float:
        """The reading, or the last one when it is lower; a new period starts over."""
        counters = self._stored["counters"]
        stored = counters.get(name, {})
        last = stored.get("last") if stored.get("period") == period else None
        if isinstance(last, (int, float)) and reading < last:
            _LOGGER.debug("%s fell from %s to %s; kept", name, last, reading)
            return last
        value = round(reading, 3)
        if value != last:
            counters[name] = {"last": value} if period is None else {"last": value, "period": period}
            self._dirty = True
        return value

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
                self._dirty = True
                return round(last + offset, 3)
            offset += last - reading
            counter["offset"] = offset
            self._dirty = True
            _LOGGER.debug("%s fell from %s to %s; offset now %s", name, last, reading, offset)
        if counter.get("falling") or counter.get("last") != reading:
            self._dirty = True
        counter["falling"] = False
        counter["last"] = reading
        return round(reading + offset, 3)

    # ------------------------------------------------------------ commands

    def reserve_duration(self, site_id: str) -> str:
        """The duration a new backup reserve gets (a RESERVE_DURATIONS key); unlimited until chosen."""
        chosen = self._stored.get("reserve_duration", {}).get(site_id)
        return chosen if chosen in mapping.RESERVE_DURATIONS else "unlimited"

    def set_reserve_duration(self, site_id: str, duration: str) -> None:
        """Remember the duration for the next backup reserve (kept across restarts)."""
        self._stored.setdefault("reserve_duration", {})[site_id] = duration
        self._store.async_delay_save(lambda: self._stored, 10)
        self.async_update_listeners()

    async def async_reset_backup_reserve(self, site_id: str) -> None:
        """Drop the reserve set from the app or here, back to the base (installer's) one.

        SolarEdge does this for a duration of 0 hours (seen on a real site); the
        percentage sent with it doesn't matter, so the current one goes. The base
        reserve isn't known until SolarEdge reports it, about 20 s later.
        """
        current = (self.data or {}).get(device_key(site_id, KIND_BATTERY), {}).get("backup_reserve")
        await self.async_set_backup_reserve(site_id, round(current) if isinstance(current, (int, float)) else 0, hours=0)

    async def async_set_backup_reserve(self, site_id: str, percent: int, hours: int | None = None) -> None:
        """Set the backup reserve for the chosen duration (or `hours`), and show it right away.

        SolarEdge's battery answer catches up about 20 s later (seen on a real
        site), so an energy tick in between would show the old reserve again
        for 5 minutes: the batteries are read once more RESERVE_RECHECK_S later.
        """
        site = self.sites[site_id]
        values = (self.data or {}).get(device_key(site_id, KIND_BATTERY), {})
        if values.get("backup_reserve_editable") is False:
            raise HomeAssistantError("SolarEdge doesn't let this site change its backup reserve")
        if hours is None:
            hours = mapping.RESERVE_DURATIONS[self.reserve_duration(site_id)]
        try:
            await self.client.set_backup_reserve(site_id, percent, hours)
        except SolarEdgeAuthError as err:
            self.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError("SolarEdge rejected the login") from err
        except SolarEdgeError as err:
            raise HomeAssistantError(f"SolarEdge did not accept the backup reserve: {err}") from err
        expires = datetime.fromtimestamp(self.now_ms() / 1000 + hours * 3600, tz=UTC) if hours > 0 else None
        if self.data is not None and hours != 0:
            # In place, without async_set_updated_data: that would restart the poll timer
            # and mark a failing coordinator as working.
            self._merge(self.data, site, KIND_BATTERY, {"backup_reserve": float(percent), "backup_reserve_expires": expires})
            self.async_update_listeners()
        if unsub := self._unsub_reserve.pop(site_id, None):
            unsub()
        self._unsub_reserve[site_id] = async_call_later(
            self.hass, RESERVE_RECHECK_S, partial(self._reserve_recheck, site_id)
        )

    @callback
    def _reserve_recheck(self, site_id: str, _now: datetime) -> None:
        self._unsub_reserve.pop(site_id, None)
        self.config_entry.async_create_background_task(
            self.hass, self._async_reserve_recheck(site_id), f"{DOMAIN} backup reserve {site_id}"
        )

    async def _async_reserve_recheck(self, site_id: str) -> None:
        """SolarEdge's battery settings after a reserve was set (one extra request)."""
        site = self.sites.get(site_id)
        if site is None or self.outages or self.auth_failed:
            return
        answer = await self._optional(f"batteries for site {site_id}", self.client.get_site_batteries(site_id))
        if answer is not None and self.data is not None:
            self._merge(self.data, site, KIND_BATTERY, mapping.battery_settings(answer))
            self.async_update_listeners()

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

    async def raw_answers(self) -> dict[str, Any]:
        """SolarEdge's own answers for the diagnostics download, redacted.

        Each one is asked for once, outside the poll, all sites at once; a
        failure is reported in place of the answer. While polling is paused
        (outage, rejected login, login held back) nothing is asked: a
        download must not add to what SolarEdge is already refusing.
        """
        if self.outages or self.auth_failed or self.client.login_blocked_for:
            return {"skipped": "polling is paused; no requests sent"}
        requests: dict[tuple[str, str], Any] = {}
        for sid, site in self.sites.items():
            requests[sid, "powerflow"] = self.client.get_site_powerflow(sid)
            requests[sid, "devices"] = self.client.get_site_devices(sid)
            if site.has_battery:
                requests[sid, "batteries"] = self.client.get_site_batteries(sid)
                requests[sid, "battery_mode"] = self.client.get_site_battery_mode(sid)
            if site.inverter_serials:
                requests[sid, "inverters"] = self.client.get_inverters_information(site.inverter_serials)
        answers = await asyncio.gather(*requests.values(), return_exceptions=True)
        out: dict[str, dict[str, Any]] = {sid: {} for sid in self.sites}
        for ((sid, kind), _), answer in zip(requests.items(), answers, strict=True):
            if isinstance(answer, BaseException):
                if not isinstance(answer, SolarEdgeError):
                    raise answer
                out[sid][kind] = {"error": str(answer)}
                continue
            out[sid][kind] = {"answer": mapping.redact_raw(answer, self.sites[sid].inverter_serials)}
            if why := mapping.stale_reason(kind, answer, self.now_ms()):
                out[sid][kind]["looks_stale"] = why
        return out

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
