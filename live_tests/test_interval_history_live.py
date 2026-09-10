"""Protected live contracts for Bright PT30M, tariffs, costs and ledger persistence."""
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

pytestmark = pytest.mark.live

GOLDEN = Path(__file__).parents[1] / "tests" / "fixtures" / "electricity_golden_hashes.json"
USAGE_COST_TOLERANCE_PENCE = 5.0
TOTAL_COST_TOLERANCE_PENCE = 15.0
STANDING_TOLERANCE_PENCE = 15.0


def _credentials() -> tuple[str, str]:
    username = os.environ.get("GLOWMARKT_USERNAME")
    password = os.environ.get("GLOWMARKT_PASSWORD")
    if not username or not password:
        pytest.skip("Protected Bright credentials are not configured")
    return username, password


def _golden() -> dict[str, Any]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def _canonical_hash(rows: list[tuple[datetime, float | None]]) -> str:
    lines = [
        f"{timestamp.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}|{float(value):.3f}"
        for timestamp, value in sorted(rows, key=lambda item: item[0])
        if value is not None
    ]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def _local_day_window(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=UK_TZ)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=UK_TZ)
    return start, end


def _window_rows(
    rows: list[tuple[datetime, float | None]], start: datetime, end: datetime
) -> list[tuple[datetime, float | None]]:
    return [
        (timestamp, value)
        for timestamp, value in rows
        if start <= timestamp.astimezone(UK_TZ) < end and value is not None
    ]


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _walk(value: Any, found: dict[str, list[Any]]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in found:
                found[key].append(child)
            _walk(child, found)
    elif isinstance(value, list):
        for child in value:
            _walk(child, found)


def _unique_numbers(values: list[Any]) -> list[float]:
    result: list[float] = []
    for value in values:
        parsed = _number(value)
        if parsed is None:
            continue
        if not any(abs(parsed - existing) < 1e-9 for existing in result):
            result.append(parsed)
    return result


def _flat_tariff_rows(rows: list[dict[str, Any]]) -> list[tuple[date, float, float]]:
    """Return explicit flat tariff evidence as (effective, unit rate, standing)."""
    result: list[tuple[date, float, float]] = []
    for row in rows:
        raw_effective = row.get("effectiveDate") or row.get("from") or row.get("effective")
        if not raw_effective:
            continue
        try:
            effective = date.fromisoformat(str(raw_effective)[:10])
        except ValueError:
            continue

        found: dict[str, list[Any]] = {
            "standing": [],
            "standingCharge": [],
            "rate": [],
            "tourate": [],
            "dynamic": [],
            "time": [],
        }
        _walk(row.get("plan", row), found)
        if any(value not in (None, "", False) for value in found["dynamic"]):
            continue
        if any(value not in (None, "", False) for value in found["time"]):
            continue
        if any(value not in (None, "", False) for value in found["tourate"]):
            continue

        rates = _unique_numbers(found["rate"])
        standing = _unique_numbers(found["standing"] + found["standingCharge"])
        if len(rates) == 1 and len(standing) == 1:
            result.append((effective, rates[0], standing[0]))

    return sorted(result, key=lambda item: item[0])


def _tariff_for_day(
    tariffs: list[tuple[date, float, float]], day: date
) -> tuple[float, float] | None:
    applicable = [item for item in tariffs if item[0] <= day]
    if not applicable:
        return None
    _, unit_rate, standing = applicable[-1]
    return unit_rate, standing


async def _known_electricity(
    client: BrightApiClient,
) -> tuple[dict[str, dict[str, Any]], datetime]:
    """Find the electricity site by matching the CSV-derived first-interval fingerprint."""
    expected = _golden()["first_interval_sha256"]
    virtual_entities = await client.get_virtual_entities()
    assert virtual_entities, "Live contract: no Bright virtual entities were returned"

    for item in virtual_entities:
        virtual_entity_id = item.get("veId") if isinstance(item, dict) else None
        if not virtual_entity_id:
            continue
        resources = await client.discover_resources(str(virtual_entity_id))
        usage = resources.get(CLASSIFIER_ELECTRICITY_CONSUMPTION)
        cost = resources.get(CLASSIFIER_ELECTRICITY_COST)
        if usage is None or cost is None:
            continue

        first = await client.get_first_available_reading_time(usage["resource_id"])
        if first is None:
            continue
        first_rows = await client.get_readings(
            usage["resource_id"], first, first + timedelta(minutes=30)
        )
        first_exact = [(timestamp, value) for timestamp, value in first_rows if timestamp == first]
        if _canonical_hash(first_exact) == expected:
            return resources, first

    pytest.fail("Live contract: no electricity site matched the golden first interval")


@pytest.mark.asyncio
async def test_live_pt30m_matches_csv_golden_and_cost_identity(socket_enabled) -> None:
    """Prove CSV PT30M fidelity and A/B/S/C tariff-cost reconciliation."""
    username, password = _credentials()
    golden = _golden()

    async with aiohttp.ClientSession() as session:
        client = BrightApiClient(username, password, session)
        resources, first = await _known_electricity(client)
        usage_id = resources[CLASSIFIER_ELECTRICITY_CONSUMPTION]["resource_id"]
        cost_id = resources[CLASSIFIER_ELECTRICITY_COST]["resource_id"]
        first_local_day = first.astimezone(UK_TZ).date()

        cases = [golden["first_day"], *golden["random_days"], *golden["dst_days"]]
        cases.append(golden["last_known_complete_day"])
        day_rows: dict[int, list[tuple[datetime, float | None]]] = {}

        for case in cases:
            offset = int(case["offset_days_from_first_local_day"])
            day = first_local_day + timedelta(days=offset)
            start, end = _local_day_window(day)
            rows = _window_rows(await client.get_readings(usage_id, start, end), start, end)
            assert len(rows) == int(case["expected_interval_count"]), (
                f"Live contract: PT30M interval count mismatch for golden offset {offset}"
            )
            assert _canonical_hash(rows) == case["sha256"], (
                f"Live contract: PT30M timestamp/value mismatch for golden offset {offset}"
            )
            day_rows[offset] = rows

        tariff_rows = await client.get_tariffs(cost_id)
        tariffs = _flat_tariff_rows(tariff_rows)
        assert tariffs, "Live contract: no explicit flat tariff with unit rate and standing charge"

        # Cost contract: first day + five fixed random days + last known good day.
        cost_cases = [golden["first_day"], *golden["random_days"]]
        cost_cases.append(golden["last_known_complete_day"])
        for case in cost_cases:
            offset = int(case["offset_days_from_first_local_day"])
            day = first_local_day + timedelta(days=offset)
            start, end = _local_day_window(day)
            tariff = _tariff_for_day(tariffs, day)
            assert tariff is not None, (
                f"Live contract: no explicit tariff evidence for golden offset {offset}"
            )
            unit_rate, standing = tariff

            usage_rows = day_rows[offset]
            a_usage_priced = sum(
                float(value) * unit_rate
                for _, value in usage_rows
                if value is not None
            )

            pt30m_cost_rows = _window_rows(
                await client.get_readings(cost_id, start, end, period="PT30M"), start, end
            )
            p1d_cost_rows = _window_rows(
                await client.get_readings(cost_id, start, end, period="P1D"), start, end
            )
            assert pt30m_cost_rows, (
                f"Live contract: Bright PT30M cost missing for golden offset {offset}"
            )
            assert p1d_cost_rows, (
                f"Live contract: Bright P1D cost missing for golden offset {offset}"
            )

            b_pt30m_cost = sum(float(value) for _, value in pt30m_cost_rows if value is not None)
            c_p1d_cost = sum(float(value) for _, value in p1d_cost_rows if value is not None)

            assert abs(a_usage_priced - b_pt30m_cost) <= USAGE_COST_TOLERANCE_PENCE, (
                f"Live contract: A != B for golden offset {offset}"
            )
            assert abs((a_usage_priced + standing) - c_p1d_cost) <= TOTAL_COST_TOLERANCE_PENCE, (
                f"Live contract: A + S != C for golden offset {offset}"
            )
            assert abs((c_p1d_cost - b_pt30m_cost) - standing) <= STANDING_TOLERANCE_PENCE, (
                f"Live contract: C - B != S for golden offset {offset}"
            )


@pytest.mark.asyncio
async def test_real_pt30m_ledger_populates_persists_and_resumes(hass, socket_enabled) -> None:
    """Exercise the production ledger path against Bright and Home Assistant Store."""
    username, password = _credentials()

    async with aiohttp.ClientSession() as session:
        client = BrightApiClient(username, password, session)
        resources, first = await _known_electricity(client)
        selected = {
            CLASSIFIER_ELECTRICITY_CONSUMPTION: resources[CLASSIFIER_ELECTRICITY_CONSUMPTION],
            CLASSIFIER_ELECTRICITY_COST: resources[CLASSIFIER_ELECTRICITY_COST],
        }
        entry_id = "protected_live_contract"

        # Use a settled boundary beyond the first day so this exercises the real
        # population code without performing a full-account backfill in CI.
        first_day = first.astimezone(UK_TZ).date()
        settled = datetime.combine(first_day + timedelta(days=2), time.min, tzinfo=UTC)
        first_pass = await async_populate_interval_history(
            hass, client, selected, entry_id, now_utc=settled + timedelta(hours=5)
        )
        state = first_pass["commodities"]["electricity"]
        assert state["status"] == "current"
        assert state.get("last_interval") is not None

        repository = IntervalHistoryStore(hass, entry_id)
        month = first.strftime("%Y-%m")
        stored = await repository.async_load_month("electricity", month)
        assert stored
        assert len(stored) == len(set(stored))

        second_pass = await async_populate_interval_history(
            hass, client, selected, entry_id, now_utc=settled + timedelta(days=1, hours=5)
        )
        second_state = second_pass["commodities"]["electricity"]
        assert second_state["cursor_utc"] != state["cursor_utc"]

        stored_after_resume = await repository.async_load_month("electricity", month)
        assert len(stored_after_resume) >= len(stored)
        assert len(stored_after_resume) == len(set(stored_after_resume))

        tariff_state = await repository.async_load_tariffs("electricity")
        assert tariff_state.get("rows")
