"""Interpret Bright PT30M labels without mutating raw ledger facts."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

# Protected live comparison against an authoritative Bright export shows that
# historical PT30M readings were labelled at the end of each half-hour until
# Bright changed to interval-start labels at this UTC boundary. The raw API
# timestamp is always retained in Store; only downstream interpretation uses
# this mapping.
PT30M_START_LABEL_CUTOVER_UTC = datetime(2026, 8, 30, 0, 0, tzinfo=UTC)


def canonical_pt30m_start(timestamp: datetime) -> datetime:
    """Return the canonical UTC interval start for a raw Bright PT30M label."""
    raw = timestamp.astimezone(UTC)
    if raw < PT30M_START_LABEL_CUTOVER_UTC:
        return raw - timedelta(minutes=30)
    return raw
