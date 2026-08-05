# Changelog

## v11 / UNCORD command integration

- Populated writer and verifier command files from `UNCORD_Router_Command_Template.xlsx`.
- Added AX3000 and AC1200 verifier profiles plus Windows profile-selection scripts.
- Writer now supports optional `[FINALIZE]` commands and runs the required `firstboot -y -r` after successful readback.
- Writer uses `prolinecmd` for MAC, hardware serial, GPON and Telnet enable.
- Added passive Telnet monitoring for the exact WPS and Reset event logs supplied in the workbook.
- Added post-reboot user-mode reconnection and `factorymode get` confirmation.
- Added static product-model part number because no part-number command was supplied.
- Added workbook Wi-Fi, BOB, optical power, firmware, LED, SSID and password commands to configuration profiles.
- Preserved source limitations and added explicit firmware-validation warnings for missing calibration criteria and the 5 GHz `ra0`/`rai0` entries.

## v9 / Verifier v4

- Added a live running-log strip to every writer and verifier router slot.
- Added a router-specific **VIEW LOG** window with complete command, response and server history.
- PASS, FAIL, ERROR and running states remain visible independently for all 8 routers.
- Added manual serial-label entry for every verification slot.
- Added direct support for USB barcode scanners through the serial entry field and Enter key.
- Added per-router camera scanning.
- Added continuous **AUTO CAMERA** mode that assigns scanned serials to available router slots in order.
- Added camera decoding for common 1D/2D formats using OpenCV and zxing-cpp.
- Added duplicate serial-scan blocking across all 8 local router slots.
- Added configurable serial extraction regex, camera index, timeout, confirmation frames and optional auto-start.
- The verifier compares the scanned label serial with the serial read from the router.
- The central server stores the scanned serial and serial-scan result for audit and rejects mismatches.
- Verification CSV export now includes router serial, scanned serial and scan result.

## v8 / Verifier v3

- MAC writer programs and verifies MAC address, serial number and GPON number.
- Central server stores the MAC/serial/GPON mapping and prevents duplicate assignment.
- Added Wi-Fi calibration data, BOB calibration data and firmware-version checks.
- Final PASS requires identity, calibration, firmware, LED, Reset, WPS and user-mode stages.
