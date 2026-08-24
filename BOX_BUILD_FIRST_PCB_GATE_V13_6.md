# PCB Gate Before MAC Write — updated for v13.7

The v13.6 document originally described Box Build as stage 1. That order is superseded by v13.7.

Current production flow:

1. Wi-Fi Calibration
2. Label Printing
3. Box Build
4. MAC Write
5. BOB Calibration
6. Wi-Fi Coupling & VoIP
7. Verification

Box Build still occurs before MAC Write. At Box Build, the operator scans the printed product label and PCB Serial Number, and the Central Server links the PCB serial to the same MAC + Serial + GPON identity row.

In `mac_writer/config.txt`:

```ini
REQUIRE_PCB_SERIAL_BEFORE_WRITE=1
```

With the gate ON, MAC Writer will not reserve or write the next identity unless that identity row already contains a PCB Serial Number. Set the value to `0` only when you intentionally want to allow MAC Write without the Box Build link.
