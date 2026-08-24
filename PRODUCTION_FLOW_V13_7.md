# Production Flow — v13.7

1. Wi-Fi Calibration
2. Label Printing
3. Box Build
4. MAC Write
5. BOB Calibration
6. Wi-Fi Coupling & VoIP
7. Verification

Box Build remains before MAC Write so the PCB Serial Number can be linked to the identity before programming. MAC Writer can enforce this with `REQUIRE_PCB_SERIAL_BEFORE_WRITE=1`.
