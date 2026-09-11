"""Coordinate settled Bright history ingestion and HA statistics projection."""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .api import BrightApiClient, BrightApiError, BrightAuthError
from .const import DOMAIN
from .history import (
    IntervalHistoryStore,
    _history_due,
    _latest_settled_boundary,
    _next_refresh_utc,
    async_populate_interval_history,
)
from .statistics import StatisticsProjectionError, async_project_interval_history

_LOGGER = logging.getLogger(__name__)


def history_updated_signal(entry_id: str) -> str:
    """Return the dispatcher signal for one Bright config entry."""
    return f"{DOMAIN}_{entry_id}_history_updated"


async def async_history_and_statistics_worker(
    hass: HomeAssistant,
    client: BrightApiClient,
    resources: dict[str, dict[str, Any]],
    entry_id: str,
    operation_lock: asyncio.Lock | None = None,
) -> None:
    """Populate settled history and project it once durable."""
    repository = IntervalHistoryStore(hass, entry_id)
    lock = operation_lock or asyncio.Lock()

    while True:
        async with lock:
            now = datetime.now(UTC)
            metadata = await repository.async_load_metadata()
            target_end = _latest_settled_boundary(now)

            if _history_due(metadata, resources, target_end):
                try:
                    metadata = await async_populate_interval_history(
                        hass,
                        client,
                        resources,
                        entry_id,
                        now_utc=now,
                    )
                    async_dispatcher_send(hass, history_updated_signal(entry_id))
                except (BrightApiError, BrightAuthError) as err:
                    _LOGGER.warning("Bright interval history population paused: %s", err)

            # Projection has its own checkpoint and is attempted even when the
            # Bright ledger is already current. This closes the crash window where
            # ledger writes completed but Recorder projection had not yet run.
            try:
                await async_project_interval_history(
                    hass,
                    entry_id,
                    history_metadata=metadata,
                )
                async_dispatcher_send(hass, history_updated_signal(entry_id))
            except StatisticsProjectionError as err:
                # The ledger is already durable. A Recorder problem must never
                # force another Bright backfill; projection can safely resume.
                _LOGGER.warning("Bright statistics projection paused: %s", err)

        next_refresh = _next_refresh_utc(datetime.now(UTC))
        await asyncio.sleep(max(1.0, (next_refresh - datetime.now(UTC)).total_seconds()))
