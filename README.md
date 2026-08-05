# Uncord Automation v11 — UNCORD Command Integration

ETE Solutions India production automation suite for router manufacturing.

## Applications

- `server/` — central MAC pool, atomic allocation, duplicate protection, identity mapping and final-verification history.
- `mac_writer/` — 8-router identity writer. Programs MAC, hardware serial number and GPON number using the UNCORD `prolinecmd` commands, verifies all values, enables Telnet and runs the required reset/apply command.
- `quality_verifier/` — 8-router final verifier with manual/USB/camera serial scan, central identity validation, Wi-Fi calibration data, BOB calibration data, firmware, LED, Reset, WPS and final user-mode confirmation.
- `mes_dashboard/` — browser MES dashboard for production volume and stage-wise analysis.
- `docs/UNCORD_Router_Command_Template.xlsx` — source workbook used to populate the command profiles.

## Product profiles

The workbook contains `AX3000` and `AC1200` command sheets. The default verifier profile is AX3000.

To activate a profile on Windows:

```bat
cd quality_verifier
use_AX3000_profile.bat
```

or:

```bat
use_AC1200_profile.bat
```

Profile files are stored under `quality_verifier/profiles/`.

## Writer command flow

`mac_writer/commands.txt` now runs:

1. `prolinecmd macaddr set {MAC_NOSEP}`
2. `prolinecmd hwsn set {SERIAL}`
3. `prolinecmd gponsn set {GPON}`
4. `prolinecmd telnetEnable set 1`
5. Read back MAC, serial and GPON.
6. Run `firstboot -y -r` from the optional `[FINALIZE]` section.

The supplied workbook shows `ZTEG` plus 8 hexadecimal characters for both serial and GPON values. The default templates therefore use:

```ini
SERIAL_TEMPLATE=ZTEG{SEQ_HEX8}
GPON_TEMPLATE=ZTEG{SEQ_HEX8}
```

Confirm the `ZTEG` prefix is approved for your product before production use.

## Verifier command flow

The AX3000 and AC1200 profiles include the workbook commands for:

- MAC, hardware serial and GPON readback
- LED on/off sequence
- passive WPS and Reset log detection
- Wi-Fi EEPROM/calibration data reads
- BOB information request
- optical TX/RX power reference command
- firmware version check against `V1.0.1_YYMMDD`
- switch to user mode, reboot, reconnect and confirm `factorymode=0`
- optional Telnet, SSID and Wi-Fi password commands

## Source limitations that require firmware confirmation

The workbook does not supply a part-number command. The profiles therefore use the product model (`AX3000` or `AC1200`) as `READ_PART_NUMBER.STATIC_VALUE`.

The workbook does not define pass criteria for Wi-Fi or BOB calibration output. Those stages currently require a non-empty Telnet response. Add exact `PASS_REGEX` and `FAIL_REGEX` values when the firmware team provides valid/invalid examples.

The workbook lists the 5 GHz EEPROM commands as `rai0 e2p 999`, followed by `ra0 e2p 08` and `ra0 e2p 09`. These values are preserved exactly and should be confirmed before release.

## Windows quick start

```bat
cd server
run_server.bat
```

```bat
cd mac_writer
run_client.bat
```

```bat
cd quality_verifier
pip install -r requirements.txt
run_verifier.bat
```

```bat
cd mes_dashboard
run_mes.bat
```

Open the MES at `http://<server-ip>:8080`.

## Database upgrade and security

Back up `server/mac_server.db` before production upgrades. Do not commit production passwords, API keys, private databases or pending-job files.
