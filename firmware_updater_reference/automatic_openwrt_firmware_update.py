"""
Automatic OpenWrt firmware updater for an authorized router.

Flow:
1. Connect over SSH
2. Upload local firmware with SFTP
3. Validate image with `sysupgrade -T`
4. Start `sysupgrade`
5. Wait for router to reboot
6. Reconnect over SSH
7. Read system/firmware information and report PASS/FAIL

Install:
    pip install paramiko

Edit CONFIG below before running.
"""

import json
import socket
import subprocess
import time
from pathlib import Path

import paramiko


# =========================
# CONFIG
# =========================

ROUTER_IP = "192.168.2.1"
SSH_PORT = 22

USERNAME = "admin"
PASSWORD = "admin"

FIRMWARE_FILE = r"V1.0.1_260316.bin"

# Leave empty if you only want to verify that the router returns after upgrade.
# Example: EXPECTED_VERSION = "1.2.3"
EXPECTED_VERSION = ""

# Normal sysupgrade preserves configuration.
# Set to True only when you intentionally want factory/default configuration.
ERASE_CONFIG = False

REMOTE_FIRMWARE = "/tmp/automatic_firmware.bin"

CONNECT_TIMEOUT = 10
REBOOT_TIMEOUT = 300
POLL_INTERVAL = 3


def log(message):
    print(time.strftime("[%H:%M:%S]"), message, flush=True)


def ssh_connect():
    client = paramiko.SSHClient()
    client.load_system_host_keys()

    # For a controlled production LAN this accepts the DUT host key on first use.
    # For a hardened deployment, replace this with host-key pinning.
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    client.connect(
        hostname=ROUTER_IP,
        port=SSH_PORT,
        username=USERNAME,
        password=PASSWORD,
        timeout=CONNECT_TIMEOUT,
        auth_timeout=CONNECT_TIMEOUT,
        banner_timeout=CONNECT_TIMEOUT,
        look_for_keys=False,
        allow_agent=False,
    )
    return client


def run_command(client, command, timeout=60):
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    exit_code = stdout.channel.recv_exit_status()

    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()

    return exit_code, out, err


def tcp_ssh_up():
    try:
        with socket.create_connection((ROUTER_IP, SSH_PORT), timeout=1.5):
            return True
    except OSError:
        return False


def wait_until_down(timeout=90):
    log("Waiting for router to go offline...")
    deadline = time.time() + timeout

    while time.time() < deadline:
        if not tcp_ssh_up():
            log("Router is rebooting.")
            return True
        time.sleep(1)

    return False


def wait_until_up(timeout=REBOOT_TIMEOUT):
    log("Waiting for router to return...")
    deadline = time.time() + timeout

    while time.time() < deadline:
        if tcp_ssh_up():
            # SSH port may open before the system is fully ready.
            try:
                client = ssh_connect()
                client.close()
                log("Router is back online and accepting SSH.")
                return True
            except Exception:
                pass

        time.sleep(POLL_INTERVAL)

    return False


def read_board_info(client):
    # `ubus call system board` is available on normal OpenWrt systems.
    code, out, err = run_command(client, "ubus call system board", timeout=20)

    if code == 0 and out:
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return {"raw": out}

    # Fallback if ubus information is unavailable.
    code, out, err = run_command(
        client,
        "cat /etc/openwrt_release 2>/dev/null || uname -a",
        timeout=20,
    )
    return {"raw": out or err}


def firmware_text(info):
    return json.dumps(info, ensure_ascii=False, sort_keys=True)


def main():
    local_firmware = Path(FIRMWARE_FILE)

    if not local_firmware.is_file():
        raise SystemExit(f"FAIL: Firmware file does not exist: {local_firmware}")

    log(f"Target router: {ROUTER_IP}")
    log(f"Firmware: {local_firmware}")

    # -----------------------------------------------------
    # 1. Connect
    # -----------------------------------------------------
    log("Connecting to router by SSH...")
    try:
        client = ssh_connect()
    except Exception as exc:
        raise SystemExit(f"FAIL: SSH connection/login failed: {exc}")

    try:
        # -------------------------------------------------
        # 2. Capture old version/information
        # -------------------------------------------------
        before = read_board_info(client)
        log("Current router information:")
        print(json.dumps(before, indent=2, ensure_ascii=False))

        # -------------------------------------------------
        # 3. Check free space
        # -------------------------------------------------
        exit_code, out, err = run_command(client, "df -k /tmp | tail -1")
        if exit_code == 0:
            log(f"/tmp space: {out}")

        # -------------------------------------------------
        # 4. Upload firmware
        # -------------------------------------------------
        log("Uploading firmware...")
        sftp = client.open_sftp()
        try:
            sftp.put(str(local_firmware), REMOTE_FIRMWARE)
        finally:
            sftp.close()

        local_size = local_firmware.stat().st_size

        exit_code, out, err = run_command(
            client,
            f"wc -c < {REMOTE_FIRMWARE}",
            timeout=20,
        )

        if exit_code != 0:
            raise RuntimeError(f"Could not verify uploaded file size: {err}")

        remote_size = int(out.strip())

        if remote_size != local_size:
            raise RuntimeError(
                f"Upload size mismatch. Local={local_size}, remote={remote_size}"
            )

        log(f"Upload verified: {remote_size:,} bytes")

        # -------------------------------------------------
        # 5. Validate firmware with OpenWrt
        # -------------------------------------------------
        log("Validating firmware image...")
        exit_code, out, err = run_command(
            client,
            f"sysupgrade -T {REMOTE_FIRMWARE}",
            timeout=120,
        )

        if exit_code != 0:
            raise RuntimeError(
                "Firmware validation failed.\n"
                f"STDOUT: {out}\n"
                f"STDERR: {err}"
            )

        log("Firmware image validation PASSED.")

        # -------------------------------------------------
        # 6. Start firmware upgrade
        # -------------------------------------------------
        upgrade_command = "sysupgrade "

        if ERASE_CONFIG:
            upgrade_command += "-n "

        upgrade_command += REMOTE_FIRMWARE

        log("Starting firmware upgrade. Router will disconnect/reboot...")

        # Do not wait for a normal exit code: SSH will usually be terminated
        # by sysupgrade during shutdown.
        transport = client.get_transport()
        channel = transport.open_session()
        channel.exec_command(upgrade_command)

        time.sleep(2)

    finally:
        try:
            client.close()
        except Exception:
            pass

    # -----------------------------------------------------
    # 7. Confirm reboot
    # -----------------------------------------------------
    went_down = wait_until_down()

    if not went_down:
        log("Warning: router-offline transition was not detected.")

    # -----------------------------------------------------
    # 8. Wait for router to come back
    # -----------------------------------------------------
    if not wait_until_up():
        raise SystemExit(
            f"FAIL: Router did not return within {REBOOT_TIMEOUT} seconds."
        )

    # -----------------------------------------------------
    # 9. Reconnect and verify firmware
    # -----------------------------------------------------
    log("Reconnecting for post-upgrade verification...")

    try:
        client = ssh_connect()
    except Exception as exc:
        raise SystemExit(f"FAIL: Router returned but SSH login failed: {exc}")

    try:
        after = read_board_info(client)

        log("Post-upgrade router information:")
        print(json.dumps(after, indent=2, ensure_ascii=False))

        # Basic post-upgrade health check
        exit_code, out, err = run_command(client, "uptime", timeout=20)
        if exit_code != 0:
            raise RuntimeError(f"Post-upgrade health check failed: {err}")

        log(f"Router uptime: {out}")

    finally:
        client.close()

    # -----------------------------------------------------
    # 10. Expected-version check, if configured
    # -----------------------------------------------------
    if EXPECTED_VERSION:
        text = firmware_text(after)

        if EXPECTED_VERSION not in text:
            raise SystemExit(
                "FAIL: Router is online, but expected firmware version "
                f"{EXPECTED_VERSION!r} was not found."
            )

    print()
    print("========================================")
    print("        FIRMWARE UPDATE: PASS")
    print("========================================")


if __name__ == "__main__":
    main()
