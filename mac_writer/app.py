import asyncio
import hashlib
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib import error, request
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

try:
    import telnetlib3
except ImportError:
    telnetlib3 = None

APP_VERSION = "13.18-ETE-FIRMWARE-LAST"
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.txt"
COMMANDS_PATH = BASE_DIR / "commands.txt"
PENDING_PATH = BASE_DIR / "pending_jobs.json"
MAX_ROUTERS = 8
PENDING_LOCK = threading.RLock()
FIRMWARE_CACHE_DIR = BASE_DIR / "firmware_cache"
FIRMWARE_CACHE_LOCK = threading.RLock()

# Reusable firmware updater adapted from the supplied automatic_openwrt_firmware_update.py.
# It uses SSH/SFTP + sysupgrade and binds every connection to this DUT's dedicated PC NIC.
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
from firmware_updater import FirmwareUpdateError, upgrade_router_firmware

# ETE Solutions India brand palette (sampled from the supplied logo)
BRAND_DARK = "#185890"
BRAND_BLUE = "#2898D0"
BRAND_DEEP = "#12466F"
BRAND_BG = "#F2F8FC"
BRAND_CARD = "#FFFFFF"
BRAND_TEXT = "#17324A"
BRAND_MUTED = "#5C7387"
BRAND_BORDER = "#D5E7F2"


class AppError(Exception):
    pass


@dataclass
class RouterTarget:
    slot: int
    name: str
    ip: str
    source_ip: str
    enabled: bool
    port: int
    username: str
    password: str
    login_prompt: str
    password_prompt: str
    command_prompt: str
    timeout: float
    command_delay: float
    preflight_timeout: float

    @property
    def key(self) -> str:
        return f"R{self.slot}"


def parse_key_value_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        raise AppError(f"Config file not found: {path}")
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if "=" not in line:
            raise AppError(f"Invalid config line {line_no}: {raw}")
        key, value = line.split("=", 1)
        data[key.strip().upper()] = value.strip()
    return data


def _cfg_value(cfg: dict[str, str], slot: int, key: str, default: str = "") -> str:
    return cfg.get(f"ROUTER_{slot}_{key}", cfg.get(key, default))


def parse_router_targets(cfg: dict[str, str]) -> list[RouterTarget]:
    try:
        count = int(cfg.get("ROUTER_COUNT", str(MAX_ROUTERS)))
    except ValueError as exc:
        raise AppError("ROUTER_COUNT must be a number") from exc
    if not 1 <= count <= MAX_ROUTERS:
        raise AppError(f"ROUTER_COUNT must be between 1 and {MAX_ROUTERS}")

    routers: list[RouterTarget] = []
    seen_source_ips: set[str] = set()
    for slot in range(1, count + 1):
        enabled = cfg.get(f"ROUTER_{slot}_ENABLED", "1").strip().lower() not in {"0", "no", "false", "off"}

        # All DUTs may use the same factory LAN address.
        ip = cfg.get(f"ROUTER_{slot}_IP", cfg.get("ROUTER_IP", "192.168.2.1")).strip()

        # Each physical PC Ethernet port must have its own static local IP.
        # Defaults:
        # DUT1 -> 192.168.2.101, DUT2 -> .102, ... DUT8 -> .108
        source_ip = cfg.get(f"ROUTER_{slot}_SOURCE_IP", "").strip()

        name = cfg.get(f"ROUTER_{slot}_NAME", f"Router {slot}").strip() or f"Router {slot}"

        if enabled and not ip:
            raise AppError(f"ROUTER_{slot}_IP is required because slot {slot} is enabled")
        if enabled and not source_ip:
            raise AppError(f"ROUTER_{slot}_SOURCE_IP is required in config.txt because slot {slot} is enabled")

        if enabled:
            try:
                socket.inet_aton(ip)
            except OSError as exc:
                raise AppError(f"ROUTER_{slot}_IP is not a valid IPv4 address: {ip}") from exc

            try:
                socket.inet_aton(source_ip)
            except OSError as exc:
                raise AppError(
                    f"ROUTER_{slot}_SOURCE_IP is not a valid IPv4 address: {source_ip}"
                ) from exc

            if source_ip in seen_source_ips:
                raise AppError(f"Duplicate PC/source IP in config: {source_ip}")
            seen_source_ips.add(source_ip)
        try:
            port = int(_cfg_value(cfg, slot, "PORT", "23"))
            timeout = float(_cfg_value(cfg, slot, "TIMEOUT", "10"))
            delay = float(_cfg_value(cfg, slot, "COMMAND_DELAY", "0.3"))
            preflight_timeout = float(_cfg_value(cfg, slot, "PING_TIMEOUT", "1.5"))
        except ValueError as exc:
            raise AppError(f"Invalid numeric Telnet setting for router {slot}") from exc
        routers.append(RouterTarget(
            slot=slot,
            name=name,
            ip=ip,
            source_ip=source_ip,
            enabled=enabled,
            port=port,
            username=_cfg_value(cfg, slot, "USERNAME", ""),
            password=_cfg_value(cfg, slot, "PASSWORD", ""),
            login_prompt=_cfg_value(cfg, slot, "LOGIN_PROMPT", "login:"),
            password_prompt=_cfg_value(cfg, slot, "PASSWORD_PROMPT", "Password:"),
            command_prompt=_cfg_value(cfg, slot, "COMMAND_PROMPT", "#"),
            timeout=timeout,
            command_delay=delay,
            preflight_timeout=preflight_timeout,
        ))
    return routers


def parse_commands_file(path: Path) -> tuple[list[str], list[str], list[str]]:
    """Load normal identity commands.

    A legacy [FIRMWARE] section is still accepted so an older commands.txt does not
    break an upgraded station, but v13.18 does not execute firmware CLI commands.
    Firmware is handled directly in Python with SSH/SFTP + sysupgrade.
    """
    if not path.exists():
        raise AppError(f"Commands file not found: {path}")
    sections = {"FIRMWARE": [], "WRITE": [], "VERIFY": [], "FINALIZE": []}
    current = None
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip().upper()
            if current not in sections:
                raise AppError(f"Unknown section [{current}] at line {line_no}")
            continue
        if current is None:
            raise AppError(f"Command outside [WRITE]/[VERIFY]/[FINALIZE] at line {line_no}")
        sections[current].append(line)
    if not sections["WRITE"] or not sections["VERIFY"]:
        raise AppError("commands.txt requires both [WRITE] and [VERIFY] commands")
    return sections["WRITE"], sections["VERIFY"], sections["FINALIZE"]


def normalize_mac(value: str) -> str:
    return re.sub(r"[^0-9A-Fa-f]", "", value).upper()


def validate_mac(value: str) -> str:
    mac = normalize_mac(value)
    if len(mac) != 12 or not re.fullmatch(r"[0-9A-F]{12}", mac):
        raise AppError(f"Invalid MAC: {value}")
    return mac


def colon_mac(value: str) -> str:
    n = validate_mac(value)
    return ":".join(n[i:i+2] for i in range(0, 12, 2))


def identifier_formats(mac: str, serial: str = "", gpon: str = "", seq: int = 0) -> dict[str, object]:
    n = validate_mac(mac)
    colon = ":".join(n[i:i+2] for i in range(0, 12, 2))
    dash = "-".join(n[i:i+2] for i in range(0, 12, 2))
    return {
        "MAC": colon, "MAC_COLON": colon, "MAC_DASH": dash,
        "MAC_NOSEP": n, "MAC_LOWER": colon.lower(),
        "MAC_LAST6": n[-6:], "MAC_LAST8": n[-8:],
        "SERIAL": serial, "GPON": gpon,
        "SEQ": seq, "SEQ_PAD6": f"{seq:06d}", "SEQ_PAD8": f"{seq:08d}",
        "SEQ_PAD10": f"{seq:010d}", "SEQ_HEX6": f"{seq:06X}", "SEQ_HEX8": f"{seq:08X}",
    }


def render_template(template: str, values: dict[str, object], label: str) -> str:
    try:
        result = template.format(**values).strip()
    except (KeyError, ValueError) as exc:
        raise AppError(f"Invalid {label} template/placeholder: {exc}") from exc
    if not result:
        raise AppError(f"{label} template generated an empty value")
    return result


def build_identifiers(allocation: dict, cfg: dict[str, str]) -> tuple[str, str, int]:
    """Use the Serial + GPON values allocated with this MAC by the central server.

    This preserves the exact identity tuple from one imported input-list row. Legacy
    template generation is retained only when REQUIRE_SERVER_IDENTIFIERS=0.
    """
    mac = allocation.get("mac", "")
    seq = int(allocation.get("seq", 0))
    serial = str(allocation.get("serial_number") or "").strip()
    gpon = str(allocation.get("gpon_number") or "").strip()
    require_server = str(cfg.get("REQUIRE_SERVER_IDENTIFIERS", "1")).strip().lower() not in {"0", "no", "false", "off"}

    if require_server and (not serial or not gpon):
        missing = []
        if not serial: missing.append("Serial Number")
        if not gpon: missing.append("GPON Serial Number")
        raise AppError(f"Server identity row for {mac} is missing {' and '.join(missing)}. Import MAC,SERIAL,GPON on the central server before writing.")

    if not serial or not gpon:
        base = identifier_formats(mac, seq=seq)
        serial = serial or render_template(cfg.get("SERIAL_TEMPLATE", "SN{SEQ_PAD10}"), base, "SERIAL")
        base["SERIAL"] = serial
        gpon = gpon or render_template(cfg.get("GPON_TEMPLATE", "UNCD{SEQ_HEX8}"), base, "GPON")

    serial_regex = cfg.get("SERIAL_VALID_REGEX", r"^[A-Za-z0-9._/-]{4,64}$")
    gpon_regex = cfg.get("GPON_VALID_REGEX", r"^[A-Za-z0-9._/-]{4,64}$")
    if serial_regex and not re.fullmatch(serial_regex, serial):
        raise AppError(f"Server serial number failed SERIAL_VALID_REGEX: {serial}")
    if gpon_regex and not re.fullmatch(gpon_regex, gpon):
        raise AppError(f"Server GPON serial failed GPON_VALID_REGEX: {gpon}")
    return serial, gpon, seq


def expand_command(command: str, mac: str, serial: str, gpon: str, seq: int) -> str:
    try:
        return command.format(**identifier_formats(mac, serial, gpon, seq))
    except (KeyError, ValueError) as exc:
        raise AppError(f"Unknown or invalid command placeholder: {exc}") from exc


def save_json_atomic(path: Path, data: dict) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(temp, path)


