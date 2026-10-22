"""Direct OpenWrt firmware updater used by the ETE MAC Writer.

Adapted from the supplied automatic_openwrt_firmware_update.py:
SSH -> SFTP upload -> sysupgrade -T -> sysupgrade -> reboot wait -> SSH verify.

The production addition is source_ip binding so eight DUTs may all use 192.168.2.1
while each connection is forced through its dedicated Windows Ethernet NIC.
"""
from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from typing import Callable

try:
    import paramiko
except ImportError:  # surfaced as a clear production error at runtime
    paramiko = None


class FirmwareUpdateError(RuntimeError):
    pass


def _emit(log: Callable[[str], None] | None, message: str) -> None:
    if log:
        log(message)


def _bound_tcp_socket(router_ip: str, source_ip: str, port: int, timeout: float):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.bind((source_ip, 0))
        sock.connect((router_ip, port))
        return sock
    except Exception:
        sock.close()
        raise


def _ssh_connect(router_ip: str, source_ip: str, port: int, username: str, password: str, timeout: float):
    if paramiko is None:
        raise FirmwareUpdateError("Missing dependency 'paramiko'. Run: pip install -r mac_writer/requirements.txt")
    sock = None
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    # Same behavior as the supplied script for a controlled production LAN.
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        sock = _bound_tcp_socket(router_ip, source_ip, port, timeout)
        client.connect(
            hostname=router_ip,
            port=port,
            username=username,
            password=password,
            timeout=timeout,
            auth_timeout=timeout,
            banner_timeout=timeout,
            look_for_keys=False,
            allow_agent=False,
            sock=sock,
        )
        return client
    except Exception:
        try:
            client.close()
        except Exception:
            pass
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        raise


def _run_command(client, command: str, timeout: float = 60):
    _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    return exit_code, out, err


def _tcp_ssh_up(router_ip: str, source_ip: str, port: int) -> bool:
    try:
        sock = _bound_tcp_socket(router_ip, source_ip, port, 1.5)
        sock.close()
        return True
    except OSError:
        return False


def _wait_until_down(router_ip: str, source_ip: str, port: int, timeout: float, log=None) -> bool:
    _emit(log, "Waiting for router to go offline...")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _tcp_ssh_up(router_ip, source_ip, port):
            _emit(log, "Router is rebooting.")
            return True
        time.sleep(1)
    return False


def _wait_until_up(router_ip: str, source_ip: str, port: int, username: str, password: str,
                   timeout: float, connect_timeout: float, poll_interval: float, log=None) -> bool:
    _emit(log, "Waiting for router to return on SSH...")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _tcp_ssh_up(router_ip, source_ip, port):
            try:
                client = _ssh_connect(router_ip, source_ip, port, username, password, connect_timeout)
                client.close()
                _emit(log, "Router is back online and accepting SSH.")
                return True
            except Exception:
                pass
        time.sleep(poll_interval)
    return False


def _read_board_info(client):
    code, out, err = _run_command(client, "ubus call system board", timeout=20)
    if code == 0 and out:
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return {"raw": out}
    code, out, err = _run_command(client, "cat /etc/openwrt_release 2>/dev/null || uname -a", timeout=20)
    return {"raw": out or err}


