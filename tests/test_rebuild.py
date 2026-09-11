from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from custom_components.bright_api.const import DOMAIN
from custom_components.bright_api.history import IntervalHistoryStore
from custom_components.bright_api import rebuild, services
from custom_components.bright_api.rebuild import async_clear_interval_history
from custom_components.bright_api.services import (
    ATTR_ENTRY,
    SERVICE_RESET_REBUILD,
    async_setup_services,
)


@pytest.mark.asyncio
async def test_clear_interval_history_removes_only_owned_store_data(hass) -> None:
    repository = IntervalHistoryStore(hass, "entry-1")
    january = {
        "timestamp": "2026-01-31T23:30:00+00:00",
        "usage_kwh": 0.2,
        "cost_pence": 4.8,
    }
    february = {
        "timestamp": "2026-02-01T00:00:00+00:00",
        "usage_kwh": 0.3,
        "cost_pence": 7.2,
    }
    await repository.async_upsert_intervals("electricity", [january, february])
    await repository.async_save_tariffs(
        "electricity",
        [{"effectiveDate": "2026-01-01 00:00:00"}],
        datetime(2026, 2, 2, tzinfo=UTC),
    )
    await repository.async_save_metadata(
        {
            "schema_version": 1,
            "commodities": {
                "electricity": {
                    "first_interval": january["timestamp"],
                    "last_interval": february["timestamp"],
                    "cursor_utc": "2026-02-02T00:00:00+00:00",
                    "status": "current",
                }
            },
        }
    )

    other = IntervalHistoryStore(hass, "entry-2")
    await other.async_upsert_intervals("electricity", [january])
    await other.async_save_metadata(
        {
            "schema_version": 1,
            "commodities": {
                "electricity": {
                    "first_interval": january["timestamp"],
                    "cursor_utc": "2026-02-01T00:00:00+00:00",
                }
            },
        }
    )

    await async_clear_interval_history(hass, "entry-1")

    assert await repository.async_load_month("electricity", "2026-01") == {}
    assert await repository.async_load_month("electricity", "2026-02") == {}
    assert await repository.async_load_tariffs("electricity") == {}
    assert (await repository.async_load_metadata())["commodities"] == {}

    assert await other.async_load_month("electricity", "2026-01")
    assert (await other.async_load_metadata())["commodities"]["electricity"]


@pytest.mark.asyncio
async def test_reset_and_rebuild_clears_then_refetches_then_projects(hass, monkeypatch) -> None:
    order: list[str] = []
    metadata = {"schema_version": 1, "commodities": {}}
    projection = {"schema_version": 1, "commodities": {}}

    async def clear_statistics(_hass, _entry_id):
        order.append("statistics")

    async def clear_history(_hass, _entry_id):
        order.append("history")

    async def populate(_hass, _client, _resources, _entry_id, *, now_utc=None):
        assert now_utc is not None
        order.append("populate")
        return metadata

    async def project(_hass, _entry_id, *, history_metadata=None):
        assert history_metadata is metadata
        order.append("project")
        return projection

    monkeypatch.setattr(rebuild, "async_clear_projected_statistics", clear_statistics)
    monkeypatch.setattr(rebuild, "async_clear_interval_history", clear_history)
    monkeypatch.setattr(rebuild, "async_populate_interval_history", populate)
    monkeypatch.setattr(rebuild, "async_project_interval_history", project)

    result = await rebuild.async_reset_and_rebuild(hass, object(), {}, "entry-1")

    assert order == ["statistics", "history", "populate", "project"]
    assert result == {"history": metadata, "projection": projection}


@pytest.mark.asyncio
async def test_reset_rebuild_action_targets_loaded_entry(hass, monkeypatch) -> None:
    operation_lock = asyncio.Lock()
    client = object()
    resources = {"electricity.consumption": {"resource_id": "usage-id"}}
    hass.data.setdefault(DOMAIN, {})["entry-1"] = {
        "client": client,
        "resources": resources,
        "operation_lock": operation_lock,
    }
    reset = AsyncMock()
    monkeypatch.setattr(services, "async_reset_and_rebuild", reset)

    async_setup_services(hass)
    await hass.services.async_call(
        DOMAIN,
        SERVICE_RESET_REBUILD,
        {ATTR_ENTRY: "entry-1"},
        blocking=True,
    )

    reset.assert_awaited_once_with(hass, client, resources, "entry-1")
