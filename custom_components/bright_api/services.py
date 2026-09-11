"""Home Assistant actions for Bright API."""
from __future__ import annotations

import asyncio

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.service import async_register_admin_service

from .api import BrightApiError, BrightAuthError
from .const import DOMAIN
from .rebuild import async_reset_and_rebuild
from .statistics import StatisticsProjectionError

SERVICE_RESET_REBUILD = "reset_rebuild"
ATTR_ENTRY = "entry"


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register destructive Bright actions as admin-only services."""
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_RESET_REBUILD,
        async_handle_reset_rebuild,
        schema=vol.Schema({vol.Required(ATTR_ENTRY): str}),
    )


async def async_handle_reset_rebuild(call: ServiceCall) -> None:
    """Reset and rebuild one loaded Bright config entry."""
    entry_id = str(call.data[ATTR_ENTRY])
    runtime = call.hass.data.get(DOMAIN, {}).get(entry_id)
    if not isinstance(runtime, dict):
        raise ServiceValidationError("The selected Bright API config entry is not loaded")

    client = runtime.get("client")
    resources = runtime.get("resources")
    operation_lock = runtime.get("operation_lock")
    if client is None or not isinstance(resources, dict) or not isinstance(operation_lock, asyncio.Lock):
        raise ServiceValidationError("The selected Bright API config entry is not ready")

    async with operation_lock:
        try:
            await async_reset_and_rebuild(
                call.hass,
                client,
                resources,
                entry_id,
            )
        except (BrightApiError, BrightAuthError, StatisticsProjectionError) as err:
            raise HomeAssistantError(f"Bright reset/rebuild failed: {err}") from err
