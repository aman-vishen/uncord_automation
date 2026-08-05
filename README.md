# Uncord Automation v12 — Hybrid Factory + Render Cloud MES

ETE Solutions India production automation suite for router manufacturing.

## Architecture

```text
Router PCBs
   │ Telnet
   ▼
MAC Writer / Quality Verifier PCs
   │ factory LAN
   ▼
Local Central Server
├── atomic MAC allocation
├── duplicate protection
├── local SQLite production database
├── durable cloud_sync_queue
└── background HTTPS sync
          │
          ▼
Render Cloud MES API
          │
          ▼
Render PostgreSQL
          │
          ▼
Online production dashboard
```

The Writer and Verifier continue using the **local central server**. Production does not stop when the internet or Render is unavailable. Completed results are committed locally first, added to `cloud_sync_queue`, and retried until Render acknowledges the permanent event ID.

## Applications

- `server/` — local MAC pool, identity mapping, final verification history and offline-tolerant cloud synchronization.
- `mac_writer/` — 8-router MAC, hardware serial and GPON writer using the UNCORD command profile.
- `quality_verifier/` — 8-router final verifier with serial scanning, Wi-Fi/BOB calibration, firmware, LED, Reset, WPS and User Mode checks.
- `mes_dashboard/` — Render-ready cloud ingestion API and web MES dashboard backed by PostgreSQL.
- `mes_dashboard/local_dashboard.py` — optional legacy LAN dashboard reading the local `server/mac_server.db` directly.
- `docs/UNCORD_Router_Command_Template.xlsx` — source workbook for the router commands.

## Factory quick start

1. Back up your existing `server/mac_server.db`.
2. Replace the applications with this version, but keep your production database.
3. Configure the local server in `server/server_config.txt`.
4. Run the server, Writer and Verifier as before.

```bat
cd server
run_server.bat
```

```bat
cd mac_writer
run_client.bat
```

```bat
cd quality_verifier
run_verifier.bat
```

## Connect the factory to Render

After deploying the Blueprint, Render provides a URL such as:

```text
https://ete-production-mes.onrender.com
```

Set the same long random key in:

- Render environment variable `INGEST_API_KEY`
- Local `server/server_config.txt` value `CLOUD_MES_API_KEY`

Then configure:

```ini
CLOUD_MES_ENABLED=1
CLOUD_MES_URL=https://ete-production-mes.onrender.com
CLOUD_MES_API_KEY=PASTE_THE_RENDER_INGEST_KEY
PLANT_ID=UNCORD-MUMBAI
CLOUD_SYNC_INTERVAL_SECONDS=30
CLOUD_BATCH_SIZE=100
CLOUD_REQUEST_TIMEOUT_SECONDS=60
```

Do **not** change the Writer or Verifier `SERVER_URL` to Render. They should continue using the local LAN URL, for example:

```ini
SERVER_URL=http://192.168.20.10:8765
```

## Render deployment

Use `render.yaml` for a free demonstration deployment. Use `render.production.yaml` for a paid persistent production deployment.

Complete instructions are in:

- `docs/RENDER_CLOUD_MES_DEPLOYMENT.md`

## Cloud data safety

- Every event has a deterministic unique `event_id`.
- Render PostgreSQL enforces `event_id` uniqueness.
- Retrying a batch cannot create duplicate production records.
- Existing completed writer and verifier records are backfilled into the queue during upgrade.
- A cloud outage never blocks local MAC allocation, writing or verification.
- The local server UI includes Cloud Pending, Cloud Synced, retry status and a **Sync Now** button.

## Router command profiles

The Writer and Verifier retain the UNCORD commands from v11. See `docs/COMMAND_MAPPING.md` and the AX3000/AC1200 profiles under `quality_verifier/profiles/`.

## Security

Never commit or share:

- `server/mac_server.db`
- `CLOUD_MES_API_KEY`
- Render `INGEST_API_KEY`
- dashboard passwords
- Telnet passwords
- production logs or pending-job files

The Render dashboard supports HTTP Basic Authentication through `DASHBOARD_USERNAME` and `DASHBOARD_PASSWORD`. The production ingestion API uses a separate Bearer key.
