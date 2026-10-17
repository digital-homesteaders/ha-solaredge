"""Base entity and devices of the SolarEdge Cloud integration."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, KIND_BATTERY, KIND_EV, KIND_HLC, KIND_INVERTER, KIND_METER, KIND_SITE
from .coordinator import SiteInfo, SolarEdgeCoordinator, device_key

PORTAL_URL = "https://monitoring.solaredge.com"

# HA 2026.8 links a device to its parent by registry id (via_device_id) and
# deprecates via_device; older versions only know via_device.
VIA_DEVICE_ID = "via_device_id" in DeviceInfo.__annotations__

MODELS = {
    KIND_SITE: "Site",
    KIND_INVERTER: "Inverter",
    KIND_METER: "Meter",
    KIND_BATTERY: "Battery",
    KIND_EV: "EV charger",
    KIND_HLC: "Home load controller",
}


@dataclass(frozen=True, kw_only=True)
class SolarEdgeEntityDescription(EntityDescription):
    """Which device the entity belongs to; optional ones appear once they have a value."""

    kind: str
    optional: bool = False


def device_identifier(site_id: str, kind: str, reporter_id: str | None = None) -> str:
    """The device registry identifier: the site id for the site itself."""
    return site_id if kind == KIND_SITE else f"{site_id}_{kind}" + (f"_{reporter_id}" if reporter_id else "")


def device_info(
    site: SiteInfo, kind: str, reporter_id: str | None = None, site_device_id: str | None = None
) -> DeviceInfo:
    """A site device, or one of its devices linked to it.

    site_device_id is the site's device registry id (newer HA links by it).
    """
    if kind == KIND_SITE:
        return DeviceInfo(
            identifiers={(DOMAIN, site.site_id)},
            name=site.name,
            manufacturer="SolarEdge",
            model=MODELS[KIND_SITE],
            configuration_url=PORTAL_URL,
        )
    if kind == KIND_EV:
        name = site.ev_chargers.get(reporter_id or "", MODELS[KIND_EV])
    elif kind == KIND_HLC:
        name = site.load_controllers.get(reporter_id or "", MODELS[KIND_HLC])
    else:
        name = f"{site.name} {MODELS[kind]}"
    info = DeviceInfo(
        identifiers={(DOMAIN, device_identifier(site.site_id, kind, reporter_id))},
        name=name,
        manufacturer="SolarEdge",
        model=MODELS[kind],
    )
    if VIA_DEVICE_ID:
        if site_device_id:
            info["via_device_id"] = site_device_id
    else:
        info["via_device"] = (DOMAIN, site.site_id)  # type: ignore[typeddict-unknown-key]
    return info


class SolarEdgeEntity(CoordinatorEntity[SolarEdgeCoordinator]):
    """An entity fed from the account's coordinator."""

    _attr_has_entity_name = True
    entity_description: SolarEdgeEntityDescription

    def __init__(
        self,
        coordinator: SolarEdgeCoordinator,
        site: SiteInfo,
        description: SolarEdgeEntityDescription,
        reporter_id: str | None = None,
    ) -> None:
        """Bind the entity to one device of a site."""
        super().__init__(coordinator)
        self.entity_description = description
        self.site = site
        self.reporter_id = reporter_id
        self._device_key = device_key(site.site_id, description.kind, reporter_id)
        self._attr_unique_id = (
            f"{device_identifier(site.site_id, description.kind, reporter_id)}_{description.key}"
            if description.kind != KIND_SITE
            else f"{site.site_id}_site_{description.key}"
        )
        self._attr_device_info = device_info(
            site, description.kind, reporter_id, coordinator.site_device_ids.get(site.site_id)
        )

    @property
    def values(self) -> dict[str, Any]:
        """The device's current values."""
        return (self.coordinator.data or {}).get(self._device_key, {})

    @property
    def value(self) -> Any:
        """This entity's value."""
        return self.values.get(self.entity_description.key)

    @property
    def has_value(self) -> bool:
        """Whether there is a value yet."""
        return self.value is not None

    @property
    def available(self) -> bool:
        """Unavailable only after several failed polls in a row, never at night."""
        return self.coordinator.available


def entities_for(
    coordinator: SolarEdgeCoordinator,
    descriptions: Sequence[SolarEdgeEntityDescription],
    factory: Callable[[SolarEdgeCoordinator, SiteInfo, Any, str | None], SolarEdgeEntity],
) -> list[SolarEdgeEntity]:
    """One entity per description for every device of that kind."""
    entities: list[SolarEdgeEntity] = []
    for site in coordinator.sites.values():
        for description in descriptions:
            if description.kind == KIND_EV:
                rids: list[str | None] = list(site.ev_chargers)
            elif description.kind == KIND_HLC:
                rids = list(site.load_controllers)
            elif description.kind == KIND_METER and not site.has_meter or description.kind == KIND_BATTERY and not site.has_battery:
                continue
            else:
                rids = [None]
            entities.extend(factory(coordinator, site, description, rid) for rid in rids)
    return entities


@callback
def async_add_when_seen(
    coordinator: SolarEdgeCoordinator,
    platform: str,
    entities: list[SolarEdgeEntity],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the entities; an optional one only once it has a value (or was added before)."""
    registry = er.async_get(coordinator.hass)
    pending: list[SolarEdgeEntity] = []
    ready: list[SolarEdgeEntity] = []
    for entity in entities:
        if (
            not entity.entity_description.optional
            or entity.has_value
            or registry.async_get_entity_id(platform, DOMAIN, entity.unique_id or "")
        ):
            ready.append(entity)
        else:
            pending.append(entity)
    async_add_entities(ready)
    if not pending:
        return

    @callback
    def add_seen() -> None:
        seen = [entity for entity in pending if entity.has_value]
        for entity in seen:
            pending.remove(entity)
        if seen:
            async_add_entities(seen)

    coordinator.config_entry.async_on_unload(coordinator.async_add_listener(add_seen))
