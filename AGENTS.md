# Development guardrails

This repository is a clean implementation, not a migration of the previous Hildebrand Glow integration.

## Architecture

- Treat Glowmarkt PT30M readings as the canonical historical data.
- Treat `first-time` as a locator only; confirm the first actual PT30M reading.
- Store raw interval facts separately from Home Assistant statistics.
- Store effective-dated tariff metadata separately from interval rows.
- Keep history population asynchronous, resumable and idempotent.
- Use UTC timestamps as interval identity; use Europe/London only for local billing-day and tariff semantics.
- Do not make historical population depend on entity IDs.
- Do not expose the Bright cumulative meter reading as a Home Assistant entity or statistic. Historical truth is the settled PT30M series, not an unverifiable cumulative total.
- After initial backfill is complete, do not continuously poll historical PT30M data. Run one scheduled historical refresh cycle at 04:00 Europe/London for newly settled data. Because Bright endpoints are resource-specific, one refresh cycle may require one HTTP request per relevant resource; the non-negotiable is one daily history-sync cycle, not one literal HTTP request across all resources.
- Do not add migration, legacy cleanup or compatibility state machines unless a future explicit requirement demands them.

## Non-negotiable acceptance contract

These requirements are release gates. A change that breaks any one of them must not be merged or released.

1. The integration's first actual historical interval must correlate with the user's authoritative Bright export CSV. `first-time` is never accepted as evidence by itself.
2. Historical PT30M usage values and timestamps must correlate with the authoritative Bright export CSV, including genuine zero values and UK DST transition days.
3. Effective unit prices and standing charges must come from the Bright tariff API and must be cross-checked against Bright's own completed-day cost resources rather than trusted in isolation.
4. For each completed Europe/London billing day define `A = Σ(PT30M kWh × applicable API unit rate)`, `B = Σ(Bright PT30M cost)`, `S = API standing charge`, and `C = Bright P1D cost`. Within documented rounding tolerance the integration must prove `A ≈ B`, `A + S ≈ C`, and `C - B ≈ S`. No tariff or costing change is acceptable unless these invariants remain true.
5. The data model must remain capable of exposing historical usage-cost and standing-charge series separately so Home Assistant can render a daily stacked cost chart whose combined bar height is total daily cost. Total cost may also be exposed separately for Energy compatibility, but must not be required as a third stacked component. Bright P1D cost is a validation oracle, not a stacked series.
6. Bright cumulative meter totals must never be exposed to Home Assistant. No entity, statistic, Energy source or derived correctness check may depend on that cumulative reading.
7. Once backfill is current, historical PT30M ingestion must settle into one daily refresh cycle at 04:00 Europe/London rather than continual historical polling. The refresh must ingest only newly settled intervals and remain restart-safe and idempotent.

The authoritative CSV-derived expectations should be represented by a small sanitised golden fixture or protected contract test so they can run repeatedly without committing credentials, resource IDs, account identifiers, or unnecessary household data.

## Safety and quality

- Never log credentials, auth tokens, resource IDs or household meter data at warning/info level.
- No live Bright API calls in normal pull-request CI.
- Behavioural changes require tests.
- Preserve Home Assistant async/non-blocking behaviour.
- Keep Recorder/statistics projection downstream of the raw ledger.
- Do not publish a release until the exact candidate SHA has passed normal CI and the relevant protected live contract.
- Any change touching first-reading discovery, PT30M retrieval, tariff parsing, cost arithmetic, statistics projection or history scheduling must explicitly prove the relevant non-negotiable acceptance criteria above.
