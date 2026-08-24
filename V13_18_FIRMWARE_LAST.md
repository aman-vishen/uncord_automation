# v13.18 — Firmware Update as Last DUT Step

Production order inside MAC Write:

1. Scan label and resolve MAC + Serial + GPON + PCB serial.
2. Confirm PCB/Telnet ready.
3. Reserve that exact identity row.
4. Run `prolinecmd clearall`.
5. Write MAC, serial and GPON.
6. Read back and verify all identifiers.
7. If firmware is ON at the Central Server, download/verify the firmware and run the integrated SSH/SFTP/sysupgrade Python updater.
8. `sysupgrade` reboots the PCB automatically and the updater verifies SSH/health after reboot.
9. Report PASS/FAIL/ERROR to the Central Server.

When firmware is ON, `[FINALIZE]` is intentionally skipped so the PCB is not rebooted before firmware flashing. When firmware is OFF, `[FINALIZE]` still runs as before.
