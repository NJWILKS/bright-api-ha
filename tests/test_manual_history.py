from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.bright_api.api import BrightApiClient
from custom_components.bright_api.history import IntervalHistoryStore
from custom_components.bright_api.manual_history import (
    ManualHistoryError,
    async_refresh_all_history,
    async_refresh_history_range,
    latest_settled_billing_day,
)


def test_latest_settled_billing_day_uses_uk_0400_boundary() -> None:
    assert latest_settled_billing_day(datetime(2026, 9, 13, 2, 30, tzinfo=UTC)) == date(
        2026, 9, 11
    )
    assert latest_settled_billing_day(datetime(2026, 9, 13, 4, 30, tzinfo=UTC)) == date(
        2026, 9, 12
    )


@pytest.mark.asyncio
async def test_manual_range_fetch_catches_pre_cutover_interval_end_label(hass) -> None:
    entry_id = "entry-1"
    repository = IntervalHistoryStore(hass, entry_id)
    await repository.async_save_metadata(
        {
            "schema_version": 1,
            "commodities": {
                "electricity": {
                    "first_interval": "2026-08-01T00:30:00+00:00",
                    "cursor_utc": "2026-09-01T23:00:00+00:00",
                    "last_interval": "2026-09-01T22:30:00+00:00",
                    "status": "current",
                }
            },
        }
    )

    client = AsyncMock(spec=BrightApiClient)
    resources = {"electricity.consumption": {"resource_id": "usage-id"}}
    # 2026-08-29 23:00 UTC is the raw end label for the final PT30M interval
    # of the 29 August UK billing day (canonical start 22:30 UTC / 23:30 BST).
    client.get_readings.return_value = [
        (datetime(2026, 8, 29, 23, 0, tzinfo=UTC), 0.42),
    ]

    with patch(
        "custom_components.bright_api.manual_history.async_project_reconciled_history",
        new=AsyncMock(),
    ) as project:
        await async_refresh_history_range(
            hass,
            client,
            resources,
            entry_id,
            asyncio.Lock(),
            date(2026, 8, 29),
            date(2026, 8, 29),
        )

    month = await repository.async_load_month("electricity", "2026-08")
    assert month["2026-08-29T23:00:00+00:00"]["usage_kwh"] == pytest.approx(0.42)
    project.assert_awaited_once()
    assert project.await_args.kwargs["replay_from"] == datetime(
        2026, 8, 28, 23, 0, tzinfo=UTC
    )


@pytest.mark.asyncio
async def test_manual_range_rejects_unsettled_day(hass) -> None:
    client = AsyncMock(spec=BrightApiClient)
    with (
        patch(
            "custom_components.bright_api.manual_history.latest_settled_billing_day",
            return_value=date(2026, 9, 12),
        ),
        pytest.raises(ManualHistoryError, match="latest settled day"),
    ):
        await async_refresh_history_range(
            hass,
            client,
            {"electricity.consumption": {"resource_id": "usage-id"}},
            "entry-1",
            asyncio.Lock(),
            date(2026, 9, 12),
            date(2026, 9, 13),
        )


@pytest.mark.asyncio
async def test_full_refresh_uses_first_retrievable_day_to_latest_settled(hass) -> None:
    entry_id = "entry-1"
    repository = IntervalHistoryStore(hass, entry_id)
    await repository.async_save_metadata(
        {
            "schema_version": 1,
            "commodities": {
                "electricity": {
                    "first_interval": "2026-09-01T00:00:00+00:00",
                    "cursor_utc": "2026-09-13T00:00:00+00:00",
                }
            },
        }
    )
    client = AsyncMock(spec=BrightApiClient)
    resources = {"electricity.consumption": {"resource_id": "usage-id"}}

    with (
        patch(
            "custom_components.bright_api.manual_history.latest_settled_billing_day",
            return_value=date(2026, 9, 12),
        ),
        patch(
            "custom_components.bright_api.manual_history._refresh_range_unlocked",
            new=AsyncMock(),
        ) as refresh,
    ):
        await async_refresh_all_history(
            hass,
            client,
            resources,
            entry_id,
            asyncio.Lock(),
        )

    refresh.assert_awaited_once_with(
        hass,
        client,
        resources,
        entry_id,
        date(2026, 9, 1),
        date(2026, 9, 12),
    )
