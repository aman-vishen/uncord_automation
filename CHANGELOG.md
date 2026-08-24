# v13.18 — firmware as final DUT step

- Moved server-controlled firmware update to the end of MAC Write.
- New order: label/PCB ready → reserve identity → `prolinecmd clearall` → write MAC/Serial/GPON → verify → firmware (if ON) → server report.
- When firmware is ON, `[FINALIZE]` is skipped because `sysupgrade` automatically reboots the PCB.
- When firmware is OFF, existing `[FINALIZE]` behavior is retained.
- Firmware updater still performs SFTP upload, `sysupgrade -T`, `sysupgrade`, reboot wait, SSH reconnect and health check.
- A firmware failure now happens after the identity has been written, so that identity is reported ERROR/blocked rather than reused.
- Recovery flow follows the same firmware-last ordering.

# v13.17 — direct Python OpenWrt firmware updater

- Replaced `mcupgrade` CLI execution with the supplied SSH/SFTP/sysupgrade Python workflow.
- Added `mac_writer/firmware_updater.py` with dedicated source-NIC binding for same-IP 8-DUT operation.
- Added Paramiko dependency.
- Firmware stays controlled by the central server and still runs before identity reservation/write.
- Preserves the original firmware image validation, reboot, SSH reconnect and health-check behavior.

# v13.16 — supplied mcupgrade CLI integration

- Integrates the supplied `mcupgrade send` PC-side multicast workflow.
- Uses each DUT's dedicated source NIC (`192.168.2.101` through `.108`).
- Uses Econet framing by default, matching the supplied bench example.
- Starts multicast first, then issues configurable DUT reboot (`reboot` by default).
- Server package preloads `V1.0.1_260316.bin` but keeps firmware update OFF until enabled by operator.
- Firmware SHA-256: `3aad6d8274752f0a4ddc2c79d2ae042cf558b5bd57d4ffa2483c056c3922b828`.
- The uploaded CLI source package `src/multicast_upgrade/` was not present, so `mcupgrade` must currently be installed separately on each writer PC.

# Changelog


## v13.15 — Server-controlled firmware update in MAC Write

- Added persistent Firmware ON/OFF control to the local Central Server GUI.
- Added server firmware file selection/storage with SHA-256 and file-size metadata.
- Added authenticated `/api/firmware/config` and `/api/firmware/download` endpoints.
- Added optional `[FIRMWARE]` CLI section to MAC Writer commands.
- Firmware runs before identity reservation, `prolinecmd clearall`, and MAC/Serial/GPON writing.
- MAC Writer downloads and verifies the firmware once, then serves it to each DUT through its dedicated source NIC.
- Added firmware CLI placeholders including `{FIRMWARE_URL}`, `{FIRMWARE_FILE}`, `{FIRMWARE_SHA256}`, `{SOURCE_IP}` and `{DUT_IP}`.
- Added configurable firmware command timeout, reboot expectation and reconnect timing.
- Firmware failures before identity reservation do not consume the identity row.
- Existing firmware CLI is intentionally blank until the production command and firmware image are supplied.

## v13.9 — MAC Writer label scan + automatic start

- Added `prolinecmd clearall` as the first router command after Telnet login/prompt.
- Added MAC Writer label scan input for each of the 8 DUTs.
- A scan may be MAC, hardware Serial Number, or GPON Serial Number.
- Added central server lookup `/api/identity/lookup` to resolve the scan to the complete identity row.
- Added exact scanned-identity reservation `/api/allocate-specific`; the writer no longer has to take the next row when label scanning is enabled.
- Preserved the configurable PCB serial gate from Box Build.
- Added PCB/Telnet ready waiting and green READY state.
- Added automatic programming after a valid scan and PCB-ready detection.
- Added config controls for scan requirement, auto-start, PCB wait timeout, poll interval, and pre-write command.

## v13.7 - Corrected production stage order

