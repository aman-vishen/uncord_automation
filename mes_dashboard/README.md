# ETE Solutions India Web MES

Browser-based MES dashboard reading the same `server/mac_server.db` used by the MAC Writer and Quality Verifier.

## Features
- Daily production volume and final yield
- Writer PASS/FAIL and central MAC pool status
- Stage-wise yield for server MAC, serial scan, Wi-Fi calibration, BOB calibration, firmware, LED, Reset, WPS and User Mode
- Station-wise production and yield
- Latest unit records with MAC, serial, GPON, part number and firmware
- Date filtering, live refresh and CSV export

## LAN deployment (recommended first)
1. Keep `mes_dashboard` beside the existing `server` folder.
2. Run `run_mes.bat` on the central server PC.
3. Open `http://SERVER-IP:8080` from any PC/tablet on the same network.
4. Allow TCP port 8080 in Windows Firewall.

## Docker
From the suite root:

```bash
docker compose up -d --build
```

## Public web deployment
SQLite must be deployed with the server and needs persistent storage. For production internet access, use a VPN/private tunnel or migrate to PostgreSQL. Do not expose the current unauthenticated dashboard directly to the public internet.
