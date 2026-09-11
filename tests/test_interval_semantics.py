from __future__ import annotations

from datetime import UTC, datetime

from custom_components.bright_api.interval_semantics import (
    PT30M_START_LABEL_CUTOVER_UTC,
    canonical_pt30m_start,
)


def test_pre_cutover_labels_are_interval_ends() -> None:
    assert canonical_pt30m_start(datetime(2026, 8, 29, 23, 30, tzinfo=UTC)) == datetime(
        2026, 8, 29, 23, 0, tzinfo=UTC
    )


def test_cutover_and_later_labels_are_interval_starts() -> None:
    assert PT30M_START_LABEL_CUTOVER_UTC == datetime(2026, 8, 30, 0, 0, tzinfo=UTC)
    assert canonical_pt30m_start(PT30M_START_LABEL_CUTOVER_UTC) == PT30M_START_LABEL_CUTOVER_UTC
    assert canonical_pt30m_start(datetime(2026, 9, 1, 12, 30, tzinfo=UTC)) == datetime(
        2026, 9, 1, 12, 30, tzinfo=UTC
    )
