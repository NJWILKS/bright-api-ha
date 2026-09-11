"""Protected live contracts for Bright PT30M, P1D billing and ledger persistence."""
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
from custom_components.bright_api.const import (
    CLASSIFIER_ELECTRICITY_CONSUMPTION,
    CLASSIFIER_ELECTRICITY_COST,
)
from custom_components.bright_api.history import (
    IntervalHistoryStore,
    async_populate_interval_history,
)
from custom_components.bright_api.interval_semantics import canonical_pt30m_start
from custom_components.bright_api.tariffs import parse_flat_tariffs, tariff_for_day

pytestmark = pytest.mark.live

GOLDEN = Path(__file__).parents[1] / "tests" / "fixtures" / "electricity_golden_hashes.json"
KNOWN_FIRST_CSV_UTC = datetime(2025, 7, 29, 23, 0, tzinfo=UTC)
LOCATOR_TOLERANCE = timedelta(days=3)
USAGE_COST_TOLERANCE_PENCE = 5.0


def _credentials() -> tuple[str, str]:
    username = os.environ.get("GLOWMARKT_USERNAME")
    password = os.environ.get("GLOWMARKT_PASSWORD")
    if not username or not password:
        pytest.fail("Protected Bright credentials are not configured")
    return username, password


def _golden() -> dict[str, Any]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def _canonical_hash(rows: list[tuple[datetime, float]]) -> str:
    lines = [
        f"{timestamp.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}|{value:.3f}"
        for timestamp, value in sorted(rows, key=lambda item: item[0])
    ]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def _local_day_window(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=UK_TZ)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=UK_TZ)
    return start, end


