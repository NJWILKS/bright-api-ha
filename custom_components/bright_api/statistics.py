"""Project the Bright PT30M ledger into Home Assistant external statistics."""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.components.recorder.util import get_instance
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util.unit_conversion import EnergyConverter

from .api import UK_TZ
from .const import DOMAIN
from .history import IntervalHistoryStore, _parse_utc
from .tariffs import FlatTariff, parse_flat_tariffs, tariff_for_day

PROJECTION_STORAGE_VERSION = 1
PROJECTION_SCHEMA_VERSION = 1

MEASURE_CONSUMPTION = "consumption"
MEASURE_USAGE_COST = "usage_cost"
MEASURE_STANDING_CHARGE = "standing_charge"
MEASURE_TOTAL_COST = "total_cost"

MEASURES = (
    MEASURE_CONSUMPTION,
    MEASURE_USAGE_COST,
    MEASURE_STANDING_CHARGE,
    MEASURE_TOTAL_COST,
)


class StatisticsProjectionError(Exception):
    """Home Assistant statistics could not be projected safely."""


def _safe_entry_id(entry_id: str) -> str:
    """Return an object-id-safe config-entry token."""
    value = re.sub(r"[^a-z0-9_]+", "_", entry_id.lower()).strip("_")
    return value or "entry"


def statistic_id(entry_id: str, commodity: str, measure: str) -> str:
    """Return one stable external statistic ID owned by this integration."""
    return f"{DOMAIN}:{_safe_entry_id(entry_id)}_{commodity}_{measure}"


def owned_statistic_ids(entry_id: str) -> list[str]:
    """Return every external statistic ID this config entry may own."""
    return [
        statistic_id(entry_id, commodity, measure)
        for commodity in ("electricity", "gas")
        for measure in MEASURES
    ]


def _hour_start(value: datetime) -> datetime:
    """Map a raw Bright timestamp to its deterministic UTC statistics hour."""
    return value.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def _series_metadata(
    entry_id: str,
    commodity: str,
    measure: str,
) -> StatisticMetaData:
    """Build 2026-era Home Assistant external-statistics metadata."""
    labels = {
        MEASURE_CONSUMPTION: "Consumption",
        MEASURE_USAGE_COST: "Usage Cost",
        MEASURE_STANDING_CHARGE: "Standing Charge",
        MEASURE_TOTAL_COST: "Total Cost",
    }
    is_energy = measure == MEASURE_CONSUMPTION
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=f"Bright {commodity.title()} {labels[measure]}",
        source=DOMAIN,
        statistic_id=statistic_id(entry_id, commodity, measure),
        unit_class=EnergyConverter.UNIT_CLASS if is_energy else None,
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR if is_energy else "GBP",
    )


def _local_midnight_rows(
    tariffs: list[FlatTariff],
    start: datetime,
    end: datetime,
) -> dict[datetime, float]:
    """Return GBP standing charge at each applicable local billing-day start."""
    if not tariffs or start >= end:
        return {}

    first_day = start.astimezone(UK_TZ).date() - timedelta(days=1)
    last_day = end.astimezone(UK_TZ).date() + timedelta(days=1)
    current = first_day
    result: dict[datetime, float] = {}
    while current <= last_day:
        local_midnight = datetime.combine(current, time.min, tzinfo=UK_TZ)
        instant = local_midnight.astimezone(UTC)
        if start <= instant < end:
            tariff = tariff_for_day(tariffs, current)
            if tariff is not None:
                result[instant] = tariff.standing_charge_pence_per_day / 100.0
        current += timedelta(days=1)
    return result