def upgrade_router_firmware(*, router_ip: str, source_ip: str, firmware_path: Path | str,
                            username: str = "admin", password: str = "admin", ssh_port: int = 22,
                            expected_version: str = "", erase_config: bool = False,
                            remote_firmware: str = "/tmp/automatic_firmware.bin",
                            connect_timeout: float = 10, down_timeout: float = 90,
                            reboot_timeout: float = 300, poll_interval: float = 3,
                            validate_timeout: float = 120, log=None) -> dict:
    local_firmware = Path(firmware_path)
    if not local_firmware.is_file():
        raise FirmwareUpdateError(f"Firmware file does not exist: {local_firmware}")
    if not source_ip:
        raise FirmwareUpdateError("Dedicated source/NIC IP is required for same-IP 8-DUT firmware update")

    _emit(log, f"Target router: {router_ip} via PC NIC {source_ip}")
    _emit(log, f"Firmware: {local_firmware.name} ({local_firmware.stat().st_size:,} bytes)")
    _emit(log, "Connecting to router by SSH...")
    try:
        client = _ssh_connect(router_ip, source_ip, ssh_port, username, password, connect_timeout)
    except Exception as exc:
        raise FirmwareUpdateError(f"SSH connection/login failed through {source_ip}: {exc}") from exc

    before = {}
    try:
        before = _read_board_info(client)
        _emit(log, "Current router information: " + json.dumps(before, ensure_ascii=False, sort_keys=True)[:500])

        code, out, err = _run_command(client, "df -k /tmp | tail -1", timeout=20)
        if code == 0 and out:
            _emit(log, f"/tmp space: {out}")

        _emit(log, f"Uploading firmware by SFTP to {remote_firmware} ...")
        sftp = client.open_sftp()
        try:
            sftp.put(str(local_firmware), remote_firmware)
        finally:
            sftp.close()

        local_size = local_firmware.stat().st_size
        code, out, err = _run_command(client, f"wc -c < {remote_firmware}", timeout=20)
        if code != 0:
            raise FirmwareUpdateError(f"Could not verify uploaded file size: {err or out}")
        try:
            remote_size = int(out.strip())
        except ValueError as exc:
            raise FirmwareUpdateError(f"Invalid remote size response: {out!r}") from exc
        if remote_size != local_size:
            raise FirmwareUpdateError(f"Upload size mismatch. Local={local_size}, remote={remote_size}")
        _emit(log, f"Upload verified: {remote_size:,} bytes")

        _emit(log, "Validating firmware image with sysupgrade -T ...")
        code, out, err = _run_command(client, f"sysupgrade -T {remote_firmware}", timeout=validate_timeout)
        if code != 0:
            raise FirmwareUpdateError(f"Firmware validation failed. STDOUT: {out} STDERR: {err}")
        _emit(log, "Firmware image validation PASSED.")

        upgrade_command = "sysupgrade " + ("-n " if erase_config else "") + remote_firmware
        _emit(log, f"Starting firmware upgrade: {upgrade_command}")
        transport = client.get_transport()
        if transport is None or not transport.is_active():
            raise FirmwareUpdateError("SSH transport is not active before sysupgrade")
        channel = transport.open_session()
        channel.exec_command(upgrade_command)
        time.sleep(2)
    except FirmwareUpdateError:
        raise
    except Exception as exc:
        raise FirmwareUpdateError(f"Firmware update failed before reboot: {exc}") from exc
    finally:
        try:
            client.close()
        except Exception:
            pass

    went_down = _wait_until_down(router_ip, source_ip, ssh_port, down_timeout, log)
    if not went_down:
        _emit(log, "Warning: router-offline transition was not detected.")

    if not _wait_until_up(router_ip, source_ip, ssh_port, username, password,
                          reboot_timeout, connect_timeout, poll_interval, log):
        raise FirmwareUpdateError(f"Router did not return within {reboot_timeout:g} seconds.")

    _emit(log, "Reconnecting for post-upgrade verification...")
    try:
        client = _ssh_connect(router_ip, source_ip, ssh_port, username, password, connect_timeout)
    except Exception as exc:
        raise FirmwareUpdateError(f"Router returned but SSH login failed: {exc}") from exc
    try:
        after = _read_board_info(client)
        _emit(log, "Post-upgrade router information: " + json.dumps(after, ensure_ascii=False, sort_keys=True)[:500])
        code, out, err = _run_command(client, "uptime", timeout=20)
        if code != 0:
            raise FirmwareUpdateError(f"Post-upgrade health check failed: {err or out}")
        _emit(log, f"Router uptime: {out}")
    finally:
        client.close()

    if expected_version:
        text = json.dumps(after, ensure_ascii=False, sort_keys=True)
        if expected_version not in text:
            raise FirmwareUpdateError(
                f"Router is online, but expected firmware version {expected_version!r} was not found."
            )

    _emit(log, "FIRMWARE UPDATE: PASS")
    return {"before": before, "after": after, "uptime": out, "firmware_file": local_firmware.name}
