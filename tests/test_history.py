from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from custom_components.bright_api.api import BrightApiClient
from custom_components.bright_api.history import (
    IntervalHistoryStore,
    _history_due,
    _latest_settled_boundary,
    _merge_rows,
    _next_refresh_utc,
    async_populate_interval_history,
)


def test_settled_boundary_waits_until_0400_uk_time() -> None:
    assert _latest_settled_boundary(datetime(2026, 9, 10, 2, 59, tzinfo=UTC)) == datetime(
        2026, 9, 8, 23, 0, tzinfo=UTC
    )
    assert _latest_settled_boundary(datetime(2026, 9, 10, 3, 0, tzinfo=UTC)) == datetime(
        2026, 9, 9, 23, 0, tzinfo=UTC
    )
    assert _latest_settled_boundary(datetime(2026, 1, 10, 3, 59, tzinfo=UTC)) == datetime(
        2026, 1, 9, 0, 0, tzinfo=UTC
    )
    assert _latest_settled_boundary(datetime(2026, 1, 10, 4, 0, tzinfo=UTC)) == datetime(
        2026, 1, 10, 0, 0, tzinfo=UTC
    )


def test_next_refresh_tracks_0400_europe_london_across_dst() -> None:
    assert _next_refresh_utc(datetime(2026, 9, 10, 2, 30, tzinfo=UTC)) == datetime(
        2026, 9, 10, 3, 0, tzinfo=UTC
    )
    assert _next_refresh_utc(datetime(2026, 9, 10, 3, 30, tzinfo=UTC)) == datetime(
        2026, 9, 11, 3, 0, tzinfo=UTC
    )
    assert _next_refresh_utc(datetime(2025, 10, 25, 5, 0, tzinfo=UTC)) == datetime(
        2025, 10, 26, 4, 0, tzinfo=UTC
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
async def test_daily_cost_store_is_idempotent_by_billing_day(hass) -> None:
    repository = IntervalHistoryStore(hass, "entry-1")
    row = (datetime(2026, 9, 1, 0, 0, tzinfo=UTC), 74.0)

    await repository.async_upsert_daily_costs("electricity", [row])
    await repository.async_upsert_daily_costs("electricity", [row])

    month = await repository.async_load_daily_cost_month("electricity", "2026-09")
    assert list(month) == ["2026-09-01"]
    assert month["2026-09-01"]["cost_pence"] == 74.0


def test_history_due_checks_pt30m_and_p1d_cursors() -> None:
    resources = {
        "electricity.consumption": {"resource_id": "usage-id"},
        "electricity.consumption.cost": {"resource_id": "cost-id"},
    }
    target = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)

    assert _history_due({}, resources, target)
    assert _history_due(
        {
            "commodities": {
                "electricity": {"cursor_utc": "2026-01-01T00:00:00+00:00"}
            }
        },
        resources,
        target,
    )
    assert _history_due(
        {
            "commodities": {
                "electricity": {
                    "cursor_utc": "2026-01-02T00:00:00+00:00",
                    "daily_cost_cursor_day": "2026-01-01",
                }
            }
        },
        resources,
        target,
    )
    assert not _history_due(
        {
            "commodities": {
                "electricity": {
                    "cursor_utc": "2026-01-02T00:00:00+00:00",
                    "daily_cost_cursor_day": "2026-01-02",
                }
            }
        },
        resources,
        target,
    )


@pytest.mark.asyncio
async def test_population_resumes_pt30m_and_daily_cost_from_durable_cursors(hass) -> None:
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
        if period == "P1D":
            values = {
                datetime(2026, 1, 1, 0, 0, tzinfo=UTC): 74.0,
                datetime(2026, 1, 2, 0, 0, tzinfo=UTC): 80.0,
            }
        else:
            values_by_resource = {
                "usage-id": {
                    datetime(2026, 1, 1, 0, 0, tzinfo=UTC): 0.1,
                    datetime(2026, 1, 1, 0, 30, tzinfo=UTC): 0.2,
                    datetime(2026, 1, 1, 1, 0, tzinfo=UTC): 0.3,
                    datetime(2026, 1, 2, 0, 0, tzinfo=UTC): 0.4,
                },
                "cost-id": {
                    datetime(2026, 1, 1, 0, 0, tzinfo=UTC): 2.4,
                    datetime(2026, 1, 1, 0, 30, tzinfo=UTC): 4.8,
                    datetime(2026, 1, 1, 1, 0, tzinfo=UTC): 7.2,
                    datetime(2026, 1, 2, 0, 0, tzinfo=UTC): 9.6,
                },
            }
            values = values_by_resource[resource_id]
        return [
            (timestamp, value)
            for timestamp, value in values.items()
            if start <= timestamp < end
        ]

    client.get_readings.side_effect = readings

    first_pass = await async_populate_interval_history(
        hass,
        client,
        resources,
        "entry-1",
        now_utc=datetime(2026, 1, 2, 5, 0, tzinfo=UTC),
    )

    electricity = first_pass["commodities"]["electricity"]
    assert electricity["status"] == "current"
    assert electricity["cursor_utc"] == "2026-01-02T00:00:00+00:00"
    assert electricity["daily_cost_cursor_day"] == "2026-01-02"
    assert client.get_first_available_reading_time.await_count == 1

    second_pass = await async_populate_interval_history(
        hass,
        client,
        resources,
        "entry-1",
        now_utc=datetime(2026, 1, 3, 5, 0, tzinfo=UTC),
    )

    electricity = second_pass["commodities"]["electricity"]
    assert electricity["cursor_utc"] == "2026-01-03T00:00:00+00:00"
    assert electricity["daily_cost_cursor_day"] == "2026-01-03"
    assert client.get_first_available_reading_time.await_count == 1

    repository = IntervalHistoryStore(hass, "entry-1")
    month = await repository.async_load_month("electricity", "2026-01")
    assert [row["usage_kwh"] for row in month.values()] == [0.1, 0.2, 0.3, 0.4]
    assert [row["cost_pence"] for row in month.values()] == [2.4, 4.8, 7.2, 9.6]

    daily = await repository.async_load_daily_cost_month("electricity", "2026-01")
    assert daily["2026-01-01"]["cost_pence"] == 74.0
    assert daily["2026-01-02"]["cost_pence"] == 80.0

    tariff_state = await repository.async_load_tariffs("electricity")
    assert tariff_state["rows"] == client.get_tariffs.return_value