- Corrected the seven-stage MES flow to: Wi-Fi Calibration -> Label Printing -> Box Build -> MAC Write -> BOB Calibration -> Wi-Fi Coupling & VoIP -> Verification.
- Kept the Box Build PCB link before MAC Write.
- Kept the configurable MAC Writer PCB gate (`REQUIRE_PCB_SERIAL_BEFORE_WRITE`).
- Changed MES Line Input and WIP calculations to use Wi-Fi Calibration as the beginning of the production line.
- Updated local MES, Render MES, documentation, configuration comments and integration tests.

## v13.6 - Box Build first + PCB serial gate before MAC Write

- Changed MES process order so Box Build is stage 1 and MAC Write is stage 2.
- Added `REQUIRE_PCB_SERIAL_BEFORE_WRITE=1/0` to `mac_writer/config.txt`.
- When enabled, MAC Writer asks the central server to confirm the next identity row already has a PCB Serial Number.
- If no PCB serial is present, the server does not reserve a MAC and the Writer tells the operator: `NO PCB SERIAL NUMBER PRESENT — Complete Box Build first.`
- The server checks the first AVAILABLE row in sequence rather than skipping to a later PCB-linked row, protecting traceability.
- Box Build no longer requires MAC Write PASS by default (`REQUIRE_MAC_WRITE_PASS=0`).
- MES Line Input and WIP now use Box Build volume as the beginning of the line.

# v13.1 - Telnet prompt bytes compatibility fix

- Fixed MAC Writer error: `argument should be integer or bytes-like object, not 'str'`.
- Fixed the same prompt-reading issue in Quality Verifier.
- Telnet prompt separators are now encoded to bytes and responses are safely normalized back to text.

# Changelog

## v13 — Six-stage MES and production log collector

- Changed MES stage flow to:
  1. MAC Write
  2. Wi-Fi Calibration
  3. Label Printing
  4. BOB Calibration
  5. Wi-Fi Coupling & VoIP
  6. Verification
- Added a branded Windows Stage Log Collector application.
- Added REGEX, CSV and JSONL log parsers.
- Added persistent file byte offsets and automatic log-rotation handling.
- Added a durable collector upload queue and retry handling.
- Added local server endpoints for external stage-log ingestion.
- Added `stage_log_history` to the local production database.
- Added cloud `STAGE_LOG_RESULT` events and PostgreSQL schema migration.
- Added stage-specific PASS, FAIL, volume and yield analytics.
- Added a visual six-stage process flow to the Render MES.
- Updated production records and CSV exports to include stage, source log and raw log data.
- Added duplicate-safe end-to-end tests for all six stages.

## v12 — Hybrid Render Cloud MES

- Added durable local-to-cloud synchronization.
- Added PostgreSQL-backed Render MES and ingestion API.
- Added automatic retry, event IDs, cloud status and offline production continuity.

## v13.2 - Responsive 8-router verifier
- Fit Quality Verifier window to the current screen size.
- Added a vertically scrollable router station area.
- Automatically stacks Routers 01–04 and Routers 05–08 on narrower displays.
- Preserves two-column router panels on wide displays.

## v13.3
- Server imports MAC + Serial Number + GPON Serial Number as one identity row.
- Writer uses server-provided Serial/GPON from the same allocated row instead of generating them locally.
- Added Identity columns to server MAC Ledger and identity sample CSV.
- Added strict REQUIRE_SERVER_IDENTIFIERS=1 production mode.

## v13.5 - Box Build PCB traceability

- Added **Box Build** as stage 6; Verification is now stage 7.
- Added `box_build` desktop scanner application.
- Product-label scan validates MAC + Serial + GPON against the central identity list.
- PCB Serial Number is stored in the same `mac_pool` row as MAC / Serial / GPON.
- Added unique PCB-serial protection and immutable MAC-to-PCB binding rules.
- Added `box_build_station` and `box_build_at` audit fields.
- Added Box Build MES synchronization through the durable local cloud queue.
- Added `pcb_serial_number` to Render PostgreSQL production events and CSV exports.
- Added MES Traceability search by MAC, Serial, GPON or PCB Serial.
- Added PCB Serial columns to local server ledger, verification history and MES records.
- Added migration, duplicate protection, parser and end-to-end traceability tests.
