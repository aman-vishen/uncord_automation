# Changelog

## v12 — Hybrid Render Cloud MES

- Added durable `cloud_sync_queue` to the local central server.
- Writer and Verifier results are committed locally and queued atomically.
- Added background HTTPS batch synchronization with automatic retry.
- Added deterministic event IDs and duplicate-safe cloud ingestion.
- Added automatic backfill of completed v11 production history.
- Added cloud status, pending/synced counts, queue table and Sync Now control to server UI.
- Added Render-ready MES ingestion API.
- Added PostgreSQL storage and schema initialization.
- Added dashboard Basic Authentication and separate ingestion Bearer authentication.
- Added Render free/demo and paid/production Blueprints.
- Preserved the optional local SQLite MES dashboard.
- Preserved all UNCORD Writer and Verifier commands from v11.
