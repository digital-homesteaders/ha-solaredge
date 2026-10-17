"""EV charging switch of the SolarEdge Cloud integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import KIND_EV
from .coordinator import SolarEdgeConfigEntry
from .entity import SolarEdgeEntity, SolarEdgeEntityDescription, entities_for


@dataclass(frozen=True, kw_only=True)
class SolarEdgeSwitchDescription(SolarEdgeEntityDescription, SwitchEntityDescription):
    """A SolarEdge switch."""


SWITCHES: tuple[SolarEdgeSwitchDescription, ...] = (
    # On: charge now; off: stop. Both put the charger in manual mode.
    SolarEdgeSwitchDescription(kind=KIND_EV, key="charging", translation_key="charging"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolarEdgeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a charging switch per EV charger."""
    async_add_entities(entities_for(entry.runtime_data, SWITCHES, SolarEdgeChargingSwitch))


class SolarEdgeChargingSwitch(SolarEdgeEntity, SwitchEntity):
    """Start or stop charging (manual mode)."""

    entity_description: SolarEdgeSwitchDescription

    @property
    def is_on(self) -> bool | None:
        """Whether the charger is charging."""
        return self.value

    async def _set(self, on: bool) -> None:
        assert self.reporter_id is not None
        await self.coordinator.async_set_activation(
            self.site.site_id,
            self.reporter_id,
            {"mode": "MANUAL", "level": 100 if on else 0, "duration": None},
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Charge now."""
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop charging."""
        await self._set(False)
