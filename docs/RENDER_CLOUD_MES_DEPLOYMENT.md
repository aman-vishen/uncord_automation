# Deploy ETE Cloud MES on Render

## What this deployment does

Render hosts:

1. The ETE MES web dashboard.
2. The production event ingestion API.
3. A PostgreSQL database shared by the dashboard and API.

The local factory server uploads records to Render. Writer and Verifier stations remain connected to the local server, so factory operation continues during internet outages.

## 1. Push this project to GitHub

Extract the package and place the files in the root of `aman-vishen/uncord_automation`.

The repository root must contain:

```text
render.yaml
render.production.yaml
mes_dashboard/
server/
mac_writer/
quality_verifier/
```

Do not commit `server/mac_server.db`, API keys, passwords or logs. The included `.gitignore` excludes database files.

## 2. Create secrets

Generate two different strong values:

- `INGEST_API_KEY` — used only by the local central server to upload production events.
- `DASHBOARD_PASSWORD` — used by people opening the online dashboard.

PowerShell example:

```powershell
[Convert]::ToBase64String((1..48 | ForEach-Object { Get-Random -Maximum 256 }))
```

Store the values securely.

## 3. Deploy the Render Blueprint

1. Sign in to Render and connect GitHub.
2. Choose **New → Blueprint**.
3. Select `aman-vishen/uncord_automation`.
4. Select branch `main`.
5. Use Blueprint path `render.yaml` for a demonstration, or `render.production.yaml` for production.
6. During initial creation, Render asks for:
   - `INGEST_API_KEY`
   - `DASHBOARD_PASSWORD`
7. Deploy the Blueprint.

The Blueprint creates:

- Web service: `ete-production-mes`
- PostgreSQL database: `ete-production-mes-db`
- `DATABASE_URL` automatically linked to the database

## 4. Verify Render

Open:

```text
https://YOUR-RENDER-HOST/api/health
```

Expected result:

```json
{
  "ok": true,
  "service": "ETE Cloud MES",
  "version": "12.0",
  "database": "postgresql"
}
```

Open the main URL. The browser asks for:

```text
Username: admin
Password: the DASHBOARD_PASSWORD entered in Render
```

## 5. Connect the factory server

Edit `server/server_config.txt` on the central factory PC:

```ini
CLOUD_MES_ENABLED=1
CLOUD_MES_URL=https://YOUR-RENDER-HOST
CLOUD_MES_API_KEY=THE_SAME_VALUE_AS_RENDER_INGEST_API_KEY
PLANT_ID=UNCORD-MUMBAI
CLOUD_SYNC_INTERVAL_SECONDS=30
CLOUD_BATCH_SIZE=100
CLOUD_REQUEST_TIMEOUT_SECONDS=60
```

Restart `server/run_server.bat`.

The server UI should show:

```text
CLOUD MES: ONLINE
CLOUD PENDING: 0
CLOUD SYNCED: increasing count
```

Use **Sync Now** for an immediate retry.

## 6. Keep Writer and Verifier local

Do not point desktop stations to Render. Their config remains similar to:

```ini
SERVER_URL=http://192.168.20.10:8765
SERVER_API_KEY=your-local-server-key
```

The data path is:

```text
Writer/Verifier → Local Server → durable queue → Render HTTPS API → PostgreSQL → Dashboard
```

## 7. Test the complete flow

1. Program one test router in the Writer.
2. Confirm one new `IDENTITY_WRITER_RESULT` changes to SYNCED in the local Cloud MES Sync tab.
3. Verify the same router.
4. Confirm one `QUALITY_VERIFICATION_RESULT` changes to SYNCED.
5. Open Render dashboard and check the MAC, serial, GPON, station, Wi-Fi, BOB and firmware fields.
6. Disconnect factory internet and run another test.
7. Confirm the local queue remains PENDING while Writer/Verifier still work.
8. Restore internet and confirm records synchronize without duplicates.

## Free vs production Blueprint

`render.yaml` uses free resources for demonstration. Free PostgreSQL is temporary and is not appropriate as the authoritative production history.

`render.production.yaml` uses:

- Starter web service
- Basic PostgreSQL instance
- Persistent database storage

Use the production Blueprint for real production data and configure backups in Render.

## API details

Batch ingestion endpoint:

```http
POST /api/v1/production-events/batch
Authorization: Bearer <INGEST_API_KEY>
Content-Type: application/json
```

Health endpoint:

```http
GET /api/health
```

Dashboard endpoint:

```http
GET /api/dashboard?start=2026-08-01&end=2026-08-31
```

The dashboard and CSV export require Basic Authentication when `DASHBOARD_PASSWORD` is set.
