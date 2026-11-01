"""Backup reserve of the SolarEdge Cloud integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import KIND_BATTERY
from .coordinator import SolarEdgeConfigEntry
from .entity import SolarEdgeEntity, SolarEdgeEntityDescription, async_add_when_seen, entities_for


@dataclass(frozen=True, kw_only=True)
class SolarEdgeNumberDescription(SolarEdgeEntityDescription, NumberEntityDescription):
    """A SolarEdge number."""


NUMBERS: tuple[SolarEdgeNumberDescription, ...] = (
    # The app's Backup Reserve; set for the duration chosen in "Backup reserve duration".
    SolarEdgeNumberDescription(
        kind=KIND_BATTERY,
        key="backup_reserve",
        translation_key="backup_reserve",
        optional=True,
        native_min_value=0,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
        mode=NumberMode.SLIDER,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolarEdgeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the backup reserve of every site with a battery, once SolarEdge reports it."""
    coordinator = entry.runtime_data
    async_add_when_seen(coordinator, "number", entities_for(coordinator, NUMBERS, SolarEdgeReserveNumber), async_add_entities)


class SolarEdgeReserveNumber(SolarEdgeEntity, NumberEntity):
    """The share of the battery kept for a power cut."""

    entity_description: SolarEdgeNumberDescription

    @property
    def native_value(self) -> float | None:
        """The reserve SolarEdge reports (or the one just set)."""
        return self.reading

    @property
    def native_max_value(self) -> float:
        """SolarEdge's limit for the site (backupReserveControlUpToLimit), else 100."""
        limit = self.values.get("backup_reserve_limit")
        return float(limit) if isinstance(limit, (int, float)) and 0 < limit <= 100 else 100.0

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Whether SolarEdge lets this site change the reserve."""
        if "backup_reserve_editable" in self.values:
            return {"editable": self.values["backup_reserve_editable"]}
        return None

    async def async_set_native_value(self, value: float) -> None:
        """Set the reserve for the chosen duration."""
        await self.coordinator.async_set_backup_reserve(self.site.site_id, round(value))
