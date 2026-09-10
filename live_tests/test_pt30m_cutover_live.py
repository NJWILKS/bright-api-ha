"""Live diagnostic for the PT30M timestamp convention change seen in Bright history."""
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

pytestmark = pytest.mark.live

FIXTURE = Path(__file__).parents[1] / "tests" / "fixtures" / "electricity_cutover_hashes.json"
EXPECTED_LOCATOR = datetime(2025, 7, 29, 23, 0, tzinfo=UTC)
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


def _rows_for_day(
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
        if locator is None:
            continue
        if abs(locator - EXPECTED_LOCATOR) <= timedelta(days=3):
            candidates.append((usage["resource_id"], locator))

    if len(candidates) != 1:
        pytest.fail(
            "Cutover diagnostic could not uniquely identify the known electricity resource "
            f"from first-time locator; candidates={len(candidates)}"
        )
    resource_id, locator = candidates[0]
    print(f"CUTOVER DIAGNOSTIC selected locator={locator.isoformat()}")
    return resource_id


async def _documented_bst_readings(
    client: BrightApiClient,
    resource_id: str,
    day: date,
) -> list[tuple[datetime, float | None]]:
    """Call readings using Bright's documented BST wall-clock request plus offset=-60."""
    start = _local_midnight(day)
    end = _local_midnight(day + timedelta(days=1))
    params = {
        "from": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "to": end.strftime("%Y-%m-%dT%H:%M:%S"),
        "period": "PT30M",
        "offset": -60,
        "function": "sum",
        "nulls": 1,
    }
    payload = await client._get_json(f"/resource/{resource_id}/readings", params=params)
    if not isinstance(payload, dict) or payload.get("status") != "OK":
        pytest.fail("Documented BST-offset diagnostic returned an invalid Bright response")

    rows: list[tuple[datetime, float | None]] = []
    for item in payload.get("data", []):
        if not isinstance(item, list) or len(item) < 2:
            continue
        timestamp = datetime.fromtimestamp(float(item[0]), tz=UTC)
        value = None if item[1] is None else float(item[1])
        rows.append((timestamp, value))
    return _rows_for_day(rows, day)


def _print_day(
    prefix: str,
    day: date,
    rows: list[tuple[datetime, float | None]],
    classification: str,
    actual_hash: str,
) -> None:
    first = _canonical_line(rows[0]) if rows else "<none>"
    last = _canonical_line(rows[-1]) if rows else "<none>"
    print(
        f"{prefix} day={day.isoformat()} count={len(rows)} classification={classification} "
        f"hash={actual_hash} first={first} last={last}"
    )


@pytest.mark.asyncio
async def test_pt30m_timestamp_cutover_and_documented_bst_offset(socket_enabled) -> None:
    """Find the daily label cutover using three bulk calls plus two offset probes."""
    contract = json.loads(FIXTURE.read_text(encoding="utf-8"))
    expected_days: dict[str, dict[str, Any]] = contract["days"]
    first_day = date.fromisoformat(contract["range"]["first_local_day"])
    last_day = date.fromisoformat(contract["range"]["last_local_day"])

    username, password = _credentials()
    async with aiohttp.ClientSession() as session:
        client = BrightApiClient(username, password, session)
        resource_id = await _known_electricity_resource(client)

        # PT30M has a documented 10-day request limit. These three calls cover
        # 2026-08-15 through 2026-09-05 without exceeding that limit.
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
        classifications: list[tuple[date, str]] = []
        unknown_days: list[str] = []

        day = first_day
        while day <= last_day:
            expected = expected_days[day.isoformat()]
            rows = _rows_for_day(all_rows, day)
            classification, actual_hash = _classify(rows, expected)
            _print_day("CUTOVER DIAGNOSTIC", day, rows, classification, actual_hash)
            classifications.append((day, classification))
            if classification == LABEL_OTHER:
                unknown_days.append(day.isoformat())
            day += timedelta(days=1)

        # Two extra calls test Bright's documented BST query form on one known
        # historically shifted day and the last known CSV-aligned day.
        for probe_day in (first_day, last_day):
            expected = expected_days[probe_day.isoformat()]
            rows = await _documented_bst_readings(client, resource_id, probe_day)
            classification, actual_hash = _classify(rows, expected)
            _print_day("BST OFFSET PROBE", probe_day, rows, classification, actual_hash)

        if unknown_days:
            pytest.fail(
                "Cutover diagnostic found days matching neither known convention: "
                + ", ".join(unknown_days)
            )

        transitions = [
            index
            for index in range(1, len(classifications))
            if classifications[index][1] != classifications[index - 1][1]
        ]
        if len(transitions) != 1:
            summary = ", ".join(
                f"{day.isoformat()}={label}" for day, label in classifications
            )
            pytest.fail(
                "Cutover diagnostic expected one daily convention transition; "
                f"found {len(transitions)}. {summary}"
            )

        transition = transitions[0]
        before_day, before_label = classifications[transition - 1]
        after_day, after_label = classifications[transition]
        print(
            "CUTOVER RESULT "
            f"last_before={before_day.isoformat()}:{before_label} "
            f"first_after={after_day.isoformat()}:{after_label}"
        )

        assert before_label == LABEL_PLUS_30M
        assert after_label == LABEL_CSV_START
