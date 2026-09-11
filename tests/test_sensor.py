from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from custom_components.bright_api.history import IntervalHistoryStore
from custom_components.bright_api.sensor import (
    BrightCurrentRateSensor,
    BrightHistoryStatusSensor,
    BrightLastSettledIntervalSensor,
    BrightStandingChargeSensor,
)


@pytest.mark.asyncio
async def test_presentation_sensors_read_durable_store_without_api_calls(hass) -> None:
    entry = SimpleNamespace(entry_id="entry-1")
    repository = IntervalHistoryStore(hass, entry.entry_id)
    await repository.async_save_metadata(
        {
            "schema_version": 1,
            "commodities": {
                "electricity": {
                    "status": "current",
                    "last_interval": "2026-01-01T23:30:00+00:00",
                }
            },
        }
    )
    await repository.async_save_tariffs(
        "electricity",
        [
            {
                "effectiveDate": "2020-01-01",
                "plan": [{"rate": 24.0, "standing": 50.0}],
            }
        ],
        datetime(2026, 1, 2, 0, 0, tzinfo=UTC),
    )

    status = BrightHistoryStatusSensor(hass, entry, "electricity")
    last = BrightLastSettledIntervalSensor(hass, entry, "electricity")
    rate = BrightCurrentRateSensor(hass, entry, "electricity")
    standing = BrightStandingChargeSensor(hass, entry, "electricity")

    await status.async_update()
    await last.async_update()
    await rate.async_update()
    await standing.async_update()

    assert status.native_value == "current"
    assert last.native_value == datetime(2026, 1, 1, 23, 30, tzinfo=UTC)
    assert rate.native_value == pytest.approx(0.24)
    assert standing.native_value == pytest.approx(0.50)