def _build_hourly_statistics(
    records: list[dict[str, Any]],
    tariffs: list[FlatTariff],
    start: datetime,
    end: datetime,
    running: dict[str, float] | None = None,
) -> tuple[dict[str, list[StatisticData]], dict[str, float]]:
    """Build deterministic hourly projections without changing raw ledger facts."""
    totals = {measure: 0.0 for measure in MEASURES}
    if running:
        for measure in MEASURES:
            totals[measure] = float(running.get(measure, 0.0))

    buckets: dict[datetime, dict[str, float | bool]] = defaultdict(
        lambda: {
            "consumption": 0.0,
            "usage_cost": 0.0,
            "has_consumption": False,
            "has_usage_cost": False,
        }
    )
    for record in records:
        timestamp = _parse_utc(str(record["timestamp"]))
        if not start <= timestamp < end:
            continue
        hour = _hour_start(timestamp)
        usage = record.get("usage_kwh")
        if usage is not None:
            buckets[hour]["consumption"] = float(buckets[hour]["consumption"]) + float(usage)
            buckets[hour]["has_consumption"] = True
        cost = record.get("cost_pence")
        if cost is not None:
            buckets[hour]["usage_cost"] = float(buckets[hour]["usage_cost"]) + float(cost) / 100.0
            buckets[hour]["has_usage_cost"] = True

    standing = _local_midnight_rows(tariffs, start, end)
    hours = sorted(set(buckets) | set(standing))
    result: dict[str, list[StatisticData]] = {measure: [] for measure in MEASURES}

    for hour in hours:
        bucket = buckets.get(hour, {})
        has_consumption = bool(bucket.get("has_consumption", False))
        has_usage_cost = bool(bucket.get("has_usage_cost", False))
        consumption = float(bucket.get("consumption", 0.0))
        usage_cost = float(bucket.get("usage_cost", 0.0))
        standing_charge = standing.get(hour)

        if has_consumption:
            totals[MEASURE_CONSUMPTION] += consumption
            result[MEASURE_CONSUMPTION].append(
                StatisticData(
                    start=hour,
                    state=consumption,
                    sum=totals[MEASURE_CONSUMPTION],
                )
            )

        if has_usage_cost:
            totals[MEASURE_USAGE_COST] += usage_cost
            result[MEASURE_USAGE_COST].append(
                StatisticData(
                    start=hour,
                    state=usage_cost,
                    sum=totals[MEASURE_USAGE_COST],
                )
            )

        if standing_charge is not None:
            totals[MEASURE_STANDING_CHARGE] += standing_charge
            result[MEASURE_STANDING_CHARGE].append(
                StatisticData(
                    start=hour,
                    state=standing_charge,
                    sum=totals[MEASURE_STANDING_CHARGE],
                )
            )

        # A total-cost projection is only emitted where a flat tariff proves
        # the standing-charge component for the local billing day. Bright PT30M
        # cost remains the usage-cost source; we never derive it from consumption.
        local_day: date = hour.astimezone(UK_TZ).date()
        if tariff_for_day(tariffs, local_day) is not None and (
            has_usage_cost or standing_charge is not None
        ):
            total_cost = usage_cost + (standing_charge or 0.0)
            totals[MEASURE_TOTAL_COST] += total_cost
            result[MEASURE_TOTAL_COST].append(
                StatisticData(
                    start=hour,
                    state=total_cost,
                    sum=totals[MEASURE_TOTAL_COST],
                )
            )

    return result, totals


