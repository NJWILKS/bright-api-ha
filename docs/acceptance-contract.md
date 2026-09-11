# Bright API acceptance contract

This contract defines the behaviours that must remain true for every release. These are release gates, not aspirations.

## 1. Historical start point and upstream gaps

The Bright `first-time` endpoint is a locator only. The integration must use it to find the neighbourhood of the first retrievable non-null PT30M reading.

The integration must never invent an interval that the readings API did not return, even when an authoritative Bright export proves that the interval existed. Known upstream omissions are recorded as non-reversible fingerprints and counts in the protected contract.

**Required evidence**

- Golden-data tests derived from an authoritative Bright export CSV.
- Protected live contract against the real API.
- The first retrievable PT30M interval must match its CSV-derived fingerprint after the documented timestamp-label interpretation is applied.
- Any known API omission at the beginning of history must remain explicitly pinned by count/fingerprint; it must not be silently filled with a synthetic zero.

## 2. Raw PT30M fidelity and canonical interval semantics

The raw interval ledger stores the PT30M timestamp and value exactly as Bright returned them. No timestamp correction, interpolation or synthetic filling is allowed in Store.

Protected live evidence shows that Bright has used more than one PT30M timestamp-label convention. Historical rows before the proven cutover are labelled at interval end; rows at and after the cutover are labelled at interval start. The raw labels remain untouched. A separate deterministic interpretation maps a raw label to the canonical UTC interval start for comparison and Home Assistant projection.

The currently proven cutover is `2026-08-30T00:00:00Z`:

- raw PT30M timestamps before the cutover are interpreted as interval end, so canonical start is `raw - 30 minutes`;
- raw timestamps at or after the cutover are interpreted as interval start and are unchanged.

This rule must remain protected by live fingerprints. It must not be generalised, moved or removed from anecdotal observations alone.

**Required evidence**

Every protected live acceptance run must compare complete canonical PT30M days against CSV-derived golden fingerprints for:

- the first retrievable billing day;
- at least five fixed randomly selected complete billing days;
- the last known complete/good billing day;
- known UK DST transition days where present in the export;
- the known timestamp-convention cutover day.

For normal complete days, both canonical timestamps and values must match exactly. Genuine `0.0` readings remain `0.0`. UTC identity must remain unambiguous through UK DST changes.

Where Bright demonstrably omits an interval, the protected contract must pin the exact retrievable count and fingerprint. A known omission is not permission to weaken unrelated days.

## 3. Tariff evidence

Tariff API data is stored separately from interval facts and may be used for current rate presentation and for arithmetic cross-checks where its effective date clearly covers the billing day being tested.

Tariff-list data must not be assumed to be a complete historical tariff ledger. A current standing charge or unit rate must never be applied backwards to a historical day without explicit effective evidence.

A tariff type that cannot be interpreted safely from the available API evidence must remain unavailable rather than using a guessed formula.

**Required evidence**

- Parser tests for explicit flat tariff evidence and tariff changes.
- Raw tariff API evidence remains separate from PT30M/P1D billing evidence.
- At least one protected live day with applicable flat unit-rate evidence must prove that consumption priced at the API rate reproduces Bright PT30M usage cost within the documented tolerance.

## 4. Historical billing identity

For a completed Europe/London billing day define:

```text
A = Σ(canonical PT30M consumption kWh × explicitly applicable API unit price)
B = Σ(Bright canonical PT30M cost resource)
C = Bright P1D cost resource
S = C - B
```

The historical sources of truth are:

- `B` for usage cost;
- `C` for total billed cost;
- `S` for the standing/residual component only when PT30M cost coverage for that local day is complete.

A complete day means the expected number of half-hour cost intervals for the Europe/London billing day: normally 48, 46 on the spring DST transition and 50 on the autumn transition.

If PT30M cost coverage is incomplete, `C` may still be projected as Bright's billed total but `S` must remain unavailable. Missing usage cost must never be disguised as standing charge.

Where an explicit flat tariff applies to the tested day, the contract additionally requires:

```text
A ≈ B
```

