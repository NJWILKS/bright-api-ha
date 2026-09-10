from __future__ import annotations

from datetime import date

from custom_components.bright_api.tariffs import parse_flat_tariffs, tariff_for_day


def test_flat_tariff_parser_only_accepts_unambiguous_flat_rates() -> None:
    rows = [
        {
            "effectiveDate": "2026-01-01 00:00:00",
            "plan": [{"planDetail": [{"rate": "24.5", "standing": "51.2"}]}],
        },
        {
            "effectiveDate": "2026-06-01 00:00:00",
            "plan": [
                {
                    "planDetail": [
                        {"rate": 18.0, "standing": 48.0, "time": "00:00-07:00"},
                        {"rate": 30.0, "standing": 48.0, "time": "07:00-00:00"},
                    ]
                }
            ],
        },
    ]

    tariffs = parse_flat_tariffs(rows)

    assert len(tariffs) == 1
    assert tariffs[0].effective_date == date(2026, 1, 1)
    assert tariffs[0].unit_rate_pence_per_kwh == 24.5
    assert tariffs[0].standing_charge_pence_per_day == 51.2


def test_tariff_for_day_uses_latest_effective_flat_tariff() -> None:
    tariffs = parse_flat_tariffs(
        [
            {
                "effectiveDate": "2026-01-01",
                "plan": [{"rate": 20.0, "standingCharge": 40.0}],
            },
            {
                "effectiveDate": "2026-03-01",
                "plan": [{"rate": 22.0, "standingCharge": 45.0}],
            },
        ]
    )

    assert tariff_for_day(tariffs, date(2025, 12, 31)) is None
    assert tariff_for_day(tariffs, date(2026, 2, 1)).unit_rate_pence_per_kwh == 20.0
    assert tariff_for_day(tariffs, date(2026, 3, 1)).standing_charge_pence_per_day == 45.0
