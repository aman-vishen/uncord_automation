# Local Central Server v12

The local server remains the authoritative source for:

- MAC allocation and duplicate prevention
- MAC/serial/GPON identity mapping
- final quality verification validation
- local production history

It now also owns a durable `cloud_sync_queue`. Writer and Verifier results are placed in this queue in the same SQLite transaction that finalizes the local result.

## Cloud configuration

Edit `server_config.txt`:

```ini
CLOUD_MES_ENABLED=1
CLOUD_MES_URL=https://ete-production-mes.onrender.com
CLOUD_MES_API_KEY=the-same-value-as-Render-INGEST_API_KEY
PLANT_ID=UNCORD-MUMBAI
CLOUD_SYNC_INTERVAL_SECONDS=30
CLOUD_BATCH_SIZE=100
CLOUD_REQUEST_TIMEOUT_SECONDS=60
```

Restart `run_server.bat` after changing the configuration.

## Offline behavior

When Render or the internet is unavailable:

- local Writer and Verifier operations continue
- records remain `PENDING`
- attempts and the last error are recorded
- the worker retries automatically
- **Sync Now** requests an immediate retry

## Upgrade

Back up the existing `mac_server.db`. Copy the new `server.py` and assets while preserving the database. The schema is migrated automatically and completed v11 records are backfilled into the cloud queue.
