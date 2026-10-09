# ETE Cloud MES production workspace

The cloud dashboard served by `app.py` is a read-only production workspace with
Overview, Operations, Quality, Stations, Production Records, Product Passport,
and Data & Sync views. It uses the existing production-event database and ingest
protocol. No production execution, queue deletion, or identity-allocation behavior
is changed.

## Metric definitions

- **Good units:** each plant + normalized MAC whose latest final-verification
  result within the selected period is PASS, counted once. This is not a shipment
  count and is not necessarily the current lifetime outcome of that product.
- **Latest failed units:** the corresponding units whose latest result within
  the selected period is FAIL or ERROR.
- **Final first-pass yield:** PASS among each plant + MAC's earliest recorded
  final-verification attempts, when those first attempts occurred in the selected
  period. Rank is calculated across all recorded history before applying dates.
  Missing earlier factory history cannot be inferred.
- **Attempt pass rate:** PASS events divided by all reported production attempts.
  Retests remain separate attempts.
- **Units retested:** linked products with multiple final-verification attempts
  within the selected period. Retest percentage is units retested / units tested;
  additional attempt count is shown separately.
- Events without MACs remain searchable and contribute to attempt counts, but
  are excluded from unit and first-pass metrics. Coverage warnings show this gap.
- No data is displayed as unavailable, rather than 0% yield.
- WIP, optional model routes, equipment availability, shift targets, packing
  genealogy, and Central Server queue counts are not inferred from stage totals.
  These require additional configuration or telemetry.

Filtering and charts use **Asia/Kolkata** production dates. Cloud receipt time is
displayed separately from operation completion time. Freshness and the latest
MAC-pool snapshot are global and independent of historical/model filters.

## Supported operations

The overview includes Wi-Fi calibration, label + PCB link, MAC write + firmware,
BOB calibration, Wi-Fi coupling & VoIP, final verification, box build, gift box
label, and master carton label. Display order is not a claim about configured
manufacturing route or model applicability.

The ingest API continues to accept `IDENTITY_WRITER_RESULT`,
`QUALITY_VERIFICATION_RESULT`, `STAGE_LOG_RESULT`, and `MAC_POOL_SNAPSHOT`.
Supported stage logs are `WIFI_CALIBRATION`, `LABEL_PRINTING`, `GIFT_BOX_LABEL`,
`MASTER_CARTON_LABEL`, `BOB_CALIBRATION`, `WIFI_COUPLING_VOIP`, and `BOX_BUILD`.
Accepted and duplicate IDs are acknowledged in `processed_event_ids` as before.

## Investigation workflows

- Click a metric, operation, or station to inspect the corresponding records.
  Good/failed unit drill-downs select one latest event per product, rather than
  all PASS/FAIL attempts.
- Search the complete selected history with server pagination; the 300-event
  limit no longer applies to the new workspace.
- Filter by model, plant, operation, status, station, text, and latest-unit
  evidence. Sort newest/oldest, select page size, and adjust row density.
- Export all matching records to CSV with the same filters as the record view.
  The export is streamed and spreadsheet formula-like values are escaped.
- Open an event for raw log, original payload, source file, identities, and both
  timestamps. Search a product passport across all dates, inspect the latest
  evidence per operation, print the passport, or copy an identifier.
- Ambiguous identifier matches require product selection. Timeline views cap
  at the latest 1,000 events and disclose truncation; latest operation evidence
  and identity lookup still consider all matching history.
- Share view/filter links, use dark appearance, or expand the overview for a
  factory display. Polling pauses in hidden tabs; obsolete requests are canceled.

## APIs and compatibility

The new protected read APIs are `/api/operations`, `/api/records`, `/api/event`,
and `/api/passport`; filtered export is `/export/records.csv`. They retain the
existing dashboard Basic authentication. Summaries use database aggregates and
a bounded eight-second cache that is invalidated on successful new ingestion.

Legacy `/api/dashboard`, `/api/traceability`, ingest, and export routes remain
available. The standalone `local_dashboard.py` retains its existing template and
assets. The new cloud workspace uses `templates/operations.html`,
`static/operations.js`, and `static/operations.css` so the local dashboard is not
broken by a different API response shape.

## Deployment and checks

The existing Dockerfile copies the new module and assets automatically.
`tzdata` is included for timezone support on minimal containers and Windows.
No schema migration is required. Render deployments continue using the existing
service settings and environment variables, including `TARGET_UPH`. The hourly
chart is explicitly labeled test attempts; a configured units/hour target is
shown as context, not misleading target attainment.

Run from the repository root:

```sh
python -m pip install -r mes_dashboard/requirements.txt
python -m unittest discover -s mes_dashboard -p test_operations.py -v
python -m py_compile mes_dashboard/app.py mes_dashboard/operations.py
node --check mes_dashboard/static/operations.js
```

Regression tests use a temporary SQLite database, never the production database.
