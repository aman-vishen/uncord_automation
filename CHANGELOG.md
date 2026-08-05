# Changelog

## v13 — Six-stage MES and production log collector

- Changed MES stage flow to:
  1. MAC Write
  2. Wi-Fi Calibration
  3. Label Printing
  4. BOB Calibration
  5. Wi-Fi Coupling & VoIP
  6. Verification
- Added a branded Windows Stage Log Collector application.
- Added REGEX, CSV and JSONL log parsers.
- Added persistent file byte offsets and automatic log-rotation handling.
- Added a durable collector upload queue and retry handling.
- Added local server endpoints for external stage-log ingestion.
- Added `stage_log_history` to the local production database.
- Added cloud `STAGE_LOG_RESULT` events and PostgreSQL schema migration.
- Added stage-specific PASS, FAIL, volume and yield analytics.
- Added a visual six-stage process flow to the Render MES.
- Updated production records and CSV exports to include stage, source log and raw log data.
- Added duplicate-safe end-to-end tests for all six stages.

## v12 — Hybrid Render Cloud MES

- Added durable local-to-cloud synchronization.
- Added PostgreSQL-backed Render MES and ingestion API.
- Added automatic retry, event IDs, cloud status and offline production continuity.