class StatisticsProjectionStore:
    """Persist only projection cursors/totals; the interval ledger remains truth."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store = Store(
            hass,
            PROJECTION_STORAGE_VERSION,
            f"{DOMAIN}_{entry_id}_statistics_projection",
        )

    async def async_load(self) -> dict[str, Any]:
        """Load restart-safe projection state."""
        state = await self._store.async_load() or {}
        if state.get("schema_version") != PROJECTION_SCHEMA_VERSION:
            return {"schema_version": PROJECTION_SCHEMA_VERSION, "commodities": {}}
        if not isinstance(state.get("commodities"), dict):
            state["commodities"] = {}
        return state

    async def async_save(self, state: dict[str, Any]) -> None:
        """Save projection state after Recorder has accepted the batch."""
        state["schema_version"] = PROJECTION_SCHEMA_VERSION
        await self._store.async_save(state)

    async def async_remove(self) -> None:
        """Remove the rebuildable projection checkpoint."""
        await self._store.async_remove()


def _month_keys(start: datetime, end: datetime) -> list[str]:
    """Return UTC month keys intersecting [start, end)."""
    if start >= end:
        return []
    current = start.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last = (end - timedelta(microseconds=1)).astimezone(UTC)
    result: list[str] = []
    while current <= last:
        result.append(f"{current.year:04d}-{current.month:02d}")
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)
    return result


async def _load_range(
    history: IntervalHistoryStore,
    commodity: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """Load durable interval rows in a UTC half-open range."""
    records: list[dict[str, Any]] = []
    for month in _month_keys(start, end):
        stored = await history.async_load_month(commodity, month)
        for record in stored.values():
            timestamp = _parse_utc(str(record["timestamp"]))
            if start <= timestamp < end:
                records.append(record)
    return sorted(records, key=lambda item: _parse_utc(str(item["timestamp"])))


async def async_project_interval_history(
    hass: HomeAssistant,
    entry_id: str,
    *,
    history_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Incrementally project durable ledger rows into HA external statistics."""
    history = IntervalHistoryStore(hass, entry_id)
    metadata = history_metadata or await history.async_load_metadata()
    projection = StatisticsProjectionStore(hass, entry_id)
    projection_state = await projection.async_load()
    projected = projection_state.setdefault("commodities", {})

    try:
        recorder = get_instance(hass)
    except (KeyError, RuntimeError) as err:
        raise StatisticsProjectionError("Home Assistant Recorder is not available") from err

    for commodity in ("electricity", "gas"):
        ledger_state = metadata.get("commodities", {}).get(commodity)
        if not isinstance(ledger_state, dict):
            continue
        first_raw = ledger_state.get("first_interval")
        end_raw = ledger_state.get("cursor_utc")
        if not first_raw or not end_raw:
            continue

        ledger_start = _hour_start(_parse_utc(str(first_raw)))
        ledger_end = _parse_utc(str(end_raw))
        commodity_state = projected.setdefault(commodity, {})
        projection_start = (
            _parse_utc(str(commodity_state["cursor_utc"]))
            if commodity_state.get("cursor_utc")
            else ledger_start
        )
        if projection_start >= ledger_end:
            continue

        records = await _load_range(history, commodity, projection_start, ledger_end)
        tariff_state = await history.async_load_tariffs(commodity)
        raw_tariffs = tariff_state.get("rows", [])
        tariffs = parse_flat_tariffs(raw_tariffs if isinstance(raw_tariffs, list) else [])
        running_raw = commodity_state.get("running", {})
        running = running_raw if isinstance(running_raw, dict) else {}
        batches, new_running = _build_hourly_statistics(
            records,
            tariffs,
            projection_start,
            ledger_end,
            running,
        )

        for measure, statistics in batches.items():
            if not statistics:
                continue
            async_add_external_statistics(
                hass,
                _series_metadata(entry_id, commodity, measure),
                statistics,
            )

        # Recorder jobs are queued. Do not advance our checkpoint until all
        # queued writes have completed; replaying the same window is harmless.
        await recorder.async_block_till_done()
        commodity_state["cursor_utc"] = ledger_end.isoformat()
        commodity_state["running"] = new_running
        await projection.async_save(projection_state)

    return projection_state


async def async_clear_projected_statistics(hass: HomeAssistant, entry_id: str) -> None:
    """Delete only statistics and projection state owned by this config entry."""
    recorder = get_instance(hass)
    recorder.async_clear_statistics(owned_statistic_ids(entry_id))
    await recorder.async_block_till_done()
    await StatisticsProjectionStore(hass, entry_id).async_remove()
