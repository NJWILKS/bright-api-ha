"""Protected regression for the PT30M timestamp-label convention cutover."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from custom_components.bright_api.api import UK_TZ, BrightApiClient
from custom_components.bright_api.const import CLASSIFIER_ELECTRICITY_CONSUMPTION
from custom_components.bright_api.interval_semantics import canonical_pt30m_start

pytestmark = pytest.mark.live

FIXTURE = Path(__file__).parents[1] / "tests" / "fixtures" / "electricity_cutover_hashes.json"
GOLDEN = Path(__file__).parents[1] / "tests" / "fixtures" / "electricity_golden_hashes.json"
EXPECTED_LOCATOR = datetime(2025, 7, 29, 23, 0, tzinfo=UTC)
CUTOVER_DAY = date(2026, 8, 30)
LAST_END_LABEL_DAY = date(2026, 8, 29)
FIRST_START_LABEL_DAY = date(2026, 8, 31)
LABEL_CSV_START = "CSV_START"
LABEL_PLUS_30M = "PLUS_30M"
LABEL_OTHER = "OTHER"


def _credentials() -> tuple[str, str]:
    username = os.environ.get("GLOWMARKT_USERNAME")
    password = os.environ.get("GLOWMARKT_PASSWORD")
    if not username or not password:
        pytest.fail("Protected Bright credentials are not configured")
    return username, password


def _local_midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UK_TZ)


def _canonical_line(row: tuple[datetime, float | None]) -> str:
    timestamp, value = row
    rendered = "null" if value is None else f"{float(value):.3f}"
    return f"{timestamp.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}|{rendered}"


def _canonical_hash(rows: list[tuple[datetime, float | None]]) -> str:
    lines = [
        _canonical_line((timestamp, value))
        for timestamp, value in sorted(rows, key=lambda item: item[0])
        if value is not None
    ]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def _raw_rows_for_day(
    rows: list[tuple[datetime, float | None]], day: date
) -> list[tuple[datetime, float | None]]:
    start = _local_midnight(day).astimezone(UTC)
    end = _local_midnight(day + timedelta(days=1)).astimezone(UTC)
    return sorted(
        [
            (timestamp.astimezone(UTC), value)
            for timestamp, value in rows
            if value is not None and start <= timestamp.astimezone(UTC) < end
        ],
        key=lambda item: item[0],
    )


def _classify(
    rows: list[tuple[datetime, float | None]], expected: dict[str, Any]
) -> tuple[str, str]:
    actual_hash = _canonical_hash(rows)
    if len(rows) == int(expected["count"]) and actual_hash == expected["csv_hash"]:
        return LABEL_CSV_START, actual_hash
    if len(rows) == int(expected["count"]) and actual_hash == expected["plus_30m_hash"]:
        return LABEL_PLUS_30M, actual_hash
    return LABEL_OTHER, actual_hash


async def _canonical_day_rows(
    client: BrightApiClient,
    resource_id: str,
    day: date,
) -> list[tuple[datetime, float | None]]:
    start = _local_midnight(day)
    end = _local_midnight(day + timedelta(days=1))
    raw_rows = await client.get_readings(resource_id, start, end + timedelta(minutes=30))
    result: list[tuple[datetime, float | None]] = []
    for raw_timestamp, value in raw_rows:
        if value is None:
            continue
        interval_start = canonical_pt30m_start(raw_timestamp)
        if start.astimezone(UTC) <= interval_start < end.astimezone(UTC):
            result.append((interval_start, value))
    return sorted(result, key=lambda item: item[0])


async def _known_electricity_resource(client: BrightApiClient) -> str:
    """Identify the known electricity resource using only the first-time locator."""
    candidates: list[tuple[str, datetime]] = []
    for item in await client.get_virtual_entities():
        virtual_entity_id = item.get("veId") if isinstance(item, dict) else None
        if not virtual_entity_id:
            continue
        resources = await client.discover_resources(str(virtual_entity_id))
        usage = resources.get(CLASSIFIER_ELECTRICITY_CONSUMPTION)
        if usage is None:
            continue
        locator = await client.get_first_reading_time(usage["resource_id"])
        if locator is not None and abs(locator - EXPECTED_LOCATOR) <= timedelta(days=3):
            candidates.append((usage["resource_id"], locator))

    if len(candidates) != 1:
        pytest.fail(
            "Cutover contract could not uniquely identify the known electricity resource; "
            f"candidates={len(candidates)}"
        )
    resource_id, locator = candidates[0]
    print(f"CUTOVER DIAGNOSTIC selected locator={locator.isoformat()}")
    return resource_id


@pytest.mark.asyncio
async def test_pt30m_timestamp_cutover_matches_protected_evidence(socket_enabled) -> None:
    """Pin the old convention, one known gap day, and the new convention."""
    contract = json.loads(FIXTURE.read_text(encoding="utf-8"))
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    expected_days: dict[str, dict[str, Any]] = contract["days"]
    first_day = date.fromisoformat(contract["range"]["first_local_day"])
    last_day = date.fromisoformat(contract["range"]["last_local_day"])

    username, password = _credentials()
    async with aiohttp.ClientSession() as session:
        client = BrightApiClient(username, password, session)
        resource_id = await _known_electricity_resource(client)

        chunks = [
            (date(2026, 8, 15), date(2026, 8, 25)),
            (date(2026, 8, 25), date(2026, 9, 4)),
            (date(2026, 9, 4), date(2026, 9, 6)),
        ]
        by_timestamp: dict[datetime, float | None] = {}
        for chunk_start, chunk_end in chunks:
            rows = await client.get_readings(
                resource_id,
                _local_midnight(chunk_start),
                _local_midnight(chunk_end),
            )
            for timestamp, value in rows:
                by_timestamp[timestamp.astimezone(UTC)] = value

        all_rows = sorted(by_timestamp.items(), key=lambda item: item[0])
        day = first_day
        while day <= last_day:
            rows = _raw_rows_for_day(all_rows, day)
            classification, actual_hash = _classify(rows, expected_days[day.isoformat()])
            print(
                "CUTOVER DIAGNOSTIC "
                f"day={day.isoformat()} count={len(rows)} "
                f"classification={classification} hash={actual_hash}"
            )

            if day <= LAST_END_LABEL_DAY:
                assert classification == LABEL_PLUS_30M
            elif day == CUTOVER_DAY:
                assert classification == LABEL_OTHER
            elif day >= FIRST_START_LABEL_DAY:
                assert classification == LABEL_CSV_START
            day += timedelta(days=1)

        gaps = golden["known_upstream_gaps"]
        assert date.fromisoformat(gaps["cutover_local_day"]) == CUTOVER_DAY
        canonical_cutover = await _canonical_day_rows(client, resource_id, CUTOVER_DAY)
        assert len(canonical_cutover) == int(gaps["cutover_retrievable_count"])
        assert _canonical_hash(canonical_cutover) == gaps["cutover_retrievable_sha256"]
