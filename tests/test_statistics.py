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
from custom_components.bright_api.sync import async_project_reconciled_history


def test_hourly_projection_separates_usage_billed_total_and_standing() -> None:
    start = datetime(2026, 8, 31, 23, 0, tzinfo=UTC)
    end = datetime(2026, 9, 1, 23, 0, tzinfo=UTC)
    records = []
    for index in range(48):
        timestamp = start + timedelta(minutes=30 * index)
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
    start = datetime(2026, 8, 31, 23, 0, tzinfo=UTC)
    end = datetime(2026, 9, 1, 23, 0, tzinfo=UTC)
    records = [
        {
            "timestamp": (start + timedelta(minutes=30 * index)).isoformat(),
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
    start = datetime(2026, 8, 31, 23, 0, tzinfo=UTC)
    end = datetime(2026, 9, 1, 23, 0, tzinfo=UTC)
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
        [(start, 74.0)],
    )
    metadata = {
        "schema_version": 1,
        "commodities": {
            "electricity": {
                "first_interval": start.isoformat(),
                "cursor_utc": end.isoformat(),
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
        assert state["commodities"]["electricity"]["cursor_utc"] == end.isoformat()

        add_stats.reset_mock()
        recorder.async_block_till_done.reset_mock()
        await async_project_interval_history(hass, entry_id)
        add_stats.assert_not_called()
        recorder.async_block_till_done.assert_not_awaited()

    persisted = await StatisticsProjectionStore(hass, entry_id).async_load()
    assert persisted["commodities"]["electricity"]["running"][MEASURE_TOTAL_COST] == pytest.approx(
        0.74
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("commodity", ["electricity", "gas"])
@pytest.mark.parametrize("replay", [False, True])
async def test_first_midday_reading_preserves_first_billed_day(hass, commodity, replay) -> None:
    entry_id = "entry-midday"
    midnight = datetime(2026, 10, 4, 23, tzinfo=UTC)
    first = midnight + timedelta(hours=12)
    end = midnight + timedelta(days=1)
    history = IntervalHistoryStore(hass, entry_id)
    await history.async_upsert_intervals(
        commodity,
        [{"timestamp": first.isoformat(), "usage_kwh": 5.0, "cost_pence": 25.0}],
    )
    await history.async_upsert_daily_costs(commodity, [(midnight, 250.0)])
    metadata = {"commodities": {commodity: {
        "first_interval": first.isoformat(), "cursor_utc": end.isoformat(),
    }}}
    if replay:
        await StatisticsProjectionStore(hass, entry_id).async_save({
            "commodities": {commodity: {"cursor_utc": end.isoformat(), "running": {}}},
        })
    module = "sync" if replay else "statistics"
    recorder = SimpleNamespace(async_block_till_done=AsyncMock())
    with (
        patch(f"custom_components.bright_api.{module}.get_instance", return_value=recorder),
        patch(f"custom_components.bright_api.{module}.async_add_external_statistics") as add_stats,
    ):
        if replay:
            state = await async_project_reconciled_history(
                hass, entry_id, history_metadata=metadata, replay_from=midnight,
            )
        else:
            state = await async_project_interval_history(hass, entry_id, history_metadata=metadata)

    series = {
        call.args[1]["statistic_id"].rsplit("_", 2)[-1]: call.args[2]
        for call in add_stats.call_args_list
    }
    billed = next(
        call.args[2] for call in add_stats.call_args_list
        if call.args[1]["statistic_id"].endswith("_total_cost")
    )
    assert billed == [{"start": midnight, "state": 2.5, "sum": 2.5}]
    running = state["commodities"][commodity]["running"]
    assert running[MEASURE_CONSUMPTION] == pytest.approx(5.0)
    assert running[MEASURE_USAGE_COST] == pytest.approx(0.25)
    assert running[MEASURE_TOTAL_COST] == pytest.approx(2.5)
    assert running[MEASURE_STANDING_CHARGE] == 0.0
    consumption = series["consumption"]
    assert len(consumption) == 1
    assert consumption[0]["start"] == first
