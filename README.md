# Uncord Automation v13.7 — Corrected Seven-Stage Production Flow

ETE Solutions India router production automation suite with local production continuity, PCB traceability and Render-hosted MES analytics.

## Correct production flow

The MES and documentation now use this exact order:

1. **Wi-Fi Calibration** — collected from the Wi-Fi calibration station log.
2. **Label Printing** — collected from the label-printing station log.
3. **Box Build** — operator scans the printed product label and PCB Serial Number. The server links the PCB serial to the matching MAC + Serial + GPON identity row.
4. **MAC Write** — writes MAC, Serial Number and GPON Serial Number to the board. With `REQUIRE_PCB_SERIAL_BEFORE_WRITE=1`, writing is blocked unless Box Build already added the PCB Serial Number.
5. **BOB Calibration** — collected from the BOB calibration station log.
6. **Wi-Fi Coupling & VoIP** — collected from the coupling/VoIP station log.
7. **Verification** — final quality verification.

## Identity traceability

Each server identity row contains:

```text
MAC
Serial Number
GPON Serial Number
PCB Serial Number
```

Wi-Fi Calibration and Label Printing occur before Box Build. Box Build scans the printed label, validates its MAC/Serial/GPON against the server identity list, then permanently links the PCB Serial Number. MAC Write can then require that PCB link before programming the board.

## MAC Writer PCB gate

In `mac_writer/config.txt`:

```ini
REQUIRE_PCB_SERIAL_BEFORE_WRITE=1
```

- `1` — require a PCB Serial Number before reserving/writing the next identity.
- `0` — disable the gate and allow MAC Write when PCB Serial Number is blank.

When the gate is enabled and the next identity has no PCB Serial Number, the Writer stops before programming and shows:

```text
NO PCB SERIAL NUMBER PRESENT — Complete Box Build before MAC Write. MAC was not written.
```

## Applications

- `server/` — identity pool, PCB traceability, production history and cloud synchronization.
- `stage_log_collector/` — monitors Wi-Fi Calibration, Label Printing, BOB Calibration and Wi-Fi Coupling & VoIP logs.
- `box_build/` — scans product label + PCB serial and links them on the server.
- `mac_writer/` — eight-router MAC/Serial/GPON writer with optional PCB gate.
- `quality_verifier/` — eight-router final verification.
- `mes_dashboard/` — Render cloud MES and traceability dashboard.

## Factory connection

Writer, Verifier, Stage Log Collector and Box Build all connect to the local Central Server. The Central Server synchronizes results to Render over HTTPS.

The MES Line Input now uses **Wi-Fi Calibration** volume, and estimated WIP is calculated from Wi-Fi Calibration input minus final Verification output.

Before upgrading, back up `server/mac_server.db`. Keep production passwords, API keys and database files out of GitHub.
