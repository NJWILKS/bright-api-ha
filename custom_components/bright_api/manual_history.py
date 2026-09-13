"""Manual non-destructive Bright history repair operations."""
from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .api import UK_TZ, BrightApiClient
from .const import DOMAIN
from .history import (
    COMMODITIES,
    HISTORY_CHUNK_DAYS,
    IntervalHistoryStore,
    _fetch_records,
    _first_interval,
    _latest_settled_boundary,
    _parse_utc,
    _utc_iso,
)
from .interval_semantics import canonical_pt30m_start
from .sync import async_project_reconciled_history


class ManualHistoryError(Exception):
    """Raised when a manual history refresh request is invalid or unavailable."""


def latest_settled_billing_day(now_utc: datetime | None = None) -> date:
    """Return the latest fully settled Europe/London billing day."""
    boundary = _latest_settled_boundary(now_utc or datetime.now(UTC))
    return boundary.astimezone(UK_TZ).date() - timedelta(days=1)


def _local_midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UK_TZ)


async def _refresh_range_unlocked(
    hass: HomeAssistant,
    client: BrightApiClient,
    resources: dict[str, dict[str, Any]],
    entry_id: str,
    start_day: date,
    end_day: date,
) -> datetime:
    """Re-fetch an inclusive local billing-day range into the durable ledger."""
    if start_day > end_day:
        raise ManualHistoryError("Start date must not be after end date")

    latest_day = latest_settled_billing_day()
    if end_day > latest_day:
        raise ManualHistoryError(
            f"End date must not be after the latest settled day ({latest_day.isoformat()})"
        )

    repository = IntervalHistoryStore(hass, entry_id)
    metadata = await repository.async_load_metadata()
    states = metadata.setdefault("commodities", {})
    if not isinstance(states, dict):
        raise ManualHistoryError("Bright history metadata is not ready")

    replay_from = _local_midnight(start_day).astimezone(UTC)
    requested_end = _local_midnight(end_day + timedelta(days=1)).astimezone(UTC)
    any_supported = False

    for commodity, (usage_classifier, cost_classifier) in COMMODITIES.items():
        usage_resource = resources.get(usage_classifier)
        if usage_resource is None:
            continue
        any_supported = True

        state = states.setdefault(commodity, {})
        first = (
            _parse_utc(str(state["first_interval"]))
            if state.get("first_interval")
            else await _first_interval(client, resources, usage_classifier)
        )
        if first is None:
            continue
        state["first_interval"] = _utc_iso(first)

        first_day = canonical_pt30m_start(first).astimezone(UK_TZ).date()
        commodity_start_day = max(start_day, first_day)
        if commodity_start_day > end_day:
            continue

        chunk_day = commodity_start_day
        newest_record: datetime | None = None
        while chunk_day <= end_day:
            chunk_end_day = min(
                chunk_day + timedelta(days=HISTORY_CHUNK_DAYS),
                end_day + timedelta(days=1),
            )
            canonical_start = _local_midnight(chunk_day).astimezone(UTC)
            canonical_end = _local_midnight(chunk_end_day).astimezone(UTC)

            # Before the known Bright cutover PT30M timestamps are interval-end labels.
            # Fetch an extra half hour and then filter by canonical interval start so a
            # manually selected billing day is complete on either timestamp regime.
            records = await _fetch_records(
                client,
                resources,
                usage_classifier,
                cost_classifier,
                canonical_start,
                canonical_end + timedelta(minutes=30),
            )
            records = [
                record
                for record in records
                if canonical_start
                <= canonical_pt30m_start(_parse_utc(str(record["timestamp"])))
                < canonical_end
            ]
            await repository.async_upsert_intervals(commodity, records)
            if records:
                candidate = _parse_utc(str(records[-1]["timestamp"]))
                newest_record = max(newest_record, candidate) if newest_record else candidate

            cost_resource = resources.get(cost_classifier)
            if cost_resource is not None:
                local_start = _local_midnight(chunk_day)
                local_end = _local_midnight(chunk_end_day)
                daily_rows = await client.get_readings(
                    cost_resource["resource_id"],
                    local_start,
                    local_end,
                    period="P1D",
                )
                daily_rows = [
                    (timestamp, value)
                    for timestamp, value in daily_rows
                    if chunk_day <= timestamp.astimezone(UK_TZ).date() < chunk_end_day
                ]
                await repository.async_upsert_daily_costs(commodity, daily_rows)

            chunk_day = chunk_end_day

        if newest_record is not None:
            existing = state.get("last_interval")
            if existing:
                newest_record = max(newest_record, _parse_utc(str(existing)))
            state["last_interval"] = _utc_iso(newest_record)

    if not any_supported:
        raise ManualHistoryError("No supported Bright history resources are loaded")

    await repository.async_save_metadata(metadata)
    await async_project_reconciled_history(
        hass,
        entry_id,
        history_metadata=metadata,
        replay_from=replay_from,
    )
    async_dispatcher_send(hass, f"{DOMAIN}_{entry_id}_history_updated")
    return requested_end


async def async_refresh_history_range(
    hass: HomeAssistant,
    client: BrightApiClient,
    resources: dict[str, dict[str, Any]],
    entry_id: str,
    operation_lock: asyncio.Lock,
    start_day: date,
    end_day: date,
) -> None:
    """Refresh an inclusive local billing-day range and replay affected statistics."""
    async with operation_lock:
        await _refresh_range_unlocked(
            hass,
            client,
            resources,
            entry_id,
            start_day,
            end_day,
        )


async def async_refresh_all_history(
    hass: HomeAssistant,
    client: BrightApiClient,
    resources: dict[str, dict[str, Any]],
    entry_id: str,
    operation_lock: asyncio.Lock,
) -> None:
    """Re-fetch all retrievable Bright history without deleting existing evidence."""
    async with operation_lock:
        repository = IntervalHistoryStore(hass, entry_id)
        metadata = await repository.async_load_metadata()
        states = metadata.setdefault("commodities", {})
        first_days: list[date] = []

        for commodity, (usage_classifier, _cost_classifier) in COMMODITIES.items():
            if usage_classifier not in resources:
                continue
            state = states.setdefault(commodity, {})
            first = (
                _parse_utc(str(state["first_interval"]))
                if state.get("first_interval")
                else await _first_interval(client, resources, usage_classifier)
            )
            if first is None:
                continue
            state["first_interval"] = _utc_iso(first)
            first_days.append(canonical_pt30m_start(first).astimezone(UK_TZ).date())

        if not first_days:
            raise ManualHistoryError("Bright returned no retrievable history")
        await repository.async_save_metadata(metadata)

        await _refresh_range_unlocked(
            hass,
            client,
            resources,
            entry_id,
            min(first_days),
            latest_settled_billing_day(),
        )
