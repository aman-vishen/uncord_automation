# ETE Cloud MES v13

The Render dashboard now reports six production stages:

1. MAC Write
2. Wi-Fi Calibration
3. Label Printing
4. BOB Calibration
5. Wi-Fi Coupling & VoIP
6. Verification

The cloud API accepts `IDENTITY_WRITER_RESULT`, `STAGE_LOG_RESULT`, `QUALITY_VERIFICATION_RESULT` and `MAC_POOL_SNAPSHOT` events. PostgreSQL schema migration adds stage name, source file, source offset and raw-log fields automatically.

The dashboard provides line input, finished output, estimated WIP, final yield, stage yield, station analysis, recent stage records and CSV export.

For Render, set:

```text
DATABASE_URL
INGEST_API_KEY
DASHBOARD_USERNAME
DASHBOARD_PASSWORD
```

The local factory server—not the individual stage applications—synchronizes records to the Render ingestion API.
