"""Protected live comparison against hashed Bright export golden data."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import aiohttp
import pytest

from custom_components.bright_api.api import UK_TZ, BrightApiClient
from custom_components.bright_api.const import CLASSIFIER_ELECTRICITY_CONSUMPTION

pytestmark = pytest.mark.live

FIXTURE_PATH = Path("tests/fixtures/electricity_golden_hashes.json")


def _credentials() -> tuple[str, str]:
    username = os.environ.get("GLOWMARKT_USERNAME")
    password = os.environ.get("GLOWMARKT_PASSWORD")
    if not username or not password:
        pytest.skip("Protected Bright credentials are not configured")
    return username, password


def _normalise_row(timestamp: datetime, value: float) -> str:
    normalised_value = Decimal(str(value)).quantize(Decimal("0.000"))
    return f"{timestamp.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}|{normalised_value}"


def _hash_rows(rows: list[tuple[datetime, float]]) -> str:
    payload = "\n".join(_normalise_row(timestamp, value) for timestamp, value in rows)
    return hashlib.sha256(payload.encode()).hexdigest()


def _local_midnight(local_day) -> datetime:
    return datetime(local_day.year, local_day.month, local_day.day, tzinfo=UK_TZ)


@pytest.mark.asyncio
async def test_real_electricity_matches_authoritative_csv_fingerprints(socket_enabled) -> None:
    """Match first, five fixed-random, DST and last-good complete CSV days."""
    contract = json.loads(FIXTURE_PATH.read_text())
    username, password = _credentials()

    async with aiohttp.ClientSession() as session:
        client = BrightApiClient(username, password, session)
        virtual_entities = await client.get_virtual_entities()
        assert virtual_entities

        virtual_entity_id = next(
            (str(item["veId"]) for item in virtual_entities if item.get("veId")),
            None,
        )
        assert virtual_entity_id is not None

        resources = await client.discover_resources(virtual_entity_id)
        resource = resources.get(CLASSIFIER_ELECTRICITY_CONSUMPTION)
        assert resource is not None

        resource_id = resource["resource_id"]
        first_actual = await client.get_first_available_reading_time(resource_id)
        assert first_actual is not None

        first_local_day = first_actual.astimezone(UK_TZ).date()
        first_day_start = _local_midnight(first_local_day)
        first_day_end = _local_midnight(first_local_day + timedelta(days=1))
        first_rows_raw = await client.get_readings(resource_id, first_day_start, first_day_end)
        first_rows = sorted(
            (
                (timestamp, float(value))
                for timestamp, value in first_rows_raw
                if value is not None
                and first_day_start.astimezone(UTC)
                <= timestamp.astimezone(UTC)
                < first_day_end.astimezone(UTC)
            ),
            key=lambda item: item[0],
        )
        assert first_rows
        assert _hash_rows([first_rows[0]]) == contract["first_interval_sha256"]

        cases = [
            contract["first_day"],
            *contract["random_days"],
            *contract["dst_days"],
            contract["last_known_complete_day"],
        ]
        assert len(contract["random_days"]) >= 5

        for case in cases:
            local_day = first_local_day + timedelta(
                days=int(case["offset_days_from_first_local_day"])
            )
            start = _local_midnight(local_day)
            end = _local_midnight(local_day + timedelta(days=1))
            raw_rows = await client.get_readings(resource_id, start, end)
            rows = sorted(
                (
                    (timestamp, float(value))
                    for timestamp, value in raw_rows
                    if value is not None
                    and start.astimezone(UTC)
                    <= timestamp.astimezone(UTC)
                    < end.astimezone(UTC)
                ),
                key=lambda item: item[0],
            )

            assert len(rows) == int(case["expected_interval_count"])
            assert _hash_rows(rows) == case["sha256"]
