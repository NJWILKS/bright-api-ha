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

## Safety and quality

- Never log credentials, auth tokens, resource IDs or household meter data at warning/info level.
- No live Bright API calls in normal pull-request CI.
- Behavioural changes require tests.
- Preserve Home Assistant async/non-blocking behaviour.
- Keep Recorder/statistics projection downstream of the raw ledger.
- Do not publish a release until the exact candidate SHA has passed normal CI and the relevant protected live contract.
