# Bright API acceptance contract

This contract defines the behaviours that must remain true for every release. These are not aspirational tests; they are release gates.

## 1. Historical start point

The first interval accepted by the integration must be the first actual non-null PT30M reading. The API `first-time` endpoint is only a locator used to find the neighbourhood of the first real reading.

**Required evidence**

- Golden-data test derived from an authoritative Bright export CSV.
- Protected live contract against the real API.
- The first actual interval must match the CSV-derived golden fingerprint exactly.

## 2. PT30M fidelity

PT30M timestamps and values are canonical historical usage data.

**Required evidence**

Every protected live acceptance run must compare complete PT30M days against CSV-derived golden fingerprints for:

- the first complete billing day in the export;
- at least five randomly selected complete billing days, selected once with a recorded deterministic seed and then fixed as regression cases;
- the last known complete/good billing day in the export;
- known UK DST transition days where present in the export.

For every selected day, both timestamps and values must match. Genuine `0.0` readings remain `0.0`, UTC timestamp identity is preserved through UK DST changes, and no interpolation or synthetic filling is allowed in the raw ledger.

To avoid publishing household interval data, the repository stores SHA-256 fingerprints of canonically normalised complete days rather than the raw export rows. The original export itself is not committed.

## 3. Tariff fidelity

Unit prices and standing charges must come from Bright tariff API evidence and must be applied only to the effective period they describe.

The tariff API is not trusted in isolation. Its unit price and standing charge must be reconciled against Bright's own cost resource for completed billing days.

**Required evidence**

- Tests for tariff changes at an effective-date boundary.
- Raw tariff evidence is stored separately from interval facts.
- Protected live reconciliation proves the effective unit rate reproduces PT30M usage cost and the standing charge reproduces the residual between PT30M and P1D cost.

## 4. Daily cost identity and four-way reconciliation

For any completed Europe/London billing day define:

```text
A = Σ(PT30M consumption kWh × effective API unit price for each interval)
B = Σ(Bright PT30M cost resource)
S = applicable standing charge from the Bright tariff API
C = Bright P1D cost resource
```

The integration must prove all of the following within an explicitly documented rounding tolerance:

```text
A ≈ B
A + S ≈ C
C - B ≈ S
```

For a flat tariff this reduces to:

```text
daily usage cost = Σ(PT30M kWh) × unit price
daily total cost = daily usage cost + standing charge
daily total cost ≈ Bright P1D cost
```

This gives independent cross-checks of consumption, unit-rate interpretation, standing-charge interpretation and Bright aggregation semantics. A tariff parser bug must not be able to produce plausible-looking Home Assistant values without failing this contract.

Standing charge is applied exactly once per local billing day, including 46-interval and 50-interval DST days.

A tariff type that cannot be calculated from the available API evidence must remain explicitly unavailable rather than silently using a different formula.

**Required evidence**

- Pure arithmetic unit tests.
- Protected live reconciliation against Bright PT30M cost and P1D cost.
- Tests spanning tariff changes and DST transitions.
- At least one known completed day must prove all three equalities above before release; broader sampled-day reconciliation should be added where the API permits it without excessive calls.

## 5. Home Assistant presentation capability

The internal model must preserve enough information to expose at least these historical monetary series independently:

- usage/unit cost;
- standing charge;
- total cost.

Home Assistant must therefore be able to display a daily stacked chart with **usage cost** and **standing charge** as the two components. Their combined height is the daily total. A separate total-cost statistic may also exist for Energy-dashboard compatibility but must not be required as a third stacked series.

The P1D cost value is an acceptance oracle for completed-day correctness. It is not a third stacked component.

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

The original household export must not be committed wholesale to the public repository. The repository may contain non-reversible fingerprints and structural expectations such as interval counts and relative day offsets. It must not contain credentials, tokens, account identifiers, resource IDs, addresses or raw household interval history.

Any PR touching first-reading discovery, PT30M retrieval, tariff interpretation, cost arithmetic, Home Assistant statistics or history scheduling must identify which contract sections it affects and show the corresponding tests passing before merge.
