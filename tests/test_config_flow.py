"""Config, reauth and options flows."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solaredge_cloud.api import (
    SolarEdgeAuthError,
    SolarEdgeConnectionError,
    SolarEdgeLoginRefusedError,
    SolarEdgeRateLimitError,
)
from custom_components.solaredge_cloud.const import CONF_POLL_INTERVAL, DOMAIN

from .conftest import PASSWORD, USERNAME

CLIENT = "custom_components.solaredge_cloud.config_flow.SolarEdgeCloudClient"
SITES = [{"id": 9999999, "name": "Synthetic site"}]


@pytest.fixture
def flow_client():
    with patch(CLIENT) as cls:
        client = cls.return_value
        client.login = AsyncMock()
        client.get_sites = AsyncMock(return_value=SITES)
        with patch("custom_components.solaredge_cloud.async_setup_entry", return_value=True):
            yield client


async def test_user_flow(hass: HomeAssistant, flow_client) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["errors"] == {}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": f" {USERNAME} ", "password": PASSWORD}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Synthetic site"
    assert result["data"] == {"username": USERNAME, "password": PASSWORD}
    assert result["result"].unique_id == USERNAME.lower()


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (SolarEdgeAuthError("x"), "invalid_auth"),
        (SolarEdgeLoginRefusedError("x"), "login_refused"),
        (SolarEdgeConnectionError("x"), "cannot_connect"),
        (SolarEdgeRateLimitError("x"), "cannot_connect"),
        (RuntimeError("x"), "unknown"),
    ],
)
async def test_user_flow_errors(hass: HomeAssistant, flow_client, error: Exception, key: str) -> None:
    flow_client.login.side_effect = error
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": USERNAME, "password": "bad"})
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"base": key}
    # It recovers on the next try.
    flow_client.login.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": USERNAME, "password": PASSWORD})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_no_sites(hass: HomeAssistant, flow_client) -> None:
    flow_client.get_sites.return_value = []
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": USERNAME, "password": PASSWORD})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "no_sites"


async def test_already_configured(hass: HomeAssistant, flow_client) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id=USERNAME.lower(), data={}).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": USERNAME.upper(), "password": PASSWORD})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"
    flow_client.login.assert_not_called()


async def test_already_configured_disabled(hass: HomeAssistant, flow_client) -> None:
    """A disabled entry is hidden from the list: the abort says it is disabled and names it."""
    MockConfigEntry(
        domain=DOMAIN,
        unique_id=USERNAME.lower(),
        title="Synthetic site",
        data={},
        disabled_by=config_entries.ConfigEntryDisabler.USER,
    ).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": USERNAME, "password": PASSWORD})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured_disabled"
    assert result["description_placeholders"] == {"title": "Synthetic site"}
    flow_client.login.assert_not_called()


async def test_reauth(hass: HomeAssistant, flow_client) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=USERNAME.lower(), title="Synthetic site",
        data={"username": USERNAME, "password": "old"},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "reauth_confirm"

    flow_client.login.side_effect = SolarEdgeAuthError("x")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"password": "still-wrong"})
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.data["password"] == "old"

    flow_client.login.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"password": "new"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    assert entry.data == {"username": USERNAME, "password": "new"}


async def test_options_flow(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={"username": USERNAME, "password": PASSWORD})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_POLL_INTERVAL: 120})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_POLL_INTERVAL: 120}


async def test_options_flow_limits(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={"username": USERNAME, "password": PASSWORD})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    for bad in (10, 301):
        with pytest.raises(Exception):  # noqa: B017 - voluptuous rejects it
            await hass.config_entries.options.async_configure(result["flow_id"], {CONF_POLL_INTERVAL: bad})
