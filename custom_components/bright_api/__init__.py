"""Bright API Home Assistant integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import BrightApiClient, BrightApiError, BrightAuthError
from .const import CONF_VIRTUAL_ENTITY_ID, DOMAIN
from .history import async_interval_history_worker


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

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client,
        "resources": resources,
    }

    # There is deliberately no 15-minute/current-day polling. Bright historical
    # PT30M data is treated as settled daily data and the history worker owns the
    # initial backfill plus the single 04:00 Europe/London refresh cycle.
    entry.async_create_background_task(
        hass,
        async_interval_history_worker(
            hass,
            client,
            resources,
            entry.entry_id,
        ),
        f"{DOMAIN} PT30M interval history",
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Bright API config entry."""
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return True
