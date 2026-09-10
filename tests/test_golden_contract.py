from __future__ import annotations

import json
from pathlib import Path

FIXTURE_PATH = Path("tests/fixtures/electricity_golden_hashes.json")


def test_golden_contract_covers_required_days() -> None:
    contract = json.loads(FIXTURE_PATH.read_text())

    assert contract["schema_version"] == 1
    assert len(contract["first_interval_sha256"]) == 64
    assert contract["first_day"]["offset_days_from_first_local_day"] == 0
    assert len(contract["random_days"]) >= 5
    assert contract["last_known_complete_day"]["offset_days_from_first_local_day"] > 0

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
