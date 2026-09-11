# Bright API Home Assistant

A clean Home Assistant custom integration for Hildebrand Bright / Glowmarkt smart-meter history.

## What it does

The integration treats Bright's settled historical data as evidence rather than trying to manufacture a live meter:

- discovers the electricity/gas resources available to the selected Bright site;
- backfills the retrievable PT30M history into an integration-owned Home Assistant Store ledger;
- stores Bright P1D billed-cost evidence separately from PT30M facts;
- projects the durable ledger into Home Assistant external statistics;
- resumes safely after restart without duplicating interval or billing-day data;
- refreshes newly settled history once each day at **04:00 Europe/London**;
- never exposes Bright's cumulative meter total as an entity or statistic.

The interval ledger is the historical source of truth for this integration. Home Assistant statistics are a rebuildable projection of that ledger.

## Historical statistics

For each supported commodity the integration can own these external statistics:

- **Consumption** — canonical PT30M usage, projected in kWh;
- **Usage Cost** — Bright PT30M cost;
- **Standing Charge** — `P1D billed total - PT30M usage cost`, only for billing days with complete PT30M cost coverage;
- **Total Cost** — Bright P1D billed cost.

Home Assistant's cumulative `sum` field is used only as external-statistics bookkeeping. It is not a synthetic cumulative meter reading.

## Bright timestamp behaviour

Raw Bright PT30M timestamps and values are stored unchanged.

Protected comparison with an authoritative Bright export proves a Bright timestamp-label convention change at `2026-08-30T00:00:00Z` for the tested service data:

- before the cutover, Bright labels PT30M rows at interval end, so projection interprets the canonical interval start as `raw timestamp - 30 minutes`;
- at and after the cutover, Bright labels rows at interval start and the timestamp is unchanged.

The repository's protected contract pins this rule with non-reversible CSV-derived fingerprints. It also pins two observed upstream omissions: one leading zero interval at the beginning of the API history and one interval on the cutover day. The integration **does not synthesize either missing interval**.

## Billing and tariffs

Historical billing does not backdate today's tariff.

- PT30M cost is the historical usage-cost evidence.
- P1D cost is the historical billed-total evidence.
- Standing charge is derived from their residual only when the local billing day contains the expected complete set of PT30M cost intervals (normally 48, 46/50 across UK DST transitions).
- Tariff API data remains available for current rate presentation and for protected unit-rate cross-checks where an explicit effective date covers the day being tested.

If the evidence is incomplete, the integration leaves the derived standing-charge value unavailable rather than guessing.

## Home Assistant entities

Presentation entities are deliberately small and read only durable integration data. Depending on the resources Bright exposes, each commodity device can provide:

- history status;
- last settled interval;
- current flat unit rate where the tariff API proves one;
- current standing charge where the tariff API proves one.

Historical consumption/cost lives in Home Assistant long-term statistics rather than thousands of synthetic sensor states.

## Reset and rebuild

The admin-only `bright_api.reset_rebuild` action is the recovery escape hatch. For the selected Bright config entry it clears only:

- that entry's PT30M Store files;
- that entry's P1D billed-cost Store files;
- that entry's tariff cache and history/projection cursors;
- external statistics owned by that entry.

It then refetches Bright history and rebuilds the statistics projection. It does not clean up earlier integrations and does not touch another Bright entry's data.

## Privacy and release contract

The public repository contains non-reversible fingerprints, counts and structural expectations derived from the authoritative household export. It does not contain the raw household CSV, credentials, account identifiers, resource IDs or household interval values.

Changes affecting history discovery, timestamp interpretation, billing arithmetic, statistics projection, scheduling or rebuild behaviour must pass normal CI and the protected Bright live contract before release. See `docs/acceptance-contract.md` for the full contract.

No migration or compatibility code from earlier Hildebrand integrations is carried into this repository.
