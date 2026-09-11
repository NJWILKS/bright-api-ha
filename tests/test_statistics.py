from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.bright_api.history import IntervalHistoryStore
from custom_components.bright_api.statistics import (
    MEASURE_CONSUMPTION,
    MEASURE_STANDING_CHARGE,
    MEASURE_TOTAL_COST,
    MEASURE_USAGE_COST,
    StatisticsProjectionStore,
    _build_hourly_statistics,
    async_project_interval_history,
    owned_statistic_ids,
)


def test_hourly_projection_separates_usage_billed_total_and_standing() -> None:
    start = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
    end = datetime(2026, 9, 2, 0, 0, tzinfo=UTC)
    records = []
    for index in range(48):
        timestamp = start + index * (end - start) / 48
        records.append(
            {
                "timestamp": timestamp.isoformat(),
                "usage_kwh": 0.1,
                "cost_pence": 0.5,
            }
        )

    series, running = _build_hourly_statistics(
        records,
        [],
        start,
        end,
        daily_costs={date(2026, 9, 1): 74.0},
    )

    assert len(series[MEASURE_CONSUMPTION]) == 24
    assert [row["state"] for row in series[MEASURE_CONSUMPTION]] == pytest.approx([0.2] * 24)
    assert [row["state"] for row in series[MEASURE_USAGE_COST]] == pytest.approx([0.01] * 24)
    assert [row["state"] for row in series[MEASURE_STANDING_CHARGE]] == pytest.approx([0.5])
    assert [row["state"] for row in series[MEASURE_TOTAL_COST]] == pytest.approx([0.74])
    assert running[MEASURE_CONSUMPTION] == pytest.approx(4.8)
    assert running[MEASURE_USAGE_COST] == pytest.approx(0.24)
    assert running[MEASURE_STANDING_CHARGE] == pytest.approx(0.5)
    assert running[MEASURE_TOTAL_COST] == pytest.approx(0.74)


def test_standing_is_not_derived_when_pt30m_cost_day_is_incomplete() -> None:
    start = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
    end = datetime(2026, 9, 2, 0, 0, tzinfo=UTC)
    records = [
        {
            "timestamp": (start + index * (end - start) / 48).isoformat(),
            "usage_kwh": 0.1,
            "cost_pence": 0.5,
        }
        for index in range(47)
    ]

    series, running = _build_hourly_statistics(
        records,
        [],
        start,
        end,
        daily_costs={date(2026, 9, 1): 74.0},
    )

    assert series[MEASURE_STANDING_CHARGE] == []
    assert [row["state"] for row in series[MEASURE_TOTAL_COST]] == pytest.approx([0.74])
    assert running[MEASURE_STANDING_CHARGE] == 0.0
    assert running[MEASURE_TOTAL_COST] == pytest.approx(0.74)


def test_pre_cutover_end_labels_are_grouped_by_canonical_interval_start() -> None:
    start = datetime(2026, 8, 29, 23, 0, tzinfo=UTC)
    end = datetime(2026, 8, 30, 1, 0, tzinfo=UTC)
    records = [
        {
            "timestamp": "2026-08-29T23:30:00+00:00",
            "usage_kwh": 0.1,
            "cost_pence": None,
        },
        {
            "timestamp": "2026-08-30T00:00:00+00:00",
            "usage_kwh": 0.2,
            "cost_pence": None,
        },
    ]

    series, _ = _build_hourly_statistics(records, [], start, end)

    consumption = series[MEASURE_CONSUMPTION]
    assert [row["start"] for row in consumption] == [
        datetime(2026, 8, 29, 23, 0, tzinfo=UTC),
        datetime(2026, 8, 30, 0, 0, tzinfo=UTC),
    ]
    assert [row["state"] for row in consumption] == pytest.approx([0.1, 0.2])


def test_owned_statistics_never_include_a_cumulative_meter_total() -> None:
    ids = owned_statistic_ids("entry-1")

    assert len(ids) == 8
    assert all("meter_total" not in statistic_id for statistic_id in ids)
    assert all("cumulative" not in statistic_id for statistic_id in ids)


@pytest.mark.asyncio
async def test_projection_checkpoint_advances_only_after_recorder_finishes(hass) -> None:
    entry_id = "entry-1"
    history = IntervalHistoryStore(hass, entry_id)
    start = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
    records = [
        {
            "timestamp": (start + timedelta(minutes=30 * index)).isoformat(),
            "usage_kwh": 0.1,
            "cost_pence": 0.5,
        }
        for index in range(48)
    ]
    await history.async_upsert_intervals("electricity", records)
    await history.async_upsert_daily_costs(
        "electricity",
        [(datetime(2026, 9, 1, 0, 0, tzinfo=UTC), 74.0)],
    )
    metadata = {
        "schema_version": 1,
        "commodities": {
            "electricity": {
                "first_interval": "2026-09-01T00:00:00+00:00",
                "cursor_utc": "2026-09-02T00:00:00+00:00",
                "daily_cost_cursor_day": "2026-09-02",
                "status": "current",
            }
        },
    }
    await history.async_save_metadata(metadata)

    recorder = SimpleNamespace(async_block_till_done=AsyncMock())
    with (
        patch("custom_components.bright_api.statistics.get_instance", return_value=recorder),
        patch("custom_components.bright_api.statistics.async_add_external_statistics") as add_stats,
    ):
        state = await async_project_interval_history(hass, entry_id)

        recorder.async_block_till_done.assert_awaited_once()
        assert add_stats.call_count == 4
        assert state["commodities"]["electricity"]["cursor_utc"] == "2026-09-02T00:00:00+00:00"

        add_stats.reset_mock()
        recorder.async_block_till_done.reset_mock()
        await async_project_interval_history(hass, entry_id)
        add_stats.assert_not_called()
        recorder.async_block_till_done.assert_not_awaited()

    persisted = await StatisticsProjectionStore(hass, entry_id).async_load()
    assert persisted["commodities"]["electricity"]["running"][MEASURE_TOTAL_COST] == pytest.approx(
        0.74
    )
