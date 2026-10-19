"""The client: login, session expiry, errors and parsing."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp
import pytest
from multidict import CIMultiDict

from custom_components.solaredge_cloud.api import (
    DENIED_FOR,
    LOGIN_RETRY_MIN,
    SolarEdgeApiError,
    SolarEdgeAuthError,
    SolarEdgeCloudClient,
    SolarEdgeConnectionError,
    SolarEdgeRateLimitError,
    cookies_from_headers,
    is_no_data,
)

from .conftest import PASSWORD, USERNAME, load_fixture

LOGIN_OK = (302, "", [("Set-Cookie", "SPRING_SECURITY_REMEMBER_ME_COOKIE=abc; Path=/; HttpOnly"),
                      ("Set-Cookie", "JSESSIONID=xyz; Path=/"), ("Location", "/solaredge-web/p/home")])


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
    """Answers each request from `routes`: (method, path suffix) -> list of answers in turn."""

    def __init__(self, routes: dict[tuple[str, str], list[Any]]) -> None:
        self.routes = {key: list(value) for key, value in routes.items()}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.request("POST", url, **kwargs)

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        for (route_method, suffix), answers in self.routes.items():
            if route_method == method and url.endswith(suffix):
                answer = answers.pop(0) if len(answers) > 1 else answers[0]
                if isinstance(answer, BaseException):
                    raise answer
                status, body, headers = answer
                return FakeResponse(status, body, headers)
        raise AssertionError(f"unexpected {method} {url}")

    def count(self, method: str, suffix: str) -> int:
        return sum(1 for m, url, _ in self.calls if m == method and url.endswith(suffix))


class Clock:
    """A settable monotonic clock."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def client_for(routes: dict[tuple[str, str], list[Any]], clock: Clock | None = None) -> tuple[SolarEdgeCloudClient, FakeSession]:
    session = FakeSession(routes)
    return SolarEdgeCloudClient(session, USERNAME, PASSWORD, clock=clock or Clock()), session  # type: ignore[arg-type]


SITES = ("GET", "/services/m/so/sites/")
LOGIN = ("POST", "/solaredge-apigw/api/login")
POWERFLOW = ("GET", "/powerflow/latest")


def capture(method: str) -> Any:
    return next(call["result"] for call in load_fixture("synthetic-site.json")["calls"] if call["method"] == method)


async def test_login_keeps_cookies_and_sends_them() -> None:
    client, session = client_for({LOGIN: [LOGIN_OK], SITES: [(200, {"sites": capture("getSites")}, [])]})
    sites = await client.get_sites()
    assert [site["id"] for site in sites] == [9999999]
    method, url, kwargs = session.calls[0]
    assert kwargs["allow_redirects"] is False
    assert kwargs["data"] == {"j_username": USERNAME, "j_password": PASSWORD}
    assert session.calls[1][2]["headers"]["Cookie"] == "SPRING_SECURITY_REMEMBER_ME_COOKIE=abc; JSESSIONID=xyz"
    assert session.calls[1][2]["timeout"].total == 20


def test_cookie_parsing() -> None:
    assert cookies_from_headers(["A=1; Path=/; Expires=Wed, 21 Oct 2026 07:28:00 GMT", "B=2", "junk"]) == ["A=1", "B=2"]


@pytest.mark.parametrize(
    "answer",
    [
        (200, "<html><input name='j_password'></html>", []),
        (401, "", []),
        (302, "", [("Location", "/login?error=true"), ("Set-Cookie", "JSESSIONID=x")]),
        (302, "", [("Location", "/solaredge-web/p/home")]),
    ],
)
async def test_rejected_login(answer: tuple[int, str, list[tuple[str, str]]]) -> None:
    client, _ = client_for({LOGIN: [answer]})
    with pytest.raises(SolarEdgeAuthError):
        await client.login()
    assert not client.logged_in


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        ((503, "", []), SolarEdgeConnectionError),
        ((429, "", []), SolarEdgeRateLimitError),
        (aiohttp.ClientConnectionError("down"), SolarEdgeConnectionError),
        (TimeoutError(), SolarEdgeConnectionError),
    ],
)
async def test_login_outage_is_not_a_rejection(answer: Any, error: type[Exception]) -> None:
    client, session = client_for({LOGIN: [answer]})
    with pytest.raises(error) as info:
        await client.login()
    assert not isinstance(info.value, SolarEdgeAuthError)
    # An outage does not hold back the next login.
    with pytest.raises(error):
        await client.ensure_logged_in()
    assert session.count("POST", "/login") == 2


async def test_rejected_login_holds_back_automatic_logins() -> None:
    clock = Clock()
    client, session = client_for({LOGIN: [(200, "", [])], SITES: [(200, {"sites": {}}, [])]}, clock)
    with pytest.raises(SolarEdgeAuthError):
        await client.get_sites()
    # No new login attempt until the retry delay has passed.
    for _ in range(5):
        with pytest.raises(SolarEdgeAuthError):
            await client.get_sites()
    assert session.count("POST", "/login") == 1
    assert client.login_blocked_for == LOGIN_RETRY_MIN
    clock.now += LOGIN_RETRY_MIN
    with pytest.raises(SolarEdgeAuthError):
        await client.get_sites()
    assert session.count("POST", "/login") == 2
    # Doubles per rejection.
    assert client.login_blocked_for == 2 * LOGIN_RETRY_MIN
    # An explicit login (config or reauth flow) is not held back.
    with pytest.raises(SolarEdgeAuthError):
        await client.login()
    assert session.count("POST", "/login") == 3


