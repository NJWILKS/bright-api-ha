"""Tariff parsing helpers for Bright API."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass(frozen=True, slots=True)
class FlatTariff:
    """One effective-dated flat Bright tariff."""

    effective_date: date
    unit_rate_pence_per_kwh: float
    standing_charge_pence_per_day: float


def _number(value: Any) -> float | None:
    """Return a numeric Bright tariff value when possible."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _walk(value: Any, found: dict[str, list[Any]]) -> None:
    """Collect tariff fields recursively without assuming one API shape."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key in found:
                found[key].append(child)
            _walk(child, found)
    elif isinstance(value, list):
        for child in value:
            _walk(child, found)


def _unique_numbers(values: list[Any]) -> list[float]:
    """Return distinct numeric values while preserving API order."""
    result: list[float] = []
    for value in values:
        parsed = _number(value)
        if parsed is None:
            continue
        if not any(abs(parsed - existing) < 1e-9 for existing in result):
            result.append(parsed)
    return result


def parse_flat_tariffs(rows: list[dict[str, Any]]) -> list[FlatTariff]:
    """Extract only unambiguous flat rate + standing-charge tariff evidence.

    Time-of-use and dynamic tariffs are deliberately not guessed. They remain
    stored as raw tariff evidence in the history ledger until an explicit
    projection rule is implemented and proven by the live contract.
    """
    result: list[FlatTariff] = []
    for row in rows:
        raw_effective = row.get("effectiveDate") or row.get("from") or row.get("effective")
        if not raw_effective:
            continue
        try:
            effective = date.fromisoformat(str(raw_effective)[:10])
        except ValueError:
            continue

        found: dict[str, list[Any]] = {
            "standing": [],
            "standingCharge": [],
            "rate": [],
            "tourate": [],
            "dynamic": [],
            "time": [],
        }
        _walk(row.get("plan", row), found)
        if any(value not in (None, "", False) for value in found["dynamic"]):
            continue
        if any(value not in (None, "", False) for value in found["time"]):
            continue
        if any(value not in (None, "", False) for value in found["tourate"]):
            continue

        rates = _unique_numbers(found["rate"])
        standing = _unique_numbers(found["standing"] + found["standingCharge"])
        if len(rates) == 1 and len(standing) == 1:
            result.append(
                FlatTariff(
                    effective_date=effective,
                    unit_rate_pence_per_kwh=rates[0],
                    standing_charge_pence_per_day=standing[0],
                )
            )

    return sorted(result, key=lambda item: item.effective_date)


def tariff_for_day(tariffs: list[FlatTariff], day: date) -> FlatTariff | None:
    """Return the latest tariff effective on a Europe/London billing day."""
    applicable = [item for item in tariffs if item.effective_date <= day]
    return applicable[-1] if applicable else None
