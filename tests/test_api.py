"""The client: OAuth login, token renewal, refusals vs outages, errors and parsing."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import parse_qs, urlparse

import aiohttp
import pytest
from multidict import CIMultiDict

from custom_components.solaredge_cloud.api import (
    DENIED_FOR,
    LOGIN_RETRY_MIN,
    REDIRECT_URI,
    SolarEdgeApiError,
    SolarEdgeAuthError,
    SolarEdgeCloudClient,
    SolarEdgeConnectionError,
    SolarEdgeLoginRefusedError,
    SolarEdgeRateLimitError,
    cookies_from_headers,
    is_no_data,
    pkce_challenge,
)

from .conftest import PASSWORD, USERNAME, load_fixture


class FakeResponse:
    """Enough of aiohttp's response."""

    def __init__(self, status: int = 200, body: Any = "", headers: list[tuple[str, str]] | None = None) -> None:
        self.status = status
        self._body = body if isinstance(body, str) else json.dumps(body)
        self.headers = CIMultiDict(headers or [])
        self.reason = "OK" if status < 400 else "Error"

    async def text(self) -> str:
        return self._body

    async def read(self) -> bytes:
        return self._body.encode()

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None


class FakeSession:
    """Answers each request from `routes`: (method, path suffix) -> answers in turn.

    An answer is (status, body, headers), an exception to raise, or a callable
    taking (url, kwargs) that returns one of those.
    """

    def __init__(self, routes: dict[tuple[str, str], list[Any]]) -> None:
        self.routes = {key: list(value) for key, value in routes.items()}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.request("POST", url, **kwargs)

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        path = urlparse(url).path
        for (route_method, suffix), answers in self.routes.items():
            if route_method == method and path.endswith(suffix):
                answer = answers.pop(0) if len(answers) > 1 else answers[0]
                if callable(answer) and not isinstance(answer, BaseException):
                    answer = answer(url, kwargs)
                if isinstance(answer, BaseException):
                    raise answer
                status, body, headers = answer
                return FakeResponse(status, body, headers)
        raise AssertionError(f"unexpected {method} {url}")

    def count(self, method: str, suffix: str) -> int:
        return sum(1 for m, url, _ in self.calls if m == method and urlparse(url).path.endswith(suffix))


class LoginServer:
    """A fake login.solaredge.com, after com.solaredge v2.1.1 and the Hubitat port's test server."""

    def __init__(self) -> None:
        self.challenge: str | None = None
        self.codes = 0
        self.tokens = 0
        self.refresh_tokens = {"RT1"}

    def authorize(self, url: str, kwargs: dict[str, Any]) -> Any:
        params = kwargs["params"]
        assert params["client_id"] and params["redirect_uri"] == REDIRECT_URI
        assert params["code_challenge_method"] == "S256"
        self.challenge = params["code_challenge"]
        return 302, "", [("Location", "/login?client_id=c&state=s1"), ("Set-Cookie", "XSRF-TOKEN=x1; Path=/; Secure")]

    def form(self, url: str, kwargs: dict[str, Any]) -> Any:
        assert "XSRF-TOKEN=x1" in kwargs["headers"]["Cookie"]
        return 200, '<form method="post"><input type="hidden" name="csrf" value="c&amp;1"></form>', [("Set-Cookie", "session=s2")]

    def post(self, url: str, kwargs: dict[str, Any]) -> Any:
        assert "XSRF-TOKEN=x1" in kwargs["headers"]["Cookie"] and "session=s2" in kwargs["headers"]["Cookie"]
        data = kwargs["data"]
        assert data["csrf"] == "c&1" and data["cognitoAsfData"] == ""
        if data["username"] == USERNAME and data["password"] == PASSWORD:
            self.codes += 1
            return 302, "", [("Location", f"{REDIRECT_URI}?code=CODE{self.codes}")]
        return 302, "", [("Location", "/login?client_id=c&state=s1&error=true")]

    def token(self, url: str, kwargs: dict[str, Any]) -> Any:
        data = kwargs["data"]
        if data["grant_type"] == "authorization_code":
            assert data["code"] == f"CODE{self.codes}" and data["redirect_uri"] == REDIRECT_URI
            assert pkce_challenge(data["code_verifier"]) == self.challenge
            self.tokens += 1
            return 200, {"access_token": f"AT{self.tokens}", "refresh_token": "RT1", "expires_in": 3600}, []
        assert data["grant_type"] == "refresh_token"
        if data["refresh_token"] in self.refresh_tokens:
            self.tokens += 1
            return 200, {"access_token": f"AT{self.tokens}", "expires_in": 3600}, []
        return 400, {"error": "invalid_grant"}, []

    def routes(self) -> dict[tuple[str, str], list[Any]]:
        return {
            ("GET", "/oauth2/authorize"): [self.authorize],
            ("GET", "/login"): [self.form],
            ("POST", "/login"): [self.post],
            ("POST", "/oauth2/token"): [self.token],
        }


