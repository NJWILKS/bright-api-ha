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
from .sync import async_project_reconciled_history, async_reconcile_recent_history

_LOGGER = logging.getLogger(__name__)


def history_updated_signal(entry_id: str) -> str:
    """Return the dispatcher signal for one Bright config entry."""
    return f"{DOMAIN}_{entry_id}_history_updated"


async def async_sync_history_and_statistics_once(
    hass: HomeAssistant,
    client: BrightApiClient,
    resources: dict[str, dict[str, Any]],
    entry_id: str,
    operation_lock: asyncio.Lock | None = None,
) -> None:
    """Run one durable sync, recent reconciliation and Recorder projection."""
    repository = IntervalHistoryStore(hass, entry_id)
    lock = operation_lock or asyncio.Lock()

    async with lock:
        now = datetime.now(UTC)
        metadata = await repository.async_load_metadata()
        target_end = _latest_settled_boundary(now)
        bright_error: BrightApiError | BrightAuthError | None = None
        replay_from = None

        try:
            if _history_due(metadata, resources, target_end):
                metadata = await async_populate_interval_history(
                    hass,
                    client,
                    resources,
                    entry_id,
                    now_utc=now,
                )

            # Bright can publish settled data later than our 04:00 query. Always
            # re-fetch a short overlap instead of trusting an advanced cursor to
            # mean every row was actually available at the earlier request time.
            metadata, replay_from = await async_reconcile_recent_history(
                hass,
                client,
                resources,
                entry_id,
                metadata=metadata,
                now_utc=now,
            )
            async_dispatcher_send(hass, history_updated_signal(entry_id))
        except (BrightApiError, BrightAuthError) as err:
            # Durable ledger facts can still be projected even when Bright is
            # temporarily unavailable. Surface the network/auth failure after the
            # safe projection attempt so a manual caller still gets a real error.
            bright_error = err

        if replay_from is not None:
            await async_project_reconciled_history(
                hass,
                entry_id,
                history_metadata=metadata,
                replay_from=replay_from,
            )
        else:
            await async_project_interval_history(
                hass,
                entry_id,
                history_metadata=metadata,
            )
        async_dispatcher_send(hass, history_updated_signal(entry_id))

        if bright_error is not None:
            raise bright_error


async def async_history_and_statistics_worker(
    hass: HomeAssistant,
    client: BrightApiClient,
    resources: dict[str, dict[str, Any]],
    entry_id: str,
    operation_lock: asyncio.Lock | None = None,
) -> None:
    """Populate settled history and project it once durable."""
    lock = operation_lock or asyncio.Lock()

    while True:
        try:
            await async_sync_history_and_statistics_once(
                hass,
                client,
                resources,
                entry_id,
                lock,
            )
        except asyncio.CancelledError:
            raise
        except (BrightApiError, BrightAuthError) as err:
            _LOGGER.warning("Bright history sync paused: %s", err)
        except StatisticsProjectionError as err:
            _LOGGER.warning("Bright statistics projection paused: %s", err)
        except Exception:
            # A long-lived daily worker must not disappear permanently because of
            # one unexpected cycle failure. Log the traceback and retry next day.
            _LOGGER.exception("Unexpected Bright daily sync failure")

        next_refresh = _next_refresh_utc(datetime.now(UTC))
        await asyncio.sleep(max(1.0, (next_refresh - datetime.now(UTC)).total_seconds()))
