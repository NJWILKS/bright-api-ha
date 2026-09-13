from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bright_api.config_flow import BrightApiConfigFlow
from custom_components.bright_api.const import CONF_VIRTUAL_ENTITY_ID, DOMAIN


@pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")
@pytest.mark.asyncio
async def test_history_maintenance_options_flow(hass) -> None:
    # Importing the ConfigFlow class registers the domain's options-flow factory.
    assert BrightApiConfigFlow.VERSION == 1
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="entry-1",
        data={
            CONF_USERNAME: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_VIRTUAL_ENTITY_ID: "site-1",
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "init"
    assert result["menu_options"] == ["fetch_history", "refresh_all"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"next_step_id": "fetch_history"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "fetch_history"
    assert {str(key) for key in result["data_schema"].schema} == {"start_date", "end_date"}

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": object(),
        "resources": {"electricity.consumption": {"resource_id": "usage-id"}},
        "operation_lock": asyncio.Lock(),
    }
    with patch(
        "custom_components.bright_api.config_flow.async_refresh_history_range",
        new=AsyncMock(),
    ) as refresh:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"start_date": "2026-09-10", "end_date": "2026-09-11"},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    refresh.assert_awaited_once()
