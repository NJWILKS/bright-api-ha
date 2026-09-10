"""Protected live contract for the real Bright interval ledger path."""
from __future__ import annotations

import os
from datetime import timedelta

import aiohttp
import pytest

from custom_components.bright_api.api import BrightApiClient
from custom_components.bright_api.history import COMMODITIES, IntervalHistoryStore, async_populate_interval_history

pytestmark = pytest.mark.live


def _credentials() -> tuple[str, str]:
    username = os.environ.get("GLOWMARKT_USERNAME")
    password = os.environ.get("GLOWMARKT_PASSWORD")
    if not username or not password:
        pytest.skip("Protected Bright credentials are not configured")
    return username, password


@pytest.mark.asyncio
async def test_real_pt30m_ledger_populates_persists_and_resumes(hass) -> None:
    """Exercise the production history path against Bright and HA Store."""
    username, password = _credentials()

    async with aiohttp.ClientSession() as session:
        client = BrightApiClient(username, password, session)
        virtual_entities = await client.get_virtual_entities()
        assert virtual_entities

        virtual_entity_id = next(
            (str(item["veId"]) for item in virtual_entities if item.get("veId")),
            None,
        )
        assert virtual_entity_id is not None

        discovered = await client.discover_resources(virtual_entity_id)

        selected_name = None
        selected_resources = {}
        selected_first = None
        for commodity, (usage_classifier, cost_classifier) in COMMODITIES.items():
            usage_resource = discovered.get(usage_classifier)
            if usage_resource is None:
                continue
            first = await client.get_first_available_reading_time(usage_resource["resource_id"])
            if first is None:
                continue
            selected_name = commodity
            selected_first = first
            selected_resources[usage_classifier] = usage_resource
            if cost_resource := discovered.get(cost_classifier):
                selected_resources[cost_classifier] = cost_resource
            break

        assert selected_name is not None
        assert selected_first is not None

        entry_id = "protected_live_contract"
        first_target = selected_first + timedelta(hours=2)
        first_pass = await async_populate_interval_history(
            hass,
            client,
            selected_resources,
            entry_id,
            now_utc=first_target,
        )

        first_state = first_pass["commodities"][selected_name]
        assert first_state["status"] == "current"
        assert first_state.get("last_interval") is not None

        repository = IntervalHistoryStore(hass, entry_id)
        month = selected_first.astimezone().strftime("%Y-%m")
        # Store partitions use UTC months; recompute explicitly without exposing data.
        month = selected_first.strftime("%Y-%m")
        stored = await repository.async_load_month(selected_name, month)
        assert stored
        assert len(stored) == len(set(stored))

        second_target = selected_first + timedelta(hours=3)
        second_pass = await async_populate_interval_history(
            hass,
            client,
            selected_resources,
            entry_id,
            now_utc=second_target,
        )
        second_state = second_pass["commodities"][selected_name]
        assert second_state["status"] == "current"
        assert second_state["cursor_utc"] != first_state["cursor_utc"]

        stored_after_resume = await repository.async_load_month(selected_name, month)
        assert len(stored_after_resume) >= len(stored)
        assert len(stored_after_resume) == len(set(stored_after_resume))

        cost_classifier = COMMODITIES[selected_name][1]
        if cost_classifier in selected_resources:
            tariff_state = await repository.async_load_tariffs(selected_name)
            assert "rows" in tariff_state
