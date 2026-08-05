# UNCORD Command Mapping

Source workbook: `UNCORD_Router_Command_Template.xlsx`

## Writer

| Workbook function | Application location | Configured command |
|---|---|---|
| Write MAC | `mac_writer/commands.txt [WRITE]` | `prolinecmd macaddr set {MAC_NOSEP}` |
| Write Serial Number | `[WRITE]` | `prolinecmd hwsn set {SERIAL}` |
| Write GPON Number | `[WRITE]` | `prolinecmd gponsn set {GPON}` |
| Read identity | `[VERIFY]` | `prolinecmd macaddr get`, `hwsn get`, `gponsn get` |
| Enable Telnet | `[WRITE]` | `prolinecmd telnetEnable set 1` |
| Apply/reset | `[FINALIZE]` | `firstboot -y -r` |

## Verifier

| Workbook function | Config section |
|---|---|
| Read MAC / Serial / GPON | `[READ_MAC]`, `[READ_SERIAL]`, `[READ_GPON_NUMBER]` |
| LED sequence | `[LED_TEST]` |
| WPS logs | `[WPS_TEST]` passive monitoring |
| Reset logs | `[RESET_TEST]` passive monitoring |
| Wi-Fi calibration data | `[WIFI_CALIBRATION]` |
| BOB calibration data | `[BOB_CALIBRATION]` |
| Optical TX/RX power | `[OPTICAL_POWER]` reference section |
| Firmware version | `[FIRMWARE_VERSION]` |
| User mode | `[USER_MODE]` plus post-reboot verification |
| SSID / password | Disabled reference sections |

## Items not defined by the workbook

- No part-number read command: profile model is used as a static part number.
- No Wi-Fi or BOB valid/invalid output examples: stages currently check for non-empty command output.
- Button log transport is not specified: passive Telnet monitoring assumes logs appear in the active session.
