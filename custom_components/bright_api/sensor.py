"""Non-historical presentation sensors for Bright API."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import UK_TZ
from .const import (
    CLASSIFIER_ELECTRICITY_CONSUMPTION,
    CLASSIFIER_ELECTRICITY_COST,
    CLASSIFIER_GAS_CONSUMPTION,
    CLASSIFIER_GAS_COST,
    DOMAIN,
)
from .history import IntervalHistoryStore, _parse_utc
from .orchestrator import history_updated_signal
from .tariffs import parse_flat_tariffs, tariff_for_day

COMMODITY_CLASSIFIERS = {
    "electricity": (
        CLASSIFIER_ELECTRICITY_CONSUMPTION,
        CLASSIFIER_ELECTRICITY_COST,
    ),
    "gas": (
        CLASSIFIER_GAS_CONSUMPTION,
        CLASSIFIER_GAS_COST,
    ),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Bright presentation/diagnostic sensors."""
    resources: dict[str, dict[str, Any]] = hass.data[DOMAIN][entry.entry_id]["resources"]
    entities: list[SensorEntity] = []

    for commodity, (usage_classifier, cost_classifier) in COMMODITY_CLASSIFIERS.items():
        if usage_classifier not in resources:
            continue
        entities.extend(
            [
                BrightHistoryStatusSensor(hass, entry, commodity),
                BrightLastSettledIntervalSensor(hass, entry, commodity),
            ]
        )
        if cost_classifier in resources:
            entities.extend(
                [
                    BrightCurrentRateSensor(hass, entry, commodity),
                    BrightStandingChargeSensor(hass, entry, commodity),
                ]
            )

    async_add_entities(entities, update_before_add=True)


class BrightBaseSensor(SensorEntity):
    """Base class for state projected from our own durable stores."""

    _attr_has_entity_name = True

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        commodity: str,
        suffix: str,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._commodity = commodity
        self._repository = IntervalHistoryStore(hass, entry.entry_id)
        self._attr_unique_id = f"{entry.entry_id}_{commodity}_{suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{commodity}")},
            manufacturer="Hildebrand Technology",
            model="Bright / Glowmarkt DCC",
            name=f"Bright {commodity.title()} Meter",
        )

    async def async_added_to_hass(self) -> None:
        """Refresh when durable history/tariff data changes."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                history_updated_signal(self._entry.entry_id),
                self._handle_history_update,
            )
        )

    @callback
    def _handle_history_update(self) -> None:
        """Schedule an entity refresh without causing another Bright API call."""
        self.async_schedule_update_ha_state(force_refresh=True)


class BrightHistoryStatusSensor(BrightBaseSensor):
    """Expose backfill status for diagnostics."""

    _attr_name = "History status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:database-clock"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, commodity: str) -> None:
        super().__init__(hass, entry, commodity, "history_status")

    async def async_update(self) -> None:
        """Read status from the integration ledger metadata."""
        metadata = await self._repository.async_load_metadata()
        state = metadata.get("commodities", {}).get(self._commodity, {})
        value = state.get("status") if isinstance(state, dict) else None
        self._attr_native_value = str(value) if value else "pending"


class BrightLastSettledIntervalSensor(BrightBaseSensor):
    """Expose the newest settled interval timestamp for diagnostics."""

    _attr_name = "Last settled interval"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:timeline-clock"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, commodity: str) -> None:
        super().__init__(hass, entry, commodity, "last_settled_interval")

    async def async_update(self) -> None:
        """Read the last durable Bright interval timestamp."""
        metadata = await self._repository.async_load_metadata()
        state = metadata.get("commodities", {}).get(self._commodity, {})
        raw = state.get("last_interval") if isinstance(state, dict) else None
        self._attr_native_value = _parse_utc(str(raw)) if raw else None


class BrightCurrentRateSensor(BrightBaseSensor):
    """Expose the currently effective proven flat unit rate."""

    _attr_name = "Unit rate"
    _attr_icon = "mdi:cash-multiple"
    _attr_native_unit_of_measurement = "GBP/kWh"
    _attr_suggested_display_precision = 4

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, commodity: str) -> None:
        super().__init__(hass, entry, commodity, "unit_rate")

    async def async_update(self) -> None:
        """Read the current flat tariff from stored API evidence."""
        tariff_state = await self._repository.async_load_tariffs(self._commodity)
        rows = tariff_state.get("rows", [])
        tariffs = parse_flat_tariffs(rows if isinstance(rows, list) else [])
        current = tariff_for_day(tariffs, datetime.now(UK_TZ).date())
        self._attr_available = current is not None
        self._attr_native_value = (
            current.unit_rate_pence_per_kwh / 100.0 if current is not None else None
        )


class BrightStandingChargeSensor(BrightBaseSensor):
    """Expose the currently effective proven flat standing charge."""

    _attr_name = "Standing charge"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "GBP"
    _attr_suggested_display_precision = 4

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, commodity: str) -> None:
        super().__init__(hass, entry, commodity, "standing_charge")

    async def async_update(self) -> None:
        """Read current standing charge from stored API evidence."""
        tariff_state = await self._repository.async_load_tariffs(self._commodity)
        rows = tariff_state.get("rows", [])
        tariffs = parse_flat_tariffs(rows if isinstance(rows, list) else [])
        current = tariff_for_day(tariffs, datetime.now(UK_TZ).date())
        self._attr_available = current is not None
        self._attr_native_value = (
            current.standing_charge_pence_per_day / 100.0 if current is not None else None
        )
