# MAC Writer v13.9

Workflow per DUT:

1. Operator scans MAC, hardware Serial Number, or GPON Serial Number.
2. Central server resolves the complete identity row.
3. If enabled, PCB Serial Number is required from Box Build.
4. Writer waits for `192.168.2.1` through that DUT's configured source NIC.
5. Card becomes green `READY`.
6. With auto-start enabled, programming starts immediately.
7. First router command is `prolinecmd clearall`.
8. MAC + Serial + GPON are written, read back, verified and reported.

Config defaults:

```ini
REQUIRE_LABEL_SCAN_BEFORE_WRITE = 1
AUTO_START_AFTER_SCAN = 1
PCB_READY_WAIT_SECONDS = 60
PCB_READY_POLL_SECONDS = 0.5
PRE_WRITE_COMMAND = prolinecmd clearall
REQUIRE_PCB_SERIAL_BEFORE_WRITE = 1
```
