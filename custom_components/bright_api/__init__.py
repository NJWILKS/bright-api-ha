"""Bright API Home Assistant integration."""
from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import BrightApiClient, BrightApiError, BrightAuthError
from .const import CONF_VIRTUAL_ENTITY_ID, DOMAIN
from .orchestrator import async_history_and_statistics_worker
from .services import async_setup_services

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up integration-level services."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Bright API from a config entry."""
    client = BrightApiClient(
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        async_get_clientsession(hass),
    )

    try:
        resources = await client.discover_resources(entry.data[CONF_VIRTUAL_ENTITY_ID])
    except (BrightApiError, BrightAuthError) as err:
        raise ConfigEntryNotReady(str(err)) from err

    operation_lock = asyncio.Lock()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client,
        "resources": resources,
        "operation_lock": operation_lock,
    }

    # Presentation entities only read the integration's durable Store data.
    # They never become the source of truth for historical consumption/cost.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # There is deliberately no 15-minute/current-day polling. Bright historical
    # PT30M data is treated as settled daily data: the ledger worker owns the
    # initial backfill and one 04:00 Europe/London refresh, then the projector
    # publishes only durable ledger facts to Home Assistant statistics.
    entry.async_create_background_task(
        hass,
        async_history_and_statistics_worker(
            hass,
            client,
            resources,
            entry.entry_id,
            operation_lock,
        ),
        f"{DOMAIN} PT30M history and statistics",
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Bright API config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok
