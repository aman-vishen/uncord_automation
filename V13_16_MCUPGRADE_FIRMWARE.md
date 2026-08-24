# v13.16 mcupgrade firmware integration

Firmware file: `V1.0.1_260316.bin`
Size: 27,035,648 bytes
SHA-256: `3aad6d8274752f0a4ddc2c79d2ae042cf558b5bd57d4ffa2483c056c3922b828`

Default per-DUT command:

```text
mcupgrade send <downloaded firmware> -i <DUT source NIC> --platform econet --group 224.1.2.3 -p 8000 --delay 10 --senders 1 --passes 2 -q
```

The server owns the firmware image and ON/OFF control. The writer downloads and verifies the image before running the local CLI. The sender starts before the configurable DUT reboot command, then the writer waits for Telnet recovery before MAC/Serial/GPON programming.

Important: the upload contained `pyproject.toml` and README but did not contain `src/multicast_upgrade/`, so the executable is not bundled. Install `mcupgrade` separately until the source/wheel/executable is provided.
