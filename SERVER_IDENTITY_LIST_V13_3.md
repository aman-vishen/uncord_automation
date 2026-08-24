# v13.3 — Server-managed MAC + Serial + GPON identities

Import one production identity per line into the central server.

Preferred CSV format:

```csv
MAC,SERIAL_NUMBER,GPON_SERIAL_NUMBER
14:D6:7C:00:00:00,SN00000001,ZTEG00000001
14:D6:7C:00:00:08,SN00000002,ZTEG00000002
```

The central server reserves the entire row atomically. The MAC Writer receives the MAC, Serial Number and GPON Serial Number together and uses those exact values in `commands.txt`:

```text
prolinecmd macaddr set {MAC_NOSEP}
prolinecmd hwsn set {SERIAL}
prolinecmd gponsn set {GPON}
```

With `REQUIRE_SERVER_IDENTIFIERS=1`, the Writer refuses to program a board if either Serial Number or GPON Serial Number is missing from the server row.
