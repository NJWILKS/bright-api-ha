from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from custom_components.bright_api.api import BrightApiClient
from custom_components.bright_api.history import (
    IntervalHistoryStore,
    _floor_half_hour,
    _merge_rows,
    async_populate_interval_history,
)


def test_floor_half_hour_uses_completed_interval_boundary() -> None:
    assert _floor_half_hour(datetime(2026, 9, 10, 10, 14, 59, tzinfo=UTC)) == datetime(
        2026, 9, 10, 10, 0, tzinfo=UTC
    )
    assert _floor_half_hour(datetime(2026, 9, 10, 10, 44, tzinfo=UTC)) == datetime(
        2026, 9, 10, 10, 30, tzinfo=UTC
    )


def test_merge_rows_preserves_zero_and_distinct_dst_instants() -> None:
    start = datetime(2025, 10, 26, 0, 0, tzinfo=UTC)
    end = datetime(2025, 10, 26, 3, 0, tzinfo=UTC)
    usage = [
        (datetime(2025, 10, 26, 0, 30, tzinfo=UTC), 0.0),
        (datetime(2025, 10, 26, 1, 30, tzinfo=UTC), 0.25),
    ]
    cost = [
        (datetime(2025, 10, 26, 0, 30, tzinfo=UTC), 0.0),
        (datetime(2025, 10, 26, 1, 30, tzinfo=UTC), 6.1),
    ]

    records = _merge_rows(usage, cost, start, end)

    assert len(records) == 2
    assert records[0]["usage_kwh"] == 0.0
    assert records[0]["cost_pence"] == 0.0
    assert records[0]["timestamp"] != records[1]["timestamp"]


@pytest.mark.asyncio
async def test_store_upsert_is_idempotent(hass) -> None:
    repository = IntervalHistoryStore(hass, "entry-1")
    record = {
        "timestamp": "2026-01-01T00:00:00+00:00",
        "usage_kwh": 0.2,
        "cost_pence": 4.8,
    }

    await repository.async_upsert_intervals("electricity", [record])
    await repository.async_upsert_intervals("electricity", [record])

    month = await repository.async_load_month("electricity", "2026-01")
    assert list(month.values()) == [record]


@pytest.mark.asyncio
async def test_population_resumes_from_durable_cursor(hass) -> None:
    client = AsyncMock(spec=BrightApiClient)
    resources = {
        "electricity.consumption": {"resource_id": "usage-id"},
        "electricity.consumption.cost": {"resource_id": "cost-id"},
    }
    first = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    client.get_first_available_reading_time.return_value = first
    client.get_tariffs.return_value = [
        {"effectiveDate": "2026-01-01 00:00:00", "plan": [{"planDetail": []}]}
    ]

    async def readings(resource_id, start, end, *, period="PT30M"):
        assert period == "PT30M"
        values = {
            "usage-id": {
                datetime(2026, 1, 1, 0, 0, tzinfo=UTC): 0.1,
                datetime(2026, 1, 1, 0, 30, tzinfo=UTC): 0.2,
                datetime(2026, 1, 1, 1, 0, tzinfo=UTC): 0.3,
            },
            "cost-id": {
                datetime(2026, 1, 1, 0, 0, tzinfo=UTC): 2.4,
                datetime(2026, 1, 1, 0, 30, tzinfo=UTC): 4.8,
                datetime(2026, 1, 1, 1, 0, tzinfo=UTC): 7.2,
            },
        }
        return [
            (timestamp, value)
            for timestamp, value in values[resource_id].items()
            if start <= timestamp < end
        ]

    client.get_readings.side_effect = readings

    first_pass = await async_populate_interval_history(
        hass,
        client,
        resources,
        "entry-1",
        now_utc=datetime(2026, 1, 1, 1, 0, tzinfo=UTC),
    )

    electricity = first_pass["commodities"]["electricity"]
    assert electricity["status"] == "current"
    assert electricity["cursor_utc"] == "2026-01-01T01:00:00+00:00"
    assert client.get_first_available_reading_time.await_count == 2

    second_pass = await async_populate_interval_history(
        hass,
        client,
        resources,
        "entry-1",
        now_utc=datetime(2026, 1, 1, 1, 30, tzinfo=UTC),
    )

    electricity = second_pass["commodities"]["electricity"]
    assert electricity["cursor_utc"] == "2026-01-01T01:30:00+00:00"
    assert client.get_first_available_reading_time.await_count == 2

    repository = IntervalHistoryStore(hass, "entry-1")
    month = await repository.async_load_month("electricity", "2026-01")
    assert [row["usage_kwh"] for row in month.values()] == [0.1, 0.2, 0.3]
    assert [row["cost_pence"] for row in month.values()] == [2.4, 4.8, 7.2]

    tariff_state = await repository.async_load_tariffs("electricity")
    assert tariff_state["rows"] == client.get_tariffs.return_value
