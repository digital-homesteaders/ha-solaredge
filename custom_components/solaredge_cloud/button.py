"""EV charger "automatic" button of the SolarEdge Cloud integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import KIND_EV
from .coordinator import SolarEdgeConfigEntry
from .entity import SolarEdgeEntity, SolarEdgeEntityDescription, entities_for


@dataclass(frozen=True, kw_only=True)
class SolarEdgeButtonDescription(SolarEdgeEntityDescription, ButtonEntityDescription):
    """A SolarEdge button."""


BUTTONS: tuple[SolarEdgeButtonDescription, ...] = (
    # 2.0.0's "set to automatic" flow action: back to the charger's own
    # schedule and solar surplus charging.
    SolarEdgeButtonDescription(kind=KIND_EV, key="set_automatic", translation_key="set_automatic"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolarEdgeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add an automatic-mode button per EV charger."""
    async_add_entities(entities_for(entry.runtime_data, BUTTONS, SolarEdgeAutomaticButton))


class SolarEdgeAutomaticButton(SolarEdgeEntity, ButtonEntity):
    """Put the EV charger back in automatic mode."""

    entity_description: SolarEdgeButtonDescription

    @property
    def has_value(self) -> bool:
        """A button has no value to wait for."""
        return True

    async def async_press(self) -> None:
        """Send activationState AUTO."""
        assert self.reporter_id is not None
        await self.coordinator.async_set_activation(
            self.site.site_id, self.reporter_id, {"mode": "AUTO"}
        )
