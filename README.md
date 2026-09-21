# Uncord Automation v13.61 — Current Production Flow

ETE Solutions India router/ONT production automation suite with local factory execution, identity traceability, offline-tolerant cloud synchronization and Cloud MES analytics.

## Current production flow

1. **Wi-Fi Calibration**
   - Stage log collector records PASS/FAIL.
   - X31 final verification uses the calibrated E2P locations captured from the production calibration log.

2. **Label Printing + PCB Link**
   - Central Server imports **MAC + GPON Serial Number only**.
   - Label Printing generates the product Serial Number from the live date.
   - Year encoding starts at **2026=A, 2027=B, 2028=C ...**
   - PCB Serial Number is linked during label printing.

3. **MAC Write + Firmware Upgrade**
   - MAC Writer scans/loads the linked identity.
   - Writes MAC, generated Serial Number and GPON Serial Number.
   - Verifies the programmed identity.
   - Downloads the Central Server-selected firmware, validates SHA256, runs firmware validation/upgrade, waits for reboot and only then reports PASS.

4. **BOB Calibration**
   - Required for GPON models.
   - Skipped for router-only models.

5. **Wi-Fi Coupling & VoIP**
   - Remains one combined production stage: `WIFI_COUPLING_VOIP`.
   - The collector supports date subfolders followed by `SUCCESS` / `FAIL`.
   - MAC is parsed from the beginning of the log filename.
   - VoIP is ignored for models where VoIP is disabled.

6. **Final Verification**
   - Validates identity and all required previous stages.
   - Checks Wi-Fi calibration memory / E2P data.
   - Checks BOB calibration data for applicable models.
   - Runs LED and WPS tests.
   - Verifies the firmware installed by MAC Write.
   - Switches and confirms user mode.
   - **Reset-button testing is no longer part of Final Verification.**

## Removed stage

**Box Build has been removed from the production flow and is no longer a dependency for MAC Write.**

PCB traceability is established during **Label Printing + PCB Link** instead.

## Model rules

- `AC1200-X13` / `AC3000-X31` — GPON + VoIP: BOB + Wi-Fi Coupling & VoIP.
- `AC1200-X12` / `AC3000-X30` — GPON: BOB + Wi-Fi Coupling; VoIP disabled.
- `AC1200-R12` / `AC3000-R30` — Router: no BOB; Wi-Fi Coupling; VoIP disabled.

## Cloud MES v13.61

The Cloud MES follows the six-stage flow above and provides:

- Date-range and model filtering.
- Line input, finished output, Final FPY, MAC Write FPY, WIP, retest count/rate, current UPH and MAC-pool status.
- Six-stage live process visualization.
- Daily finished-production trend.
- Hourly output visualization with optional `TARGET_UPH` comparison.
- Stage FPY analysis.
- Failure Pareto.
- WIP funnel.
- Station volume / yield comparison.
- Production records with firmware information.
- MAC / Serial / GPON / PCB traceability.
- Automated production exception insights.

Set `TARGET_UPH` in the Cloud MES environment when an explicit production target should be displayed.

The MES intentionally keeps advanced analytics in the **Cloud MES**, while the local Central Server remains focused on production execution, identity/model control, stage validation, firmware control, SQLite/offline continuity and cloud synchronization.

## Main applications

- `server/` — Central production identity, model control, stage validation, firmware selection and cloud synchronization.
- `label_printing/` — Serial generation, PCB linking and label printing.
- `stage_log_collector/` — Wi-Fi Calibration, BOB Calibration and Wi-Fi Coupling & VoIP log collection.
- `mac_writer/` — eight-DUT identity writer and firmware-upgrade stage.
- `quality_verifier/` — final verification.
- `mes_dashboard/` — Cloud MES, traceability and production analytics.

Before replacing production server files, back up `server/mac_server.db`. Keep production passwords, API keys and database files out of GitHub.