def load_pending_jobs() -> dict[str, dict]:
    with PENDING_LOCK:
        if not PENDING_PATH.exists():
            return {}
        try:
            data = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}


def set_pending_job(slot_key: str, job: dict | None) -> None:
    with PENDING_LOCK:
        jobs = load_pending_jobs()
        if job is None:
            jobs.pop(slot_key, None)
        else:
            jobs[slot_key] = job
        if jobs:
            save_json_atomic(PENDING_PATH, jobs)
        else:
            try:
                PENDING_PATH.unlink(missing_ok=True)
            except OSError:
                pass


def check_router_reachable(router: RouterTarget) -> None:
    """Check the DUT through its dedicated Windows Ethernet interface."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(router.preflight_timeout)

        # Binding to the unique local IP selects the physical NIC assigned
        # to this DUT on Windows 11.
        sock.bind((router.source_ip, 0))
        sock.connect((router.ip, router.port))
        return
    except OSError as exc:
        raise AppError(
            f"{router.name} is not reachable at {router.ip}:{router.port} "
            f"through PC NIC {router.source_ip}. No MAC was allocated. "
            "Check DUT power, Ethernet cable, Windows static IP, and Telnet service."
        ) from exc
    finally:
        try:
            sock.close()
        except OSError:
            pass


class ServerClient:
    def __init__(self, config: dict[str, str]):
        self.base_url = config.get("SERVER_URL", "http://127.0.0.1:8765").rstrip("/")
        self.api_key = config.get("SERVER_API_KEY", "")
        self.station_id = config.get("STATION_ID", "").strip() or socket.gethostname()
        self.hostname = socket.gethostname()
        self.timeout = float(config.get("SERVER_TIMEOUT", "5"))

    def _client_id(self, router: RouterTarget) -> str:
        return f"{self.station_id}/{router.key}"

    def call(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        req = request.Request(self.base_url + path, data=body, method=method)
        req.add_header("Accept", "application/json")
        req.add_header("X-API-Key", self.api_key)
        req.add_header("X-Station-ID", self.station_id)
        req.add_header("X-Hostname", self.hostname)
        req.add_header("X-App-Version", APP_VERSION)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except error.HTTPError as exc:
            try:
                data = json.loads(exc.read().decode("utf-8"))
                msg = data.get("error", str(exc))
            except Exception:
                msg = str(exc)
            raise AppError(f"Server rejected request: {msg}") from exc
        except (error.URLError, TimeoutError, OSError) as exc:
            raise AppError(f"Cannot reach MAC server {self.base_url}: {exc}") from exc

    def health(self) -> dict:
        return self.call("GET", "/api/health")

    def stats(self) -> dict:
        return self.call("GET", "/api/stats")

    def firmware_config(self) -> dict:
        return self.call("GET", "/api/firmware/config")

    def download_firmware(self, firmware: dict) -> Path:
        if not firmware.get("available"):
            raise AppError("Firmware update is enabled but the central server has no usable firmware file selected.")
        file_name = _safe_firmware_name(str(firmware.get("file_name") or "firmware.bin"))
        expected_sha = str(firmware.get("sha256") or "").strip().lower()
        expected_size = int(firmware.get("size") or 0)
        download_path = str(firmware.get("download_path") or "/api/firmware/download")
        if not expected_sha:
            raise AppError("Central server firmware metadata is missing SHA-256.")

        FIRMWARE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path = FIRMWARE_CACHE_DIR / f"{expected_sha[:16]}_{file_name}"
        with FIRMWARE_CACHE_LOCK:
            if cache_path.is_file():
                if (not expected_size or cache_path.stat().st_size == expected_size) and _sha256_file(cache_path).lower() == expected_sha:
                    return cache_path
                cache_path.unlink(missing_ok=True)

            temp_path = cache_path.with_suffix(cache_path.suffix + ".part")
            temp_path.unlink(missing_ok=True)
            req = request.Request(self.base_url + download_path, method="GET")
            req.add_header("X-API-Key", self.api_key)
            req.add_header("X-Station-ID", self.station_id)
            req.add_header("X-Hostname", self.hostname)
            req.add_header("X-App-Version", APP_VERSION)
            digest = hashlib.sha256()
            written = 0
            try:
                with request.urlopen(req, timeout=max(self.timeout, 60.0)) as resp, temp_path.open("wb") as out:
                    while True:
                        chunk = resp.read(1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        digest.update(chunk)
                        written += len(chunk)
            except Exception as exc:
                temp_path.unlink(missing_ok=True)
                raise AppError(f"Could not download firmware from central server: {exc}") from exc

            actual_sha = digest.hexdigest().lower()
            if expected_size and written != expected_size:
                temp_path.unlink(missing_ok=True)
                raise AppError(f"Firmware download size mismatch: expected {expected_size}, received {written}")
            if actual_sha != expected_sha:
                temp_path.unlink(missing_ok=True)
                raise AppError(f"Firmware SHA-256 mismatch: expected {expected_sha}, received {actual_sha}")
            os.replace(temp_path, cache_path)
            return cache_path

    def allocate(self, router: RouterTarget, request_id: str, require_pcb_serial: bool = False) -> dict:
        return self.call("POST", "/api/allocate", {
            "client_id": self._client_id(router),
            "request_id": request_id,
            "require_pcb_serial": bool(require_pcb_serial),
        })

    def lookup_identity(self, scan_value: str) -> dict:
        return self.call("POST", "/api/identity/lookup", {"scan_value": scan_value})

    def allocate_specific(self, router: RouterTarget, request_id: str, mac: str, require_pcb_serial: bool = False) -> dict:
        return self.call("POST", "/api/allocate-specific", {
            "client_id": self._client_id(router),
            "request_id": request_id,
            "mac": mac,
            "require_pcb_serial": bool(require_pcb_serial),
        })

    def report(self, router: RouterTarget, reservation_id: str, status: str, detail: str = "",
               serial: str = "", gpon: str = "") -> dict:
        detail_with_router = f"{router.name} {router.ip} | {detail}"
        return self.call("POST", "/api/result", {
            "client_id": self._client_id(router),
            "reservation_id": reservation_id,
            "status": status,
            "serial_number": serial,
            "gpon_number": gpon,
            "detail": detail_with_router,
        })


def _telnet_text(value) -> str:
    """Normalize telnetlib3 reader output to text.

    TelnetReaderUnicode.read() returns str, while readuntil() is inherited
    from the byte reader and therefore accepts/returns bytes. Supporting both
    here keeps the application compatible across telnetlib3 releases.
    """
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


async def read_until(reader, prompt: str, timeout: float) -> str:
    if not prompt:
        await asyncio.sleep(0.2)
        chunks: list[str] = []
        while True:
            try:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=0.15)
            except asyncio.TimeoutError:
                break
            if not chunk:
                break
            chunks.append(_telnet_text(chunk))
        return "".join(chunks)

    # telnetlib3's readuntil() searches its internal bytearray, so the
    # separator must be bytes even when open_connection() uses Unicode mode.
    separator = prompt.encode("utf-8")
    try:
        result = await asyncio.wait_for(reader.readuntil(separator), timeout=timeout)
        return _telnet_text(result)
    except asyncio.TimeoutError as exc:
        raise AppError(f"Timed out waiting for prompt {prompt!r}") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _safe_firmware_name(value: str) -> str:
    name = Path(value or "firmware.bin").name
    return re.sub(r"[^A-Za-z0-9._-]", "_", name) or "firmware.bin"


def _cfg_bool(cfg: dict[str, str], key: str, default: bool = False) -> bool:
    raw = str(cfg.get(key, "1" if default else "0")).strip().lower()
    return raw not in {"0", "no", "false", "off", ""}


def run_direct_firmware_update(router: RouterTarget, firmware_path: Path, cfg: dict[str, str], emit) -> dict:
    """Run the supplied OpenWrt updater logic directly from the MAC Writer.

    The original standalone script targets one router at 192.168.2.1.  This wrapper
    preserves its SSH/SFTP/sysupgrade workflow but also supplies ROUTER_n_SOURCE_IP
    so eight routers with the same DUT IP are kept on their dedicated Windows NICs.
    """
    username = cfg.get("FIRMWARE_SSH_USERNAME", cfg.get("USERNAME", "admin")).strip()
    password = cfg.get("FIRMWARE_SSH_PASSWORD", cfg.get("PASSWORD", "admin"))
    try:
        return upgrade_router_firmware(
            router_ip=router.ip,
            source_ip=router.source_ip,
            firmware_path=firmware_path,
            ssh_port=int(cfg.get("FIRMWARE_SSH_PORT", "22")),
            username=username,
            password=password,
            expected_version=cfg.get("FIRMWARE_EXPECTED_VERSION", "").strip(),
            erase_config=_cfg_bool(cfg, "FIRMWARE_ERASE_CONFIG", False),
            remote_firmware=cfg.get("FIRMWARE_REMOTE_PATH", "/tmp/automatic_firmware.bin").strip() or "/tmp/automatic_firmware.bin",
            connect_timeout=float(cfg.get("FIRMWARE_CONNECT_TIMEOUT_SECONDS", "10")),
            down_timeout=float(cfg.get("FIRMWARE_DOWN_TIMEOUT_SECONDS", "90")),
            reboot_timeout=float(cfg.get("FIRMWARE_REBOOT_TIMEOUT_SECONDS", "300")),
            poll_interval=float(cfg.get("FIRMWARE_POLL_SECONDS", "3")),
            validate_timeout=float(cfg.get("FIRMWARE_VALIDATE_TIMEOUT_SECONDS", "120")),
            log=emit,
        )
    except FirmwareUpdateError as exc:
        raise AppError(str(exc)) from exc


def wait_for_router_after_firmware(router: RouterTarget, initial_delay: float, timeout: float, poll_seconds: float, emit) -> None:
    """After SSH confirms the upgrade, wait until the normal Telnet production service returns."""
    if initial_delay > 0:
        emit(f"Waiting {initial_delay:g}s for production Telnet service ...")
        time.sleep(initial_delay)
    deadline = time.monotonic() + max(1.0, timeout)
    last_error = ""
    while time.monotonic() < deadline:
        try:
            check_router_reachable(router)
            emit("Router Telnet service is reachable after firmware update.")
            return
        except Exception as exc:
            last_error = str(exc)
            time.sleep(max(0.2, poll_seconds))
    raise AppError(f"Router Telnet service did not return after firmware update within {timeout:g}s. Last check: {last_error}")


async def telnet_session(router: RouterTarget, mac: str, serial: str, gpon: str, seq: int,
                         write_cmds: list[str], verify_cmds: list[str], finalize_cmds: list[str], emit):
    if telnetlib3 is None:
        raise AppError("Missing dependency 'telnetlib3'. Run: pip install -r requirements.txt")

    emit(
        f"Connecting to {router.name} at {router.ip}:{router.port} "
        f"via PC NIC {router.source_ip} ..."
    )
    try:
        reader, writer = await asyncio.wait_for(
            telnetlib3.open_connection(
                host=router.ip,
                port=router.port,
                local_addr=(router.source_ip, 0),
                connect_minwait=0.05,
            ),
            timeout=router.timeout,
        )
    except Exception as exc:
        raise AppError(f"Could not connect to {router.name} ({router.ip}:{router.port}): {exc}") from exc

    try:
        if router.username:
            text = await read_until(reader, router.login_prompt, router.timeout)
            if text:
                emit(text.rstrip())
            writer.write(router.username + "\r\n")
            emit(f"> Username sent: {router.username}")
        if router.password:
            text = await read_until(reader, router.password_prompt, router.timeout)
            if text:
                emit(text.rstrip())
            writer.write(router.password + "\r\n")
            emit("> Password sent: ********")
        if router.command_prompt:
            text = await read_until(reader, router.command_prompt, router.timeout)
            if text:
                emit(text.rstrip())

        # Always clear any previous production identity before the first write command.
        pre_write_command = parse_key_value_file(CONFIG_PATH).get("PRE_WRITE_COMMAND", "prolinecmd clearall").strip() or "prolinecmd clearall"
        emit("--- PRE-WRITE CLEAR ---")
        emit(f"> {pre_write_command}")
        writer.write(pre_write_command + "\r\n")
        await asyncio.sleep(router.command_delay)
        response = await read_until(reader, router.command_prompt, router.timeout)
        if response:
            emit(response.rstrip())

        emit("--- WRITE MAC / SERIAL / GPON ---")
        for raw in write_cmds:
            cmd = expand_command(raw, mac, serial, gpon, seq)
            emit(f"> {cmd}")
            writer.write(cmd + "\r\n")
            await asyncio.sleep(router.command_delay)
            response = await read_until(reader, router.command_prompt, router.timeout)
            if response:
                emit(response.rstrip())

        emit("--- VERIFY MAC / SERIAL / GPON ---")
        verify_output: list[str] = []
        for raw in verify_cmds:
            cmd = expand_command(raw, mac, serial, gpon, seq)
            emit(f"> {cmd}")
            writer.write(cmd + "\r\n")
            await asyncio.sleep(router.command_delay)
            response = await read_until(reader, router.command_prompt, router.timeout)
            verify_output.append(response)
            if response:
                emit(response.rstrip())

        output = "\n".join(verify_output)
        checks = {
            "MAC": validate_mac(mac) in normalize_mac(output),
            "SERIAL": serial.upper() in output.upper(),
            "GPON": gpon.upper() in output.upper(),
        }
        for label, passed in checks.items():
            emit(f"VERIFY {label}: {'PASS' if passed else 'FAIL'}")
        passed = all(checks.values())
        if passed and finalize_cmds:
            emit("--- FINALIZE / APPLY CONFIGURATION ---")
            for index, raw in enumerate(finalize_cmds):
                cmd = expand_command(raw, mac, serial, gpon, seq)
                emit(f"> {cmd}")
                writer.write(cmd + "\r\n")
                await asyncio.sleep(router.command_delay)
                try:
                    response = await read_until(reader, router.command_prompt, router.timeout)
                    if response:
                        emit(response.rstrip())
                except Exception as exc:
                    if index == len(finalize_cmds) - 1:
                        emit(f"Connection ended after final apply/reboot command (allowed): {exc}")
                        break
                    raise
            checks["FINALIZE"] = True
        emit("RESULT: PASS - all identifiers verified and configuration applied." if passed else "RESULT: FAIL - one or more identifiers did not match.")
        return passed, checks
    finally:
        try:
            writer.write("exit\r\n")
            await asyncio.sleep(0.1)
            writer.close()
        except Exception:
            pass



class SlotCard:
    STEP_LABELS = {
        "scan": "LABEL",
        "connect": "CONNECT",
        "reserve": "IDENTITY",
        "write": "WRITE",
        "verify": "VERIFY",
        "firmware": "FW",
        "report": "RPT",
    }

    def __init__(self, app, parent, router: RouterTarget, row: int, column: int):
        self.app = app
        self.router = router
        self.running = False
        self.status = tk.StringVar(value="DISABLED" if not router.enabled else ("WAIT SCAN" if app.require_label_scan else "READY"))
        self.mac = tk.StringVar(value="—")
        self.serial = tk.StringVar(value="—")
        self.gpon = tk.StringVar(value="—")
        self.pcb_serial = tk.StringVar(value="—")
        self.scan_value = tk.StringVar(value="")
        self.scanned_identity: dict | None = None
        self.detail = tk.StringVar(value="Disabled in config" if not router.enabled else ("Scan MAC, Serial Number or GPON Serial Number" if app.require_label_scan else "Waiting for PCB"))
        self.live_log = tk.StringVar(value="No activity yet" if router.enabled else "Slot disabled")
        self.log_history: list[str] = []
        self.step_values = {key: "—" for key in self.STEP_LABELS}
        self.step_widgets: dict[str, tk.Label] = {}

        self.frame = tk.Frame(parent, bg="#FFFFFF", highlightthickness=1,
                              highlightbackground="#E7ECF5", bd=0)
        self.frame.grid(row=row, column=column, sticky="nsew", padx=0, pady=(0, 2))
        self.frame.grid_columnconfigure(2, weight=1)

        badge = tk.Label(self.frame, text=f"{router.slot:02d}", bg="#EDF3FF", fg="#4F70E8",
                         font=("Segoe UI", 12, "bold"), width=3, pady=6)
        badge.grid(row=0, column=0, rowspan=2, padx=(10, 8), pady=7, sticky="ns")

        identity = tk.Frame(self.frame, bg="#FFFFFF")
        identity.grid(row=0, column=1, sticky="nw", pady=6, padx=(0, 10))
        tk.Label(identity, text=router.name, bg="#FFFFFF", fg="#27364F",
                 font=("Segoe UI", 15, "bold")).pack(anchor="w")
        tk.Label(
            identity,
            text=f"DUT {router.ip}  |  NIC {router.source_ip}",
            bg="#FFFFFF",
            fg="#8C98AD",
            font=("Consolas", 10, "bold"),
        ).pack(anchor="w", pady=(1, 0))

        identity_values = tk.Frame(self.frame, bg="#FFFFFF")
        identity_values.grid(row=0, column=2, sticky="w", pady=(6, 2))
        for idx, (label, var) in enumerate((("MAC", self.mac), ("SERIAL NO.", self.serial), ("GPON SN", self.gpon), ("PCB SERIAL", self.pcb_serial))):
            block = tk.Frame(identity_values, bg="#FFFFFF")
            block.grid(row=0, column=idx, sticky="w", padx=(0, 16))
            tk.Label(block, text=label, bg="#FFFFFF", fg="#A1AABD",
                     font=("Segoe UI", 10, "bold")).pack(anchor="w")
            tk.Label(block, textvariable=var, bg="#FFFFFF", fg="#28354B",
                     font=("Consolas", 13, "bold")).pack(anchor="w")

        steps = tk.Frame(self.frame, bg="#FFFFFF")
        steps.grid(row=1, column=1, columnspan=2, sticky="w", pady=(2, 4), padx=(0, 6))
        for idx, (key, label) in enumerate(self.STEP_LABELS.items()):
            pill = tk.Label(steps, text=f"{label}  —", bg="#F4F6FA", fg="#8D98AA",
                            font=("Segoe UI", 9, "bold"), padx=8, pady=4)
            pill.grid(row=0, column=idx, padx=(0, 3))
            self.step_widgets[key] = pill

        controls = tk.Frame(self.frame, bg="#FFFFFF")
        controls.grid(row=0, column=3, rowspan=2, padx=(6, 10), pady=6, sticky="e")
        self.status_label = tk.Label(controls, textvariable=self.status, bg="#EDF3FF", fg="#4F70E8",
                                     font=("Segoe UI", 12, "bold"), padx=13, pady=5)
        self.status_label.pack(anchor="e", pady=(0, 3))
        self.button = tk.Button(controls, text="PROGRAM PCB", command=self._clicked,
                                bg="#4F70E8", fg="#FFFFFF", activebackground="#3C5DD9",
                                activeforeground="#FFFFFF", relief="flat", bd=0, cursor="hand2",
                                font=("Segoe UI", 11, "bold"), padx=12, pady=6, width=15)
        self.button.pack(anchor="e")
        self.log_button = tk.Button(controls, text="VIEW LOG", command=lambda: self.app.show_router_log(self),
                                    bg="#F2F5FA", fg="#68758A", activebackground="#E7EDF8",
                                    activeforeground="#4F70E8", relief="flat", bd=0, cursor="hand2",
                                    font=("Segoe UI", 10, "bold"), padx=12, pady=4, width=15)
        self.log_button.pack(anchor="e", pady=(3, 0))

        scan_row = tk.Frame(self.frame, bg="#FFFFFF")
        scan_row.grid(row=2, column=1, columnspan=3, sticky="ew", padx=(0, 10), pady=(2, 3))
        scan_row.grid_columnconfigure(1, weight=1)
        tk.Label(scan_row, text="LABEL SCAN", bg="#FFFFFF", fg="#355CC9",
                 font=("Segoe UI", 11, "bold")).grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.scan_entry = tk.Entry(scan_row, textvariable=self.scan_value, bg="#FFFDF2", fg="#172A3A",
                                   insertbackground="#172A3A", relief="solid", bd=2,
                                   highlightthickness=1, highlightbackground="#7D9DF2", highlightcolor="#4F70E8",
                                   font=("Consolas", 13, "bold"))
        self.scan_entry.grid(row=0, column=1, sticky="ew", ipady=4)
        self.scan_entry.bind("<Return>", self._scan_entered)
        tk.Label(scan_row, text="SCAN HERE  →  ENTER", bg="#FFFFFF", fg="#A1AABD",
                 font=("Segoe UI", 10)).grid(row=0, column=2, sticky="e", padx=(10, 0))

        self.detail_label = tk.Label(self.frame, textvariable=self.detail, bg="#FFFFFF", fg="#7E8A9D",
                                     font=("Segoe UI", 10), anchor="w", wraplength=760, justify="left")
        self.detail_label.grid(row=3, column=1, columnspan=3, sticky="ew", padx=(0, 10), pady=(1, 3))
        # Keep live log data for VIEW LOG / Process Log, but do not spend scarce
        # production-screen height on a repeated per-DUT log strip.
        self.live_log_label = None

        if not router.enabled:
            self.button.configure(state="disabled", bg="#C8CED8")
            self.scan_entry.configure(state="disabled")
            self._apply_status_style("DISABLED")
        elif app.require_label_scan:
            self.button.configure(state="disabled", text="SCAN LABEL FIRST", bg="#C8CED8")

    def _scan_entered(self, _event=None):
        self.app.handle_label_scan(self.router)
        return "break"

    def set_scan_enabled(self, enabled: bool, focus: bool = False):
        if not self.router.enabled:
            return
        self.scan_entry.configure(state="normal" if enabled else "disabled")
        if enabled and focus:
            self.scan_entry.focus_set()
            self.scan_entry.selection_range(0, "end")

    def add_log(self, text: str):
        stamp = time.strftime("%H:%M:%S")
        line = f"[{stamp}] {text}"
        self.log_history.append(line)
        if len(self.log_history) > 1000:
            self.log_history = self.log_history[-1000:]
        compact = " ".join(str(text).split())
        self.live_log.set(compact[:125] + ("…" if len(compact) > 125 else ""))

    def _clicked(self):
        if self.app.has_pending(self.router.key):
            self.app.resolve_pending(self.router)
        else:
            self.app.start_router(self.router)

    def reset_steps(self):
        self.mac.set("—")
        self.serial.set("—")
        self.gpon.set("—")
        self.pcb_serial.set("—")
        for key in self.step_values:
            self.step_values[key] = "—"
            self._render_step(key)

    def set_step(self, name: str, mark: str):
        if name not in self.step_values:
            return
        self.step_values[name] = mark
        self._render_step(name)

    def _render_step(self, name: str):
        widget = self.step_widgets.get(name)
        if widget is None:
            return
        mark = self.step_values.get(name, "—")
        if mark == "✓":
            bg, fg = "#DDF7E9", "#087C4A"
        elif mark == "✕":
            bg, fg = "#FFF0F0", "#C43C4A"
        elif mark == "…":
            bg, fg = "#FFF7E6", "#B77A12"
        else:
            bg, fg = "#F4F6FA", "#8D98AA"
        widget.configure(text=f"{self.STEP_LABELS[name]}  {mark}", bg=bg, fg=fg)

    def _apply_status_style(self, state: str):
        if state in {"PASS", "READY"}:
            bg, fg = "#DDF7E9", "#087C4A"
        elif state in {"FAIL", "ERROR", "RECOVERY", "OFFLINE", "BLOCKED"}:
            bg, fg = "#FFF0F0", "#C43C4A"
        elif state in {"PROGRAMMING", "LOOKUP", "WAIT PCB"}:
            bg, fg = "#FFF7E6", "#B77A12" if state == "WAIT PCB" else "#248BC3"
        elif state == "DISABLED":
            bg, fg = "#F0F2F5", "#9AA3B1"
        else:
            bg, fg = "#EDF3FF", "#4F70E8"
        self.status_label.configure(bg=bg, fg=fg)

    def set_state(self, status: str, detail: str | None = None, mac: str | None = None,
                  serial: str | None = None, gpon: str | None = None, pcb_serial: str | None = None):
        self.status.set(status)
        if detail is not None:
            self.detail.set(detail)
        if mac is not None:
            self.mac.set(mac)
        if serial is not None:
            self.serial.set(serial)
        if gpon is not None:
            self.gpon.set(gpon)
        if pcb_serial is not None:
            self.pcb_serial.set(pcb_serial or "—")
        self._apply_status_style(status)


class ClientApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ETE Solutions India | 8-Router Identity Programming Station")
        # Use almost the full production monitor. The compact router cards are
        # designed so all eight DUTs fit simultaneously on a 1920x1080 screen.
        # The router canvas keeps scrolling as a fallback on smaller displays.
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        win_w = min(1900, max(1100, screen_w - 16))
        win_h = min(1040, max(650, screen_h - 38))
        self.geometry(f"{win_w}x{win_h}")
        self.minsize(1100, 650)
        self.compact_router_cards = True
        self.configure(bg="#DDE6F5")

        self.events: queue.Queue = queue.Queue()
        self.running_slots: set[str] = set()
        self.server_online = False
        self.routers: list[RouterTarget] = []
        self.cards: dict[str, SlotCard] = {}
        self.server_var = tk.StringVar(value="CHECKING")
        self.station_var = tk.StringVar(value="-")
        self.available_var = tk.StringVar(value="-")
        self.reserved_var = tk.StringVar(value="-")
        self.pass_var = tk.StringVar(value="-")
        self.fail_var = tk.StringVar(value="-")
        self.total_var = tk.StringVar(value="-")
        self.active_var = tk.StringVar(value="0")

        self.require_label_scan = True
        self.auto_start_after_scan = True
        self.pcb_ready_wait_seconds = 60.0
        self.pcb_ready_poll_seconds = 0.5

        try:
            cfg = self._cfg()
            self.require_label_scan = str(cfg.get("REQUIRE_LABEL_SCAN_BEFORE_WRITE", "1")).strip().lower() not in {"0", "no", "false", "off"}
            self.auto_start_after_scan = str(cfg.get("AUTO_START_AFTER_SCAN", "1")).strip().lower() not in {"0", "no", "false", "off"}
            self.pcb_ready_wait_seconds = max(1.0, float(cfg.get("PCB_READY_WAIT_SECONDS", "60")))
            self.pcb_ready_poll_seconds = max(0.1, float(cfg.get("PCB_READY_POLL_SECONDS", "0.5")))
            self.routers = parse_router_targets(cfg)
            self.station_var.set(ServerClient(cfg).station_id)
        except Exception as exc:
            messagebox.showerror("Configuration error", str(exc))
            self.routers = []

        self.brand_logo = None
        self._load_brand_assets()
        self._style()
        self._build()
        if self.require_label_scan:
            self.after(300, self._focus_first_scanner)
        self.after(100, self._drain_events)
        self.after(250, self._startup_check)
        self.after(4000, self._poll_server)

    def _load_brand_assets(self):
        logo_path = BASE_DIR / "assets" / "ete_logo.png"
        if logo_path.exists():
            try:
                self.brand_logo = tk.PhotoImage(file=str(logo_path))
                self.iconphoto(True, self.brand_logo)
            except tk.TclError:
                self.brand_logo = None

    def _style(self):
        s = ttk.Style(self)
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        s.configure("TFrame", background="#FFFFFF")
        s.configure("TLabel", background="#FFFFFF", foreground="#2E3A50", font=("Segoe UI", 9))
        s.configure("TButton", font=("Segoe UI", 8, "bold"), padding=(9, 6),
                    background="#F2F5FA", foreground="#647089", borderwidth=0)
        s.map("TButton", background=[("active", "#E7EDF8")], foreground=[("active", "#4F70E8")])

    def _build(self):
        # v13.13: remove the permanent left sidebar and move navigation into a
        # compact top-bar drop-down. This gives the 8-DUT workspace the full
        # monitor width and makes labels/identity values easier to read.
        shell = tk.Frame(self, bg="#DDE6F5")
        shell.pack(fill="both", expand=True, padx=10, pady=8)
        shell.grid_rowconfigure(0, weight=1)
        shell.grid_columnconfigure(0, weight=1)

        main = tk.Frame(shell, bg="#F5F7FB", highlightthickness=1, highlightbackground="#D8E0EE")
        main.grid(row=0, column=0, sticky="nsew")
        main.grid_rowconfigure(1, weight=1)
        main.grid_columnconfigure(0, weight=1)

        # ----- top navigation bar -----
        header = tk.Frame(main, bg="#FFFFFF", height=76)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        header.grid_columnconfigure(1, weight=1)

        brand_wrap = tk.Frame(header, bg="#FFFFFF")
        brand_wrap.grid(row=0, column=0, sticky="w", padx=(16, 12), pady=5)
        if self.brand_logo:
            # Keep the logo compact in the navbar so it does not steal DUT space.
            logo = self.brand_logo
            try:
                if logo.width() > 120:
                    factor = max(2, int(round(logo.width() / 95)))
                    self.nav_logo = logo.subsample(factor, factor)
                else:
                    self.nav_logo = logo
                tk.Label(brand_wrap, image=self.nav_logo, bg="#FFFFFF").pack(side="left", padx=(0, 10))
            except Exception:
                tk.Label(brand_wrap, text="ETE", bg="#4F70E8", fg="#FFFFFF",
                         font=("Segoe UI", 15, "bold"), padx=10, pady=7).pack(side="left", padx=(0, 10))
        else:
            tk.Label(brand_wrap, text="ETE", bg="#4F70E8", fg="#FFFFFF",
                     font=("Segoe UI", 15, "bold"), padx=10, pady=7).pack(side="left", padx=(0, 10))

        title_wrap = tk.Frame(brand_wrap, bg="#FFFFFF")
        title_wrap.pack(side="left")
        tk.Label(title_wrap, text="Router Identity Programming", bg="#FFFFFF", fg="#29364B",
                 font=("Segoe UI", 20, "bold")).pack(anchor="w")
        tk.Label(title_wrap, text="MAC · Serial · GPON · PCB traceability · 8 parallel DUTs",
                 bg="#FFFFFF", fg="#8E9AAF", font=("Segoe UI", 11)).pack(anchor="w", pady=(1, 0))

        header_right = tk.Frame(header, bg="#FFFFFF")
        header_right.grid(row=0, column=2, sticky="e", padx=16)

        station_chip = tk.Frame(header_right, bg="#F6F8FC", highlightthickness=1, highlightbackground="#E9EDF4")
        station_chip.pack(side="left", padx=(0, 8))
        tk.Label(station_chip, text="STATION", bg="#F6F8FC", fg="#A0A9B9",
                 font=("Segoe UI", 9, "bold")).pack(side="left", padx=(10, 6), pady=10)
        tk.Label(station_chip, textvariable=self.station_var, bg="#F6F8FC", fg="#3D5FCA",
                 font=("Segoe UI", 11, "bold")).pack(side="left", padx=(0, 10), pady=10)

        # Navigation drop-down requested for the production station.
        menu_button = tk.Menubutton(
            header_right, text="MENU  ▾", bg="#F2F5FA", fg="#52647E",
            activebackground="#E7EDF8", activeforeground="#3459C7",
            relief="flat", bd=0, cursor="hand2", font=("Segoe UI", 11, "bold"),
            padx=16, pady=9
        )
        nav_menu = tk.Menu(menu_button, tearoff=False, font=("Segoe UI", 11))
        nav_menu.add_command(label="Dashboard", command=lambda: self._show_page("dashboard"))
        nav_menu.add_command(label="Process Log", command=lambda: self._show_page("log"))
        nav_menu.add_separator()
        nav_menu.add_command(label="Refresh Server", command=self._check_server_async)
        nav_menu.add_command(label="Open Config", command=self._open_config)
        nav_menu.add_command(label="Open Commands", command=self._open_commands)
        menu_button.configure(menu=nav_menu)
        menu_button.pack(side="left", padx=(0, 8))
        self.nav_buttons = {}

        server_chip = tk.Frame(header_right, bg="#F6F8FC", highlightthickness=1, highlightbackground="#E9EDF4")
        server_chip.pack(side="left", padx=(0, 8))
        tk.Label(server_chip, text="SERVER", bg="#F6F8FC", fg="#A0A9B9",
                 font=("Segoe UI", 9, "bold")).pack(side="left", padx=(10, 6), pady=10)
        self.header_server = tk.Label(server_chip, textvariable=self.server_var, bg="#F6F8FC", fg="#4F70E8",
                                      font=("Segoe UI", 11, "bold"))
        self.header_server.pack(side="left", padx=(0, 10), pady=10)
        self.all_btn = tk.Button(header_right, text="PROGRAM ALL", command=self.start_all,
                                 bg="#4F70E8", fg="#FFFFFF", activebackground="#3D5FD6",
                                 activeforeground="#FFFFFF", relief="flat", bd=0, cursor="hand2",
                                 font=("Segoe UI", 11, "bold"), padx=22, pady=10)
        self.all_btn.pack(side="left")

        self.page_host = tk.Frame(main, bg="#F5F7FB")
        self.page_host.grid(row=1, column=0, sticky="nsew")
        self.page_host.grid_rowconfigure(0, weight=1)
        self.page_host.grid_columnconfigure(0, weight=1)

        self.dashboard_page = tk.Frame(self.page_host, bg="#F5F7FB")
        self.log_page = tk.Frame(self.page_host, bg="#F5F7FB")
        for page in (self.dashboard_page, self.log_page):
            page.grid(row=0, column=0, sticky="nsew")

        dash = self.dashboard_page
        dash.grid_columnconfigure(0, weight=1)
        dash.grid_rowconfigure(2, weight=1)

        metrics = tk.Frame(dash, bg="#F5F7FB")
        metrics.grid(row=0, column=0, sticky="ew", padx=12, pady=(4, 2))
        for i in range(7):
            metrics.grid_columnconfigure(i, weight=1)

        metric_specs = [
            ("SERVER", self.server_var, "#4F70E8", "S"),
            ("ACTIVE", self.active_var, "#2BAEC7", "A"),
            ("AVAILABLE", self.available_var, "#2AB879", "Q"),
            ("RESERVED", self.reserved_var, "#E0A13A", "R"),
            ("PASS", self.pass_var, "#25A76F", "P"),
            ("FAIL / ERROR", self.fail_var, "#E06873", "F"),
            ("TOTAL MAC", self.total_var, "#7F7DEB", "T"),
        ]
        for i, (label, var, accent, icon_text) in enumerate(metric_specs):
            card = tk.Frame(metrics, bg="#FFFFFF", highlightthickness=1, highlightbackground="#E8ECF3")
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 5, 0 if i == 6 else 5))
            top = tk.Frame(card, bg="#FFFFFF")
            top.pack(fill="x", padx=10, pady=(2, 0))
            tk.Label(top, text=label, bg="#FFFFFF", fg="#9CA7B8", font=("Segoe UI", 9, "bold")).pack(side="left")
            tk.Label(top, text=icon_text, bg=accent, fg="#FFFFFF", font=("Segoe UI", 9, "bold"),
                     width=2, pady=2).pack(side="right")
            tk.Label(card, textvariable=var, bg="#FFFFFF", fg="#263247",
                     font=("Segoe UI", 18, "bold")).pack(anchor="w", padx=10, pady=(0, 3))

        helper = tk.Frame(dash, bg="#F5F7FB")
        helper.grid(row=1, column=0, sticky="ew", padx=12, pady=(1, 2))
        helper.grid_columnconfigure(0, weight=1)
        tk.Label(helper, text="8-DUT PRODUCTION  •  SCAN → PCB CHECK → GREEN READY → AUTO PROGRAM → VERIFY → REPORT",
                 bg="#F5F7FB", fg="#2E3A50", font=("Segoe UI", 12, "bold")).grid(row=0, column=0, sticky="w")
        tk.Button(helper, text="REFRESH SERVER", command=self._check_server_async, bg="#FFFFFF", fg="#617089",
                  activebackground="#EDF2FA", relief="flat", bd=0, cursor="hand2",
                  font=("Segoe UI", 10, "bold"), padx=14, pady=6).grid(row=0, column=1, sticky="e")

        router_host = tk.Frame(dash, bg="#F5F7FB")
        router_host.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 6))
        router_host.grid_rowconfigure(0, weight=1)
        router_host.grid_columnconfigure(0, weight=1)

        router_canvas = tk.Canvas(router_host, bg="#F5F7FB", highlightthickness=0, borderwidth=0)
        router_scroll = ttk.Scrollbar(router_host, orient="vertical", command=router_canvas.yview)
        router_canvas.configure(yscrollcommand=router_scroll.set)
        router_canvas.grid(row=0, column=0, sticky="nsew")
        router_scroll.grid(row=0, column=1, sticky="ns", padx=(6, 0))

        body = tk.Frame(router_canvas, bg="#F5F7FB")
        body.grid_columnconfigure(0, weight=1, uniform="dut_cols")
        body.grid_columnconfigure(1, weight=1, uniform="dut_cols")
        body.grid_rowconfigure(0, weight=1)
        body_window = router_canvas.create_window((0, 0), window=body, anchor="nw")

        def _refresh_router_scrollregion(_event=None):
            bbox = router_canvas.bbox("all")
            if bbox:
                router_canvas.configure(scrollregion=bbox)
                content_h = max(0, bbox[3] - bbox[1])
                visible_h = max(0, router_canvas.winfo_height())
                if visible_h > 1 and content_h <= visible_h + 2:
                    router_scroll.grid_remove()
                else:
                    router_scroll.grid()

        def _fit_router_body(event):
            router_canvas.itemconfigure(body_window, width=max(1, event.width))
            self.update_idletasks()
            requested_h = max(1, body.winfo_reqheight())
            target_h = max(int(event.height), requested_h)
            router_canvas.itemconfigure(body_window, height=target_h)
            _refresh_router_scrollregion()

        def _router_mousewheel(event):
            delta = getattr(event, "delta", 0)
            if delta:
                router_canvas.yview_scroll(-1 if delta > 0 else 1, "units")
            return "break"

        def _enable_router_wheel(_event=None):
            router_canvas.bind_all("<MouseWheel>", _router_mousewheel)

        def _disable_router_wheel(_event=None):
            router_canvas.unbind_all("<MouseWheel>")

        body.bind("<Configure>", _refresh_router_scrollregion)
        router_canvas.bind("<Configure>", _fit_router_body)
        router_canvas.bind("<Enter>", _enable_router_wheel)
        router_canvas.bind("<Leave>", _disable_router_wheel)
        body.bind("<Enter>", _enable_router_wheel)
        body.bind("<Leave>", _disable_router_wheel)

        panels = []
        for col, title in enumerate(("DUT 01–04", "DUT 05–08")):
            panel = tk.Frame(body, bg="#FFFFFF", highlightthickness=1, highlightbackground="#DDE4EF")
            panel.grid(row=0, column=col, sticky="nsew", padx=(0, 7) if col == 0 else (7, 0))
            panel.grid_columnconfigure(0, weight=1)
            tk.Label(panel, text=title, bg="#FFFFFF", fg="#344159", font=("Segoe UI", 14, "bold")).grid(
                row=0, column=0, sticky="w", padx=14, pady=(4, 3))
            tk.Frame(panel, bg="#EFF2F7", height=1).grid(row=1, column=0, sticky="ew")
            holder = tk.Frame(panel, bg="#FFFFFF")
            holder.grid(row=2, column=0, sticky="nsew", padx=7, pady=3)
            holder.grid_columnconfigure(0, weight=1)
            for dut_row in range(4):
                holder.grid_rowconfigure(dut_row, weight=1, uniform="dut_rows")
            panel.grid_rowconfigure(2, weight=1)
            panels.append(holder)

        for idx, router in enumerate(self.routers):
            panel_idx = 0 if idx < 4 else 1
            row_idx = idx if idx < 4 else idx - 4
            card = SlotCard(self, panels[panel_idx], router, row_idx, 0)
            self.cards[router.key] = card

        self.router_canvas = router_canvas
        self.router_scrollbar = router_scroll

        self.log_page.grid_columnconfigure(0, weight=1)
        self.log_page.grid_rowconfigure(1, weight=1)
        log_head = tk.Frame(self.log_page, bg="#F5F7FB")
        log_head.grid(row=0, column=0, sticky="ew", padx=18, pady=(18, 8))
        log_head.grid_columnconfigure(0, weight=1)
        tk.Label(log_head, text="Process Log", bg="#F5F7FB", fg="#2E3A50",
                 font=("Segoe UI", 15, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(log_head, text="Telnet commands, MAC/Serial/GPON assignments and central server responses",
                 bg="#F5F7FB", fg="#98A2B4", font=("Segoe UI", 9)).grid(row=1, column=0, sticky="w", pady=(2, 0))
        tk.Button(log_head, text="CLEAR LOG", command=self._clear_log,
                  bg="#FFFFFF", fg="#617089", relief="flat", bd=0, cursor="hand2",
                  font=("Segoe UI", 9, "bold"), padx=12, pady=7).grid(row=0, column=1, rowspan=2, sticky="e")

        log_card = tk.Frame(self.log_page, bg="#FFFFFF", highlightthickness=1, highlightbackground="#E6EAF1")
        log_card.grid(row=1, column=0, sticky="nsew", padx=18, pady=(0, 18))
        log_card.grid_rowconfigure(0, weight=1)
        log_card.grid_columnconfigure(0, weight=1)
        self.log = scrolledtext.ScrolledText(log_card, font=("Consolas", 10, "bold"), bg="#FBFCFE", fg="#465269",
                                             insertbackground="#465269", relief="flat", borderwidth=0,
                                             padx=12, pady=12, wrap="word")
        self.log.grid(row=0, column=0, sticky="nsew")
        self.log.configure(state="disabled")

        self._show_page("dashboard")

    def _focus_first_scanner(self):
        for router in self.routers:
            card = self.cards.get(router.key)
            if card and router.enabled and router.key not in self.running_slots:
                card.set_scan_enabled(True, focus=True)
                break

    def _show_page(self, page: str):
        target = self.dashboard_page if page == "dashboard" else self.log_page
        target.tkraise()
        for key, button in getattr(self, "nav_buttons", {}).items():
            active = (key == page)
            button.configure(bg="#EDF3FF" if active else "#FFFFFF",
                             fg="#4F70E8" if active else "#78849A",
                             font=("Segoe UI", 9, "bold" if active else "normal"))

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def show_router_log(self, card: SlotCard):
        win = tk.Toplevel(self)
        win.title(f"{card.router.name} | Live Programming Log")
        win.geometry("920x560")
        win.minsize(700, 420)
        win.configure(bg="#F5F7FB")
        head = tk.Frame(win, bg="#FFFFFF", highlightthickness=1, highlightbackground="#E5EAF2")
        head.pack(fill="x", padx=14, pady=(14, 8))
        tk.Label(head, text=f"{card.router.name}  ·  {card.router.ip}", bg="#FFFFFF", fg="#29364B",
                 font=("Segoe UI", 13, "bold")).pack(side="left", padx=14, pady=12)
        tk.Label(head, textvariable=card.status, bg="#EDF3FF", fg="#4F70E8",
                 font=("Segoe UI", 9, "bold"), padx=12, pady=6).pack(side="right", padx=14, pady=10)
        viewer = scrolledtext.ScrolledText(win, font=("Consolas", 9), bg="#FFFFFF", fg="#465269",
                                           relief="flat", padx=12, pady=12, wrap="word")
        viewer.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        viewer.insert("1.0", "\n".join(card.log_history) or "No log entries for this router yet.")
        viewer.configure(state="disabled")


    def _cfg(self) -> dict[str, str]:
        return parse_key_value_file(CONFIG_PATH)

    def has_pending(self, slot_key: str) -> bool:
        return slot_key in load_pending_jobs()

    def _append(self, slot_key: str, text: str):
        ts = time.strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"[{ts}] [{slot_key}] {text}\n")
        self.log.see("end")
        self.log.configure(state="disabled")
        card = self.cards.get(slot_key)
        if card:
            card.add_log(text)

    def _set_stats(self, stats: dict):
        self.available_var.set(str(stats.get("available", "-")))
        self.reserved_var.set(str(stats.get("reserved", "-")))
        self.pass_var.set(str(stats.get("pass", "-")))
        fail = stats.get("fail", 0)
        err = stats.get("error", 0)
        self.fail_var.set(str(fail + err) if isinstance(fail, int) and isinstance(err, int) else "-")
        self.total_var.set(str(stats.get("total", "-")))

    def _startup_check(self):
        pending = load_pending_jobs()
        for router in self.routers:
            card = self.cards.get(router.key)
            if card is None or not router.enabled:
                continue
            job = pending.get(router.key)
            if job:
                mac = job.get("mac", "RESERVATION PENDING")
                card.set_state("RECOVERY", "Interrupted job must be resolved before this slot can continue", mac,
                               job.get("serial_number", "—"), job.get("gpon_number", "—"))
                if job.get("reservation_id"):
                    card.set_step("connect", "✓")
                    card.set_step("reserve", "✓")
                card.button.configure(text="RESOLVE PENDING")
                self._append(router.key, f"Pending job detected: {mac}")
        self._check_server_async()

    def _check_server_async(self):
        def worker():
            try:
                c = ServerClient(self._cfg())
                h = c.health()
                s = c.stats()
                self.events.put(("server", (True, c.base_url, s, h)))
            except Exception as exc:
                self.events.put(("server", (False, str(exc), {}, {})))
        threading.Thread(target=worker, daemon=True).start()

    def _poll_server(self):
        self._check_server_async()
        self.after(4000, self._poll_server)

    def handle_label_scan(self, router: RouterTarget):
        card = self.cards.get(router.key)
        if card is None or not router.enabled or router.key in self.running_slots:
            return
        if self.has_pending(router.key):
            self.resolve_pending(router)
            return
        raw = card.scan_value.get().strip()
        if not raw:
            messagebox.showerror("Scan required", f"{router.name}: scan MAC, Serial Number or GPON Serial Number.")
            card.set_scan_enabled(True, focus=True)
            return
        if not self.server_online:
            messagebox.showerror("Server offline", "Central identity server is offline. Label cannot be validated.")
            return
        try:
            cfg = self._cfg()
        except Exception as exc:
            messagebox.showerror("Configuration error", str(exc)); return
        card.scanned_identity = None
        card.reset_steps()
        card.set_step("scan", "…")
        card.set_state("LOOKUP", "Checking scanned label against central identity list", "LOOKUP...", "—", "—", "—")
        card.button.configure(state="disabled", text="CHECKING...", bg="#C8CED8")
        card.set_scan_enabled(False)
        self._append(router.key, f"Label scanned: {raw}")
        threading.Thread(target=self._scan_worker, args=(router, raw, cfg), daemon=True).start()

    def _scan_worker(self, router: RouterTarget, raw: str, cfg: dict[str, str]):
        try:
            client = ServerClient(cfg)
            identity = client.lookup_identity(raw)
            if str(identity.get("state", "")).upper() != "AVAILABLE":
                raise AppError(f"Scanned identity is not AVAILABLE (state={identity.get('state')}).")
            require_pcb = str(cfg.get("REQUIRE_PCB_SERIAL_BEFORE_WRITE", "1")).strip().lower() not in {"0", "no", "false", "off"}
            if require_pcb and not str(identity.get("pcb_serial_number", "")).strip():
                raise AppError("NO PCB SERIAL NUMBER PRESENT — Complete Box Build before MAC Write.")
            self.events.put(("scan_identity", (router.key, identity)))

            deadline = time.monotonic() + self.pcb_ready_wait_seconds
            last_error = "PCB not reachable"
            while time.monotonic() < deadline:
                try:
                    check_router_reachable(router)
                    self.events.put(("scan_ready", (router.key, identity)))
                    return
                except Exception as exc:
                    last_error = str(exc)
                    time.sleep(self.pcb_ready_poll_seconds)
            self.events.put(("scan_wait_timeout", (router.key, identity, last_error)))
        except Exception as exc:
            self.events.put(("scan_error", (router.key, str(exc))))

    def start_all(self):
        if self.require_label_scan:
            messagebox.showinfo("Label scan required", "Scan the MAC, Serial Number or GPON Serial Number in each DUT card. Each ready PCB starts automatically after a successful scan.")
            return
        if not self.server_online:
            messagebox.showerror("MAC server offline", "The central identity server is offline. Programming was not started.")
            return
        started = 0
        for router in self.routers:
            if not router.enabled or router.key in self.running_slots or self.has_pending(router.key):
                continue
            if self.start_router(router, quiet=True):
                started += 1
        if started == 0:
            messagebox.showinfo("Nothing started", "No ready router slots are available. Resolve pending slots or enable routers in config.txt.")

    def start_router(self, router: RouterTarget, quiet: bool = False, selected_identity: dict | None = None) -> bool:
        if not router.enabled or router.key in self.running_slots:
            return False
        if self.has_pending(router.key):
            if not quiet:
                self.resolve_pending(router)
            return False
        card = self.cards.get(router.key)
        selected_identity = selected_identity or (card.scanned_identity if card else None)
        if self.require_label_scan and not selected_identity:
            if not quiet:
                messagebox.showerror("Label scan required", f"{router.name}: scan MAC, Serial Number or GPON Serial Number first.")
            return False
        try:
            cfg = self._cfg()
            write_cmds, verify_cmds, finalize_cmds = parse_commands_file(COMMANDS_PATH)
            client = ServerClient(cfg)
        except Exception as exc:
            if not quiet:
                messagebox.showerror("Configuration error", str(exc))
            return False
        if not self.server_online and not quiet:
            messagebox.showerror("MAC server offline", "The central identity server is offline. Programming was not started.")
            return False

        request_id = str(uuid.uuid4())
        set_pending_job(router.key, {
            "slot": router.slot,
            "router_name": router.name,
            "router_ip": router.ip,
            "source_ip": router.source_ip,
            "request_id": request_id,
            "selected_mac": (selected_identity or {}).get("mac", ""),
            "state": "REQUESTING",
            "created_at": time.time(),
        })
        self.running_slots.add(router.key)
        self.active_var.set(str(len(self.running_slots)))
        card = self.cards[router.key]
        card.running = True
        card.set_scan_enabled(False)
        if selected_identity:
            card.set_step("scan", "✓")
            card.set_step("connect", "✓")
            card.set_step("reserve", "…")
            card.set_step("firmware", "—")
            card.set_state("PROGRAMMING", "PCB ready; reserving scanned identity", selected_identity.get("mac", "—"),
                           selected_identity.get("serial_number", "—"), selected_identity.get("gpon_number", "—"),
                           selected_identity.get("pcb_serial_number", "—"))
        else:
            card.reset_steps()
            card.set_step("connect", "…")
            card.set_state("PROGRAMMING", "Checking router connection before identity allocation", "ALLOCATING...", "—", "—")
        card.button.configure(state="disabled", text="PROGRAMMING...", bg="#C8CED8")
        self._append(router.key, f"Cycle started for {router.name} {router.ip} via NIC {router.source_ip}")
        threading.Thread(
            target=self._cycle_worker,
            args=(router, client, request_id, cfg, write_cmds, verify_cmds, finalize_cmds, selected_identity),
            daemon=True,
        ).start()
        return True

    def _cycle_worker(self, router: RouterTarget, client: ServerClient, request_id: str, cfg, write_cmds, verify_cmds, finalize_cmds, selected_identity=None):
        def emit(msg):
            self.events.put(("log", (router.key, msg)))
        reservation_id = None
        mac = None
        serial = ""
        gpon = ""
        firmware_applied = False
        firmware_name = ""
        try:
            emit(f"Pre-checking Telnet connection to {router.ip}:{router.port} via NIC {router.source_ip} ...")
            try:
                check_router_reachable(router)
            except Exception as exc:
                set_pending_job(router.key, None)
                self.events.put(("router_offline", (router.key, str(exc))))
                return
            self.events.put(("preflight_ok", (router.key, selected_identity)))

            # Read the server firmware policy now, but DO NOT flash yet.
            # Firmware is intentionally the final DUT operation because sysupgrade
            # automatically reboots the PCB.  If firmware is ON, the normal
            # [FINALIZE] reboot command is skipped to avoid a reboot before flashing.
            firmware = client.firmware_config()
            firmware_enabled = bool(firmware.get("enabled"))
            if firmware_enabled and not firmware.get("available"):
                raise AppError("Firmware update is ON on the central server, but no firmware file is available.")
            if firmware_enabled:
                emit(f"Firmware update ON: {firmware.get('file_name')} will run AFTER MAC/Serial/GPON verification as the last DUT step.")
            else:
                emit("Firmware update OFF on central server — normal FINALIZE command will be used after identifier verification.")
                self.events.put(("firmware_skipped", (router.key,)))

            require_pcb_serial = str(cfg.get("REQUIRE_PCB_SERIAL_BEFORE_WRITE", "1")).strip().lower() not in {"0", "no", "false", "off"}
            emit(f"PCB serial gate: {'ON' if require_pcb_serial else 'OFF'}")
            emit("Router ready. Checking identity with central server.")
            try:
                if selected_identity:
                    emit(f"Reserving scanned identity {selected_identity.get('mac', '')}.")
                    alloc = client.allocate_specific(router, request_id, selected_identity.get("mac", ""), require_pcb_serial=require_pcb_serial)
                else:
                    alloc = client.allocate(router, request_id, require_pcb_serial=require_pcb_serial)
            except AppError as exc:
                if "NO PCB SERIAL NUMBER PRESENT" in str(exc).upper():
                    set_pending_job(router.key, None)
                    self.events.put((
                        "pcb_missing",
                        (
                            router.key,
                            "NO PCB SERIAL NUMBER PRESENT — Complete Box Build before MAC Write. "
                            "MAC / Serial / GPON were not written.",
                        ),
                    ))
                    return
                raise
            reservation_id = alloc["reservation_id"]
            mac = alloc["mac"]
            serial, gpon, seq = build_identifiers(alloc, cfg)
            alloc.update({"serial_number": serial, "gpon_number": gpon, "seq": seq})
            set_pending_job(router.key, {
                "slot": router.slot,
                "router_name": router.name,
                "router_ip": router.ip,
                "source_ip": router.source_ip,
                "request_id": request_id,
                "reservation_id": reservation_id,
                "mac": mac,
                "serial_number": serial,
                "gpon_number": gpon,
                "seq": seq,
                "state": "RESERVED",
                "created_at": time.time(),
            })
            self.events.put(("allocated", (router.key, alloc)))
            self.events.put(("telnet_start", (router.key,)))

            # When firmware is ON, do not run [FINALIZE] here. sysupgrade is the
            # last board operation and performs the reboot itself.
            effective_finalize_cmds = [] if firmware_enabled else finalize_cmds
            passed, checks = asyncio.run(
                telnet_session(router, mac, serial, gpon, seq, write_cmds, verify_cmds, effective_finalize_cmds, emit)
            )
            self.events.put(("verify_result", (router.key, passed, checks, firmware_enabled)))

            if not passed:
                status = "FAIL"
                detail = f"Identifier verification failed: {checks} | Firmware: not run because identifier verification failed"
            else:
                # FIRMWARE IS THE LAST DUT STEP. The supplied updater uploads over
                # SFTP, validates with sysupgrade -T, runs sysupgrade, waits for the
                # automatic reboot, reconnects over SSH and performs its health check.
                if firmware_enabled:
                    self.events.put(("firmware_start", (router.key, firmware)))
                    emit(f"Starting LAST DUT STEP — firmware update: {firmware.get('file_name')} ({firmware.get('size', 0)} bytes)")
                    local_fw = client.download_firmware(firmware)
                    emit(f"Firmware downloaded from server and SHA-256 verified: {local_fw.name}")
                    emit(f"Using direct SSH/SFTP updater via dedicated PC NIC {router.source_ip}.")
                    fw_result = run_direct_firmware_update(router, local_fw, cfg, emit)
                    after_text = json.dumps(fw_result.get("after", {}), ensure_ascii=False, sort_keys=True)
                    if after_text:
                        emit(f"Post-upgrade board info: {after_text[:500]}")
                    firmware_applied = True
                    firmware_name = str(firmware.get("file_name") or "")
                    checks["FIRMWARE"] = True
                    self.events.put(("firmware_done", (router.key, firmware)))
                    detail = f"MAC, serial and GPON verified successfully | Firmware: {firmware_name} applied as final DUT step"
                else:
                    detail = "MAC, serial and GPON verified successfully | Firmware: skipped (server OFF); normal FINALIZE command used"
                status = "PASS"

            # Server reporting is bookkeeping only; no more DUT command is executed
            # after firmware when firmware is enabled.
            report = client.report(router, reservation_id, status, detail, serial, gpon)
            set_pending_job(router.key, None)
            self.events.put(("done", (router.key, mac, serial, gpon, status, report)))
        except Exception as exc:
            error_text = str(exc)
            if reservation_id:
                try:
                    # With firmware-last sequencing, a firmware failure occurs after
                    # the identity has already been written. Keep that identity blocked
                    # and report ERROR rather than allowing it to be reused.
                    report = client.report(router, reservation_id, "ERROR", error_text, serial, gpon)
                    set_pending_job(router.key, None)
                    self.events.put(("done", (router.key, mac or "UNKNOWN", serial, gpon, "ERROR", report)))
                    self.events.put(("log", (router.key, "ERROR: " + error_text)))
                    return
                except Exception as report_exc:
                    self.events.put(("log", (router.key, f"Could not report ERROR to server: {report_exc}")))
            self.events.put(("cycle_error", (router.key, mac, error_text)))

    def resolve_pending(self, router: RouterTarget):
        pending = load_pending_jobs().get(router.key)
        if not pending:
            self._reset_slot(router.key)
            return
        if router.key in self.running_slots:
            return
        if not pending.get("reservation_id"):
            # A crash before identity reservation has not consumed any identity row.
            set_pending_job(router.key, None)
            messagebox.showinfo(
                "Cycle reset",
                "The interrupted cycle had not reserved an identity. Scan the label again.",
            )
            self._reset_slot(router.key)
            return
        answer = messagebox.askyesnocancel(
            f"Interrupted job - {router.name}",
            f"Router: {router.name} ({router.ip})\n"
            f"MAC: {pending.get('mac', 'not confirmed')}\n"
            f"Serial: {pending.get('serial_number', 'not confirmed')}\n"
            f"GPON: {pending.get('gpon_number', 'not confirmed')}\n\n"
            "YES = reserve/retrieve the same request and program it now\n"
            "NO = reserve/retrieve it, mark ERROR, and move on\n"
            "CANCEL = leave it unresolved",
        )
        if answer is None:
            return
        try:
            cfg = self._cfg()
            client = ServerClient(cfg)
            if not pending.get("request_id"):
                raise AppError("Pending job has no request_id")
            if not pending.get("reservation_id") or not pending.get("mac"):
                raise AppError("Pending job has no reserved identity")
        except Exception as exc:
            messagebox.showerror("Recovery failed", str(exc))
            return

        if answer is False:
            try:
                report = client.report(router, pending["reservation_id"], "ERROR", "Operator discarded interrupted cycle", pending.get("serial_number", ""), pending.get("gpon_number", ""))
                set_pending_job(router.key, None)
                self._set_stats(report.get("stats", {}))
                card = self.cards[router.key]
                card.set_state("READY", "Interrupted identifiers blocked as ERROR. Ready for new PCB.", "—", "—", "—")
                card.reset_steps()
                card.button.configure(text="PROGRAM PCB", state="normal")
            except Exception as exc:
                messagebox.showerror("Could not update server", str(exc))
            return

        try:
            write_cmds, verify_cmds, finalize_cmds = parse_commands_file(COMMANDS_PATH)
        except Exception as exc:
            messagebox.showerror("Command file error", str(exc))
            return
        self.running_slots.add(router.key)
        self.active_var.set(str(len(self.running_slots)))
        card = self.cards[router.key]
        card.running = True
        card.set_step("connect", "✓")
        card.set_step("reserve", "✓")
        card.set_step("write", "…")
        card.set_state("PROGRAMMING", "Resuming interrupted reservation", pending["mac"], pending.get("serial_number", "—"), pending.get("gpon_number", "—"))
        card.button.configure(state="disabled", text="RECOVERING...")
        threading.Thread(
            target=self._resume_worker,
            args=(router, client, pending, cfg, write_cmds, verify_cmds, finalize_cmds),
            daemon=True,
        ).start()

    def _resume_worker(self, router, client, pending, cfg, write_cmds, verify_cmds, finalize_cmds):
        def emit(msg):
            self.events.put(("log", (router.key, msg)))
        mac = pending["mac"]
        serial = pending.get("serial_number", "")
        gpon = pending.get("gpon_number", "")
        seq = int(pending.get("seq", 0))
        reservation_id = pending["reservation_id"]
        try:
            firmware = client.firmware_config()
            firmware_enabled = bool(firmware.get("enabled"))
            if firmware_enabled and not firmware.get("available"):
                raise AppError("Firmware update is ON on the central server, but no firmware file is available.")

            effective_finalize_cmds = [] if firmware_enabled else finalize_cmds
            passed, checks = asyncio.run(
                telnet_session(router, mac, serial, gpon, seq, write_cmds, verify_cmds, effective_finalize_cmds, emit)
            )
            self.events.put(("verify_result", (router.key, passed, checks, firmware_enabled)))

            if passed and firmware_enabled:
                self.events.put(("firmware_start", (router.key, firmware)))
                local_fw = client.download_firmware(firmware)
                emit(f"Recovery cycle: firmware is the FINAL DUT STEP: {local_fw.name}")
                run_direct_firmware_update(router, local_fw, cfg, emit)
                checks["FIRMWARE"] = True
                self.events.put(("firmware_done", (router.key, firmware)))
                detail = f"Recovered interrupted cycle | Firmware: {firmware.get('file_name', '')} applied as final DUT step"
            elif passed:
                detail = "Recovered interrupted cycle | Firmware skipped (server OFF); normal FINALIZE command used"
            else:
                detail = f"Recovered interrupted cycle | Identifier verification failed: {checks}"

            status = "PASS" if passed else "FAIL"
            report = client.report(router, reservation_id, status, detail, serial, gpon)
            set_pending_job(router.key, None)
            self.events.put(("done", (router.key, mac, serial, gpon, status, report)))
        except Exception as exc:
            try:
                report = client.report(router, reservation_id, "ERROR", str(exc), serial, gpon)
                set_pending_job(router.key, None)
                self.events.put(("done", (router.key, mac, serial, gpon, "ERROR", report)))
                self.events.put(("log", (router.key, "ERROR: " + str(exc))))
            except Exception:
                self.events.put(("cycle_error", (router.key, mac, str(exc))))

    def _reset_slot(self, slot_key: str):
        card = self.cards.get(slot_key)
        if not card:
            return
        card.running = False
        card.scanned_identity = None
        card.scan_value.set("")
        card.set_scan_enabled(True, focus=self.require_label_scan)
        if self.require_label_scan:
            card.button.configure(state="disabled", text="SCAN LABEL FIRST", bg="#C8CED8")
            if card.status.get() not in {"PASS", "FAIL", "ERROR"}:
                card.set_state("WAIT SCAN", "Scan MAC, Serial Number or GPON Serial Number", "—", "—", "—", "—")
        else:
            card.button.configure(state="normal", text="PROGRAM PCB", bg="#4F70E8")
            if card.status.get() not in {"PASS", "FAIL", "ERROR"}:
                card.set_state("READY", "Waiting for PCB", "—")

    def _finish_slot(self, slot_key: str):
        self.running_slots.discard(slot_key)
        self.active_var.set(str(len(self.running_slots)))
        card = self.cards.get(slot_key)
        if card:
            card.running = False
            if self.has_pending(slot_key):
                card.button.configure(state="normal", text="RESOLVE PENDING")
            else:
                card.scanned_identity = None
                card.scan_value.set("")
                card.set_scan_enabled(True, focus=self.require_label_scan)
                if self.require_label_scan:
                    card.button.configure(state="disabled", text="SCAN NEXT LABEL", bg="#C8CED8")
                else:
                    card.button.configure(state="normal", text="PROGRAM NEXT PCB", bg="#4F70E8")
        self._check_server_async()

    def _drain_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    slot_key, text = payload
                    self._append(slot_key, text)
                elif kind == "server":
                    ok, info, stats, _ = payload
                    self.server_online = ok
                    self.server_var.set("ONLINE" if ok else "OFFLINE")
                    self.header_server.configure(fg="#159260" if ok else "#C43C4A")
                    if ok:
                        self._set_stats(stats)
                    else:
                        self._append("SERVER", info)
                elif kind == "firmware_start":
                    slot_key, firmware = payload
                    card = self.cards[slot_key]
                    card.set_step("firmware", "…")
                    card.set_step("report", "—")
                    card.set_state("FW UPDATE", f"FINAL DUT STEP — updating firmware: {firmware.get('file_name', '')}")
                    card.button.configure(state="disabled", text="FIRMWARE...", bg="#C8CED8")
                elif kind == "firmware_done":
                    slot_key, firmware = payload
                    card = self.cards[slot_key]
                    card.set_step("firmware", "✓")
                    card.set_step("report", "…")
                    card.set_state("PROGRAMMING", "Firmware complete and PCB reboot verified. Reporting PASS to server.")
                elif kind == "firmware_skipped":
                    slot_key, = payload
                    card = self.cards[slot_key]
                    card.set_step("firmware", "—")
                elif kind == "scan_identity":
                    slot_key, identity = payload
                    card = self.cards[slot_key]
                    card.scanned_identity = identity
                    card.set_step("scan", "✓")
                    card.set_step("connect", "…")
                    matched = "/".join(identity.get("matched_by") or ["LABEL"])
                    card.set_state("WAIT PCB", f"{matched} matched. Identity loaded; waiting for PCB/Telnet to become ready.",
                                   identity.get("mac", "—"), identity.get("serial_number", "—"),
                                   identity.get("gpon_number", "—"), identity.get("pcb_serial_number", "—"))
                    self._append(slot_key, f"Identity found: MAC={identity.get('mac')} SERIAL={identity.get('serial_number')} GPON={identity.get('gpon_number')} PCB={identity.get('pcb_serial_number')}")
                elif kind == "scan_ready":
                    slot_key, identity = payload
                    card = self.cards[slot_key]
                    card.scanned_identity = identity
                    card.set_step("connect", "✓")
                    card.set_state("READY", "PCB READY — label validated and Telnet is reachable. Starting automatically.",
                                   identity.get("mac", "—"), identity.get("serial_number", "—"),
                                   identity.get("gpon_number", "—"), identity.get("pcb_serial_number", "—"))
                    card.button.configure(state="disabled" if self.auto_start_after_scan else "normal",
                                          text="AUTO START..." if self.auto_start_after_scan else "PROGRAM PCB",
                                          bg="#159260" if not self.auto_start_after_scan else "#C8CED8")
                    self._append(slot_key, "PCB READY: green ready state reached.")
                    if self.auto_start_after_scan:
                        self.after(150, lambda r=card.router, ident=dict(identity): self.start_router(r, selected_identity=ident))
                elif kind == "scan_wait_timeout":
                    slot_key, identity, text = payload
                    card = self.cards[slot_key]
                    card.scanned_identity = identity
                    card.set_step("connect", "✕")
                    card.set_state("WAIT PCB", "Label is valid, but PCB/Telnet did not become ready before timeout. Check cable/power, then scan again.",
                                   identity.get("mac", "—"), identity.get("serial_number", "—"),
                                   identity.get("gpon_number", "—"), identity.get("pcb_serial_number", "—"))
                    card.scan_value.set("")
                    card.set_scan_enabled(True, focus=True)
                    card.button.configure(state="disabled", text="SCAN AGAIN", bg="#C8CED8")
                    self._append(slot_key, text)
                elif kind == "scan_error":
                    slot_key, text = payload
                    card = self.cards[slot_key]
                    card.scanned_identity = None
                    card.set_step("scan", "✕")
                    card.set_state("BLOCKED", text, "—", "—", "—", "—")
                    card.scan_value.set("")
                    card.set_scan_enabled(True, focus=True)
                    card.button.configure(state="disabled", text="SCAN AGAIN", bg="#C8CED8")
                    self._append(slot_key, "SCAN ERROR: " + text)
                    messagebox.showerror("Label scan rejected", f"{card.router.name}\n\n{text}")
                elif kind == "preflight_ok":
                    slot_key, selected_identity = payload
                    card = self.cards[slot_key]
                    card.set_step("connect", "✓")
                    card.set_step("reserve", "…")
                    card.set_step("firmware", "—")
                    if selected_identity:
                        card.set_state("PROGRAMMING", "Router online; reserving scanned identity",
                                       selected_identity.get("mac", "—"), selected_identity.get("serial_number", "—"),
                                       selected_identity.get("gpon_number", "—"), selected_identity.get("pcb_serial_number", "—"))
                    else:
                        card.set_state("PROGRAMMING", "Router online; allocating identity", "ALLOCATING...", "—", "—")
                elif kind == "allocated":
                    slot_key, alloc = payload
                    card = self.cards[slot_key]
                    card.set_step("reserve", "✓")
                    card.set_step("write", "…")
                    card.set_state("PROGRAMMING", "Identity reserved; clearing old identity then writing MAC, serial and GPON over Telnet",
                                   alloc["mac"], alloc.get("serial_number", "—"), alloc.get("gpon_number", "—"), alloc.get("pcb_serial_number", "—"))
                    self._set_stats(alloc.get("stats", {}))
                    self._append(slot_key, f"Reserved MAC={alloc['mac']} SERIAL={alloc.get('serial_number','')} GPON={alloc.get('gpon_number','')} reservation={alloc['reservation_id']}")
                elif kind == "telnet_start":
                    slot_key, = payload
                    self.cards[slot_key].set_step("write", "…")
                elif kind == "verify_result":
                    slot_key, passed, checks, firmware_enabled = payload
                    card = self.cards[slot_key]
                    card.set_step("write", "✓")
                    card.set_step("verify", "✓" if passed else "✕")
                    if passed and firmware_enabled:
                        card.set_step("firmware", "…")
                        card.set_state("PROGRAMMING", "Identity verified; starting firmware as FINAL DUT STEP", card.mac.get())
                    else:
                        card.set_step("report", "…")
                        card.set_state("PROGRAMMING", "Verification complete; reporting result to central server", card.mac.get())
                elif kind == "done":
                    slot_key, mac, serial, gpon, status, report = payload
                    card = self.cards[slot_key]
                    card.set_step("report", "✓")
                    if report.get("stats"):
                        self._set_stats(report["stats"])
                    if status == "PASS":
                        card.set_step("connect", "✓"); card.set_step("reserve", "✓"); card.set_step("write", "✓"); card.set_step("verify", "✓")
                        card.set_state("PASS", "Cycle complete. If firmware was enabled, it was written last and the PCB reboot was verified. Replace PCB when ready.", mac, serial, gpon)
                    elif status == "FAIL":
                        card.set_step("verify", "✕")
                        card.set_state("FAIL", "Identifier verification failed. This MAC remains blocked.", mac, serial, gpon)
                    else:
                        if card.step_values.get("verify") == "—":
                            card.set_step("write", "✕")
                        card.set_state("ERROR", "Programming error. This identity remains blocked.", mac, serial, gpon)
                    self._append(slot_key, f"CYCLE {status}: {mac}")
                    self._finish_slot(slot_key)
                elif kind == "pcb_missing":
                    slot_key, text = payload
                    card = self.cards[slot_key]
                    card.set_step("reserve", "✕")
                    card.set_state("BLOCKED", "NO PCB SERIAL NUMBER PRESENT — Complete Box Build first.", "—", "—", "—", "—")
                    card.scan_value.set("")
                    card.scanned_identity = None
                    card.set_scan_enabled(True, focus=True)
                    self._append(slot_key, text)
                    self._finish_slot(slot_key)
                    messagebox.showerror("PCB serial missing", f"{card.router.name}\n\n{text}")
                elif kind == "router_offline":
                    slot_key, text = payload
                    card = self.cards[slot_key]
                    card.set_step("connect", "✕")
                    card.set_state("OFFLINE", "Router unreachable; no identity allocated.", "—", "—", "—")
                    self._append(slot_key, text)
                    self._finish_slot(slot_key)
                elif kind == "cycle_error":
                    slot_key, mac, text = payload
                    card = self.cards[slot_key]
                    if card.step_values.get("report") == "…":
                        card.set_step("report", "✕")
                    elif card.step_values.get("write") == "…":
                        card.set_step("write", "✕")
                    card.set_state("RECOVERY", "Server reservation may be unresolved. Use Resolve Pending.", mac or "UNKNOWN")
                    self._append(slot_key, "ERROR: " + text)
                    self._finish_slot(slot_key)
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _open_path(self, path: Path, title: str):
        try:
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            messagebox.showerror(title, str(exc))

    def _open_config(self):
        self._open_path(CONFIG_PATH, "Open config")

    def _open_commands(self):
        self._open_path(COMMANDS_PATH, "Open commands")


if __name__ == "__main__":
    ClientApp().mainloop()
