from __future__ import annotations

from datetime import UTC, datetime
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
from custom_components.bright_api.tariffs import parse_flat_tariffs


def _flat_tariffs():
    return parse_flat_tariffs(
        [
            {
                "effectiveDate": "2026-01-01",
                "plan": [{"rate": 24.0, "standing": 50.0}],
            }
        ]
    )


def test_hourly_projection_preserves_interval_values_and_separates_costs() -> None:
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    end = datetime(2026, 1, 1, 2, 0, tzinfo=UTC)
    records = [
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "usage_kwh": 0.1,
            "cost_pence": 2.4,
        },
        {
            "timestamp": "2026-01-01T00:30:00+00:00",
            "usage_kwh": 0.2,
            "cost_pence": 4.8,
        },
        {
            "timestamp": "2026-01-01T01:00:00+00:00",
            "usage_kwh": 0.3,
            "cost_pence": 7.2,
        },
        {
            "timestamp": "2026-01-01T01:30:00+00:00",
            "usage_kwh": 0.4,
            "cost_pence": 9.6,
        },
    ]

    series, running = _build_hourly_statistics(records, _flat_tariffs(), start, end)

    assert [row["state"] for row in series[MEASURE_CONSUMPTION]] == pytest.approx([0.3, 0.7])
    assert [row["state"] for row in series[MEASURE_USAGE_COST]] == pytest.approx([0.072, 0.168])
    assert [row["state"] for row in series[MEASURE_STANDING_CHARGE]] == pytest.approx([0.5])
    assert [row["state"] for row in series[MEASURE_TOTAL_COST]] == pytest.approx([0.572, 0.168])
    assert running[MEASURE_CONSUMPTION] == pytest.approx(1.0)
    assert running[MEASURE_USAGE_COST] == pytest.approx(0.24)
    assert running[MEASURE_STANDING_CHARGE] == pytest.approx(0.5)
    assert running[MEASURE_TOTAL_COST] == pytest.approx(0.74)


def test_projection_does_not_shift_bright_timestamps_to_make_pairs() -> None:
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    end = datetime(2026, 1, 1, 2, 0, tzinfo=UTC)
    records = [
        {
            "timestamp": "2026-01-01T00:30:00+00:00",
            "usage_kwh": 0.1,
            "cost_pence": None,
        },
        {
            "timestamp": "2026-01-01T01:00:00+00:00",
            "usage_kwh": 0.2,
            "cost_pence": None,
        },
    ]

    series, _ = _build_hourly_statistics(records, [], start, end)

    consumption = series[MEASURE_CONSUMPTION]
    assert [row["start"] for row in consumption] == [
        datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 1, 0, tzinfo=UTC),
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
    records = [
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "usage_kwh": 0.1,
            "cost_pence": 2.4,
        },
        {
            "timestamp": "2026-01-01T00:30:00+00:00",
            "usage_kwh": 0.2,
            "cost_pence": 4.8,
        },
    ]
    await history.async_upsert_intervals("electricity", records)
    metadata = {
        "schema_version": 1,
        "commodities": {
            "electricity": {
                "first_interval": "2026-01-01T00:00:00+00:00",
                "cursor_utc": "2026-01-02T00:00:00+00:00",
                "status": "current",
            }
        },
    }
    await history.async_save_metadata(metadata)
    await history.async_save_tariffs(
        "electricity",
        [
            {
                "effectiveDate": "2026-01-01",
                "plan": [{"rate": 24.0, "standing": 50.0}],
            }
        ],
        datetime(2026, 1, 2, 0, 0, tzinfo=UTC),
    )

    recorder = SimpleNamespace(async_block_till_done=AsyncMock())
    with (
        patch("custom_components.bright_api.statistics.get_instance", return_value=recorder),
        patch("custom_components.bright_api.statistics.async_add_external_statistics") as add_stats,
    ):
        state = await async_project_interval_history(hass, entry_id)

        recorder.async_block_till_done.assert_awaited_once()
        assert add_stats.call_count == 4
        assert state["commodities"]["electricity"]["cursor_utc"] == "2026-01-02T00:00:00+00:00"

        add_stats.reset_mock()
        recorder.async_block_till_done.reset_mock()
        await async_project_interval_history(hass, entry_id)
        add_stats.assert_not_called()
        recorder.async_block_till_done.assert_not_awaited()

    persisted = await StatisticsProjectionStore(hass, entry_id).async_load()
    assert persisted["commodities"]["electricity"]["running"][MEASURE_TOTAL_COST] == pytest.approx(
        0.572
    )
