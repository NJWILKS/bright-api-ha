"""Resumable PT30M usage, cost and tariff history for Bright API."""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import UTC, datetime, time, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .api import UK_TZ, BrightApiClient, BrightApiError
from .const import (
    CLASSIFIER_ELECTRICITY_CONSUMPTION,
    CLASSIFIER_ELECTRICITY_COST,
    CLASSIFIER_GAS_CONSUMPTION,
    CLASSIFIER_GAS_COST,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

HISTORY_STORAGE_VERSION = 1
HISTORY_SCHEMA_VERSION = 1
HISTORY_CHUNK_DAYS = 9
HISTORY_REFRESH_LOCAL_TIME = time(4, 0)

COMMODITIES: dict[str, tuple[str, str]] = {
    "electricity": (
        CLASSIFIER_ELECTRICITY_CONSUMPTION,
        CLASSIFIER_ELECTRICITY_COST,
    ),
    "gas": (
        CLASSIFIER_GAS_CONSUMPTION,
        CLASSIFIER_GAS_COST,
    ),
}


def _utc_iso(value: datetime) -> str:
    """Return one stable UTC timestamp representation."""
    return value.astimezone(UTC).isoformat()


def _parse_utc(value: str) -> datetime:
    """Parse a stored timestamp and normalise it to UTC."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _latest_settled_boundary(value: datetime) -> datetime:
    """Return the latest UTC midnight considered settled after 04:00 UK time."""
    local = value.astimezone(UK_TZ)
    settled_date = local.date()
    if local.timetz().replace(tzinfo=None) < HISTORY_REFRESH_LOCAL_TIME:
        settled_date -= timedelta(days=1)
    return datetime(settled_date.year, settled_date.month, settled_date.day, tzinfo=UTC)


def _next_refresh_utc(value: datetime) -> datetime:
    """Return the next 04:00 Europe/London refresh instant in UTC."""
    local = value.astimezone(UK_TZ)
    candidate = datetime.combine(local.date(), HISTORY_REFRESH_LOCAL_TIME, tzinfo=UK_TZ)
    if local >= candidate:
        next_day = local.date() + timedelta(days=1)
        candidate = datetime.combine(next_day, HISTORY_REFRESH_LOCAL_TIME, tzinfo=UK_TZ)
    return candidate.astimezone(UTC)


def _month_key(value: datetime) -> str:
    """Return the UTC storage month for an interval."""
    value = value.astimezone(UTC)
    return f"{value.year:04d}-{value.month:02d}"


def _merge_rows(
    usage_rows: list[tuple[datetime, float | None]],
    cost_rows: list[tuple[datetime, float | None]],
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """Join raw PT30M facts by UTC timestamp without deriving values."""
    usage = {
        _utc_iso(timestamp): float(value)
        for timestamp, value in usage_rows
        if value is not None and start <= timestamp.astimezone(UTC) < end
    }
    cost = {
        _utc_iso(timestamp): float(value)
        for timestamp, value in cost_rows
        if value is not None and start <= timestamp.astimezone(UTC) < end
    }

    return [
        {
            "timestamp": timestamp,
            "usage_kwh": usage.get(timestamp),
            "cost_pence": cost.get(timestamp),
        }
        for timestamp in sorted(set(usage) | set(cost))
    ]


class IntervalHistoryStore:
    """Persist small metadata plus bounded monthly interval files."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._metadata_store = Store(
            hass,
            HISTORY_STORAGE_VERSION,
            f"{DOMAIN}_{entry_id}_interval_history",
        )

    def _interval_store(self, commodity: str, month: str) -> Store:
        return Store(
            self._hass,
            HISTORY_STORAGE_VERSION,
            f"{DOMAIN}_{self._entry_id}_interval_history_{commodity}_{month.replace('-', '_')}",
        )

    def _tariff_store(self, commodity: str) -> Store:
        return Store(
            self._hass,
            HISTORY_STORAGE_VERSION,
            f"{DOMAIN}_{self._entry_id}_tariffs_{commodity}",
        )

    async def async_load_metadata(self) -> dict[str, Any]:
        """Load resumable history metadata."""
        state = await self._metadata_store.async_load() or {}
        if state.get("schema_version") != HISTORY_SCHEMA_VERSION:
            return {"schema_version": HISTORY_SCHEMA_VERSION, "commodities": {}}
        commodities = state.get("commodities")
        if not isinstance(commodities, dict):
            state["commodities"] = {}
        return state

    async def async_save_metadata(self, state: dict[str, Any]) -> None:
        """Persist history metadata."""
        state["schema_version"] = HISTORY_SCHEMA_VERSION
        await self._metadata_store.async_save(state)

    async def async_save_tariffs(
        self,
        commodity: str,
        rows: list[dict[str, Any]],
        refreshed_at: datetime,
    ) -> None:
        """Persist the API's effective-dated tariff rows separately from intervals."""
        await self._tariff_store(commodity).async_save(
            {
                "schema_version": HISTORY_SCHEMA_VERSION,
                "commodity": commodity,
                "refreshed_at": _utc_iso(refreshed_at),
                "rows": rows,
            }
        )

    async def async_load_tariffs(self, commodity: str) -> dict[str, Any]:
        """Load stored tariff evidence."""
        state = await self._tariff_store(commodity).async_load() or {}
        return state if isinstance(state, dict) else {}

    async def async_upsert_intervals(
        self,
        commodity: str,
        records: list[dict[str, Any]],
    ) -> None:
        """Timestamp-keyed idempotent upsert into UTC monthly files."""
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in records:
            grouped[_month_key(_parse_utc(record["timestamp"]))].append(record)

        for month, month_records in grouped.items():
            store = self._interval_store(commodity, month)
            state = await store.async_load() or {}
            intervals = state.get("intervals", {})
            if not isinstance(intervals, dict):
                intervals = {}

            for record in month_records:
                intervals[record["timestamp"]] = record

            await store.async_save(
                {
                    "schema_version": HISTORY_SCHEMA_VERSION,
                    "commodity": commodity,
                    "month": month,
                    "intervals": intervals,
                }
            )

    async def async_load_month(
        self,
        commodity: str,
        month: str,
    ) -> dict[str, dict[str, Any]]:
        """Load interval rows for diagnostics and tests."""
        state = await self._interval_store(commodity, month).async_load() or {}
        intervals = state.get("intervals", {})
        return intervals if isinstance(intervals, dict) else {}


