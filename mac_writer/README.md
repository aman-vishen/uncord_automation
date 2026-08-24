# ETE Router Identity Writer v11

Programs up to 8 router PCBs in parallel through Telnet using commands populated from `UNCORD_Router_Command_Template.xlsx`.

Each router slot:

1. Checks the configured router IP and Telnet port.
2. Atomically reserves a unique MAC from the central server.
3. Generates `ZTEG` + 8-hex serial and GPON values from the server sequence.
4. Writes MAC, hardware serial, GPON number and Telnet-enable state using `prolinecmd`.
5. Reads MAC, serial and GPON back and checks exact identity values.
6. Runs commands in `[FINALIZE]`; the supplied profile uses `firstboot -y -r`.
7. Reports PASS, FAIL or ERROR to the server and shows full per-router live logs.

## Commands file

Edit `commands.txt`. Supported sections are:

- `[WRITE]` — required
- `[VERIFY]` — required
- `[FINALIZE]` — optional; the final command may reboot/drop Telnet

Supported placeholders include `{MAC_NOSEP}`, `{MAC}`, `{SERIAL}`, `{GPON}`, `{SEQ_HEX8}` and other documented sequence/MAC formats.

## Important

The workbook says `prolinecmd` changes require a reset. The writer therefore verifies before issuing the reset command. Perform a production validation to confirm the identifiers persist after reboot on the target firmware.

## v13.9 scan-to-program workflow

For each DUT, place the cursor in its **LABEL SCAN** field and scan one value: MAC, hardware Serial Number, or GPON Serial Number. Pressing Enter (normally sent by the barcode scanner) makes the Writer query the central server and load the complete MAC + Serial + GPON + PCB Serial identity.

With `REQUIRE_PCB_SERIAL_BEFORE_WRITE=1`, the scan is blocked if Box Build has not linked a PCB serial. The Writer then waits for the DUT Telnet port through its configured source NIC. When reachable, the card turns green **READY** and, with `AUTO_START_AFTER_SCAN=1`, programming starts automatically.

The first router command after login/prompt is `PRE_WRITE_COMMAND`, defaulting to `prolinecmd clearall`.

## Server-controlled firmware update (v13.15)

The Central Server owns the firmware image and global ON/OFF switch. In v13.18 the Writer first reserves and writes the scanned identity, runs `prolinecmd clearall`, writes MAC/Serial/GPON, and verifies them. If firmware is enabled, the supplied SSH/SFTP/sysupgrade Python updater then runs as the **last DUT step**. Its `sysupgrade` automatically reboots the PCB, so `[FINALIZE]` is skipped in that case. If firmware is OFF, the normal `[FINALIZE]` command is used.



## v13.18 direct Python firmware updater — firmware last

When firmware is enabled on the central server, MAC Writer downloads and SHA-256 verifies the selected image and then runs the supplied OpenWrt update workflow directly in Python: SSH login, SFTP upload to `/tmp/automatic_firmware.bin`, `sysupgrade -T`, `sysupgrade`, reboot detection, SSH reconnect, board-info readback and `uptime` health check.

Because all eight DUTs use `192.168.2.1`, every firmware SSH socket is explicitly bound to that DUT's configured `ROUTER_n_SOURCE_IP` (`192.168.2.101` through `.108`). Install dependencies with `pip install -r requirements.txt`. No `mcupgrade` executable is required.
