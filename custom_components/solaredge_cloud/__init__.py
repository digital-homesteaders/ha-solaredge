"""SolarEdge Cloud: SolarEdge sites through the mySolarEdge app API.

A port of com.solaredge v2.0.0 for Homey by Robin de Gruijter, GPL-3.0.
"""

from __future__ import annotations

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .api import SolarEdgeCloudClient
from .const import CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
from .coordinator import SolarEdgeConfigEntry, SolarEdgeCoordinator, async_create_session
from .entity import device_info

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup_entry(hass: HomeAssistant, entry: SolarEdgeConfigEntry) -> bool:
    """Set up one mySolarEdge account."""
    client = SolarEdgeCloudClient(
        async_create_session(hass), entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD]
    )
    coordinator = SolarEdgeCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    # The site devices first: the others link to them (via_device).
    registry = dr.async_get(hass)
    for site in coordinator.sites.values():
        device = registry.async_get_or_create(config_entry_id=entry.entry_id, **device_info(site, "site"))
        coordinator.site_device_ids[site.site_id] = device.id

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.async_start_energy_ticks()
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: SolarEdgeConfigEntry) -> None:
    """A new poll interval applies without a reload."""
    entry.runtime_data.async_set_poll_interval(
        int(entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL))
    )


async def async_unload_entry(hass: HomeAssistant, entry: SolarEdgeConfigEntry) -> bool:
    """Unload an account."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

