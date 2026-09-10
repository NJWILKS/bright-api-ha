"""Constants for the Bright API integration."""
from __future__ import annotations

from typing import Final

DOMAIN: Final = "bright_api"
API_BASE: Final = "https://api.glowmarkt.com/api/v0-1"
APP_ID: Final = "b0f1b774-a586-4f72-9edd-27ead8aa7a8d"
ATTRIBUTION: Final = "Data provided by Hildebrand Technology via Glowmarkt API"

CONF_VIRTUAL_ENTITY_ID: Final = "virtual_entity_id"

CLASSIFIER_ELECTRICITY_CONSUMPTION: Final = "electricity.consumption"
CLASSIFIER_ELECTRICITY_COST: Final = "electricity.consumption.cost"
CLASSIFIER_GAS_CONSUMPTION: Final = "gas.consumption"
CLASSIFIER_GAS_COST: Final = "gas.consumption.cost"

SUPPORTED_CLASSIFIERS: Final = (
    CLASSIFIER_ELECTRICITY_CONSUMPTION,
    CLASSIFIER_ELECTRICITY_COST,
    CLASSIFIER_GAS_CONSUMPTION,
    CLASSIFIER_GAS_COST,
)
