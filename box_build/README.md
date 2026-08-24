# Box Build Traceability — v13.7

Box Build is **stage 3**, after Wi-Fi Calibration and Label Printing, and before MAC Write.

Operator workflow:

1. Scan the printed product label containing MAC + Serial Number + GPON Serial Number.
2. The app validates that identity against the Central Server.
3. Scan the PCB Serial Number.
4. The server writes the PCB Serial Number into the same identity row.
5. MES records a `BOX_BUILD` PASS result.
6. MAC Writer can then require the PCB link before programming.

`BOX_BUILD_BEFORE_MAC_WRITE=1` should remain enabled for the normal production flow. `REQUIRE_MAC_WRITE_PASS=0` should remain disabled because Box Build occurs before MAC Write.
