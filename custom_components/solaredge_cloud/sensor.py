"""Sensors of the SolarEdge Cloud integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import KIND_BATTERY, KIND_EV, KIND_HLC, KIND_INVERTER, KIND_METER, KIND_SITE
from .coordinator import SolarEdgeConfigEntry
from .entity import SolarEdgeEntity, SolarEdgeEntityDescription, async_add_when_seen, entities_for
from .mapping import BATTERY_STATES, EV_CHARGING_STATES, GRID_STATES, INVERTER_STATUSES


@dataclass(frozen=True, kw_only=True)
class SolarEdgeSensorDescription(SolarEdgeEntityDescription, SensorEntityDescription):
    """A SolarEdge sensor."""


def _power(kind: str, key: str = "power", optional: bool = False, translation_key: str | None = None) -> SolarEdgeSensorDescription:
    return SolarEdgeSensorDescription(
        kind=kind,
        key=key,
        translation_key=translation_key,
        optional=optional,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
    )


def _energy(
    kind: str, key: str, *, increasing: bool = True, optional: bool = False
) -> SolarEdgeSensorDescription:
    return SolarEdgeSensorDescription(
        kind=kind,
        key=key,
        translation_key=key,
        optional=optional,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING if increasing else None,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
    )


def _voltage(phase: int) -> SolarEdgeSensorDescription:
    return SolarEdgeSensorDescription(
        kind=KIND_INVERTER,
        key=f"voltage_l{phase}",
        translation_key=f"voltage_l{phase}",
        optional=True,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
        entity_registry_enabled_default=phase == 1,
    )


SENSORS: tuple[SolarEdgeSensorDescription, ...] = (
    # site
    SolarEdgeSensorDescription(
        kind=KIND_SITE,
        key="alerts",
        translation_key="alerts",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    # inverter
    _power(KIND_INVERTER),
    _energy(KIND_INVERTER, "energy_lifetime"),
    _energy(KIND_INVERTER, "energy_today"),
    _energy(KIND_INVERTER, "energy_month"),
    _energy(KIND_INVERTER, "energy_year"),
    _voltage(1),
    _voltage(2),
    _voltage(3),
    SolarEdgeSensorDescription(
        kind=KIND_INVERTER,
        key="status",
        translation_key="inverter_status",
        optional=True,
        device_class=SensorDeviceClass.ENUM,
        options=INVERTER_STATUSES,
    ),
    SolarEdgeSensorDescription(
        kind=KIND_INVERTER,
        key="optimizer_temperature_max",
        translation_key="optimizer_temperature_max",
        optional=True,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=0,
    ),
    # meter
    _power(KIND_METER, "grid_power", translation_key="grid_power"),
    SolarEdgeSensorDescription(
        kind=KIND_METER,
        key="grid_status",
        translation_key="grid_status",
        device_class=SensorDeviceClass.ENUM,
        options=GRID_STATES,
    ),
    _power(KIND_METER, "load_power", translation_key="load_power"),
    _power(KIND_METER, "solar_power", translation_key="solar_power"),
    _energy(KIND_METER, "energy_imported"),
    _energy(KIND_METER, "energy_exported"),
    _energy(KIND_METER, "energy_imported_year"),
    _energy(KIND_METER, "energy_exported_year"),
    # battery
    _power(KIND_BATTERY),
    SolarEdgeSensorDescription(
        kind=KIND_BATTERY,
        key="soc",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=0,
    ),
    SolarEdgeSensorDescription(
        kind=KIND_BATTERY,
        key="state",
        translation_key="battery_state",
        device_class=SensorDeviceClass.ENUM,
        options=BATTERY_STATES,
    ),
    # Integrated from the battery power: SolarEdge's own counters stay at 0.
    _energy(KIND_BATTERY, "energy_charged"),
    _energy(KIND_BATTERY, "energy_discharged"),
    SolarEdgeSensorDescription(
        kind=KIND_BATTERY,
        key="residual_energy",
        translation_key="residual_energy",
        optional=True,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
    ),
    SolarEdgeSensorDescription(
        kind=KIND_BATTERY,
        key="state_of_health",
        translation_key="state_of_health",
        optional=True,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # EV charger
    _power(KIND_EV),
    SolarEdgeSensorDescription(
        kind=KIND_EV,
        key="charging_state",
        translation_key="ev_charging_state",
        device_class=SensorDeviceClass.ENUM,
        options=EV_CHARGING_STATES,
    ),
    _energy(KIND_EV, "session_energy", optional=True),
    SolarEdgeSensorDescription(
        kind=KIND_EV,
        key="session_duration",
        translation_key="session_duration",
        optional=True,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.MINUTES,
    ),
    SolarEdgeSensorDescription(
        kind=KIND_EV,
        key="charge_mode",
        translation_key="charge_mode",
        optional=True,
        device_class=SensorDeviceClass.ENUM,
        options=["auto", "manual"],
    ),
    SolarEdgeSensorDescription(
        kind=KIND_EV, key="vehicle", translation_key="vehicle", optional=True
    ),
    _energy(KIND_EV, "energy_charged", optional=True),
    # home load controller
    _power(KIND_HLC, optional=True),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolarEdgeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the sensors of every site."""
    coordinator = entry.runtime_data
    async_add_when_seen(
        coordinator,
        "sensor",
        entities_for(coordinator, SENSORS, SolarEdgeSensor),
        async_add_entities,
    )


class SolarEdgeSensor(SolarEdgeEntity, SensorEntity):
    """A SolarEdge reading."""

    entity_description: SolarEdgeSensorDescription

    @property
    def native_value(self) -> Any:
        """The reading."""
        return self.value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """A load controller's power is estimated unless it has a meter."""
        if self.entity_description.kind == KIND_HLC and "power_measured" in self.values:
            return {"estimated": not self.values["power_measured"]}
        return None
