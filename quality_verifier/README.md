# ETE Router Calibration & Quality Verifier v5

Final-production verifier for up to 8 router PCBs with ETE dashboard UI and UNCORD AX3000/AC1200 command profiles.

## Select product profile

Default: AX3000.

```bat
use_AX3000_profile.bat
```

or:

```bat
use_AC1200_profile.bat
```

The selected file is copied to `config.ini`.

## Verification stages

1. Manual, USB scanner or camera serial-label capture.
2. Read MAC, hardware serial and GPON with `prolinecmd`.
3. Use product model as part number because the source workbook provides no part-number command.
4. Confirm the label serial, router identity and central writer record match without duplication.
5. Read Wi-Fi calibration EEPROM data.
6. Request BOB calibration information.
7. Validate firmware format `V1.0.1_YYMMDD`.
8. Turn listed LEDs on, obtain operator PASS/FAIL, then turn them off.
9. Passively monitor Telnet output for the exact Reset and WPS button logs from the workbook.
10. Set `factorymode` to 0, reboot, reconnect and confirm `Value=0`.

## Passive button monitoring

The workbook gives physical button instructions and expected logs but no shell polling command. `PASSIVE_MONITOR=1` keeps the Telnet session open and watches for:

- Reset: `the button was BTN_0 and the action was pressed`
- WPS: both pressed and released log messages

This requires the firmware to emit those messages to the active Telnet session. If it writes them only to `logread` or `dmesg`, replace passive monitoring with the correct `CHECK_COMMAND_n` supplied by firmware engineering.

## Calibration pass criteria

The workbook contains commands but no valid/invalid output examples. `PASS_REGEX` and `FAIL_REGEX` are blank, so Wi-Fi and BOB stages currently pass when a non-empty command response is received. Add validated regex rules before mass production.

## Installation

```bat
pip install -r requirements.txt
run_verifier.bat
```

All commands, extraction regex, expected firmware pattern, LED paths, button logs, user-mode confirmation and router IPs are in `config.ini`.
