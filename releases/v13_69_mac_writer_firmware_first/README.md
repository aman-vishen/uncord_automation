# MAC Writer v13.69 — Firmware First

Production sequence:

1. Scan/resolve PCB identity.
2. Check DUT Telnet reachability.
3. Download Central Server-selected firmware and verify SHA-256.
4. SSH/SFTP firmware upgrade (sysupgrade -T -> sysupgrade) and verify post-upgrade health.
5. Wait for Telnet.
6. Run prolinecmd factorymode set 1.
7. Run firstboot -y -r.
8. Wait for Telnet after firstboot.
9. Reserve the scanned MAC/SN/GPON identity from Central Server.
10. Write MAC, Serial, GPON and telnetEnable.
11. Read back and verify MAC/SN/GPON.
12. Report MAC Write PASS/FAIL.

Important:
- PRE_WRITE_COMMAND is blank. prolinecmd clearall is no longer forced after firstboot.
- If firmware/factorymode/firstboot fails, no identity is reserved.
- Existing 8-DUT same-IP source-NIC binding is preserved.