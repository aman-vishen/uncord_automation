# v13.8 MAC Writer update

MAC Writer now uses the user-provided 8-DUT same-IP architecture:

- All DUTs use 192.168.2.1.
- Each DUT is selected through a dedicated Windows NIC source IP (192.168.2.101–108 by default).
- Telnet preflight and telnetlib3 connections bind to the configured source IP.
- Server-provided MAC + Serial Number + GPON Serial Number remain linked.
- REQUIRE_PCB_SERIAL_BEFORE_WRITE remains configurable.
- When the PCB gate is enabled and no PCB serial is linked by Box Build, no identifiers are written.
