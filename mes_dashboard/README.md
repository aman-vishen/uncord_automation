# ETE Cloud MES v12

Render-ready web MES with an HTTPS ingestion API and PostgreSQL storage.

## Cloud features

- Idempotent batch event ingestion
- PostgreSQL production history
- Daily volume, PASS/FAIL and yield
- Writer and final-verification analysis
- Stage-wise Wi-Fi, BOB, firmware, LED, Reset, WPS and User Mode results
- Station performance
- Unit traceability and CSV export
- Basic Authentication for dashboard users
- Separate Bearer API key for factory ingestion

## Render environment variables

```text
DATABASE_URL          automatically linked by render.yaml
INGEST_API_KEY        required secret
DASHBOARD_USERNAME    admin by default
DASHBOARD_PASSWORD    required secret
REFRESH_SECONDS       10
MAX_BATCH_SIZE        500
```

## Local cloud-mode test

`app.py` falls back to `cloud_mes.db` when `DATABASE_URL` is absent.

```bat
run_mes_cloud_local.bat
```

Defaults for local testing:

```text
Dashboard: http://localhost:8080
Username: admin
Password: admin
Ingest key: local-development-key
```

Do not use these defaults in production.

## Legacy local factory dashboard

To view the local SQLite factory database directly without cloud sync:

```bat
run_mes_local_factory.bat
```

This runs `local_dashboard.py` and reads `../server/mac_server.db` through `config.ini`.
