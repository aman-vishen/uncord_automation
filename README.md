# Uncord Automation v13 — Six-Stage Production MES

ETE Solutions India automation suite for router manufacturing, with local production continuity and Render-hosted MES analytics.

## Production stages

The MES now tracks the line in this exact order:

1. **MAC Write** — reported automatically by `mac_writer`.
2. **Wi-Fi Calibration** — read from the calibration log by `stage_log_collector`.
3. **Label Printing** — read from the printing log by `stage_log_collector`.
4. **BOB Calibration** — read from the BOB log by `stage_log_collector`.
5. **Wi-Fi Coupling & VoIP** — read from the station log by `stage_log_collector`.
6. **Verification** — reported automatically by `quality_verifier`.

## Architecture

```text
Router PCBs / production machines
       │
       ├── MAC Writer ─────────────────────────┐
       ├── Wi-Fi calibration log ────────┐     │
       ├── Label printing log ───────────┤     │
       ├── BOB calibration log ──────────┼─ Stage Log Collector
       ├── Coupling & VoIP log ──────────┘     │
       └── Quality Verifier ───────────────────┤
                                               ▼
                                      Local Central Server
                                      ├── local SQLite history
                                      ├── duplicate prevention
                                      ├── durable cloud queue
                                      └── automatic HTTPS retry
                                               │
                                               ▼
                                      Render MES + PostgreSQL
```

The factory applications use the local server. Internet or Render downtime does not stop production. Results remain queued locally and synchronize after connectivity returns.

## Applications

- `server/` — local MAC pool, production history, external-stage API and cloud synchronization.
- `mac_writer/` — eight-router MAC, serial and GPON writer.
- `stage_log_collector/` — monitors Wi-Fi, label, BOB, coupling and VoIP logs.
- `quality_verifier/` — eight-router final verification application.
- `mes_dashboard/` — Render cloud API and six-stage MES dashboard.

## Factory quick start

1. Back up `server/mac_server.db`.
2. Keep your existing production database while replacing the application files.
3. Configure and start the local server.
4. Configure the stage collector with the actual machine log paths and formats.
5. Start the Writer, Collector and Verifier.

```bat
server\run_server.bat
mac_writer\run_client.bat
stage_log_collector\run_collector.bat
quality_verifier\run_verifier.bat
```

The collector can run once on each stage PC, or one collector can read all logs through network-share paths.

## Stage log formats

`stage_log_collector/config.ini` supports:

- `REGEX` — one completed result per line with named capture groups.
- `CSV` — column order defined by `CSV_COLUMNS`.
- `JSONL` — one JSON object per line with configurable field names.

Each parsed record needs a recognized PASS/FAIL/ERROR value. Example logs are provided in `stage_log_collector/sample_logs/`.

## Connect to Render

Set the same key in Render `INGEST_API_KEY` and local `server/server_config.txt`:

```ini
CLOUD_MES_ENABLED=1
CLOUD_MES_URL=https://your-service.onrender.com
CLOUD_MES_API_KEY=YOUR_SHARED_INGEST_KEY
PLANT_ID=UNCORD-MUMBAI
```

Writer, Verifier and Collector continue to use the local server URL, for example `http://192.168.20.10:8765`.

## Data safety

- Local SQLite is written before cloud synchronization.
- Collector file offsets and pending records survive application restarts.
- Event IDs are deterministic and unique.
- The local server and Render reject duplicate event IDs.
- Log rotation/truncation is detected.
- Do not commit production databases, API keys, Telnet credentials or production logs.

See `docs/STAGE_LOG_COLLECTOR_SETUP.md` and `docs/RENDER_CLOUD_MES_DEPLOYMENT.md` for full setup instructions.
