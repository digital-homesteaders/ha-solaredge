"""EV charger "automatic" and backup reserve reset buttons of the SolarEdge Cloud integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import KIND_BATTERY, KIND_EV
from .coordinator import SolarEdgeConfigEntry
from .entity import SolarEdgeEntity, SolarEdgeEntityDescription, async_add_when_seen, entities_for


@dataclass(frozen=True, kw_only=True)
class SolarEdgeButtonDescription(SolarEdgeEntityDescription, ButtonEntityDescription):
    """A SolarEdge button."""


BUTTONS: tuple[SolarEdgeButtonDescription, ...] = (
    # 2.0.0's "set to automatic" flow action: back to the charger's own
    # schedule and solar surplus charging.
    SolarEdgeButtonDescription(kind=KIND_EV, key="set_automatic", translation_key="set_automatic"),
)

# Back to the base (installer's) backup reserve; not offered by the app.
RESERVE_BUTTONS: tuple[SolarEdgeButtonDescription, ...] = (
    SolarEdgeButtonDescription(
        kind=KIND_BATTERY, key="reset_backup_reserve", translation_key="reset_backup_reserve", optional=True
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolarEdgeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add an automatic-mode button per EV charger, and a reserve reset per battery that has a reserve."""
    async_add_entities(entities_for(entry.runtime_data, BUTTONS, SolarEdgeAutomaticButton))
    coordinator = entry.runtime_data
    async_add_when_seen(
        coordinator, "button", entities_for(coordinator, RESERVE_BUTTONS, SolarEdgeResetReserveButton), async_add_entities
    )


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


class SolarEdgeResetReserveButton(SolarEdgeEntity, ButtonEntity):
    """Drop the reserve set from the app or Home Assistant, back to the installer's."""

    entity_description: SolarEdgeButtonDescription

    @property
    def has_value(self) -> bool:
        """Added once SolarEdge reports a backup reserve for the site."""
        return self.values.get("backup_reserve") is not None

    async def async_press(self) -> None:
        """Send the reserve with a duration of 0 hours."""
        await self.coordinator.async_reset_backup_reserve(self.site.site_id)
