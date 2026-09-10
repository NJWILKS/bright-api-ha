from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from custom_components.bright_api.api import BrightApiClient


@pytest.fixture
def client() -> BrightApiClient:
    return BrightApiClient("user@example.com", "secret", object())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_discover_resources_indexes_by_classifier(client: BrightApiClient) -> None:
    client._get_json = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "resources": [
                {
                    "resourceId": "resource-1",
                    "classifier": "electricity.consumption",
                    "name": "Electricity",
                    "baseUnit": "kWh",
                },
                {"classifier": "ignored-without-id"},
            ]
        }
    )

    resources = await client.discover_resources("site-1")

    assert resources == {
        "electricity.consumption": {
            "resource_id": "resource-1",
            "name": "Electricity",
            "base_unit": "kWh",
        }
    }


@pytest.mark.asyncio
async def test_get_readings_preserves_zero_and_null(client: BrightApiClient) -> None:
    client._get_json = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "status": "OK",
            "data": [[1_700_000_000, 0], [1_700_001_800, None], [1_700_003_600, 1.25]],
        }
    )

    rows = await client.get_readings(
        "resource-1",
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 2, tzinfo=UTC),
    )

    assert [value for _, value in rows] == [0.0, None, 1.25]
    assert all(timestamp.tzinfo is UTC for timestamp, _ in rows)


@pytest.mark.asyncio
async def test_first_available_reading_uses_actual_pt30m_data(client: BrightApiClient) -> None:
    locator = datetime(2025, 10, 26, 0, 0, tzinfo=UTC)
    actual = datetime(2025, 10, 26, 1, 30, tzinfo=UTC)
    client.get_first_reading_time = AsyncMock(return_value=locator)  # type: ignore[method-assign]
    client.get_readings = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            (datetime(2025, 10, 26, 0, 30, tzinfo=UTC), None),
            (actual, 0.0),
            (datetime(2025, 10, 26, 2, 0, tzinfo=UTC), 0.2),
        ]
    )

    result = await client.get_first_available_reading_time("resource-1")

    assert result == actual


@pytest.mark.asyncio
async def test_tariff_history_preserves_effective_rows(client: BrightApiClient) -> None:
    rows = [
        {"effectiveDate": "2026-01-01 00:00:00", "plan": [{"planDetail": []}]},
        {"effectiveDate": "2026-07-01 00:00:00", "plan": [{"planDetail": []}]},
    ]
    client._get_json = AsyncMock(return_value={"status": "OK", "data": rows})  # type: ignore[method-assign]

    assert await client.get_tariffs("resource-1") == rows
