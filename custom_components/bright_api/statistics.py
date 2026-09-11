"""Project the Bright raw ledger into Home Assistant external statistics."""
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
from .interval_semantics import canonical_pt30m_start

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
    """Map a raw Bright PT30M label to its canonical UTC statistics hour."""
    return canonical_pt30m_start(value).replace(minute=0, second=0, microsecond=0)


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


def _expected_pt30m_count(day: date) -> int:
    """Return the number of half-hours in one Europe/London billing day."""
    start = datetime.combine(day, time.min, tzinfo=UK_TZ).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=UK_TZ).astimezone(UTC)
    return int((end - start).total_seconds() // 1800)


def _billing_midnight(day: date) -> datetime:
    """Return a billing day's timezone-aware midnight as a UTC instant."""
    return datetime.combine(day, time.min, tzinfo=UK_TZ).astimezone(UTC)


def _build_hourly_statistics(
    records: list[dict[str, Any]],
    tariffs: list[Any],
    start: datetime,
    end: datetime,
    running: dict[str, float] | None = None,
    *,
    daily_costs: dict[date, float] | None = None,
) -> tuple[dict[str, list[StatisticData]], dict[str, float]]:
    """Build deterministic projections without changing raw Bright facts."""
    del tariffs  # Tariffs describe current rates; historical billing comes from Bright cost data.
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
    daily_usage_cost: dict[date, float] = defaultdict(float)
    daily_usage_cost_count: dict[date, int] = defaultdict(int)

    for record in records:
        raw_timestamp = _parse_utc(str(record["timestamp"]))
        if not start <= raw_timestamp < end:
            continue
        interval_start = canonical_pt30m_start(raw_timestamp)
        hour = interval_start.replace(minute=0, second=0, microsecond=0)
        billing_day = interval_start.astimezone(UK_TZ).date()

        usage = record.get("usage_kwh")
        if usage is not None:
            buckets[hour]["consumption"] = float(buckets[hour]["consumption"]) + float(usage)
            buckets[hour]["has_consumption"] = True

        cost = record.get("cost_pence")
        if cost is not None:
            cost_gbp = float(cost) / 100.0
            buckets[hour]["usage_cost"] = float(buckets[hour]["usage_cost"]) + cost_gbp
            buckets[hour]["has_usage_cost"] = True
            daily_usage_cost[billing_day] += cost_gbp
            daily_usage_cost_count[billing_day] += 1

    result: dict[str, list[StatisticData]] = {measure: [] for measure in MEASURES}

    for hour in sorted(buckets):
        bucket = buckets[hour]
        if bool(bucket["has_consumption"]):
            consumption = float(bucket["consumption"])
            totals[MEASURE_CONSUMPTION] += consumption
            result[MEASURE_CONSUMPTION].append(
                StatisticData(
                    start=hour,
                    state=consumption,
                    sum=totals[MEASURE_CONSUMPTION],
                )
            )

        if bool(bucket["has_usage_cost"]):
            usage_cost = float(bucket["usage_cost"])
            totals[MEASURE_USAGE_COST] += usage_cost
            result[MEASURE_USAGE_COST].append(
                StatisticData(
                    start=hour,
                    state=usage_cost,
                    sum=totals[MEASURE_USAGE_COST],
                )
            )

    # Bright P1D cost is the authoritative billed total for a completed local day.
    # Standing charge is only derived where PT30M cost coverage for that day is
    # complete, so a missing upstream interval can never be silently folded into it.
    for billing_day, total_pence in sorted((daily_costs or {}).items()):
        midnight = _billing_midnight(billing_day)
        if not start <= midnight < end:
            continue

        total_cost = float(total_pence) / 100.0
        totals[MEASURE_TOTAL_COST] += total_cost
        result[MEASURE_TOTAL_COST].append(
            StatisticData(
                start=midnight,
                state=total_cost,
                sum=totals[MEASURE_TOTAL_COST],
            )
        )

        if daily_usage_cost_count.get(billing_day, 0) != _expected_pt30m_count(billing_day):
            continue
        standing_charge = total_cost - daily_usage_cost.get(billing_day, 0.0)
        if standing_charge < -0.005:
            continue
        standing_charge = max(0.0, standing_charge)
        totals[MEASURE_STANDING_CHARGE] += standing_charge
        result[MEASURE_STANDING_CHARGE].append(
            StatisticData(
                start=midnight,
                state=standing_charge,
                sum=totals[MEASURE_STANDING_CHARGE],
            )
        )

    return result, totals


class StatisticsProjectionStore:
    """Persist only projection cursors/totals; the raw history remains truth."""

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


def _local_month_keys(start: datetime, end: datetime) -> list[str]:
    """Return local calendar months that can contribute P1D billing evidence."""
    if start >= end:
        return []
    first = canonical_pt30m_start(start).astimezone(UK_TZ).date().replace(day=1)
    last = (end - timedelta(microseconds=1)).astimezone(UK_TZ).date().replace(day=1)
    current = first
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
    """Load durable raw interval rows in a UTC half-open range."""
    records: list[dict[str, Any]] = []
    for month in _month_keys(start, end):
        stored = await history.async_load_month(commodity, month)
        for record in stored.values():
            timestamp = _parse_utc(str(record["timestamp"]))
            if start <= timestamp < end:
                records.append(record)
    return sorted(records, key=lambda item: _parse_utc(str(item["timestamp"])))


async def _load_daily_costs(
    history: IntervalHistoryStore,
    commodity: str,
    start: datetime,
    end: datetime,
) -> dict[date, float]:
    """Load Bright P1D cost evidence keyed by Europe/London billing day."""
    result: dict[date, float] = {}
    for month in _local_month_keys(start, end):
        stored = await history.async_load_daily_cost_month(commodity, month)
        for record in stored.values():
            raw_day = record.get("billing_day")
            raw_cost = record.get("cost_pence")
            if raw_day is None or raw_cost is None:
                continue
            billing_day = date.fromisoformat(str(raw_day))
            midnight = _billing_midnight(billing_day)
            if start <= midnight < end:
                result[billing_day] = float(raw_cost)
    return result


async def async_project_interval_history(
    hass: HomeAssistant,
    entry_id: str,
    *,
    history_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Incrementally project durable Bright history into HA external statistics."""
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
        daily_costs = await _load_daily_costs(history, commodity, projection_start, ledger_end)
        running_raw = commodity_state.get("running", {})
        running = running_raw if isinstance(running_raw, dict) else {}
        batches, new_running = _build_hourly_statistics(
            records,
            [],
            projection_start,
            ledger_end,
            running,
            daily_costs=daily_costs,
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
