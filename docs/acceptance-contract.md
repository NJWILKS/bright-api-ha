# Bright API acceptance contract

This contract defines the behaviours that must remain true for every release. These are not aspirational tests; they are release gates.

## 1. Historical start point

The first interval accepted by the integration must be the first actual non-null PT30M reading. The API `first-time` endpoint is only a locator used to find the neighbourhood of the first real reading.

**Required evidence**

- Golden-data test against a sanitised extract derived from an authoritative Bright export CSV.
- Protected live contract against the real API.
- The timestamp must match the CSV's first actual PT30M interval.

## 2. PT30M fidelity

PT30M timestamps and values are canonical historical usage data.

**Required evidence**

- Golden-data comparison against the authoritative Bright export CSV.
- Genuine `0.0` readings remain `0.0`, not missing/null.
- UTC timestamp identity is preserved through UK DST changes; local duplicate clock times must remain distinct instants.
- No interpolation or synthetic filling is allowed in the raw ledger.

## 3. Tariff fidelity

Unit prices and standing charges must come from Bright tariff API evidence and must be applied only to the effective period they describe.

**Required evidence**

- Golden-data comparison against tariff values evidenced by the authoritative export/reference data.
- Tests for tariff changes at an effective-date boundary.
- Raw tariff evidence is stored separately from interval facts.

## 4. Daily cost identity

For any Europe/London billing day:

```text
daily usage cost = Σ(PT30M usage kWh × effective unit price for that interval)
daily total cost = daily usage cost + one applicable standing charge
```

For a flat tariff this reduces to:

```text
daily total cost = Σ(PT30M usage kWh) × unit price + standing charge
```

Standing charge is applied exactly once per local billing day, including 46-interval and 50-interval DST days.

A tariff type that cannot be calculated from the available API evidence must remain explicitly unavailable rather than silently using a different formula.

**Required evidence**

- Pure arithmetic unit tests.
- Golden-data daily reconciliation against one or more known CSV days.
- Tests spanning tariff changes and DST transitions.

## 5. Home Assistant presentation capability

The internal model must preserve enough information to expose at least these historical monetary series independently:

- usage/unit cost;
- standing charge;
- total cost.

Home Assistant must therefore be able to display a daily stacked chart with **usage cost** and **standing charge** as the two components. Their combined height is the daily total. A separate total-cost statistic may also exist for Energy-dashboard compatibility but must not be required as a third stacked series.

**Required evidence**

- Projection-layer tests proving usage and standing remain separate.
- Home Assistant statistics integration test proving both series can be queried historically.
- Real HA acceptance showing a daily stacked chart can be configured from those series.

## 6. No cumulative meter total in Home Assistant

The Bright cumulative meter reading is not part of the Home Assistant contract and must never be exposed as an entity, long-term statistic, Energy source or correctness baseline.

The integration's historical truth is the settled PT30M interval series. A cumulative meter total that cannot be independently reconciled during the current day is not useful evidence and must not leak into presentation or projection code.

**Required evidence**

- Entity/statistics tests assert that no cumulative-total entity or statistic is created.
- Projection code accepts interval data, not a cumulative meter total.

## 7. Post-backfill history cadence

Initial backfill may make multiple bounded requests until the ledger is current. Once current, historical PT30M ingestion must switch to one scheduled refresh cycle at **04:00 Europe/London** each day.

The daily refresh ingests only newly settled intervals. It must be restart-safe and idempotent. It must not repeatedly poll historical readings throughout the day.

Bright resource APIs are resource-specific, so a single scheduled refresh cycle can contain one request per relevant resource. The requirement is one daily history-sync cycle, not one literal HTTP request for all commodities/resources.

**Required evidence**

- Scheduler test proving the next run resolves to 04:00 Europe/London across BST/GMT transitions.
- Test proving a current ledger requests only the un-ingested settled range.
- Restart test proving a missed/duplicated scheduler invocation cannot duplicate intervals.

## Golden data policy

The original household export must not be committed wholesale to the public repository. Instead, derive a minimal sanitised fixture containing only the timestamps, PT30M values, tariff values and daily expected totals necessary to prove this contract. Do not commit credentials, tokens, account identifiers, resource IDs, addresses or unrelated household history.

Any PR touching first-reading discovery, PT30M retrieval, tariff interpretation, cost arithmetic, Home Assistant statistics or history scheduling must identify which contract sections it affects and show the corresponding tests passing before merge.
