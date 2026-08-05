# Central Production Server v13

The local server remains the factory system of record for MAC allocation, Writer results, external stage logs and Verification results.

New endpoints:

```text
POST /api/stage-log/report
POST /api/stage-log/batch
GET  /api/stage-log/stats
```

External stage records are stored in `stage_log_history` and atomically added to `cloud_sync_queue` as `STAGE_LOG_RESULT` events.

The allowed external stages are:

```text
WIFI_CALIBRATION
LABEL_PRINTING
BOB_CALIBRATION
WIFI_COUPLING_VOIP
```

Use the same `API_KEY` in the server and Stage Log Collector. Back up `mac_server.db` before upgrading; the new table is added automatically.
