# Bright API Home Assistant

A clean Home Assistant custom integration for Hildebrand Bright / Glowmarkt smart-meter data.

This repository deliberately starts from a minimal architecture:

- Bright/Glowmarkt authentication and resource discovery;
- settled PT30M interval history as the source of truth;
- effective-dated tariff metadata stored separately from interval facts;
- resumable asynchronous initial backfill;
- one post-backfill history refresh cycle at 04:00 Europe/London;
- no cumulative meter-total entity or statistic;
- no continuously polled or misleading current-day consumption/cost sensors;
- statistics projection only after the raw ledger is proven against protected live and CSV-derived golden contracts.

The public repository stores only non-reversible fingerprints and structural expectations derived from the authoritative household export, not the raw household interval history.

No migration or compatibility code from earlier integrations is carried into this repository.
