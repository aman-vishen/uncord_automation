# ETE Cloud MES v13.7

The dashboard tracks the exact production flow:

1. Wi-Fi Calibration
2. Label Printing
3. Box Build
4. MAC Write
5. BOB Calibration
6. Wi-Fi Coupling & VoIP
7. Verification

Line Input is based on Wi-Fi Calibration tested volume. Finished Output is based on Verification tested volume. Estimated WIP is input minus finished output.

The cloud API accepts `IDENTITY_WRITER_RESULT`, `STAGE_LOG_RESULT`, `QUALITY_VERIFICATION_RESULT` and `MAC_POOL_SNAPSHOT`. `BOX_BUILD` is a `STAGE_LOG_RESULT` carrying `pcb_serial_number`.

Traceability search supports MAC, Serial Number, GPON Serial Number and PCB Serial Number.
