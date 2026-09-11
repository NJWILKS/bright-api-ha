from __future__ import annotations

import json
from pathlib import Path

FIXTURE_PATH = Path("tests/fixtures/electricity_golden_hashes.json")


def test_golden_contract_covers_required_days_and_upstream_gaps() -> None:
    contract = json.loads(FIXTURE_PATH.read_text())

    assert contract["schema_version"] == 2
    assert len(contract["first_interval_sha256"]) == 64
    assert contract["first_day"]["offset_days_from_first_local_day"] == 0
    assert len(contract["random_days"]) >= 5
    assert contract["last_known_complete_day"]["offset_days_from_first_local_day"] > 0

    gaps = contract["known_upstream_gaps"]
    assert gaps["leading_pt30m_intervals"] == 1
    assert gaps["first_day_retrievable_count"] == 47
    assert gaps["cutover_local_day"] == "2026-08-30"
    assert gaps["cutover_missing_intervals"] == 1
    assert gaps["cutover_retrievable_count"] == 47
    assert len(gaps["first_retrievable_interval_sha256"]) == 64
    assert len(gaps["first_day_retrievable_sha256"]) == 64
    assert len(gaps["cutover_retrievable_sha256"]) == 64

    cases = [
        contract["first_day"],
        *contract["random_days"],
        *contract["dst_days"],
        contract["last_known_complete_day"],
    ]
    offsets = [case["offset_days_from_first_local_day"] for case in cases]
    assert len(offsets) == len(set(offsets))
    assert all(len(case["sha256"]) == 64 for case in cases)


def test_golden_contract_keeps_both_uk_dst_day_lengths() -> None:
    contract = json.loads(FIXTURE_PATH.read_text())
    counts = {case["expected_interval_count"] for case in contract["dst_days"]}

    assert counts == {46, 50}
