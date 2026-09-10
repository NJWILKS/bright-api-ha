# Bright API Home Assistant

A clean Home Assistant custom integration for Hildebrand Bright / Glowmarkt smart-meter data.

This repository deliberately starts from a minimal architecture:

- Bright/Glowmarkt authentication and resource discovery;
- current-value presentation sensors;
- PT30M interval history as the source of truth;
- effective-dated tariff metadata;
- resumable asynchronous population;
- statistics projection only after the raw ledger is proven.

No migration or compatibility code from earlier integrations is carried into this repository.
