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
