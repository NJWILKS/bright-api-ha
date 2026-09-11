"""Protected live comparison against hashed Bright export golden data."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import aiohttp
import pytest

from custom_components.bright_api.api import UK_TZ, BrightApiClient
from custom_components.bright_api.const import CLASSIFIER_ELECTRICITY_CONSUMPTION
from custom_components.bright_api.interval_semantics import canonical_pt30m_start

pytestmark = pytest.mark.live

FIXTURE_PATH = Path("tests/fixtures/electricity_golden_hashes.json")
EXPECTED_LOCATOR = datetime(2025, 7, 29, 23, 0, tzinfo=UTC)
LOCATOR_TOLERANCE = timedelta(days=3)


def _credentials() -> tuple[str, str]:
    username = os.environ.get("GLOWMARKT_USERNAME")
    password = os.environ.get("GLOWMARKT_PASSWORD")
    if not username or not password:
        pytest.fail("Protected Bright credentials are not configured")
    return username, password


def _normalise_row(timestamp: datetime, value: float) -> str:
    normalised_value = Decimal(str(value)).quantize(Decimal("0.000"))
    return f"{timestamp.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}|{normalised_value}"


def _hash_rows(rows: list[tuple[datetime, float]]) -> str:
    payload = "\n".join(_normalise_row(timestamp, value) for timestamp, value in rows)
    return hashlib.sha256(payload.encode()).hexdigest()


def _local_midnight(local_day: date) -> datetime:
    return datetime.combine(local_day, time.min, tzinfo=UK_TZ)


async def _canonical_day_rows(
    client: BrightApiClient,
    resource_id: str,
    local_day: date,
) -> list[tuple[datetime, float]]:
    """Fetch one local day and interpret Bright labels without mutating them."""
    start = _local_midnight(local_day)
    end = _local_midnight(local_day + timedelta(days=1))
    raw_rows = await client.get_readings(resource_id, start, end + timedelta(minutes=30))
    canonical: list[tuple[datetime, float]] = []
    for raw_timestamp, value in raw_rows:
        if value is None:
            continue
        interval_start = canonical_pt30m_start(raw_timestamp)
        if start.astimezone(UTC) <= interval_start < end.astimezone(UTC):
            canonical.append((interval_start, float(value)))
    return sorted(canonical, key=lambda item: item[0])


def _print_actuals(label: str, rows: list[tuple[datetime, float]]) -> None:
    """Emit exact canonical live values when a golden comparison fails."""
    print(f"LIVE DIAGNOSTIC {label}: {len(rows)} row(s)")
    for timestamp, value in rows:
        print(
            "LIVE DIAGNOSTIC canonical: "
            f"{timestamp.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}|{value:.12g} "
            f"canonical={value:.3f}"
        )


async def _known_electricity_resource(client: BrightApiClient) -> tuple[str, datetime]:
    """Find the known resource using first-time only as a locator."""
    candidates: list[tuple[str, datetime]] = []
    for item in await client.get_virtual_entities():
        virtual_entity_id = item.get("veId") if isinstance(item, dict) else None
        if not virtual_entity_id:
            continue
        resources = await client.discover_resources(str(virtual_entity_id))
        resource = resources.get(CLASSIFIER_ELECTRICITY_CONSUMPTION)
        if resource is None:
            continue
        locator = await client.get_first_reading_time(resource["resource_id"])
        if locator is not None and abs(locator - EXPECTED_LOCATOR) <= LOCATOR_TOLERANCE:
            candidates.append((resource["resource_id"], locator))

    if len(candidates) != 1:
        pytest.fail(
            "Live contract could not uniquely identify the known electricity resource; "
            f"candidates={len(candidates)}"
        )
    return candidates[0]


@pytest.mark.asyncio
async def test_real_electricity_matches_authoritative_csv_fingerprints(socket_enabled) -> None:
    """Match canonical PT30M data while pinning known upstream omissions."""
    contract = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    gaps = contract["known_upstream_gaps"]
    username, password = _credentials()

    async with aiohttp.ClientSession() as session:
        client = BrightApiClient(username, password, session)
        resource_id, locator = await _known_electricity_resource(client)
        first_raw = await client.get_first_available_reading_time(resource_id)
        assert first_raw is not None
        first_canonical = canonical_pt30m_start(first_raw)
        first_local_day = first_canonical.astimezone(UK_TZ).date()

        print(
            "LIVE DIAGNOSTIC selected resource: "
            f"locator={locator.isoformat()} first_raw={first_raw.isoformat()} "
            f"first_canonical={first_canonical.isoformat()}"
        )

        first_retrievable_hash = _hash_rows([(first_canonical, 0.203)])
        assert first_retrievable_hash == gaps["first_retrievable_interval_sha256"]

        failures: list[str] = []
        cases = [
            contract["first_day"],
            *contract["random_days"],
            *contract["dst_days"],
            contract["last_known_complete_day"],
        ]
        assert len(contract["random_days"]) >= 5

        for case in cases:
            offset = int(case["offset_days_from_first_local_day"])
            local_day = first_local_day + timedelta(days=offset)
            rows = await _canonical_day_rows(client, resource_id, local_day)

            if offset == 0:
                expected_count = int(gaps["first_day_retrievable_count"])
                expected_hash = str(gaps["first_day_retrievable_sha256"])
            else:
                expected_count = int(case["expected_interval_count"])
                expected_hash = str(case["sha256"])

            actual_hash = _hash_rows(rows)
            if len(rows) != expected_count or actual_hash != expected_hash:
                _print_actuals(
                    f"golden offset={offset} local_day={local_day.isoformat()} "
                    f"expected_count={expected_count} actual_count={len(rows)} "
                    f"expected_hash={expected_hash} actual_hash={actual_hash}",
                    rows,
                )
                failures.append(
                    f"offset {offset}: expected_count={expected_count} actual_count={len(rows)} "
                    f"expected_hash={expected_hash} actual_hash={actual_hash}"
                )

        cutover_day = date.fromisoformat(gaps["cutover_local_day"])
        cutover_rows = await _canonical_day_rows(client, resource_id, cutover_day)
        cutover_hash = _hash_rows(cutover_rows)
        expected_cutover_count = int(gaps["cutover_retrievable_count"])
        expected_cutover_hash = str(gaps["cutover_retrievable_sha256"])
        if (
            len(cutover_rows) != expected_cutover_count
            or cutover_hash != expected_cutover_hash
        ):
            _print_actuals("known cutover-day upstream gap changed", cutover_rows)
            failures.append(
                "cutover gap changed: "
                f"expected_count={expected_cutover_count} actual_count={len(cutover_rows)} "
                f"expected_hash={expected_cutover_hash} actual_hash={cutover_hash}"
            )

        if failures:
            pytest.fail("Live CSV golden mismatches:\n" + "\n".join(failures))
