"""Home load controller mode of the SolarEdge Cloud integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import KIND_HLC
from .coordinator import SolarEdgeConfigEntry
from .entity import SolarEdgeEntity, SolarEdgeEntityDescription, entities_for
from .mapping import HLC_STATES


@dataclass(frozen=True, kw_only=True)
class SolarEdgeSelectDescription(SolarEdgeEntityDescription, SelectEntityDescription):
    """A SolarEdge select."""


SELECTS: tuple[SolarEdgeSelectDescription, ...] = (
    SolarEdgeSelectDescription(
        kind=KIND_HLC, key="state", translation_key="load_controller_mode", options=list(HLC_STATES)
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolarEdgeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a mode select per load controller."""
    async_add_entities(entities_for(entry.runtime_data, SELECTS, SolarEdgeLoadControllerSelect))


class SolarEdgeLoadControllerSelect(SolarEdgeEntity, SelectEntity):
    """On, off, or auto (SolarEdge decides, e.g. on solar surplus)."""

    entity_description: SolarEdgeSelectDescription

    @property
    def current_option(self) -> str | None:
        """The mode; unknown for a manual level other than 0 or 100."""
        return self.value

    async def async_select_option(self, option: str) -> None:
        """Switch the load controller."""
        assert self.reporter_id is not None
        await self.coordinator.async_set_activation(
            self.site.site_id, self.reporter_id, dict(HLC_STATES[option])
        )
