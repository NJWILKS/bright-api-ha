from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.bright_api.api import BrightApiClient
from custom_components.bright_api.history import IntervalHistoryStore
from custom_components.bright_api.orchestrator import async_sync_history_and_statistics_once


@pytest.mark.asyncio
@pytest.mark.parametrize("commodity", ["electricity", "gas"])
@pytest.mark.parametrize("tariffs", [[], [{"effectiveDate": "2026-09-01", "rate": 5.04}]])
async def test_current_history_sync_refreshes_tariffs(hass, commodity, tariffs) -> None:
    repository = IntervalHistoryStore(hass, "entry-1")
    metadata = {"schema_version": 1, "commodities": {commodity: {"status": "current"}}}
    await repository.async_save_metadata(metadata)
    await repository.async_save_tariffs(
        commodity, [{"effectiveDate": "2026-08-01", "rate": 99.0}],
        datetime(2026, 8, 1, tzinfo=UTC),
    )
    client = AsyncMock(spec=BrightApiClient)
    client.get_tariffs.return_value = tariffs
    resources = {
        f"{commodity}.consumption": {"resource_id": "usage-id"},
        f"{commodity}.consumption.cost": {"resource_id": "cost-id"},
    }
    with (
        patch("custom_components.bright_api.orchestrator._history_due", return_value=False),
        patch(
            "custom_components.bright_api.orchestrator.async_populate_interval_history",
            new=AsyncMock(),
        ) as populate,
        patch(
            "custom_components.bright_api.orchestrator.async_reconcile_recent_history",
            new=AsyncMock(return_value=(metadata, None)),
        ),
        patch(
            "custom_components.bright_api.orchestrator.async_project_interval_history",
            new=AsyncMock(),
        ),
    ):
        await async_sync_history_and_statistics_once(hass, client, resources, "entry-1")

    populate.assert_not_awaited()
    client.get_tariffs.assert_awaited_once_with("cost-id")
    assert (await repository.async_load_tariffs(commodity))["rows"] == tariffs
    assert (await repository.async_load_metadata())["commodities"][commodity][
        "tariff_rows"
    ] == len(tariffs)
