"""Diagnostics of the SolarEdge Cloud integration; the login is redacted."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .coordinator import SolarEdgeConfigEntry

TO_REDACT = {CONF_USERNAME, CONF_PASSWORD, "vehicle", "unique_id", "title"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SolarEdgeConfigEntry
) -> dict[str, Any]:
    """The account's polling state and current values."""
    return async_redact_data(
        {
            "entry": {"data": dict(entry.data), "options": dict(entry.options)},
            "coordinator": entry.runtime_data.diagnostics(),
            "raw": await entry.runtime_data.raw_answers(),
        },
        TO_REDACT,
    )