The tariff API standing charge is not used as a historical oracle unless its effective evidence explicitly covers that day. The integration therefore does not require a current tariff standing charge to equal `C - B` for older history.

**Required evidence**

- Pure arithmetic tests for complete and incomplete 46/48/50-interval days.
- Protected live PT30M/P1D reconciliation.
- At least one live applicable-unit-rate check proving `A ≈ B`.
- Historical total cost must be sourced from P1D rather than reconstructed from a current tariff.

## 5. Home Assistant presentation capability

The internal model must preserve enough evidence to expose these historical monetary series independently:

- usage cost from Bright PT30M cost;
- standing/residual charge for complete billing days;
- total billed cost from Bright P1D cost.

Home Assistant must therefore be able to display a daily stacked chart with **usage cost** and **standing charge** as components where the day is complete. Their combined value should reconcile to the Bright P1D billed total within normal source rounding.

A separate total-cost statistic is provided for billing/Energy presentation and is sourced directly from P1D evidence.

**Required evidence**

- Projection-layer tests proving usage and standing remain separate.
- Tests proving total cost is P1D evidence rather than tariff reconstruction.
- Home Assistant statistics integration test proving the historical series can be queried.
- Real HA acceptance proving the statistics can be selected and graphed.

## 6. No cumulative meter total in Home Assistant

The Bright cumulative meter reading is not part of the Home Assistant contract and must never be exposed as an entity, long-term statistic, Energy source or correctness baseline.

The integration's consumption truth is the settled PT30M interval series. Home Assistant `sum` fields required by external-statistics semantics are projection bookkeeping only; they are not a synthetic source meter.

**Required evidence**

- Entity/statistics tests assert that no cumulative-total entity or statistic is created.
- Projection code accepts interval data, not a cumulative meter total.

## 7. Billing-day boundaries and refresh cadence

All billing-day boundaries are defined in `Europe/London`, then converted to UTC for API/storage cursors. During BST, local midnight is therefore `23:00Z` on the preceding UTC date. Code must never substitute UTC midnight for a local billing boundary.

Initial backfill may make multiple bounded requests until PT30M and P1D evidence are current. Once current, historical ingestion switches to one scheduled refresh cycle at **04:00 Europe/London** each day.

The daily refresh ingests only newly settled evidence. It must be restart-safe and idempotent. It must not repeatedly poll historical readings throughout the day.

Bright resource APIs are resource-specific, so one scheduled cycle may contain one request per relevant resource/period. The requirement is one daily history-sync cycle, not one literal HTTP request.

**Required evidence**

- Scheduler tests proving the next run resolves to 04:00 Europe/London across BST/GMT transitions.
- Settled-boundary tests proving local midnight is converted correctly to UTC.
- Tests proving current PT30M and P1D cursors request only un-ingested settled history.
- Restart tests proving replay cannot duplicate PT30M intervals or P1D billing days.
- API-client test proving an expired session is reauthenticated once before failing.

## 8. Reset and rebuild

Reset/Rebuild is an explicit admin action for one Bright config entry. It may delete only data owned by that entry:

- raw PT30M Store files;
- raw P1D billed-cost Store files;
- tariff evidence;
- history/projection cursors;
- external statistics with statistic IDs owned by that entry.

It must not touch statistics or stores belonging to another Bright entry or another integration. After clearing, it refetches Bright evidence from the first retrievable interval and rebuilds the projection.

## Golden data policy

The original household export must not be committed wholesale to the public repository. The repository may contain non-reversible fingerprints, counts, cutover dates and structural expectations. It must not contain credentials, tokens, account identifiers, resource IDs, addresses or raw household interval values.

Live diagnostic logs may expose values transiently inside the protected workflow when a comparison fails, but those values must not be copied into repository fixtures or source code.

Any PR touching first-reading discovery, PT30M label interpretation, tariff interpretation, cost arithmetic, P1D billing evidence, Home Assistant statistics, history scheduling or Reset/Rebuild must identify which contract sections it affects and show the corresponding normal and protected tests passing before release.
