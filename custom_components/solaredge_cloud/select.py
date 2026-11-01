"""Load controller mode and backup reserve duration of the SolarEdge Cloud integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import KIND_BATTERY, KIND_HLC
from .coordinator import SolarEdgeConfigEntry
from .entity import SolarEdgeEntity, SolarEdgeEntityDescription, entities_for
from .mapping import HLC_STATES, RESERVE_DURATIONS


@dataclass(frozen=True, kw_only=True)
class SolarEdgeSelectDescription(SolarEdgeEntityDescription, SelectEntityDescription):
    """A SolarEdge select."""


SELECTS: tuple[SolarEdgeSelectDescription, ...] = (
    SolarEdgeSelectDescription(
        kind=KIND_HLC, key="state", translation_key="load_controller_mode", options=list(HLC_STATES)
    ),
)

# How long a backup reserve set from Home Assistant lasts, as the app offers.
# Kept by the integration, not SolarEdge: it applies to the next reserve set.
DURATIONS: tuple[SolarEdgeSelectDescription, ...] = (
    SolarEdgeSelectDescription(
        kind=KIND_BATTERY,
        key="backup_reserve_duration",
        translation_key="backup_reserve_duration",
        options=list(RESERVE_DURATIONS),
        entity_category=EntityCategory.CONFIG,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolarEdgeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a mode select per load controller, and a reserve duration per site with a battery."""
    async_add_entities(entities_for(entry.runtime_data, SELECTS, SolarEdgeLoadControllerSelect))
    async_add_entities(entities_for(entry.runtime_data, DURATIONS, SolarEdgeReserveDurationSelect))


class SolarEdgeLoadControllerSelect(SolarEdgeEntity, SelectEntity):
    """On, off, or auto (SolarEdge decides, e.g. on solar surplus)."""

    entity_description: SolarEdgeSelectDescription

    @property
    def current_option(self) -> str | None:
        """The mode; unknown for a manual level other than 0 or 100."""
        return self.reading

    async def async_select_option(self, option: str) -> None:
        """Switch the load controller."""
        assert self.reporter_id is not None
        await self.coordinator.async_set_activation(
            self.site.site_id, self.reporter_id, dict(HLC_STATES[option])
        )


class SolarEdgeReserveDurationSelect(SolarEdgeEntity, SelectEntity):
    """Unlimited, 24, 48 or 72 hours for the next backup reserve."""

    entity_description: SolarEdgeSelectDescription

    @property
    def current_option(self) -> str:
        """The chosen duration."""
        return self.coordinator.reserve_duration(self.site.site_id)

    async def async_select_option(self, option: str) -> None:
        """Remember the duration; it changes nothing at SolarEdge by itself."""
        self.coordinator.set_reserve_duration(self.site.site_id, option)
