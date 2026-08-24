# Central Production Server v13.7

The server stores MAC, Serial Number and GPON Serial Number from the imported identity list. Box Build later adds `PCB_SERIAL_NUMBER` to the same row.

Correct process order:

1. Wi-Fi Calibration
2. Label Printing
3. Box Build
4. MAC Write
5. BOB Calibration
6. Wi-Fi Coupling & VoIP
7. Verification

## Box Build API

`POST /api/box-build/lookup` validates the scanned label identity.

`POST /api/box-build/bind` links a unique PCB Serial Number to that identity and creates the Box Build MES event.

## MAC Writer PCB gate

The allocation API accepts the `require_pcb_serial` option. When enabled, the first AVAILABLE identity must already contain a PCB Serial Number; otherwise allocation is rejected with `NO PCB SERIAL NUMBER PRESENT`.

## MAC Writer v13.9 identity scan API

- `POST /api/identity/lookup` with `scan_value` resolves MAC, Serial Number, or GPON Serial Number.
- `POST /api/allocate-specific` atomically reserves the exact MAC selected by the scan.
- When `require_pcb_serial` is enabled, exact reservation is rejected unless Box Build has already linked a PCB Serial Number.

## Firmware repository control (v13.15)

Use **Select Firmware File** in the server GUI to copy a firmware image into the local server repository, then use **Enable Firmware / Disable Firmware** to control whether MAC Writer stations perform the update. The selected image is SHA-256 protected. See `../V13_15_SERVER_CONTROLLED_FIRMWARE.md`.


## v13.17 writer firmware path

The server firmware file selection and ON/OFF API are unchanged. MAC Writer v13.17 now consumes the selected image with direct Python SSH/SFTP + sysupgrade instead of an external multicast CLI.
