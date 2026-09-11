"""Config flow for Bright API."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import BrightApiClient, BrightApiError, BrightAuthError
from .const import CONF_VIRTUAL_ENTITY_ID, DOMAIN


class BrightApiConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Create a Bright API config entry."""

    VERSION = 1

    def __init__(self) -> None:
        self._credentials: dict[str, str] = {}
        self._sites: dict[str, str] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            username = str(user_input[CONF_USERNAME])
            password = str(user_input[CONF_PASSWORD])
            client = BrightApiClient(
                username,
                password,
                async_get_clientsession(self.hass),
            )
            try:
                await client.authenticate()
                virtual_entities = await client.get_virtual_entities()
            except BrightAuthError:
                errors["base"] = "invalid_auth"
            except BrightApiError:
                errors["base"] = "cannot_connect"
            else:
                self._credentials = {
                    CONF_USERNAME: username,
                    CONF_PASSWORD: password,
                }
                self._sites = {
                    str(item["veId"]): str(item.get("name") or item.get("veId"))
                    for item in virtual_entities
                    if isinstance(item, dict) and item.get("veId")
                }
                if not self._sites:
                    errors["base"] = "no_sites"
                elif len(self._sites) == 1:
                    site_id = next(iter(self._sites))
                    return await self._create_entry(site_id)
                else:
                    return await self.async_step_site()

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_site(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return await self._create_entry(str(user_input[CONF_VIRTUAL_ENTITY_ID]))

        schema = vol.Schema(
            {
                vol.Required(CONF_VIRTUAL_ENTITY_ID): vol.In(self._sites),
            }
        )
        return self.async_show_form(step_id="site", data_schema=schema)

    async def _create_entry(self, site_id: str):
        await self.async_set_unique_id(site_id)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=self._sites.get(site_id, "Bright Smart Meter"),
            data={**self._credentials, CONF_VIRTUAL_ENTITY_ID: site_id},
        )
