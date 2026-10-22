# ETE Router MAC Writer v13.19

Separate router-version MAC Writer derived from the existing v13.18 MAC Writer.

## Production sequence

1. Connect/pre-check DUT.
2. Reserve production identity.
3. Clear existing production identity using the existing PRE_WRITE_COMMAND.
4. Write MAC / hardware serial / GPON serial / Telnet enable.
5. Verify MAC / serial / GPON.
6. Complete the existing reboot path BEFORE WAN provisioning:
   - Firmware OFF: existing [FINALIZE] runs `firstboot -y -r`.
   - Firmware ON: [FINALIZE] is skipped and the existing sysupgrade flow performs its reboot.
7. Wait for Telnet to return.
8. Apply static router WAN profile.
9. Reload networking, reconnect and verify WAN settings + `ifstatus WANv4_1`.
10. Report PASS to the central server.

## Final WAN profile

- Name: WAN_1
- Protocol: static
- IPv4: 20.20.20.20
- Netmask: 255.255.255.0
- Gateway: 20.20.20.1
- DNS: 8.8.8.8 / 8.8.4.4
- NAT: enabled
- VLAN: disabled
- Application type: internet
- IP mode: ipv4
- MTU mode: recommended

## Critical safety rule

After router WAN provisioning starts, the production cycle executes **no firstboot, clearall, factory reset, or other destructive reset command**. Server reporting after the WAN step is bookkeeping only and sends no DUT command.

The original `mac_writer/` directory is unchanged.