class Clock:
    """A settable monotonic clock."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


SITES = ("GET", "/services/m/so/sites/")
POWERFLOW = ("GET", "/powerflow/latest")
AUTHORIZE = ("GET", "/oauth2/authorize")
TOKEN = ("POST", "/oauth2/token")


def capture(method: str) -> Any:
    return next(call["result"] for call in load_fixture("synthetic-site.json")["calls"] if call["method"] == method)


def client_for(
    routes: dict[tuple[str, str], list[Any]], clock: Clock | None = None, server: LoginServer | None = None
) -> tuple[SolarEdgeCloudClient, FakeSession, LoginServer]:
    server = server or LoginServer()
    session = FakeSession({**server.routes(), **routes})
    client = SolarEdgeCloudClient(session, USERNAME, PASSWORD, clock=clock or Clock())  # type: ignore[arg-type]
    return client, session, server


def logins(session: FakeSession) -> int:
    return session.count("POST", "/login")


def test_pkce_rfc7636_example() -> None:
    # RFC 7636, appendix B.
    assert pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_cookie_parsing() -> None:
    assert cookies_from_headers(["A=1; Path=/; Expires=Wed, 21 Oct 2026 07:28:00 GMT", "B=2", "junk"]) == ["A=1", "B=2"]


async def test_login_flow_and_bearer_token() -> None:
    kept: list[str | None] = []
    client, session, server = client_for({SITES: [(200, {"sites": capture("getSites")}, [])]})
    client.on_refresh_token = kept.append
    sites = await client.get_sites()
    assert [site["id"] for site in sites] == [9999999]
    # authorize, form, post, token, then the API call; no redirect is followed.
    assert [(m, urlparse(u).path) for m, u, _ in session.calls] == [
        ("GET", "/oauth2/authorize"), ("GET", "/login"), ("POST", "/login"), ("POST", "/oauth2/token"),
        ("GET", "/services/m/so/sites/"),
    ]
    assert all(kwargs["allow_redirects"] is False for _, _, kwargs in session.calls)
    api_headers = session.calls[-1][2]["headers"]
    assert api_headers["Authorization"] == "Bearer AT1"
    assert "Cookie" not in api_headers
    assert session.calls[-1][2]["timeout"].total == 20
    # The password only goes to the form post.
    assert [u for _, u, kwargs in session.calls if PASSWORD in json.dumps(kwargs, default=str)] == [session.calls[2][1]]
    assert kept == ["RT1"]
    assert client.logged_in


async def test_wrong_password_holds_automatic_logins_back() -> None:
    clock = Clock()
    client, session, server = client_for({SITES: [(200, {"sites": {}}, [])]}, clock)
    client._password = "wrong"  # noqa: SLF001
    with pytest.raises(SolarEdgeAuthError):
        await client.get_sites()
    for _ in range(5):
        with pytest.raises(SolarEdgeAuthError):
            await client.get_sites()
    assert logins(session) == 1
    assert client.login_blocked_for == LOGIN_RETRY_MIN
    clock.now += LOGIN_RETRY_MIN
    with pytest.raises(SolarEdgeAuthError):
        await client.get_sites()
    assert logins(session) == 2
    # Doubles per failure, until a token has answered.
    assert client.login_blocked_for == 2 * LOGIN_RETRY_MIN
    # An explicit login (config or reauth flow) is not held back, and a token
    # that answers resets the delay.
    client._password = PASSWORD  # noqa: SLF001
    await client.login()
    assert await client.get_sites() == []
    assert client.login_blocked_for == 0


@pytest.mark.parametrize(
    "routes",
    [
        # The old endpoints retired: the login start answers 410.
        {AUTHORIZE: [(410, "Gone", [])]},
        # A login page without its form.
        {("GET", "/login"): [(200, "<html>maintenance</html>", [])]},
        # A second factor: a redirect elsewhere, without a code.
        {("POST", "/login"): [(302, "", [("Location", "/mfa?state=s1")])]},
        # The code is not accepted.
        {TOKEN: [(400, {"error": "invalid_grant"}, [])]},
    ],
)
async def test_refused_login_is_no_wrong_password(routes: dict[tuple[str, str], list[Any]]) -> None:
    client, session, _ = client_for({**routes, SITES: [(200, {"sites": {}}, [])]})
    with pytest.raises(SolarEdgeLoginRefusedError):
        await client.get_sites()
    # Held back as well: no login per poll.
    with pytest.raises(SolarEdgeLoginRefusedError):
        await client.get_sites()
    assert session.count("GET", "/oauth2/authorize") == 1


@pytest.mark.parametrize(
    ("answer", "error", "held"),
    [
        ((503, "", []), SolarEdgeConnectionError, True),
        ((429, "", []), SolarEdgeRateLimitError, True),
        (aiohttp.ClientConnectionError("down"), SolarEdgeConnectionError, False),
        (TimeoutError(), SolarEdgeConnectionError, False),
    ],
)
async def test_login_outage(answer: Any, error: type[Exception], held: bool) -> None:
    client, session, _ = client_for({AUTHORIZE: [answer], SITES: [(200, {"sites": {}}, [])]})
    for _ in range(2):
        with pytest.raises(error) as info:
            await client.get_sites()
        # Never a wrong password or a refusal: the coordinator backs off.
        assert not isinstance(info.value, (SolarEdgeAuthError, SolarEdgeLoginRefusedError))
    # A server error or rate limit holds logins back (a locked account answers
    # 500); no answer at all doesn't.
    assert session.count("GET", "/oauth2/authorize") == (1 if held else 2)


async def test_token_renewed_before_it_expires() -> None:
    clock = Clock()
    powerflow = capture("getSitePowerflow")
    client, session, _ = client_for({POWERFLOW: [(200, powerflow, [])]}, clock)
    await client.get_site_powerflow("1")
    clock.now += 3600 - 60
    await client.get_site_powerflow("1")
    # Renewed with the refresh token, no new password login.
    assert logins(session) == 1
    assert session.count("POST", "/oauth2/token") == 2
    assert session.calls[-1][2]["headers"]["Authorization"] == "Bearer AT2"
    refresh = [kwargs["data"] for m, u, kwargs in session.calls if u.endswith("/oauth2/token")][1]
    assert refresh["grant_type"] == "refresh_token" and refresh["refresh_token"] == "RT1"


async def test_stored_refresh_token_needs_no_password() -> None:
    client, session, _ = client_for({POWERFLOW: [(200, capture("getSitePowerflow"), [])]})
    client.set_refresh_token("RT1")
    await client.get_site_powerflow("1")
    assert logins(session) == 0
    assert session.count("GET", "/oauth2/authorize") == 0


async def test_rejected_refresh_token_falls_back_to_the_password() -> None:
    kept: list[str | None] = []
    client, session, _ = client_for({POWERFLOW: [(200, capture("getSitePowerflow"), [])]})
    client.set_refresh_token("OLD")
    client.on_refresh_token = kept.append
    await client.get_site_powerflow("1")
    assert logins(session) == 1
    assert kept == [None, "RT1"]


async def test_refresh_outage_keeps_the_refresh_token() -> None:
    client, session, _ = client_for({TOKEN: [(503, "", [])], POWERFLOW: [(200, {}, [])]})
    client.set_refresh_token("RT1")
    with pytest.raises(SolarEdgeConnectionError):
        await client.get_site_powerflow("1")
    assert client._refresh_token == "RT1"  # noqa: SLF001
    assert logins(session) == 0


@pytest.mark.parametrize(
    "expired",
    [
        (401, "", []),
        (302, "", [("Location", "https://login.solaredge.com/login")]),
        (200, "<!DOCTYPE html><html><form>login</form></html>", []),
    ],
)
async def test_rejected_token_is_renewed_once(expired: tuple[int, str, list[tuple[str, str]]]) -> None:
    powerflow = capture("getSitePowerflow")
    client, session, _ = client_for({POWERFLOW: [(200, powerflow, []), expired, (200, powerflow, [])]})
    assert await client.get_site_powerflow("1") == powerflow
    assert await client.get_site_powerflow("1") == powerflow
    assert logins(session) == 1
    assert session.count("POST", "/oauth2/token") == 2
    assert session.count("GET", "/powerflow/latest") == 3


async def test_rejected_twice_is_not_a_wrong_password() -> None:
    client, session, _ = client_for({POWERFLOW: [(401, "", [])]})
    with pytest.raises(SolarEdgeApiError) as info:
        await client.get_site_powerflow("1")
    assert not isinstance(info.value, SolarEdgeAuthError)
    assert session.count("GET", "/powerflow/latest") == 2


async def test_endpoint_refused_for_the_account_does_not_cost_a_renewal_per_poll() -> None:
    clock = Clock()
    devices = ("GET", "/devices")
    powerflow = capture("getSitePowerflow")
    client, session, _ = client_for({devices: [(401, "", [])], POWERFLOW: [(200, powerflow, [])]}, clock)
    with pytest.raises(SolarEdgeApiError):
        await client.get_site_devices("1")
    # One login, one renewal to tell an expired token from a refusal.
    assert session.count("POST", "/oauth2/token") == 2
    for _ in range(5):
        with pytest.raises(SolarEdgeApiError):
            await client.get_site_devices("1")
        assert await client.get_site_powerflow("1") == powerflow
    assert session.count("POST", "/oauth2/token") == 2
    assert logins(session) == 1
    # Asked again as a possible expiry after a while.
    clock.now += DENIED_FOR
    with pytest.raises(SolarEdgeApiError):
        await client.get_site_devices("1")
    assert session.count("POST", "/oauth2/token") > 2


async def test_a_refused_endpoint_that_answers_again_is_trusted_again() -> None:
    devices = ("GET", "/devices")
    client, session, _ = client_for(
        {devices: [(401, "", []), (401, "", []), (200, {"devices": []}, []), (401, "", []), (200, {}, [])]}
    )
    with pytest.raises(SolarEdgeApiError):
        await client.get_site_devices("1")
    assert await client.get_site_devices("1") == {"devices": []}
    # A 401 now is an expiry again: one renewal and a retry.
    assert await client.get_site_devices("1") == {}
    assert session.count("POST", "/oauth2/token") == 3


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        ((500, "oops", []), SolarEdgeConnectionError),
        ((429, "", []), SolarEdgeRateLimitError),
        (aiohttp.ClientConnectionError(), SolarEdgeConnectionError),
        (TimeoutError(), SolarEdgeConnectionError),
        ((404, {"error": {"code": "NOT_FOUND"}}, []), SolarEdgeApiError),
        ((200, "not json", []), SolarEdgeApiError),
    ],
)
async def test_request_errors(answer: Any, error: type[Exception]) -> None:
    client, _, _ = client_for({POWERFLOW: [answer]})
    with pytest.raises(error):
        await client.get_site_powerflow("1")


async def test_no_data_code() -> None:
    client, _, _ = client_for({("GET", "/measurements"): [(400, {"error": {"code": "INVALID_ARGUMENTS"}}, [])]})
    with pytest.raises(SolarEdgeApiError) as info:
        await client.get_site_measurements("1", "2019-12-31")
    assert is_no_data(info.value)
    assert info.value.code == "INVALID_ARGUMENTS"
    assert not is_no_data(SolarEdgeConnectionError("x"))
    assert not is_no_data(SolarEdgeApiError("x", code="GENERAL_ERROR"))


async def test_identical_gets_in_flight_go_out_once() -> None:
    powerflow = capture("getSitePowerflow")
    client, session, _ = client_for({POWERFLOW: [(200, powerflow, [])]})
    results = await asyncio.gather(*(client.get_site_powerflow("1") for _ in range(3)))
    assert all(result == powerflow for result in results)
    assert session.count("GET", "/powerflow/latest") == 1
    assert logins(session) == 1


async def test_backup_reserve_checks_the_answer_status() -> None:
    """SolarEdge answers 200 with status FAILED when it refuses (real answers, 2026-11-01)."""
    passed = {"@c": ".ValidationResults", "status": "PASSED", "httpStatus": 200, "errorMessages": []}
    failed = {
        "@c": ".ValidationResults", "status": "FAILED", "httpStatus": 500,
        "errorMessages": [{"message": "Invalid parameter {} in request", "messageCode": 9227, "params": [None]}],
    }
    client, session, _ = client_for({("PUT", "/storage/1/backupReserve"): [(200, passed, []), (200, failed, [])]})
    await client.set_backup_reserve("1", 25, 48)
    put = next(kwargs for m, u, kwargs in session.calls if m == "PUT")
    assert put["json"] == {"backupReserve": 25, "backupReserveDurationInHours": 48}
    with pytest.raises(SolarEdgeApiError, match="Invalid parameter"):
        await client.set_backup_reserve("1", 20, -1)


async def test_query_parameters() -> None:
    client, session, _ = client_for(
        {("GET", "/information/inverters"): [(200, {}, [])], ("GET", "/measurements"): [(200, {"summary": None}, [])]}
    )
    await client.get_inverters_information(["A-1", "B-2"])
    await client.get_site_measurements("1", "2025-12-31")
    api = [kwargs for m, u, kwargs in session.calls if "monitoring.solaredge.com" in u]
    assert api[0]["params"] == [("inverter-serials", "A-1"), ("inverter-serials", "B-2")]
    assert ("end-date", "2025-12-31") in api[1]["params"]


async def test_credentials_and_tokens_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    client, _, _ = client_for({SITES: [(200, {"sites": {}}, [])]})
    client._password = "wrong-secret"  # noqa: SLF001
    with pytest.raises(SolarEdgeAuthError) as info:
        await client.get_sites()
    client2, _, _ = client_for({POWERFLOW: [(401, "", [])]})
    with pytest.raises(SolarEdgeApiError):
        await client2.get_site_powerflow("1")
    for secret in (USERNAME, PASSWORD, "wrong-secret", "AT1", "AT2", "RT1", "CODE1"):
        assert secret not in caplog.text
        assert secret not in str(info.value)


def test_redirect_code_parsing() -> None:
    assert parse_qs(urlparse(f"{REDIRECT_URI}?code=abc&state=s").query)["code"] == ["abc"]
