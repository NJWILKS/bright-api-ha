"""Config flow for Bright API."""
from __future__ import annotations

import asyncio
from datetime import date, time, timedelta
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import BrightApiClient, BrightApiError, BrightAuthError
from .const import (
    CONF_HISTORY_REFRESH_TIME,
    CONF_VIRTUAL_ENTITY_ID,
    DEFAULT_HISTORY_REFRESH_TIME,
    DOMAIN,
)
from .history import configured_history_refresh_time
from .manual_history import (
    ManualHistoryError,
    async_refresh_all_history,
    async_refresh_history_range,
    latest_settled_billing_day,
)
from .statistics import StatisticsProjectionError

CONF_START_DATE = "start_date"
CONF_END_DATE = "end_date"


class BrightApiConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Create a Bright API config entry."""

    VERSION = 1

    def __init__(self) -> None:
        self._credentials: dict[str, str] = {}
        self._sites: dict[str, str] = {}

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> BrightApiOptionsFlow:
        """Return the Bright history-maintenance options flow."""
        return BrightApiOptionsFlow()

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


class BrightApiOptionsFlow(config_entries.OptionsFlow):
    """Offer non-destructive Bright history maintenance from Configure."""

    def _runtime(self) -> tuple[Any, dict[str, dict[str, Any]], asyncio.Lock] | None:
        runtime = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        if not isinstance(runtime, dict):
            return None
        client = runtime.get("client")
        resources = runtime.get("resources")
        operation_lock = runtime.get("operation_lock")
        if (
            client is None
            or not isinstance(resources, dict)
            or not isinstance(operation_lock, asyncio.Lock)
        ):
            return None
        return client, resources, operation_lock

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Show history maintenance choices."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["fetch_history", "refresh_all", "refresh_time"],
        )

    async def async_step_refresh_time(self, user_input: dict[str, Any] | None = None):
        """Set the daily Bright history refresh time in Europe/London."""
        if user_input is not None:
            refresh_time = user_input[CONF_HISTORY_REFRESH_TIME]
            try:
                normalized = time.fromisoformat(str(refresh_time)).isoformat(
                    timespec="seconds"
                )
            except ValueError:
                return self.async_show_form(
                    step_id="refresh_time",
                    data_schema=vol.Schema(
                        {
                            vol.Required(
                                CONF_HISTORY_REFRESH_TIME,
                                default=DEFAULT_HISTORY_REFRESH_TIME,
                            ): selector.TimeSelector()
                        }
                    ),
                    errors={"base": "invalid_time"},
                )
            return self.async_create_entry(
                title="",
                data={
                    **self.config_entry.options,
                    CONF_HISTORY_REFRESH_TIME: normalized,
                },
            )

        default_time = configured_history_refresh_time(self.config_entry.options).isoformat(
            timespec="seconds"
        )
        return self.async_show_form(
            step_id="refresh_time",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HISTORY_REFRESH_TIME,
                        default=default_time,
                    ): selector.TimeSelector()
                }
            ),
        )

    async def async_step_fetch_history(self, user_input: dict[str, Any] | None = None):
        """Fetch an inclusive billing-date range and replay affected statistics."""
        errors: dict[str, str] = {}
        refresh_time = configured_history_refresh_time(self.config_entry.options)
        settled_day = latest_settled_billing_day(refresh_time=refresh_time)
        default_start = settled_day - timedelta(days=6)

        if user_input is not None:
            try:
                start_day = date.fromisoformat(str(user_input[CONF_START_DATE]))
                end_day = date.fromisoformat(str(user_input[CONF_END_DATE]))
            except ValueError:
                errors["base"] = "invalid_range"
            else:
                if start_day > end_day or end_day > settled_day:
                    errors["base"] = "invalid_range"
                elif (runtime := self._runtime()) is None:
                    errors["base"] = "not_ready"
                else:
                    client, resources, operation_lock = runtime
                    try:
                        await async_refresh_history_range(
                            self.hass,
                            client,
                            resources,
                            self.config_entry.entry_id,
                            operation_lock,
                            start_day,
                            end_day,
                            refresh_time=refresh_time,
                        )
                    except (
                        ManualHistoryError,
                        BrightApiError,
                        BrightAuthError,
                        StatisticsProjectionError,
                    ):
                        errors["base"] = "refresh_failed"
                    else:
                        return self.async_create_entry(data=dict(self.config_entry.options))

        return self.async_show_form(
            step_id="fetch_history",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_START_DATE,
                        default=default_start.isoformat(),
                    ): selector.DateSelector(),
                    vol.Required(
                        CONF_END_DATE,
                        default=settled_day.isoformat(),
                    ): selector.DateSelector(),
                }
            ),
            errors=errors,
        )

    async def async_step_refresh_all(self, user_input: dict[str, Any] | None = None):
        """Confirm and run a non-destructive refresh of all retrievable history."""
        errors: dict[str, str] = {}
        refresh_time = configured_history_refresh_time(self.config_entry.options)
        if user_input is not None:
            if (runtime := self._runtime()) is None:
                errors["base"] = "not_ready"
            else:
                client, resources, operation_lock = runtime
                try:
                    await async_refresh_all_history(
                        self.hass,
                        client,
                        resources,
                        self.config_entry.entry_id,
                        operation_lock,
                        refresh_time=refresh_time,
                    )
                except (
                    ManualHistoryError,
                    BrightApiError,
                    BrightAuthError,
                    StatisticsProjectionError,
                ):
                    errors["base"] = "refresh_failed"
                else:
                    return self.async_create_entry(data=dict(self.config_entry.options))

        return self.async_show_form(
            step_id="refresh_all",
            data_schema=vol.Schema({}),
            errors=errors,
        )
