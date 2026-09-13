"""Repair late Bright history and replay the affected statistics window."""
from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any

from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.components.recorder.util import get_instance
from homeassistant.core import HomeAssistant

from .api import UK_TZ, BrightApiClient
from .history import (
    COMMODITIES,
    IntervalHistoryStore,
    _fetch_records,
    _latest_settled_boundary,
    _parse_utc,
    _utc_iso,
)
from .statistics import (
    StatisticsProjectionError,
    StatisticsProjectionStore,
    _build_hourly_statistics,
    _hour_start,
    _load_daily_costs,
    _load_range,
    _series_metadata,
)

RECENT_RECONCILE_DAYS = 3


def _recent_reconcile_start(target_end: datetime) -> datetime:
    """Return a local-midnight overlap that catches late Bright publication."""
    target_day = target_end.astimezone(UK_TZ).date()
    start_day = target_day - timedelta(days=RECENT_RECONCILE_DAYS)
    return datetime.combine(start_day, time.min, tzinfo=UK_TZ).astimezone(UTC)


async def async_reconcile_recent_history(
    hass: HomeAssistant,
    client: BrightApiClient,
    resources: dict[str, dict[str, Any]],
    entry_id: str,
    *,
    metadata: dict[str, Any] | None = None,
    now_utc: datetime | None = None,
) -> tuple[dict[str, Any], datetime]:
    """Re-fetch a bounded overlap without trusting already-advanced cursors."""
    now = now_utc or datetime.now(UTC)
    target_end = _latest_settled_boundary(now)
    reconcile_start = _recent_reconcile_start(target_end)
    target_day = target_end.astimezone(UK_TZ).date()
    repository = IntervalHistoryStore(hass, entry_id)
    state_root = metadata or await repository.async_load_metadata()
    states = state_root.get("commodities", {})

    if not isinstance(states, dict):
        return state_root, reconcile_start

    for commodity, (usage_classifier, cost_classifier) in COMMODITIES.items():
        state = states.get(commodity)
        if not isinstance(state, dict) or usage_classifier not in resources:
            continue

        first_raw = state.get("first_interval")
        if not first_raw:
            continue
        first_interval = _parse_utc(str(first_raw))
        fetch_start = max(reconcile_start, first_interval)
        if fetch_start >= target_end:
            continue

        records = await _fetch_records(
            client,
            resources,
            usage_classifier,
            cost_classifier,
            fetch_start,
            target_end,
        )
        await repository.async_upsert_intervals(commodity, records)

        if records:
            newest = _parse_utc(str(records[-1]["timestamp"]))
            existing_last = state.get("last_interval")
            if existing_last:
                newest = max(newest, _parse_utc(str(existing_last)))
            state["last_interval"] = _utc_iso(newest)

        cost_resource = resources.get(cost_classifier)
        if cost_resource is not None:
            start_day = fetch_start.astimezone(UK_TZ).date()
            local_start = datetime.combine(start_day, time.min, tzinfo=UK_TZ)
            local_end = datetime.combine(target_day, time.min, tzinfo=UK_TZ)
            daily_rows = await client.get_readings(
                cost_resource["resource_id"],
                local_start,
                local_end,
                period="P1D",
            )
            daily_rows = [
                (timestamp, value)
                for timestamp, value in daily_rows
                if start_day <= timestamp.astimezone(UK_TZ).date() < target_day
            ]
            await repository.async_upsert_daily_costs(commodity, daily_rows)

    await repository.async_save_metadata(state_root)
    return state_root, reconcile_start


async def async_project_reconciled_history(
    hass: HomeAssistant,
    entry_id: str,
    *,
    history_metadata: dict[str, Any],
    replay_from: datetime,
) -> dict[str, Any]:
    """Replay only the repaired tail while recalculating its cumulative prefix."""
    history = IntervalHistoryStore(hass, entry_id)
    projection = StatisticsProjectionStore(hass, entry_id)
    projection_state = await projection.async_load()
    projected = projection_state.setdefault("commodities", {})

    try:
        recorder = get_instance(hass)
    except (KeyError, RuntimeError) as err:
        raise StatisticsProjectionError("Home Assistant Recorder is not available") from err

    for commodity in ("electricity", "gas"):
        ledger_state = history_metadata.get("commodities", {}).get(commodity)
        if not isinstance(ledger_state, dict):
            continue
        first_raw = ledger_state.get("first_interval")
        end_raw = ledger_state.get("cursor_utc")
        if not first_raw or not end_raw:
            continue

        ledger_start = _hour_start(_parse_utc(str(first_raw)))
        ledger_end = _parse_utc(str(end_raw))
        commodity_state = projected.setdefault(commodity, {})
        existing_cursor = (
            _parse_utc(str(commodity_state["cursor_utc"]))
            if commodity_state.get("cursor_utc")
            else None
        )

        running_raw = commodity_state.get("running", {})
        running = running_raw if isinstance(running_raw, dict) else {}
        projection_start = existing_cursor or ledger_start

        if existing_cursor is not None:
            requested_replay = max(ledger_start, replay_from.astimezone(UTC))
            if requested_replay < existing_cursor:
                prefix_records = await _load_range(
                    history,
                    commodity,
                    ledger_start,
                    requested_replay,
                )
                prefix_daily_costs = await _load_daily_costs(
                    history,
                    commodity,
                    ledger_start,
                    requested_replay,
                )
                _, running = _build_hourly_statistics(
                    prefix_records,
                    [],
                    ledger_start,
                    requested_replay,
                    {},
                    daily_costs=prefix_daily_costs,
                )
                projection_start = requested_replay

        if projection_start >= ledger_end:
            continue

        records = await _load_range(history, commodity, projection_start, ledger_end)
        daily_costs = await _load_daily_costs(history, commodity, projection_start, ledger_end)
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

        await recorder.async_block_till_done()
        commodity_state["cursor_utc"] = ledger_end.isoformat()
        commodity_state["running"] = new_running
        await projection.async_save(projection_state)

    return projection_state
