"""Client for the private mySolarEdge app API on monitoring.solaredge.com.

Port of lib/SolarEdgeCloudClient.js from com.solaredge v2.0.0 for Homey,
Copyright 2026 Robin de Gruijter, GPL-3.0.

This is not SolarEdge's public Monitoring API (monitoringapi.solaredge.com with
an API key). It logs in with the mySolarEdge e-mail and password and keeps the
session cookies, like the mySolarEdge phone app. One client per account: one
session for all devices, and identical GETs in flight at the same time go out
once.

The client never logs the e-mail address or the password.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urlencode

import aiohttp

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://monitoring.solaredge.com"
LOGIN_URL = f"{BASE_URL}/solaredge-apigw/api/login"
HOME_AUTOMATION_URL = f"{BASE_URL}/services/m/api/homeautomation/v1.0"
DASHBOARD_URL = f"{BASE_URL}/services/dashboard"
LAYOUT_URL = f"{BASE_URL}/services/layout"

# The mySolarEdge iOS app's headers.
HEADERS = {
    "User-Agent": "SolarEdge/14 CFNetwork/1568.200.51 Darwin/24.1.0",
    "CLIENT-VERSION": "3.12",
    "Accept": "application/json",
}

# Seconds per request.
REQUEST_TIMEOUT = 20

# After rejected credentials no automatic login for this long (seconds),
# doubling per rejection. An explicit login() (config or reauth flow) is not
# held back.
LOGIN_RETRY_MIN = 60
LOGIN_RETRY_MAX = 60 * 60

# An endpoint that answers 401 right after a fresh login is refused for this
# account, not logged out: for this long (seconds) its 401s no longer cost the
# session and a new login.
DENIED_FOR = 60 * 60

# Error codes and messages that mean "no data for this period".
NO_DATA_CODES = ("INVALID_ARGUMENTS", "empty_summary")


class SolarEdgeError(Exception):
    """A failed SolarEdge request."""

    def __init__(
        self, message: str, *, status: int | None = None, code: str | None = None
    ) -> None:
        """Keep the HTTP status and SolarEdge error code."""
        super().__init__(message)
        self.status = status
        self.code = code


class SolarEdgeAuthError(SolarEdgeError):
    """SolarEdge rejected the e-mail address or password."""


class SolarEdgeConnectionError(SolarEdgeError):
    """SolarEdge or the network failed: a timeout, no connection or HTTP 5xx."""


class SolarEdgeRateLimitError(SolarEdgeConnectionError):
    """SolarEdge answered HTTP 429."""


class SolarEdgeApiError(SolarEdgeError):
    """SolarEdge answered, but not with the data asked for."""


def is_no_data(err: Exception) -> bool:
    """Whether an error means the period has no data (a year before the site)."""
    if not isinstance(err, SolarEdgeApiError):
        return False
    text = f"{err.code or ''} {err}"
    return any(code in text for code in NO_DATA_CODES)


def _looks_like_login_page(text: str) -> bool:
    """An HTML answer to a JSON endpoint: the login page after a lost session."""
    head = text.lstrip()[:512].lower()
    return "j_password" in text or head.startswith("<!doctype html") or head.startswith("<html")


def cookies_from_headers(values: Iterable[str]) -> list[str]:
    """name=value of each Set-Cookie header (attributes such as Path dropped)."""
    cookies: list[str] = []
    for value in values:
        pair = value.split(";", 1)[0].strip()
        if "=" in pair and not pair.startswith("="):
            cookies.append(pair)
    return cookies


class SolarEdgeCloudClient:
    """One mySolarEdge account."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Keep the credentials; the session should have no cookie jar of its own."""
        self._session = session
        self._username = username
        self._password = password
        self._clock = clock
        self._cookies: list[str] = []
        self._login_task: asyncio.Task[None] | None = None
        self._login_retry = 0.0
        self._login_blocked_until = 0.0
        self._in_flight: dict[str, asyncio.Future[Any]] = {}
        # Endpoint path -> clock time until which its 401s are taken as refusals.
        self._denied: dict[str, float] = {}

    @property
    def logged_in(self) -> bool:
        """Whether there is a session."""
        return bool(self._cookies)

    @property
    def login_blocked_for(self) -> float:
        """Seconds before an automatic login is tried again after a rejection."""
        return max(0.0, self._login_blocked_until - self._clock())

    def _timeout(self) -> aiohttp.ClientTimeout:
        return aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)

    async def login(self) -> None:
        """Log in and keep the session cookies.

        A good login answers 302 (a redirect to the portal) with the session
        cookies. A server error or rate limit is an outage, not a rejection.
        """
        try:
            async with self._session.post(
                LOGIN_URL,
                data={"j_username": self._username, "j_password": self._password},
                headers={"User-Agent": HEADERS["User-Agent"]},
                allow_redirects=False,
                timeout=self._timeout(),
            ) as resp:
                status = resp.status
                set_cookies = resp.headers.getall("Set-Cookie", [])
                location = resp.headers.get("Location", "")
                await resp.read()
        except (TimeoutError, aiohttp.ClientError) as err:
            raise SolarEdgeConnectionError(
                f"Login failed: {type(err).__name__}"
            ) from err

        if status == 429:
            raise SolarEdgeRateLimitError("Login rate limited", status=status)
        if status >= 500:
            raise SolarEdgeConnectionError(f"Login failed: HTTP {status}", status=status)

        cookies = cookies_from_headers(set_cookies)
        # A failed form login may also redirect, back to the login page with an error.
        if not 300 <= status < 400 or not cookies or "error" in location.lower():
            self._login_retry = min(
                self._login_retry * 2 or LOGIN_RETRY_MIN, LOGIN_RETRY_MAX
            )
            self._login_blocked_until = self._clock() + self._login_retry
            raise SolarEdgeAuthError(
                "Invalid e-mail and/or password", status=status, code="INVALID_CREDENTIALS"
            )

        self._login_retry = 0
        self._login_blocked_until = 0
        self._cookies = cookies
        _LOGGER.debug("Logged in to SolarEdge (%d cookies)", len(cookies))

    async def ensure_logged_in(self) -> None:
        """Log in when there is no session; concurrent callers share one login.

        After rejected credentials this raises without a login until the retry
        delay has passed.
        """
        if self._cookies:
            return
        if self._clock() < self._login_blocked_until:
            raise SolarEdgeAuthError(
                "Invalid e-mail and/or password", code="INVALID_CREDENTIALS"
            )
        if self._login_task is None:
            self._login_task = asyncio.ensure_future(self.login())
            self._login_task.add_done_callback(self._login_done)
        await asyncio.shield(self._login_task)

    def _login_done(self, task: asyncio.Task[None]) -> None:
        self._login_task = None
        if not task.cancelled():
            task.exception()  # retrieved by the waiters; marked handled here

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: list[tuple[str, str]] | None = None,
        json_body: Any = None,
    ) -> Any:
        """One API call with the session; identical GETs in flight are merged."""
        if method != "GET":
            return await self._request(method, url, params, json_body)
        key = f"{url}?{urlencode(params or [])}"
        future = self._in_flight.get(key)
        if future is None:
            future = asyncio.ensure_future(self._request(method, url, params, None))
            self._in_flight[key] = future

            def done(task: asyncio.Future[Any]) -> None:
                self._in_flight.pop(key, None)
                if not task.cancelled():
                    task.exception()

            future.add_done_callback(done)
        return await asyncio.shield(future)

    async def _request(
        self,
        method: str,
        url: str,
        params: list[tuple[str, str]] | None,
        json_body: Any,
        retry: bool = False,
    ) -> Any:
        await self.ensure_logged_in()
        cookies = self._cookies
        headers = {**HEADERS, "Cookie": "; ".join(cookies)}
        try:
            async with self._session.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=headers,
                allow_redirects=False,
                timeout=self._timeout(),
            ) as resp:
                status = resp.status
                reason = resp.reason or ""
                text = await resp.text()
        except (TimeoutError, aiohttp.ClientError) as err:
            raise SolarEdgeConnectionError(
                f"{_path(url)}: {type(err).__name__}"
            ) from err

        # An expired session answers 401, or redirects to the login page.
        path = _path(url)
        if status == 401 or 300 <= status < 400 or (
            status == 200 and _looks_like_login_page(text)
        ):
            refused = retry or self._clock() < self._denied.get(path, 0)
            if refused:
                # Still refused with a fresh session: this endpoint is not open to
                # the account. Its next 401s keep the session (no login per poll).
                if retry:
                    _LOGGER.debug("%s: refused after a new login; not logging in for it again for a while", path)
                    self._denied[path] = self._clock() + DENIED_FOR
                raise SolarEdgeApiError(f"{path}: unauthorized", status=status, code="UNAUTHORIZED")
            # Only this session expired: a login since this request went out stays.
            if self._cookies is cookies:
                self._cookies = []
            _LOGGER.debug("%s: session expired, logging in again", path)
            return await self._request(method, url, params, json_body, retry=True)

        if status == 429:
            raise SolarEdgeRateLimitError(f"{_path(url)}: rate limited", status=status)
        if status >= 500:
            raise SolarEdgeConnectionError(f"{_path(url)}: HTTP {status}", status=status)

        body: Any = None
        if text.strip():
            try:
                body = json.loads(text)
            except ValueError:
                body = None

        if status >= 400:
            # The error code (e.g. INVALID_ARGUMENTS) comes as { error: { code } };
            # a top-level code is kept as fallback.
            code = None
            if isinstance(body, dict):
                error = body.get("error")
                code = error.get("code") if isinstance(error, dict) else None
                code = code or body.get("code")
            raise SolarEdgeApiError(
                f"{_path(url)}: {code or reason or f'HTTP {status}'}",
                status=status,
                code=code,
            )
        if body is None and text.strip():
            raise SolarEdgeApiError(f"{_path(url)}: not JSON", status=status)
        self._denied.pop(path, None)
        return body

    # ------------------------------------------------------------- endpoints

    async def get_sites(self) -> list[dict[str, Any]]:
        """The account's sites; SolarEdge answers { sites: { id: site } }."""
        result = await self.request("GET", f"{BASE_URL}/services/m/so/sites/")
        sites = result.get("sites", result) if isinstance(result, dict) else result
        if isinstance(sites, dict):
            sites = list(sites.values())
        return [site for site in sites or [] if isinstance(site, dict) and "id" in site]

    async def get_site_powerflow(self, site_id: str) -> dict[str, Any]:
        """Real-time power flow, in kW."""
        return await self.request(
            "GET",
            f"{BASE_URL}/services/m/so/dashboard/v2/site/{site_id}/powerflow/latest",
            params=[("components", "consumption,grid,storage,ev-charger")],
        )

    async def get_site_energy_overview(self, site_id: str) -> dict[str, Any]:
        """Production per period (lifetime, year, month, day), in Wh."""
        return await self.request(
            "GET", f"{BASE_URL}/services/m/so/dashboard/site/{site_id}/energyOverview"
        )

    async def get_site_measurements(self, site_id: str, end_date: str) -> dict[str, Any]:
        """Import/export (summary, Wh) for the year ending on end_date.

        A year with no data raises INVALID_ARGUMENTS or answers summary: null.
        """
        return await self.request(
            "GET",
            f"{BASE_URL}/services/m/so/dashboard/site/{site_id}/measurements",
            params=[
                ("period", "YEAR"),
                ("measurement-unit", "WATT_HOUR"),
                ("end-date", end_date),
            ],
        )

    async def get_site_devices(self, site_id: str) -> dict[str, Any]:
        """Smart-home devices: EV chargers and load controllers."""
        return await self.request("GET", f"{HOME_AUTOMATION_URL}/sites/{site_id}/devices")

    async def get_appliance_sessions_history(
        self, site_id: str, reporter_id: str
    ) -> dict[str, Any]:
        """An EV charger's sessions of the last year."""
        return await self.request(
            "GET",
            f"{HOME_AUTOMATION_URL}/{site_id}/devices/{reporter_id}/applianceSessionsHistory",
            params=[
                ("timeRange", "YEAR"),
                ("endTime", "9999999999999"),
                ("startTime", "0"),
            ],
        )

    async def set_activation_state(
        self, site_id: str, reporter_id: str, state: dict[str, Any]
    ) -> Any:
        """Switch a smart-home device: { mode: MANUAL, level } or { mode: AUTO }."""
        return await self.request(
            "PUT",
            f"{HOME_AUTOMATION_URL}/{site_id}/devices/{reporter_id}/activationState",
            json_body=state,
        )

    async def get_site_batteries(self, site_id: str) -> dict[str, Any]:
        """Batteries under devicesByType.BATTERY."""
        return await self.request(
            "GET",
            f"{HOME_AUTOMATION_URL}/storage/{site_id}/getBatteries",
            params=[("triggerHF", "false")],
        )

    async def get_site_equipment(self, site_id: str) -> dict[str, Any]:
        """Models and serials per kind: inverters, optimizers, storage, meters."""
        return await self.request(
            "GET", f"{DASHBOARD_URL}/v2/site-details/{site_id}/equipment"
        )

    async def get_inverters_information(self, serials: list[str]) -> dict[str, Any]:
        """Live readings and firmware per inverter."""
        return await self.request(
            "GET",
            f"{LAYOUT_URL}/information/inverters",
            params=[("inverter-serials", serial) for serial in serials],
        )

    async def get_site_energy_by_inverter(
        self, site_id: str, date: str, serials: list[str]
    ) -> dict[str, Any]:
        """Energy and the highest temperature per optimizer on one day."""
        return await self.request(
            "GET",
            f"{LAYOUT_URL}/energy/site/{site_id}/by-inverter",
            params=[
                ("start-date", date),
                ("end-date", date),
                *[("inverter-serials", serial) for serial in serials],
                ("include-max-temperature", "true"),
                ("include-color", "true"),
            ],
        )

    async def get_site_communication(self, site_id: str) -> dict[str, Any]:
        """connectivityStatus (e.g. ONLINE) and active."""
        return await self.request(
            "GET", f"{DASHBOARD_URL}/site-details/{site_id}/communication"
        )

    async def get_site_alerts(self, site_id: str) -> dict[str, Any]:
        """totalAlertsCount and topAlerts."""
        return await self.request("GET", f"{DASHBOARD_URL}/alerts/sites/{site_id}")


def _path(url: str) -> str:
    """The URL without host, for messages."""
    return url.removeprefix(BASE_URL)