@pytest.mark.parametrize(
    "expired",
    [
        (401, "", []),
        (302, "", [("Location", "https://monitoring.solaredge.com/solaredge-web/p/login")]),
        (200, "<!DOCTYPE html><html><form>login</form></html>", []),
    ],
)
async def test_expired_session_logs_in_again_once(expired: tuple[int, str, list[tuple[str, str]]]) -> None:
    powerflow = capture("getSitePowerflow")
    client, session = client_for({LOGIN: [LOGIN_OK], POWERFLOW: [(200, powerflow, []), expired, (200, powerflow, [])]})
    assert await client.get_site_powerflow("1") == powerflow
    assert await client.get_site_powerflow("1") == powerflow
    assert session.count("POST", "/login") == 2
    assert session.count("GET", "/powerflow/latest") == 3


async def test_expired_twice_is_not_a_rejected_login() -> None:
    client, session = client_for({LOGIN: [LOGIN_OK], POWERFLOW: [(401, "", [])]})
    with pytest.raises(SolarEdgeApiError) as info:
        await client.get_site_powerflow("1")
    assert not isinstance(info.value, SolarEdgeAuthError)
    assert session.count("GET", "/powerflow/latest") == 2


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
    client, _ = client_for({LOGIN: [LOGIN_OK], POWERFLOW: [answer]})
    with pytest.raises(error):
        await client.get_site_powerflow("1")


async def test_no_data_code() -> None:
    client, _ = client_for({LOGIN: [LOGIN_OK], ("GET", "/measurements"): [(400, {"error": {"code": "INVALID_ARGUMENTS"}}, [])]})
    with pytest.raises(SolarEdgeApiError) as info:
        await client.get_site_measurements("1", "2019-12-31")
    assert is_no_data(info.value)
    assert info.value.code == "INVALID_ARGUMENTS"
    assert not is_no_data(SolarEdgeConnectionError("x"))
    assert not is_no_data(SolarEdgeApiError("x", code="GENERAL_ERROR"))


async def test_identical_gets_in_flight_go_out_once() -> None:
    powerflow = capture("getSitePowerflow")
    client, session = client_for({LOGIN: [LOGIN_OK], POWERFLOW: [(200, powerflow, [])]})
    results = await asyncio.gather(*(client.get_site_powerflow("1") for _ in range(3)))
    assert all(result == powerflow for result in results)
    assert session.count("GET", "/powerflow/latest") == 1
    assert session.count("POST", "/login") == 1


async def test_query_parameters() -> None:
    client, session = client_for({LOGIN: [LOGIN_OK], ("GET", "/information/inverters"): [(200, {}, [])],
                                  ("GET", "/measurements"): [(200, {"summary": None}, [])]})
    await client.get_inverters_information(["A-1", "B-2"])
    await client.get_site_measurements("1", "2025-12-31")
    assert session.calls[1][2]["params"] == [("inverter-serials", "A-1"), ("inverter-serials", "B-2")]
    assert ("end-date", "2025-12-31") in session.calls[2][2]["params"]


async def test_credentials_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    client, _ = client_for({LOGIN: [(200, "", [])]})
    with pytest.raises(SolarEdgeAuthError) as info:
        await client.get_sites()
    client2, _ = client_for({LOGIN: [LOGIN_OK], POWERFLOW: [(401, "", [])]})
    with pytest.raises(SolarEdgeApiError):
        await client2.get_site_powerflow("1")
    assert USERNAME not in caplog.text and PASSWORD not in caplog.text
    assert USERNAME not in str(info.value) and PASSWORD not in str(info.value)


async def test_endpoint_refused_for_the_account_does_not_cost_a_login_per_poll() -> None:
    clock = Clock()
    devices = ("GET", "/devices")
    powerflow = capture("getSitePowerflow")
    client, session = client_for({LOGIN: [LOGIN_OK], devices: [(401, "", [])], POWERFLOW: [(200, powerflow, [])]}, clock)
    with pytest.raises(SolarEdgeApiError):
        await client.get_site_devices("1")
    # One login for the poll, one more to tell expiry from refusal.
    assert session.count("POST", "/login") == 2
    for _ in range(5):
        with pytest.raises(SolarEdgeApiError):
            await client.get_site_devices("1")
        assert await client.get_site_powerflow("1") == powerflow
    # The refusal keeps the session: no new logins.
    assert session.count("POST", "/login") == 2
    assert client.logged_in
    # Asked again as a possible expiry after a while.
    clock.now += DENIED_FOR
    with pytest.raises(SolarEdgeApiError):
        await client.get_site_devices("1")
    assert session.count("POST", "/login") == 3


async def test_a_refused_endpoint_that_answers_again_is_trusted_again() -> None:
    devices = ("GET", "/devices")
    client, session = client_for({LOGIN: [LOGIN_OK], devices: [(401, "", []), (401, "", []), (200, {"devices": []}, []), (401, "", []), (200, {}, [])]})
    with pytest.raises(SolarEdgeApiError):
        await client.get_site_devices("1")
    assert await client.get_site_devices("1") == {"devices": []}
    # A 401 now is an expiry again: one new login and a retry.
    assert await client.get_site_devices("1") == {}
    assert session.count("POST", "/login") == 3
