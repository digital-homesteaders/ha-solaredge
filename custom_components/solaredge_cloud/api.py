"""Client for the private mySolarEdge app API on monitoring.solaredge.com.

Port of lib/SolarEdgeCloudClient.js from com.solaredge for Homey (v2.0.0, and
the OAuth login of v2.1.1), Copyright 2026 Robin de Gruijter, GPL-3.0.

This is not SolarEdge's public Monitoring API (monitoringapi.solaredge.com with
an API key). It logs in with the mySolarEdge e-mail and password through the web
portal's OAuth2 PKCE login on login.solaredge.com, and sends the access token
on every API call. One client per account: one token for all devices, and
identical GETs in flight at the same time go out once.

The client never logs the e-mail address, the password or a token.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import logging
import re
import secrets
import time
from collections.abc import Callable, Iterable
from typing import Any, NoReturn
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import aiohttp

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://monitoring.solaredge.com"
HOME_AUTOMATION_URL = f"{BASE_URL}/services/m/api/homeautomation/v1.0"
DASHBOARD_URL = f"{BASE_URL}/services/dashboard"
LAYOUT_URL = f"{BASE_URL}/services/layout"

# Login: the web portal's OAuth2 PKCE flow. The old form login
# (/solaredge-apigw/api/login) answers 410 Gone since 2026-10-21. The client id
# is the portal's public one, not a secret (com.solaredge v2.1.0).
AUTH_URL = "https://login.solaredge.com"
CLIENT_ID = "ugfnsujd3384sshcjehaphlh3"
REDIRECT_URI = f"{BASE_URL}/mfe/auth/callback"

# The mySolarEdge iOS app's headers.
HEADERS = {
    "User-Agent": "SolarEdge/14 CFNetwork/1568.200.51 Darwin/24.1.0",
    "CLIENT-VERSION": "3.12",
    "Accept": "application/json",
}

# Seconds per request.
REQUEST_TIMEOUT = 20

# After a login that wasn't accepted (a wrong password, a refusal, or SolarEdge
# failing during the login) no automatic login for this long (seconds),
# doubling per failure; reset once a token has answered. An explicit login()
# (config or reauth flow) is not held back.
LOGIN_RETRY_MIN = 60
LOGIN_RETRY_MAX = 60 * 60

# An access token is renewed this long (seconds) before it expires.
TOKEN_MARGIN = 60

# An endpoint that answers 401 right after a fresh token is refused for this
# account, not logged out: for this long (seconds) its 401s no longer cost a
# token renewal.
DENIED_FOR = 60 * 60

# Error codes and messages that mean "no data for this period".
NO_DATA_CODES = ("INVALID_ARGUMENTS", "empty_summary")

CSRF_RE = re.compile(r'name="csrf"\s+value="([^"]*)"')


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


class SolarEdgeLoginRefusedError(SolarEdgeError):
    """SolarEdge did not complete the login, but not for a wrong password.

    For example a second factor, or a login step that answers something the
    flow doesn't expect (such as 410 Gone).
    """


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
    """An HTML answer to a JSON endpoint: a login page instead of data."""
    head = text.lstrip()[:512].lower()
    return head.startswith("<!doctype html") or head.startswith("<html")


def cookies_from_headers(values: Iterable[str]) -> list[str]:
    """name=value of each Set-Cookie header (attributes such as Path dropped)."""
    cookies: list[str] = []
    for value in values:
        pair = value.split(";", 1)[0].strip()
        if "=" in pair and not pair.startswith("="):
            cookies.append(pair)
    return cookies


def pkce_challenge(verifier: str) -> str:
    """The S256 code challenge of a PKCE verifier (RFC 7636): base64url, no padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class _LoginStep:
    """One answer of a login step."""

    def __init__(self, status: int, location: str | None, text: str, body: Any) -> None:
        self.status = status
        self.location = location
        self.text = text
        self.body = body


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
        self._token: str | None = None
        self._token_expires = 0.0
        # Counts tokens, so a 401 only drops the token it was sent with.
        self._token_gen = 0
        self._refresh_token: str | None = None
        # Called with the new refresh token (None when dropped), to keep it.
        self.on_refresh_token: Callable[[str | None], None] | None = None
        self._login_task: asyncio.Task[None] | None = None
        self._login_retry = 0.0
        self._login_blocked_until = 0.0
        self._blocked_error: SolarEdgeError | None = None
        self._in_flight: dict[str, asyncio.Future[Any]] = {}
        # Endpoint path -> clock time until which its 401s are taken as refusals.
        self._denied: dict[str, float] = {}

    @property
    def logged_in(self) -> bool:
        """Whether there is an access token that hasn't expired."""
        return bool(self._token) and self._clock() < self._token_expires

    @property
    def login_blocked_for(self) -> float:
        """Seconds before an automatic login is tried again after a failed one."""
        return max(0.0, self._login_blocked_until - self._clock())

    def set_refresh_token(self, token: str | None) -> None:
        """A refresh token kept from an earlier run: renewal without the password."""
        self._refresh_token = token

    def _timeout(self) -> aiohttp.ClientTimeout:
        return aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)

    # ------------------------------------------------------------- login

    async def login(self) -> None:
        """Log in with the password: authorize, login form, form post, code for tokens.

        Raises SolarEdgeAuthError for a wrong password, SolarEdgeLoginRefusedError
        when SolarEdge doesn't complete the login for another reason, and
        SolarEdgeConnectionError when SolarEdge or the network fails. A server
        error or rate limit during the login also holds automatic logins back:
        a locked account answered 500, then 401 (com.solaredge, 2026-10-16), and
        logging in on keeps it locked.
        """
        verifier = secrets.token_urlsafe(48)
        jar: dict[str, str] = {}

        # 1. The authorize endpoint redirects to the login form.
        step = await self._login_step(
            "GET",
            f"{AUTH_URL}/oauth2/authorize",
            jar,
            params={
                "client_id": CLIENT_ID,
                "response_type": "code",
                "redirect_uri": REDIRECT_URI,
                "code_challenge": pkce_challenge(verifier),
                "code_challenge_method": "S256",
            },
        )
        if not step.location:
            self._refuse(f"the login start answered HTTP {step.status} without a redirect")
        form_url = urljoin(f"{AUTH_URL}/", step.location)

        # 2. The login form, for its csrf value.
        step = await self._login_step("GET", form_url, jar)
        match = CSRF_RE.search(step.text) if step.status == 200 else None
        if match is None:
            self._refuse(f"the login page answered HTTP {step.status} without its form")
        csrf = html.unescape(match.group(1))

        # 3. The credentials, to the form's own address; it redirects to the
        # portal's callback with the code.
        step = await self._login_step(
            "POST",
            form_url,
            jar,
            data={"csrf": csrf, "username": self._username, "password": self._password, "cognitoAsfData": ""},
        )
        target = urljoin(form_url, step.location) if step.location else None
        code = parse_qs(urlparse(target).query).get("code", [None])[0] if target else None
        if not code:
            # The form again (or a redirect back to it) is a wrong password;
            # anything else (a second factor, a retired endpoint) is no wrong password.
            if target is not None and urlparse(target).path == urlparse(form_url).path or (
                target is None and step.status == 200
            ):
                self._hold(SolarEdgeAuthError(
                    "Invalid e-mail and/or password", status=step.status, code="INVALID_CREDENTIALS"
                ))
            self._refuse(f"the login answered HTTP {step.status}")

        # 4. The code for the tokens.
        await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
            },
            refresh=False,
        )
        _LOGGER.debug("Logged in to SolarEdge")

    async def _login_step(
        self,
        method: str,
        url: str,
        jar: dict[str, str],
        *,
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
    ) -> _LoginStep:
        """One login request without following redirects; keeps its cookies in jar."""
        headers = {"Accept": "text/html,application/json"}
        if jar:
            headers["Cookie"] = "; ".join(f"{name}={value}" for name, value in jar.items())
        try:
            async with self._session.request(
                method,
                url,
                params=params,
                data=data,
                headers=headers,
                allow_redirects=False,
                timeout=self._timeout(),
            ) as resp:
                status = resp.status
                location = resp.headers.get("Location")
                for cookie in cookies_from_headers(resp.headers.getall("Set-Cookie", [])):
                    name, value = cookie.split("=", 1)
                    jar[name] = value
                text = await resp.text()
        except (TimeoutError, aiohttp.ClientError) as err:
            raise SolarEdgeConnectionError(f"Login failed: {type(err).__name__}") from err
        if status == 429:
            self._hold(SolarEdgeRateLimitError("Login rate limited", status=status))
        if status >= 500:
            self._hold(SolarEdgeConnectionError(f"Login failed: HTTP {status}", status=status))
        body = None
        if text.strip():
            try:
                body = json.loads(text)
            except ValueError:
                body = None
        return _LoginStep(status, location, text, body)

    async def _token_request(self, grant: dict[str, str], *, refresh: bool) -> None:
        """POST /oauth2/token and keep the tokens."""
        try:
            async with self._session.post(
                f"{AUTH_URL}/oauth2/token",
                data={"client_id": CLIENT_ID, **grant},
                headers={"Accept": "application/json"},
                allow_redirects=False,
                timeout=self._timeout(),
            ) as resp:
                status = resp.status
                text = await resp.text()
        except (TimeoutError, aiohttp.ClientError) as err:
            raise SolarEdgeConnectionError(f"Token request failed: {type(err).__name__}") from err
        if status == 429:
            raise SolarEdgeRateLimitError("Token request rate limited", status=status)
        if status >= 500:
            raise SolarEdgeConnectionError(f"Token request failed: HTTP {status}", status=status)
        try:
            body = json.loads(text) if text.strip() else None
        except ValueError:
            body = None
        token = body.get("access_token") if isinstance(body, dict) else None
        if status != 200 or not isinstance(token, str) or not token:
            error = body.get("error") if isinstance(body, dict) else None
            reason = f"the token request answered HTTP {status}" + (f" ({error})" if error else "")
            if refresh:
                raise SolarEdgeLoginRefusedError(reason, status=status)
            self._refuse(reason)
        assert isinstance(body, dict)
        expires_in = body.get("expires_in")
        lifetime = expires_in if isinstance(expires_in, (int, float)) and expires_in > 0 else 3600
        self._token = token
        self._token_gen += 1
        self._token_expires = self._clock() + max(lifetime - TOKEN_MARGIN, 30)
        # A refresh answers without a new refresh token: the old one stays.
        new_refresh = body.get("refresh_token")
        if isinstance(new_refresh, str) and new_refresh and new_refresh != self._refresh_token:
            self._refresh_token = new_refresh
            if self.on_refresh_token:
                self.on_refresh_token(new_refresh)

    def _hold(self, error: SolarEdgeError) -> NoReturn:
        """No automatic login for a while (doubling per failure); raise the error."""
        self._login_retry = min(self._login_retry * 2 or LOGIN_RETRY_MIN, LOGIN_RETRY_MAX)
        self._login_blocked_until = self._clock() + self._login_retry
        self._blocked_error = error
        self._token = None
        raise error

    def _refuse(self, why: str) -> NoReturn:
        """A login answer that is no wrong password, but no login either."""
        self._hold(SolarEdgeLoginRefusedError(f"SolarEdge refused the login: {why}"))

    def _drop_refresh_token(self) -> None:
        if self._refresh_token is not None:
            self._refresh_token = None
            if self.on_refresh_token:
                self.on_refresh_token(None)

    async def _renew(self) -> None:
        """A new access token: from the refresh token when there is one, else a login."""
        if self._refresh_token:
            try:
                await self._token_request(
                    {"grant_type": "refresh_token", "refresh_token": self._refresh_token}, refresh=True
                )
                _LOGGER.debug("SolarEdge access token renewed")
                return
            except SolarEdgeLoginRefusedError as err:
                # The refresh token is no longer accepted: log in with the password.
                _LOGGER.debug("Token renewal not accepted (%s); logging in again", err)
                self._drop_refresh_token()
        await self.login()

    async def ensure_logged_in(self) -> None:
        """Renew or log in when there is no valid token; concurrent callers share one.

        After a login that wasn't accepted this raises without a login until the
        retry delay has passed.
        """
        if self.logged_in:
            return
        if self._clock() < self._login_blocked_until:
            error = self._blocked_error or SolarEdgeLoginRefusedError("Login held back")
            raise type(error)(str(error), status=error.status, code=error.code)
        if self._login_task is None:
            self._login_task = asyncio.ensure_future(self._renew())
            self._login_task.add_done_callback(self._login_done)
        await asyncio.shield(self._login_task)

    def _login_done(self, task: asyncio.Task[None]) -> None:
        self._login_task = None
        if not task.cancelled():
            task.exception()  # retrieved by the waiters; marked handled here

    # ------------------------------------------------------------- requests

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: list[tuple[str, str]] | None = None,
        json_body: Any = None,
    ) -> Any:
        """One API call with the token; identical GETs in flight are merged."""
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
        gen = self._token_gen
        headers = {**HEADERS, "Authorization": f"Bearer {self._token}"}
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

        # An expired token answers 401 (a redirect to a login page counts the same).
        path = _path(url)
        if status == 401 or 300 <= status < 400 or (
            status == 200 and _looks_like_login_page(text)
        ):
            refused = retry or self._clock() < self._denied.get(path, 0)
            if refused:
                # Still refused with a fresh token: this endpoint is not open to
                # the account. Its next 401s keep the token (no renewal per poll).
                if retry:
                    _LOGGER.debug("%s: refused with a new token; not renewing for it again for a while", path)
                    self._denied[path] = self._clock() + DENIED_FOR
                raise SolarEdgeApiError(f"{path}: unauthorized", status=status, code="UNAUTHORIZED")
            # Only this token expired: a renewal since this request went out stays.
            if self._token_gen == gen:
                self._token = None
            _LOGGER.debug("%s: token not accepted, renewing", path)
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
        # The token has answered: the login worked, so failed logins start over.
        self._login_retry = 0
        self._login_blocked_until = 0
        self._blocked_error = None
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

    async def set_backup_reserve(self, site_id: str, percent: int, hours: int) -> None:
        """Set the backup reserve for `hours` hours; -1 is unlimited.

        Checked on a real site (2026-11-01): -1 holds with no end time, while 0
        drops the reserve set from the app and goes back to the base one (the
        installer's) a moment later. The field names come from the app's
        code. SolarEdge answers HTTP 200 even when it refuses: the body's status
        says PASSED or FAILED. Without the duration it answers FAILED ("Invalid
        parameter {} in request") but may still apply the value for 24 hours,
        so the duration is always sent.
        """
        answer = await self.request(
            "PUT",
            f"{HOME_AUTOMATION_URL}/storage/{site_id}/backupReserve",
            json_body={"backupReserve": percent, "backupReserveDurationInHours": hours},
        )
        if isinstance(answer, dict) and answer.get("status") not in (None, "PASSED"):
            messages = [m.get("message") for m in answer.get("errorMessages") or [] if isinstance(m, dict)]
            raise SolarEdgeApiError(
                f"backupReserve: {'; '.join(m for m in messages if m) or answer.get('status')}",
                status=answer.get("httpStatus") if isinstance(answer.get("httpStatus"), int) else None,
            )

    async def get_site_batteries(self, site_id: str) -> dict[str, Any]:
        """Batteries under devicesByType.BATTERY."""
        return await self.request(
            "GET",
            f"{HOME_AUTOMATION_URL}/storage/{site_id}/getBatteries",
            params=[("triggerHF", "false")],
        )

    async def get_site_battery_mode(self, site_id: str) -> dict[str, Any]:
        """The battery mode the app shows (batteryMode: MSC, MANUAL_TOU, ...) and its TOU schedule.

        Not in 2.0.0's polling. Read only: the matching PUT replaces the whole
        schedule and is not used.
        """
        return await self.request("GET", f"{HOME_AUTOMATION_URL}/storage/{site_id}/batteryMode")

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