async def _first_interval(
    client: BrightApiClient,
    resources: dict[str, dict[str, Any]],
    usage_classifier: str,
) -> datetime | None:
    """Return the first actual PT30M usage row; cost never defines history start."""
    resource = resources.get(usage_classifier)
    if resource is None:
        return None
    first = await client.get_first_available_reading_time(resource["resource_id"])
    return first.astimezone(UTC) if first is not None else None


async def _fetch_records(
    client: BrightApiClient,
    resources: dict[str, dict[str, Any]],
    usage_classifier: str,
    cost_classifier: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """Fetch one bounded usage/cost window and return joined raw facts."""
    usage_resource = resources.get(usage_classifier)
    cost_resource = resources.get(cost_classifier)

    usage_rows = (
        await client.get_readings(usage_resource["resource_id"], start, end)
        if usage_resource is not None
        else []
    )
    cost_rows = (
        await client.get_readings(cost_resource["resource_id"], start, end)
        if cost_resource is not None
        else []
    )
    return _merge_rows(usage_rows, cost_rows, start, end)


def _history_due(
    metadata: dict[str, Any],
    resources: dict[str, dict[str, Any]],
    target_end: datetime,
) -> bool:
    """Return whether any supported usage ledger is behind the settled boundary."""
    states = metadata.get("commodities")
    if not isinstance(states, dict):
        return True

    for commodity, (usage_classifier, _cost_classifier) in COMMODITIES.items():
        if usage_classifier not in resources:
            continue
        state = states.get(commodity)
        if not isinstance(state, dict) or not state.get("cursor_utc"):
            return True
        if _parse_utc(state["cursor_utc"]) < target_end:
            return True
    return False


async def async_populate_interval_history(
    hass: HomeAssistant,
    client: BrightApiClient,
    resources: dict[str, dict[str, Any]],
    entry_id: str,
    *,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Populate or resume raw PT30M history to the latest settled UTC boundary."""
    now = now_utc or datetime.now(UTC)
    target_end = _latest_settled_boundary(now)
    repository = IntervalHistoryStore(hass, entry_id)
    metadata = await repository.async_load_metadata()
    commodities = metadata.setdefault("commodities", {})

    for commodity, (usage_classifier, cost_classifier) in COMMODITIES.items():
        if usage_classifier not in resources:
            continue

        state = commodities.setdefault(commodity, {})
        existing_cursor = _parse_utc(state["cursor_utc"]) if state.get("cursor_utc") else None
        if existing_cursor is not None and existing_cursor >= target_end:
            continue

        cost_resource = resources.get(cost_classifier)
        if cost_resource is not None:
            tariffs = await client.get_tariffs(cost_resource["resource_id"])
            await repository.async_save_tariffs(commodity, tariffs, target_end)
            state["tariff_rows"] = len(tariffs)

        first = (
            _parse_utc(state["first_interval"])
            if state.get("first_interval")
            else await _first_interval(client, resources, usage_classifier)
        )
        if first is None:
            state["status"] = "no_data"
            await repository.async_save_metadata(metadata)
            continue

        state["first_interval"] = _utc_iso(first)
        cursor = existing_cursor or first
        cursor = max(cursor, first)

        while cursor < target_end:
            chunk_end = min(cursor + timedelta(days=HISTORY_CHUNK_DAYS), target_end)
            records = await _fetch_records(
                client,
                resources,
                usage_classifier,
                cost_classifier,
                cursor,
                chunk_end,
            )

            # Data is durable before the cursor advances. If HA stops between
            # these two writes, this chunk is replayed and timestamp-keyed upserts
            # make the replay harmless.
            await repository.async_upsert_intervals(commodity, records)
            state["cursor_utc"] = _utc_iso(chunk_end)
            if records:
                state["last_interval"] = records[-1]["timestamp"]
            state["status"] = "current" if chunk_end >= target_end else "populating"
            await repository.async_save_metadata(metadata)
            cursor = chunk_end

        if cursor >= target_end:
            state["cursor_utc"] = _utc_iso(target_end)
            state["status"] = "current"
            await repository.async_save_metadata(metadata)

        _LOGGER.info(
            "Bright PT30M history for %s is %s through %s",
            commodity,
            state.get("status"),
            state.get("cursor_utc"),
        )

    return metadata


async def async_interval_history_worker(
    hass: HomeAssistant,
    client: BrightApiClient,
    resources: dict[str, dict[str, Any]],
    entry_id: str,
) -> None:
    """Backfill when needed, then refresh settled history once daily at 04:00 UK time."""
    repository = IntervalHistoryStore(hass, entry_id)

    while True:
        now = datetime.now(UTC)
        metadata = await repository.async_load_metadata()
        target_end = _latest_settled_boundary(now)

        if _history_due(metadata, resources, target_end):
            try:
                await async_populate_interval_history(
                    hass,
                    client,
                    resources,
                    entry_id,
                    now_utc=now,
                )
            except BrightApiError as err:
                _LOGGER.warning("Bright interval history population paused: %s", err)

        next_refresh = _next_refresh_utc(now)
        await asyncio.sleep(max(1.0, (next_refresh - datetime.now(UTC)).total_seconds()))
