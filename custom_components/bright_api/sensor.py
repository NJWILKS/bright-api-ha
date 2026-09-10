"""Presentation sensors for Bright API."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTRIBUTION,
    CLASSIFIER_ELECTRICITY_CONSUMPTION,
    CLASSIFIER_ELECTRICITY_COST,
    CLASSIFIER_GAS_CONSUMPTION,
    CLASSIFIER_GAS_COST,
    CONF_VIRTUAL_ENTITY_ID,
    DOMAIN,
)
from .coordinator import BrightDataCoordinator


@dataclass(frozen=True)
class BrightSensorDescription:
    """Description of a current-day sensor."""

    classifier: str
    name: str
    icon: str
    device_class: SensorDeviceClass
    unit: str
    pence_to_gbp: bool = False


DESCRIPTIONS = (
    BrightSensorDescription(
        CLASSIFIER_ELECTRICITY_CONSUMPTION,
        "Electricity Consumption Today",
        "mdi:flash",
        SensorDeviceClass.ENERGY,
        UnitOfEnergy.KILO_WATT_HOUR,
    ),
    BrightSensorDescription(
        CLASSIFIER_ELECTRICITY_COST,
        "Electricity Cost Today",
        "mdi:currency-gbp",
        SensorDeviceClass.MONETARY,
        "GBP",
        True,
    ),
    BrightSensorDescription(
        CLASSIFIER_GAS_CONSUMPTION,
        "Gas Consumption Today",
        "mdi:fire",
        SensorDeviceClass.ENERGY,
        UnitOfEnergy.KILO_WATT_HOUR,
    ),
    BrightSensorDescription(
        CLASSIFIER_GAS_COST,
        "Gas Cost Today",
        "mdi:currency-gbp",
        SensorDeviceClass.MONETARY,
        "GBP",
        True,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: BrightDataCoordinator = hass.data[DOMAIN][entry.entry_id]
    site_id = str(entry.data[CONF_VIRTUAL_ENTITY_ID])
    async_add_entities(
        BrightSensor(coordinator, description, site_id)
        for description in DESCRIPTIONS
        if description.classifier in coordinator.resources
    )


class BrightSensor(CoordinatorEntity[BrightDataCoordinator], SensorEntity):
    """One current-day Bright API sensor."""

    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: BrightDataCoordinator,
        description: BrightSensorDescription,
        site_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._description = description
        self._attr_unique_id = f"{site_id}_{description.classifier}"
        self._attr_name = description.name
        self._attr_icon = description.icon
        self._attr_device_class = description.device_class
        self._attr_native_unit_of_measurement = description.unit
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, site_id)},
            name="Bright Smart Meter",
            manufacturer="Hildebrand Technology",
            model="Bright / Glowmarkt",
        )

    @property
    def native_value(self) -> float | None:
        data: dict[str, Any] = self.coordinator.data or {}
        value = data.get("values", {}).get(self._description.classifier)
        if value is None:
            return None
        if self._description.pence_to_gbp:
            return round(float(value) / 100.0, 2)
        return round(float(value), 3)
