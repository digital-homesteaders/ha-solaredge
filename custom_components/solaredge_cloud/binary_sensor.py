"""Binary sensors of the SolarEdge Cloud integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import KIND_BATTERY, KIND_EV, KIND_SITE
from .coordinator import SolarEdgeConfigEntry
from .entity import SolarEdgeEntity, SolarEdgeEntityDescription, async_add_when_seen, entities_for


@dataclass(frozen=True, kw_only=True)
class SolarEdgeBinarySensorDescription(SolarEdgeEntityDescription, BinarySensorEntityDescription):
    """A SolarEdge binary sensor."""


BINARY_SENSORS: tuple[SolarEdgeBinarySensorDescription, ...] = (
    # Not offline while the inverters sleep for the night.
    SolarEdgeBinarySensorDescription(
        kind=KIND_SITE,
        key="connectivity",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # Open site alerts.
    SolarEdgeBinarySensorDescription(
        kind=KIND_SITE,
        key="problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
    ),
    SolarEdgeBinarySensorDescription(
        kind=KIND_EV, key="solar_charging", translation_key="solar_charging", optional=True
    ),
    # The app's Weather Guard setting (on = enabled), not whether it is holding the battery now.
    SolarEdgeBinarySensorDescription(
        kind=KIND_BATTERY, key="weather_guard", translation_key="weather_guard", optional=True
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolarEdgeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the binary sensors of every site."""
    coordinator = entry.runtime_data
    async_add_when_seen(
        coordinator,
        "binary_sensor",
        entities_for(coordinator, BINARY_SENSORS, SolarEdgeBinarySensor),
        async_add_entities,
    )


class SolarEdgeBinarySensor(SolarEdgeEntity, BinarySensorEntity):
    """A SolarEdge on/off reading."""

    entity_description: SolarEdgeBinarySensorDescription

    @property
    def is_on(self) -> bool | None:
        """The reading."""
        return self.value
