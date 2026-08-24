# Box Build Traceability — updated for v13.7

The Box Build feature introduced in v13.5/v13.6 remains active, but the current stage order is:

1. Wi-Fi Calibration
2. Label Printing
3. Box Build
4. MAC Write
5. BOB Calibration
6. Wi-Fi Coupling & VoIP
7. Verification

Box Build scans the product label containing MAC + Serial Number + GPON Serial Number, validates it against the Central Server, scans the PCB Serial Number, and stores that PCB serial in the same identity row.

The MAC Writer can enforce the link with `REQUIRE_PCB_SERIAL_BEFORE_WRITE=1`.
