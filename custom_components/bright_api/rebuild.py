"""Destructive-but-scoped history reset and rebuild support."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .api import UK_TZ, BrightApiClient
from .const import DOMAIN
from .history import (
    COMMODITIES,
    HISTORY_STORAGE_VERSION,
    IntervalHistoryStore,
    _parse_utc,
    async_populate_interval_history,
)
from .orchestrator import history_updated_signal
from .statistics import async_clear_projected_statistics, async_project_interval_history


def _month_keys(start: datetime, end: datetime, *, local: bool = False) -> list[str]:
    """Return storage month keys covering a history range, inclusively."""
    zone = UK_TZ if local else UTC
    current = start.astimezone(zone).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last = end.astimezone(zone).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    months: list[str] = []
    while current <= last:
        months.append(f"{current.year:04d}_{current.month:02d}")
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)
    return months


async def async_clear_interval_history(hass: HomeAssistant, entry_id: str) -> None:
    """Delete only raw/tariff/billing Store data owned by one Bright config entry."""
    repository = IntervalHistoryStore(hass, entry_id)
    metadata = await repository.async_load_metadata()
    commodity_states = metadata.get("commodities", {})
    if not isinstance(commodity_states, dict):
        commodity_states = {}

    for commodity in COMMODITIES:
        state = commodity_states.get(commodity, {})
        if isinstance(state, dict) and state.get("first_interval"):
            start = _parse_utc(str(state["first_interval"]))
            end_raw = state.get("cursor_utc") or state.get("last_interval") or state["first_interval"]
            end = _parse_utc(str(end_raw))
            for month in _month_keys(start, end):
                await Store(
                    hass,
                    HISTORY_STORAGE_VERSION,
                    f"{DOMAIN}_{entry_id}_interval_history_{commodity}_{month}",
                ).async_remove()
            for month in _month_keys(start, end, local=True):
                await Store(
                    hass,
                    HISTORY_STORAGE_VERSION,
                    f"{DOMAIN}_{entry_id}_daily_cost_{commodity}_{month}",
                ).async_remove()

        await Store(
            hass,
            HISTORY_STORAGE_VERSION,
            f"{DOMAIN}_{entry_id}_tariffs_{commodity}",
        ).async_remove()

    await Store(
        hass,
        HISTORY_STORAGE_VERSION,
        f"{DOMAIN}_{entry_id}_interval_history",
    ).async_remove()


async def async_reset_and_rebuild(
    hass: HomeAssistant,
    client: BrightApiClient,
    resources: dict[str, dict[str, Any]],
    entry_id: str,
) -> dict[str, Any]:
    """Clear this entry's rebuildable state, refetch Bright and re-project."""
    # Recorder is cleared first. If it is unavailable, abort before touching the
    # canonical raw ledger. Everything cleared below is owned by this entry.
    await async_clear_projected_statistics(hass, entry_id)
    await async_clear_interval_history(hass, entry_id)

    metadata = await async_populate_interval_history(
        hass,
        client,
        resources,
        entry_id,
        now_utc=datetime.now(UTC),
    )
    projection = await async_project_interval_history(
        hass,
        entry_id,
        history_metadata=metadata,
    )
    async_dispatcher_send(hass, history_updated_signal(entry_id))
    return {"history": metadata, "projection": projection}