def _expected_pt30m_count(day: date) -> int:
    start, end = _local_day_window(day)
    return int((end.astimezone(UTC) - start.astimezone(UTC)).total_seconds() // 1800)


async def _canonical_pt30m_day(
    client: BrightApiClient,
    resource_id: str,
    day: date,
) -> list[tuple[datetime, float]]:
    """Fetch one billing day and map raw Bright labels to interval starts."""
    start, end = _local_day_window(day)
    rows = await client.get_readings(resource_id, start, end + timedelta(minutes=30))
    result: list[tuple[datetime, float]] = []
    for raw_timestamp, value in rows:
        if value is None:
            continue
        interval_start = canonical_pt30m_start(raw_timestamp)
        if start.astimezone(UTC) <= interval_start < end.astimezone(UTC):
            result.append((interval_start, float(value)))
    return sorted(result, key=lambda item: item[0])


async def _p1d_cost_for_day(
    client: BrightApiClient,
    resource_id: str,
    day: date,
) -> float | None:
    """Return Bright's P1D billed total for one Europe/London day."""
    start, end = _local_day_window(day)
    rows = await client.get_readings(resource_id, start, end, period="P1D")
    values = [
        float(value)
        for timestamp, value in rows
        if value is not None and start <= timestamp.astimezone(UK_TZ) < end
    ]
    if len(values) != 1:
        return None
    return values[0]


async def _known_electricity(
    client: BrightApiClient,
) -> tuple[dict[str, dict[str, Any]], datetime]:
    """Find the known electricity site using the proven first-time locator rule."""
    virtual_entities = await client.get_virtual_entities()
    assert virtual_entities, "Live contract: no Bright virtual entities were returned"

    candidates: list[tuple[dict[str, dict[str, Any]], datetime]] = []
    for item in virtual_entities:
        virtual_entity_id = item.get("veId") if isinstance(item, dict) else None
        if not virtual_entity_id:
            continue
        resources = await client.discover_resources(str(virtual_entity_id))
        usage = resources.get(CLASSIFIER_ELECTRICITY_CONSUMPTION)
        cost = resources.get(CLASSIFIER_ELECTRICITY_COST)
        if usage is None or cost is None:
            continue

        locator = await client.get_first_reading_time(usage["resource_id"])
        if locator is None or abs(locator - KNOWN_FIRST_CSV_UTC) > LOCATOR_TOLERANCE:
            continue
        first = await client.get_first_available_reading_time(usage["resource_id"])
        if first is not None:
            candidates.append((resources, first))

    if len(candidates) != 1:
        pytest.fail(
            "Live contract could not uniquely identify the known electricity site; "
            f"candidates={len(candidates)}"
        )

    resources, first = candidates[0]
    print(
        "LIVE DIAGNOSTIC selected site: "
        f"first_raw={first.astimezone(UTC).isoformat()} "
        f"first_canonical={canonical_pt30m_start(first).isoformat()}"
    )
    return resources, first


@pytest.mark.asyncio
async def test_live_pt30m_matches_csv_golden_and_cost_identity(socket_enabled) -> None:
    """Prove canonical PT30M fidelity and Bright PT30M/P1D billing identity."""
    username, password = _credentials()
    golden = _golden()
    gaps = golden["known_upstream_gaps"]

    async with aiohttp.ClientSession() as session:
        client = BrightApiClient(username, password, session)
        resources, first_raw = await _known_electricity(client)
        usage_id = resources[CLASSIFIER_ELECTRICITY_CONSUMPTION]["resource_id"]
        cost_id = resources[CLASSIFIER_ELECTRICITY_COST]["resource_id"]
        first_local_day = canonical_pt30m_start(first_raw).astimezone(UK_TZ).date()

        failures: list[str] = []
        cases = [golden["first_day"], *golden["random_days"], *golden["dst_days"]]
        cases.append(golden["last_known_complete_day"])

        usage_by_offset: dict[int, list[tuple[datetime, float]]] = {}
        for case in cases:
            offset = int(case["offset_days_from_first_local_day"])
            day = first_local_day + timedelta(days=offset)
            rows = await _canonical_pt30m_day(client, usage_id, day)
            usage_by_offset[offset] = rows

            if offset == 0:
                expected_count = int(gaps["first_day_retrievable_count"])
                expected_hash = str(gaps["first_day_retrievable_sha256"])
            else:
                expected_count = int(case["expected_interval_count"])
                expected_hash = str(case["sha256"])

            actual_hash = _canonical_hash(rows)
            if len(rows) != expected_count or actual_hash != expected_hash:
                failures.append(
                    f"PT30M golden offset {offset}: expected_count={expected_count} "
                    f"actual_count={len(rows)} expected_hash={expected_hash} "
                    f"actual_hash={actual_hash}"
                )

        cutover_day = date.fromisoformat(gaps["cutover_local_day"])
        cutover_rows = await _canonical_pt30m_day(client, usage_id, cutover_day)
        if (
            len(cutover_rows) != int(gaps["cutover_retrievable_count"])
            or _canonical_hash(cutover_rows) != gaps["cutover_retrievable_sha256"]
        ):
            failures.append("known PT30M cutover-day upstream gap changed")

        # P1D is the billed-total oracle. PT30M cost is the usage-cost evidence.
        # Historical standing is their residual only for days with complete PT30M
        # cost coverage. Current/effective tariff evidence is used only to prove
        # unit-rate arithmetic where it is actually applicable.
        tariff_rows = await client.get_tariffs(cost_id)
        flat_tariffs = parse_flat_tariffs(tariff_rows)
        unit_rate_checks = 0

        cost_cases = [golden["first_day"], *golden["random_days"]]
        cost_cases.append(golden["last_known_complete_day"])
        for case in cost_cases:
            offset = int(case["offset_days_from_first_local_day"])
            day = first_local_day + timedelta(days=offset)
            usage_rows = usage_by_offset[offset]
            cost_rows = await _canonical_pt30m_day(client, cost_id, day)
            p1d_total = await _p1d_cost_for_day(client, cost_id, day)
            if p1d_total is None:
                failures.append(f"P1D billed total missing for golden offset {offset}")
                continue

            b_pt30m_cost = sum(value for _, value in cost_rows)
            complete_cost_day = len(cost_rows) == _expected_pt30m_count(day)
            residual = p1d_total - b_pt30m_cost

            print(
                "LIVE COST DIAGNOSTIC: "
                f"offset={offset} day={day.isoformat()} pt30m_count={len(cost_rows)} "
                f"complete={complete_cost_day} B={b_pt30m_cost:.12g} "
                f"C={p1d_total:.12g} C_minus_B={residual:.12g}"
            )

            if complete_cost_day and residual < -USAGE_COST_TOLERANCE_PENCE:
                failures.append(f"negative billed residual for golden offset {offset}")

            tariff = tariff_for_day(flat_tariffs, day)
            if tariff is not None and len(usage_rows) == len(cost_rows):
                a_usage_priced = sum(
                    value * tariff.unit_rate_pence_per_kwh for _, value in usage_rows
                )
                unit_rate_checks += 1
                print(
                    "LIVE UNIT RATE DIAGNOSTIC: "
                    f"offset={offset} day={day.isoformat()} "
                    f"rate={tariff.unit_rate_pence_per_kwh:.12g} "
                    f"A={a_usage_priced:.12g} B={b_pt30m_cost:.12g}"
                )
                if abs(a_usage_priced - b_pt30m_cost) > USAGE_COST_TOLERANCE_PENCE:
                    failures.append(f"A != B for tariff-covered golden offset {offset}")

        if unit_rate_checks < 1:
            failures.append("no sampled day had applicable flat unit-rate evidence")

        if failures:
            pytest.fail("Live contract mismatches:\n" + "\n".join(failures))


@pytest.mark.asyncio
async def test_real_pt30m_ledger_populates_persists_and_resumes(hass, socket_enabled) -> None:
    """Exercise raw PT30M and P1D Store paths against Bright."""
    username, password = _credentials()

    async with aiohttp.ClientSession() as session:
        client = BrightApiClient(username, password, session)
        resources, first = await _known_electricity(client)
        selected = {
            CLASSIFIER_ELECTRICITY_CONSUMPTION: resources[CLASSIFIER_ELECTRICITY_CONSUMPTION],
            CLASSIFIER_ELECTRICITY_COST: resources[CLASSIFIER_ELECTRICITY_COST],
        }
        entry_id = "protected_live_contract"

        first_day = canonical_pt30m_start(first).astimezone(UK_TZ).date()
        settled_local = datetime.combine(first_day + timedelta(days=2), time.min, tzinfo=UK_TZ)
        now_utc = settled_local.astimezone(UTC) + timedelta(hours=5)
        first_pass = await async_populate_interval_history(
            hass, client, selected, entry_id, now_utc=now_utc
        )
        state = first_pass["commodities"]["electricity"]
        assert state["status"] == "current"
        assert state.get("last_interval") is not None
        assert state.get("daily_cost_cursor_day") is not None

        repository = IntervalHistoryStore(hass, entry_id)
        month = first.strftime("%Y-%m")
        stored = await repository.async_load_month("electricity", month)
        assert stored
        assert len(stored) == len(set(stored))

        # The Store remains raw: the first stored key is a Bright timestamp, not
        # the canonicalised projection timestamp.
        first_stored_timestamp = min(datetime.fromisoformat(key) for key in stored)
        assert first_stored_timestamp == first.astimezone(UTC)
        assert canonical_pt30m_start(first_stored_timestamp) != first_stored_timestamp

        billing_month = first_day.strftime("%Y-%m")
        daily_before = await repository.async_load_daily_cost_month(
            "electricity", billing_month
        )
        assert daily_before

        second_pass = await async_populate_interval_history(
            hass,
            client,
            selected,
            entry_id,
            now_utc=now_utc + timedelta(days=1),
        )
        second_state = second_pass["commodities"]["electricity"]
        assert second_state["cursor_utc"] != state["cursor_utc"]
        assert second_state["daily_cost_cursor_day"] != state["daily_cost_cursor_day"]

        stored_after_resume = await repository.async_load_month("electricity", month)
        assert len(stored_after_resume) >= len(stored)
        assert len(stored_after_resume) == len(set(stored_after_resume))

        daily_after = await repository.async_load_daily_cost_month(
            "electricity", billing_month
        )
        assert len(daily_after) >= len(daily_before)
        assert len(daily_after) == len(set(daily_after))

        tariff_state = await repository.async_load_tariffs("electricity")
        assert tariff_state.get("rows")
