from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.bright_api import services
from custom_components.bright_api.api import BrightApiClient
from custom_components.bright_api.const import DOMAIN
from custom_components.bright_api.history import IntervalHistoryStore
from custom_components.bright_api.services import (
    ATTR_ENTRY,
    SERVICE_SYNC_NOW,
    async_setup_services,
)
from custom_components.bright_api.statistics import (
    MEASURE_CONSUMPTION,
    StatisticsProjectionStore,
)
from custom_components.bright_api.sync import (
    _recent_reconcile_start,
    async_project_reconciled_history,
    async_reconcile_recent_history,
)


def test_recent_reconcile_start_uses_local_billing_midnight() -> None:
    target_end = datetime(2026, 9, 12, 23, 0, tzinfo=UTC)

    assert _recent_reconcile_start(target_end) == datetime(
        2026,
        9,
        9,
        23,
        0,
        tzinfo=UTC,
    )


@pytest.mark.asyncio
async def test_recent_reconciliation_repairs_rows_after_cursor_already_advanced(hass) -> None:
    entry_id = "entry-1"
    repository = IntervalHistoryStore(hass, entry_id)
    metadata = {
        "schema_version": 1,
        "commodities": {
            "electricity": {
                "first_interval": "2026-09-01T00:00:00+00:00",
                "cursor_utc": "2026-09-13T00:00:00+00:00",
                "daily_cost_cursor_day": "2026-09-13",
                "last_interval": "2026-09-10T23:30:00+00:00",
                "status": "current",
            }
        },
    }
    await repository.async_save_metadata(metadata)

    client = AsyncMock(spec=BrightApiClient)
    resources = {
        "electricity.consumption": {"resource_id": "usage-id"},
        "electricity.consumption.cost": {"resource_id": "cost-id"},
    }
    late_timestamp = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)

    async def readings(resource_id, start, end, *, period="PT30M"):
        if period == "P1D":
            return [(datetime(2026, 9, 11, 23, 0, tzinfo=UTC), 123.0)]
        if resource_id == "usage-id":
            return [(late_timestamp, 0.42)]
        if resource_id == "cost-id":
            return [(late_timestamp, 8.4)]
        return []

    client.get_readings.side_effect = readings

    repaired, replay_from = await async_reconcile_recent_history(
        hass,
        client,
        resources,
        entry_id,
        metadata=metadata,
        now_utc=datetime(2026, 9, 13, 5, 0, tzinfo=UTC),
    )

    assert replay_from == datetime(2026, 9, 9, 23, 0, tzinfo=UTC)
    month = await repository.async_load_month("electricity", "2026-09")
    assert month[late_timestamp.isoformat()]["usage_kwh"] == pytest.approx(0.42)
    assert month[late_timestamp.isoformat()]["cost_pence"] == pytest.approx(8.4)

    daily = await repository.async_load_daily_cost_month("electricity", "2026-09")
    assert daily["2026-09-12"]["cost_pence"] == pytest.approx(123.0)
    assert repaired["commodities"]["electricity"]["last_interval"] == late_timestamp.isoformat()


@pytest.mark.asyncio
async def test_reconciled_projection_replays_recent_tail_with_correct_prefix(hass) -> None:
    entry_id = "entry-1"
    history = IntervalHistoryStore(hass, entry_id)
    first = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
    replay_from = datetime(2026, 9, 2, 0, 0, tzinfo=UTC)
    end = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)

    await history.async_upsert_intervals(
        "electricity",
        [
            {
                "timestamp": first.isoformat(),
                "usage_kwh": 1.0,
                "cost_pence": None,
            },
            {
                "timestamp": replay_from.isoformat(),
                "usage_kwh": 2.0,
                "cost_pence": None,
            },
        ],
    )
    metadata = {
        "schema_version": 1,
        "commodities": {
            "electricity": {
                "first_interval": first.isoformat(),
                "cursor_utc": end.isoformat(),
                "status": "current",
            }
        },
    }
    await history.async_save_metadata(metadata)

    projection = StatisticsProjectionStore(hass, entry_id)
    await projection.async_save(
        {
            "schema_version": 1,
            "commodities": {
                "electricity": {
                    "cursor_utc": end.isoformat(),
                    "running": {MEASURE_CONSUMPTION: 1.0},
                }
            },
        }
    )

    recorder = SimpleNamespace(async_block_till_done=AsyncMock())
    with (
        patch("custom_components.bright_api.sync.get_instance", return_value=recorder),
        patch("custom_components.bright_api.sync.async_add_external_statistics") as add_stats,
    ):
        state = await async_project_reconciled_history(
            hass,
            entry_id,
            history_metadata=metadata,
            replay_from=replay_from,
        )

    recorder.async_block_till_done.assert_awaited_once()
    assert add_stats.call_count == 1
    statistics = add_stats.call_args.args[2]
    assert len(statistics) == 1
    assert statistics[0]["state"] == pytest.approx(2.0)
    assert statistics[0]["sum"] == pytest.approx(3.0)
    assert state["commodities"]["electricity"]["running"][MEASURE_CONSUMPTION] == pytest.approx(
        3.0
    )


@pytest.mark.asyncio
async def test_sync_now_action_targets_loaded_entry(hass, monkeypatch) -> None:
    operation_lock = asyncio.Lock()
    client = object()
    resources = {"electricity.consumption": {"resource_id": "usage-id"}}
    hass.data.setdefault(DOMAIN, {})["entry-1"] = {
        "client": client,
        "resources": resources,
        "operation_lock": operation_lock,
    }
    sync_now = AsyncMock()
    monkeypatch.setattr(services, "async_sync_history_and_statistics_once", sync_now)

    async_setup_services(hass)
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SYNC_NOW,
        {ATTR_ENTRY: "entry-1"},
        blocking=True,
    )

    sync_now.assert_awaited_once_with(
        hass,
        client,
        resources,
        "entry-1",
        operation_lock,
    )
