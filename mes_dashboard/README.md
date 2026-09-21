# ETE Cloud MES v13.61

The Cloud MES follows the current production flow:

1. Wi-Fi Calibration
2. Label Printing + PCB Link
3. MAC Write + Firmware Upgrade
4. BOB Calibration (model-dependent)
5. Wi-Fi Coupling & VoIP
6. Final Verification

Box Build is no longer a production stage. Reset-button testing is no longer part of Final Verification.

## Dashboard analytics

- Date-range filtering.
- Model filtering from event payload/model data.
- Line Input, Finished Output, Final FPY, MAC Write FPY, Current UPH, Estimated WIP, Retest count/rate and MAC-pool status.
- Six-stage process flow.
- Daily finished-production trend.
- Hourly finished-output trend.
- Optional actual-vs-target UPH using `TARGET_UPH`.
- Stage FPY.
- Failure Pareto.
- WIP funnel.
- Station performance comparison.
- Production record search.
- Full MAC / Serial / GPON / PCB traceability.
- Automated exception insights based on stage yield, failure concentration, WIP and UPH.

The cloud API continues to accept `IDENTITY_WRITER_RESULT`, `STAGE_LOG_RESULT`, `QUALITY_VERIFICATION_RESULT` and `MAC_POOL_SNAPSHOT`.

Accepted production stage logs are `WIFI_CALIBRATION`, `LABEL_PRINTING`, `BOB_CALIBRATION` and `WIFI_COUPLING_VOIP`.

Advanced analytics stay in Cloud MES. The local Central Server remains responsible for production execution, identity/model control, stage validation, firmware control, offline continuity and synchronization.
