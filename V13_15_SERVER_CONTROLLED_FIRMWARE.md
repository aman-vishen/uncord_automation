# v13.15 - Server-Controlled Firmware Update in MAC Write

## Production sequence inside MAC Write

1. Operator scans MAC / Hardware Serial / GPON Serial.
2. Central server resolves the complete identity row.
3. PCB serial gate is checked.
4. DUT/Telnet becomes READY.
5. MAC Writer reads the firmware policy from the local central server.
6. If firmware is OFF, firmware is skipped.
7. If firmware is ON:
   - Writer downloads the selected firmware from the central server.
   - SHA-256 and file size are verified.
   - Writer temporarily serves the firmware through the dedicated DUT NIC.
   - Commands under `[FIRMWARE]` in `mac_writer/commands.txt` are executed.
   - If configured, Writer waits for the DUT to reboot and Telnet to return.
8. Only after firmware succeeds does the server reserve the scanned identity.
9. `prolinecmd clearall` runs.
10. MAC + Serial + GPON are written, read back, verified, and reported.

This order prevents a firmware failure from consuming or writing an identity row.

## Central Server controls

The server GUI has a **MAC Writer Firmware Update** bar with:

- `Select Firmware File` - copies the selected image to `server/firmware/` and stores SHA-256 metadata.
- `Enable Firmware` / `Disable Firmware` - persistent production ON/OFF switch.

The default is OFF.

The local API used by MAC Writer is:

- `GET /api/firmware/config`
- `GET /api/firmware/download`

Both use the existing `X-API-Key` authentication.

## Firmware CLI

Add the actual device CLI under `[FIRMWARE]` in `mac_writer/commands.txt`.

Available placeholders:

- `{FIRMWARE_URL}` - URL exposed by the Writer on that DUT's dedicated NIC.
- `{FIRMWARE_FILE}` - local cached firmware filename.
- `{FIRMWARE_SERVER_FILE}` - original firmware filename selected on the server.
- `{FIRMWARE_SHA256}` - expected SHA-256.
- `{FIRMWARE_SIZE}` - size in bytes.
- `{SOURCE_IP}` - dedicated PC NIC IP.
- `{DUT_IP}` / `{ROUTER_IP}` - DUT IP.

The `[FIRMWARE]` section is intentionally empty in this release because the exact production CLI has not yet been supplied. Keep the server firmware switch OFF until the real command is added.

## MAC Writer firmware timing settings

`mac_writer/config.txt` includes:

- `FIRMWARE_LOCAL_HTTP_PORT`
- `FIRMWARE_COMMAND_TIMEOUT_SECONDS`
- `FIRMWARE_EXPECT_REBOOT`
- `FIRMWARE_RECONNECT_DELAY_SECONDS`
- `FIRMWARE_RECONNECT_TIMEOUT_SECONDS`
- `FIRMWARE_RECONNECT_POLL_SECONDS`
- `FIRMWARE_SUCCESS_REGEX`
- `FIRMWARE_FAIL_REGEX`

If the router must download `{FIRMWARE_URL}`, Windows Firewall must allow Python on the DUT-facing private NICs.
