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
- Do not add migration, legacy cleanup or compatibility state machines unless a future explicit requirement demands them.

## Non-negotiable acceptance contract

These requirements are release gates. A change that breaks any one of them must not be merged or released.

1. The integration's first actual historical interval must correlate with the user's authoritative Bright export CSV. `first-time` is never accepted as evidence by itself.
2. Historical PT30M usage values and timestamps must correlate with the authoritative Bright export CSV, including genuine zero values and UK DST transition days.
3. Effective unit prices and standing charges obtained from the Bright API must correlate with the tariff values evidenced by the authoritative export/reference data for the same effective period.
4. For each Europe/London billing day, derived total cost must equal the sum of each PT30M usage interval multiplied by the effective unit price for that interval, plus exactly one applicable daily standing charge. Flat, time-of-use and dynamic tariff handling must never silently substitute a different arithmetic model.
5. The data model must remain capable of exposing historical usage-cost and standing-charge series separately so Home Assistant can render a daily stacked cost chart whose combined bar height is total daily cost. Total cost may also be exposed separately for Energy compatibility, but must not be required as a third stacked component.

The authoritative CSV-derived expectations should be represented by a small sanitised golden fixture or protected contract test so they can run repeatedly without committing credentials, resource IDs, account identifiers, or unnecessary household data.

## Safety and quality

- Never log credentials, auth tokens, resource IDs or household meter data at warning/info level.
- No live Bright API calls in normal pull-request CI.
- Behavioural changes require tests.
- Preserve Home Assistant async/non-blocking behaviour.
- Keep Recorder/statistics projection downstream of the raw ledger.
- Do not publish a release until the exact candidate SHA has passed normal CI and the relevant protected live contract.
- Any change touching first-reading discovery, PT30M retrieval, tariff parsing, cost arithmetic or statistics projection must explicitly prove the relevant non-negotiable acceptance criteria above.
