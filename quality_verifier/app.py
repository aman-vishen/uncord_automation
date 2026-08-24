import asyncio
import configparser
import json
import os
import queue
import re
import socket
import subprocess
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

try:
    import cv2  # type: ignore
except ImportError:
    cv2 = None

try:
    import zxingcpp  # type: ignore
except ImportError:
    zxingcpp = None

APP_VERSION = "5.1-ETE-UNCORD-RESPONSIVE"
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.ini"
PENDING_PATH = BASE_DIR / "pending_verifications.json"
MAX_ROUTERS = 8
PENDING_LOCK = threading.RLock()

BRAND_DARK = "#185890"
BRAND_BLUE = "#2898D0"
BRAND_DEEP = "#12466F"
BRAND_BG = "#F2F8FC"
BRAND_CARD = "#FFFFFF"
BRAND_TEXT = "#17324A"
BRAND_MUTED = "#5C7387"
PASS_GREEN = "#198754"
FAIL_RED = "#C33A3A"
WARN_AMBER = "#C07A00"


class AppError(Exception):
    pass


@dataclass
class RouterTarget:
    slot: int
    name: str
    ip: str
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
    exit_command: str

    @property
    def key(self) -> str:
        return f"R{self.slot}"


@dataclass
class VerifyConfig:
    parser: configparser.ConfigParser
    routers: list[RouterTarget]
    server_url: str
    server_api_key: str
    station_id: str
    server_timeout: float
    serial_scan_enabled: bool
    serial_scan_required: bool
    serial_scan_extract_regex: str
    serial_scan_group: str
    serial_scan_case_sensitive: bool
    serial_scan_camera_index: int
    serial_scan_camera_timeout: float
    serial_scan_confirm_frames: int
    serial_scan_auto_start: bool
    serial_scan_duplicate_block: bool
    read_mac_commands: list[str]
    read_mac_regex: str
    read_mac_group: str
    read_serial_commands: list[str]
    read_serial_regex: str
    read_serial_group: str
    read_gpon_commands: list[str]
    read_gpon_regex: str
    read_gpon_group: str
    read_part_commands: list[str]
    read_part_static: str
    read_part_regex: str
    read_part_group: str
    serial_valid_regex: str
    gpon_valid_regex: str
    part_valid_regex: str
    expected_part_numbers: list[str]
    expected_part_regex: str
    wifi_calibration_commands: list[str]
    wifi_calibration_pass_regex: str
    wifi_calibration_fail_regex: str
    bob_calibration_commands: list[str]
    bob_calibration_pass_regex: str
    bob_calibration_fail_regex: str
    firmware_commands: list[str]
    firmware_regex: str
    firmware_group: str
    expected_firmware_versions: list[str]
    expected_firmware_regex: str
    led_start_commands: list[str]
    led_stop_commands: list[str]
    led_instruction: str
    reset_prepare_commands: list[str]
    reset_check_commands: list[str]
    reset_pass_regex: str
    reset_timeout: float
    reset_poll_interval: float
    reset_instruction: str
    reset_disconnect_is_pass: bool
    reset_passive_monitor: bool
    wps_prepare_commands: list[str]
    wps_check_commands: list[str]
    wps_pass_regex: str
    wps_timeout: float
    wps_poll_interval: float
    wps_instruction: str
    wps_disconnect_is_pass: bool
    wps_passive_monitor: bool
    user_mode_commands: list[str]
    user_mode_pass_regex: str
    user_mode_allow_disconnect: bool
    user_mode_reconnect_delay: float
    user_mode_verify_timeout: float
    user_mode_verify_commands: list[str]
    user_mode_verify_pass_regex: str


def is_true(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_ini(path: Path) -> configparser.ConfigParser:
    if not path.exists():
        raise AppError(f"Config file not found: {path}")
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.optionxform = str
    try:
        with path.open("r", encoding="utf-8-sig") as f:
            cfg.read_file(f)
    except configparser.Error as exc:
        raise AppError(f"Invalid config.ini: {exc}") from exc
    return cfg


def sec_get(cfg: configparser.ConfigParser, section: str, key: str, default: str = "") -> str:
    if not cfg.has_section(section):
        return default
    for actual_key, value in cfg.items(section):
        if actual_key.upper() == key.upper():
            return value.strip()
    return default


def command_list(cfg: configparser.ConfigParser, section: str, prefix: str = "COMMAND") -> list[str]:
    if not cfg.has_section(section):
        return []
    found: list[tuple[int, str]] = []
    prefix_u = prefix.upper() + "_"
    for key, value in cfg.items(section):
        key_u = key.upper()
        if key_u.startswith(prefix_u):
            suffix = key_u[len(prefix_u):]
            try:
                order = int(suffix)
            except ValueError:
                continue
            if value.strip():
                found.append((order, value.strip()))
    return [value for _, value in sorted(found)]


def load_config(path: Path) -> VerifyConfig:
    cfg = load_ini(path)
    count = int(sec_get(cfg, "ROUTERS", "COUNT", "8"))
    if not 1 <= count <= MAX_ROUTERS:
        raise AppError(f"[ROUTERS] COUNT must be between 1 and {MAX_ROUTERS}")

    def telnet_value(slot_section: str, key: str, default: str) -> str:
        value = sec_get(cfg, slot_section, key, "")
        return value if value != "" else sec_get(cfg, "TELNET", key, default)

    routers: list[RouterTarget] = []
    seen_ips: set[str] = set()
    for slot in range(1, count + 1):
        section = f"ROUTER_{slot}"
        enabled = is_true(sec_get(cfg, section, "ENABLED", "1"), True)
        name = sec_get(cfg, section, "NAME", f"Router {slot}") or f"Router {slot}"
        ip = sec_get(cfg, section, "IP", "")
        if enabled:
            if not ip:
                raise AppError(f"[{section}] IP is required")
            try:
                socket.inet_aton(ip)
            except OSError as exc:
                raise AppError(f"[{section}] invalid IPv4 address: {ip}") from exc
            if ip in seen_ips:
                raise AppError(f"Duplicate router IP in config: {ip}")
            seen_ips.add(ip)
        try:
            router = RouterTarget(
                slot=slot, name=name, ip=ip, enabled=enabled,
                port=int(telnet_value(section, "PORT", "23")),
                username=telnet_value(section, "USERNAME", "admin"),
                password=telnet_value(section, "PASSWORD", "admin"),
                login_prompt=telnet_value(section, "LOGIN_PROMPT", "login:"),
                password_prompt=telnet_value(section, "PASSWORD_PROMPT", "Password:"),
                command_prompt=telnet_value(section, "COMMAND_PROMPT", "#"),
                timeout=float(telnet_value(section, "TIMEOUT", "10")),
                command_delay=float(telnet_value(section, "COMMAND_DELAY", "0.3")),
                preflight_timeout=float(telnet_value(section, "PREFLIGHT_TIMEOUT", "1.5")),
                exit_command=telnet_value(section, "EXIT_COMMAND", "exit"),
            )
        except ValueError as exc:
            raise AppError(f"Invalid numeric Telnet value in [{section}] or [TELNET]") from exc
        routers.append(router)

    read_part_static = sec_get(cfg, "READ_PART_NUMBER", "STATIC_VALUE", "")
    reset_passive_monitor = is_true(sec_get(cfg, "RESET_TEST", "PASSIVE_MONITOR", "0"))
    wps_passive_monitor = is_true(sec_get(cfg, "WPS_TEST", "PASSIVE_MONITOR", "0"))
    required = {
        "READ_MAC": command_list(cfg, "READ_MAC"),
        "READ_SERIAL": command_list(cfg, "READ_SERIAL"),
        "READ_GPON_NUMBER": command_list(cfg, "READ_GPON_NUMBER"),
        "READ_PART_NUMBER": command_list(cfg, "READ_PART_NUMBER"),
        "WIFI_CALIBRATION": command_list(cfg, "WIFI_CALIBRATION"),
        "BOB_CALIBRATION": command_list(cfg, "BOB_CALIBRATION"),
        "FIRMWARE_VERSION": command_list(cfg, "FIRMWARE_VERSION"),
        "LED_TEST START": command_list(cfg, "LED_TEST", "START_COMMAND"),
        "RESET_TEST CHECK": command_list(cfg, "RESET_TEST", "CHECK_COMMAND"),
        "WPS_TEST CHECK": command_list(cfg, "WPS_TEST", "CHECK_COMMAND"),
        "USER_MODE": command_list(cfg, "USER_MODE"),
    }
    missing = []
    for name in ("READ_MAC", "READ_SERIAL", "READ_GPON_NUMBER", "WIFI_CALIBRATION", "BOB_CALIBRATION", "FIRMWARE_VERSION", "LED_TEST START", "USER_MODE"):
        if not required[name]:
            missing.append(name)
    if not required["READ_PART_NUMBER"] and not read_part_static:
        missing.append("READ_PART_NUMBER command or STATIC_VALUE")
    if not reset_passive_monitor and not required["RESET_TEST CHECK"]:
        missing.append("RESET_TEST CHECK_COMMAND or PASSIVE_MONITOR=1")
    if not wps_passive_monitor and not required["WPS_TEST CHECK"]:
        missing.append("WPS_TEST CHECK_COMMAND or PASSIVE_MONITOR=1")
    if missing:
        raise AppError("Missing commands in config sections: " + ", ".join(missing))

    expected = [x.strip() for x in sec_get(cfg, "VALIDATION", "EXPECTED_PART_NUMBERS", "").split("|") if x.strip()]
    expected_fw = [x.strip() for x in sec_get(cfg, "FIRMWARE_VERSION", "EXPECTED_VERSIONS", "").split("|") if x.strip()]
    return VerifyConfig(
        parser=cfg, routers=routers,
        server_url=sec_get(cfg, "SERVER", "URL", "http://127.0.0.1:8765").rstrip("/"),
        server_api_key=sec_get(cfg, "SERVER", "API_KEY", ""),
        station_id=sec_get(cfg, "SERVER", "STATION_ID", socket.gethostname()) or socket.gethostname(),
        server_timeout=float(sec_get(cfg, "SERVER", "TIMEOUT", "5")),
        serial_scan_enabled=is_true(sec_get(cfg, "SERIAL_SCAN", "ENABLED", "1"), True),
        serial_scan_required=is_true(sec_get(cfg, "SERIAL_SCAN", "REQUIRED", "1"), True),
        serial_scan_extract_regex=sec_get(cfg, "SERIAL_SCAN", "EXTRACT_REGEX", r"^\s*([A-Za-z0-9._/-]{4,64})\s*$"),
        serial_scan_group=sec_get(cfg, "SERIAL_SCAN", "GROUP", "1"),
        serial_scan_case_sensitive=is_true(sec_get(cfg, "SERIAL_SCAN", "CASE_SENSITIVE", "0")),
        serial_scan_camera_index=int(sec_get(cfg, "SERIAL_SCAN", "CAMERA_INDEX", "0")),
        serial_scan_camera_timeout=float(sec_get(cfg, "SERIAL_SCAN", "CAMERA_TIMEOUT", "30")),
        serial_scan_confirm_frames=max(1, int(sec_get(cfg, "SERIAL_SCAN", "CONFIRM_FRAMES", "2"))),
        serial_scan_auto_start=is_true(sec_get(cfg, "SERIAL_SCAN", "AUTO_START_AFTER_SCAN", "0")),
        serial_scan_duplicate_block=is_true(sec_get(cfg, "SERIAL_SCAN", "BLOCK_DUPLICATE_SCANS", "1"), True),
        read_mac_commands=required["READ_MAC"], read_mac_regex=sec_get(cfg, "READ_MAC", "REGEX", r"(?i)([0-9A-F]{2}(?::[0-9A-F]{2}){5})"), read_mac_group=sec_get(cfg, "READ_MAC", "GROUP", "1"),
        read_serial_commands=required["READ_SERIAL"], read_serial_regex=sec_get(cfg, "READ_SERIAL", "REGEX", r"(?i)serial\s*[:=]\s*(\S+)"), read_serial_group=sec_get(cfg, "READ_SERIAL", "GROUP", "1"),
        read_gpon_commands=required["READ_GPON_NUMBER"], read_gpon_regex=sec_get(cfg, "READ_GPON_NUMBER", "REGEX", r"(?i)gpon(?:\s*(?:serial|number))?\s*[:=]\s*(\S+)"), read_gpon_group=sec_get(cfg, "READ_GPON_NUMBER", "GROUP", "1"),
        read_part_commands=required["READ_PART_NUMBER"], read_part_static=read_part_static, read_part_regex=sec_get(cfg, "READ_PART_NUMBER", "REGEX", r"(?i)part(?:\s*number)?\s*[:=]\s*(\S+)"), read_part_group=sec_get(cfg, "READ_PART_NUMBER", "GROUP", "1"),
        serial_valid_regex=sec_get(cfg, "VALIDATION", "SERIAL_VALID_REGEX", r"^[A-Za-z0-9._/-]{4,64}$"),
        gpon_valid_regex=sec_get(cfg, "VALIDATION", "GPON_VALID_REGEX", r"^[A-Za-z0-9._/-]{4,64}$"),
        part_valid_regex=sec_get(cfg, "VALIDATION", "PART_NUMBER_VALID_REGEX", r"^[A-Za-z0-9._/-]{2,64}$"),
        expected_part_numbers=expected, expected_part_regex=sec_get(cfg, "VALIDATION", "EXPECTED_PART_NUMBER_REGEX", ""),
        wifi_calibration_commands=required["WIFI_CALIBRATION"], wifi_calibration_pass_regex=sec_get(cfg, "WIFI_CALIBRATION", "PASS_REGEX", r"(?i)(pass|valid|calibrated|status\s*[:=]\s*1)"), wifi_calibration_fail_regex=sec_get(cfg, "WIFI_CALIBRATION", "FAIL_REGEX", r"(?i)(fail|invalid|missing|not\s+calibrated)"),
        bob_calibration_commands=required["BOB_CALIBRATION"], bob_calibration_pass_regex=sec_get(cfg, "BOB_CALIBRATION", "PASS_REGEX", r"(?i)(pass|valid|calibrated|status\s*[:=]\s*1)"), bob_calibration_fail_regex=sec_get(cfg, "BOB_CALIBRATION", "FAIL_REGEX", r"(?i)(fail|invalid|missing|not\s+calibrated)"),
        firmware_commands=required["FIRMWARE_VERSION"], firmware_regex=sec_get(cfg, "FIRMWARE_VERSION", "REGEX", r"(?i)(?:firmware|version|fw)\s*[:=]\s*([^\s#]+)"), firmware_group=sec_get(cfg, "FIRMWARE_VERSION", "GROUP", "1"), expected_firmware_versions=expected_fw, expected_firmware_regex=sec_get(cfg, "FIRMWARE_VERSION", "EXPECTED_REGEX", ""),
        led_start_commands=required["LED_TEST START"], led_stop_commands=command_list(cfg, "LED_TEST", "STOP_COMMAND"), led_instruction=sec_get(cfg, "LED_TEST", "INSTRUCTION", "Confirm all required LEDs are working correctly."),
        reset_prepare_commands=command_list(cfg, "RESET_TEST", "PREPARE_COMMAND"), reset_check_commands=required["RESET_TEST CHECK"], reset_pass_regex=sec_get(cfg, "RESET_TEST", "PASS_REGEX", r"(?i)(pressed|pass|detected|\b1\b)"), reset_timeout=float(sec_get(cfg, "RESET_TEST", "TIMEOUT", "15")), reset_poll_interval=float(sec_get(cfg, "RESET_TEST", "POLL_INTERVAL", "1")), reset_instruction=sec_get(cfg, "RESET_TEST", "INSTRUCTION", "Press and release the RESET button now."), reset_disconnect_is_pass=is_true(sec_get(cfg, "RESET_TEST", "DISCONNECT_IS_PASS", "0")), reset_passive_monitor=reset_passive_monitor,
        wps_prepare_commands=command_list(cfg, "WPS_TEST", "PREPARE_COMMAND"), wps_check_commands=required["WPS_TEST CHECK"], wps_pass_regex=sec_get(cfg, "WPS_TEST", "PASS_REGEX", r"(?i)(pressed|pass|detected|\b1\b)"), wps_timeout=float(sec_get(cfg, "WPS_TEST", "TIMEOUT", "15")), wps_poll_interval=float(sec_get(cfg, "WPS_TEST", "POLL_INTERVAL", "1")), wps_instruction=sec_get(cfg, "WPS_TEST", "INSTRUCTION", "Press and release the WPS button now."), wps_disconnect_is_pass=is_true(sec_get(cfg, "WPS_TEST", "DISCONNECT_IS_PASS", "0")), wps_passive_monitor=wps_passive_monitor,
        user_mode_commands=required["USER_MODE"], user_mode_pass_regex=sec_get(cfg, "USER_MODE", "PASS_REGEX", ""), user_mode_allow_disconnect=is_true(sec_get(cfg, "USER_MODE", "ALLOW_DISCONNECT", "1"), True), user_mode_reconnect_delay=float(sec_get(cfg, "USER_MODE", "RECONNECT_DELAY", "0")), user_mode_verify_timeout=float(sec_get(cfg, "USER_MODE", "VERIFY_TIMEOUT", "90")), user_mode_verify_commands=command_list(cfg, "USER_MODE", "VERIFY_COMMAND"), user_mode_verify_pass_regex=sec_get(cfg, "USER_MODE", "VERIFY_PASS_REGEX", ""),
    )


def normalize_mac(value: str) -> str:
    return re.sub(r"[^0-9A-Fa-f]", "", value).upper()


def validate_mac(value: str) -> str:
    mac = normalize_mac(value)
    if len(mac) != 12 or not re.fullmatch(r"[0-9A-F]{12}", mac):
        raise AppError(f"Invalid MAC read from router: {value!r}")
    return mac


def colon_mac(value: str) -> str:
    mac = validate_mac(value)
    return ":".join(mac[i:i + 2] for i in range(0, 12, 2))


def extract_value(output: str, pattern: str, group: str, label: str) -> str:
    try:
        match = re.search(pattern, output, re.MULTILINE)
    except re.error as exc:
        raise AppError(f"Invalid {label} REGEX in config: {exc}") from exc
    if not match:
        raise AppError(f"Could not extract {label} from router response. Check command and REGEX in config.ini.")
    try:
        index = int(group)
        value = match.group(index)
    except ValueError:
        value = match.group(group)
    except (IndexError, KeyError) as exc:
        raise AppError(f"Invalid GROUP for {label}: {group}") from exc
    return value.strip()


def extract_scanned_serial(raw_value: str, cfg: VerifyConfig) -> str:
    raw = (raw_value or "").strip()
    if not raw:
        raise AppError("Serial scan is empty")
    try:
        match = re.search(cfg.serial_scan_extract_regex, raw, re.MULTILINE)
    except re.error as exc:
        raise AppError(f"Invalid [SERIAL_SCAN] EXTRACT_REGEX: {exc}") from exc
    if not match:
        raise AppError(f"Scanned value does not match SERIAL_SCAN EXTRACT_REGEX: {raw!r}")
    try:
        try:
            value = match.group(int(cfg.serial_scan_group))
        except ValueError:
            value = match.group(cfg.serial_scan_group)
    except (IndexError, KeyError) as exc:
        raise AppError(f"Invalid [SERIAL_SCAN] GROUP: {cfg.serial_scan_group}") from exc
    value = value.strip()
    if cfg.serial_valid_regex and not re.fullmatch(cfg.serial_valid_regex, value):
        raise AppError(f"Scanned serial format failed validation: {value}")
    return value


def serials_equal(left: str, right: str, case_sensitive: bool = False) -> bool:
    if case_sensitive:
        return (left or "").strip() == (right or "").strip()
    return (left or "").strip().upper() == (right or "").strip().upper()


def decode_camera_frame(frame) -> list[str]:
    values: list[str] = []
    if zxingcpp is not None:
        try:
            for result in zxingcpp.read_barcodes(frame):
                text = str(getattr(result, "text", "") or "").strip()
                if text and text not in values:
                    values.append(text)
        except Exception:
            pass
    if not values and cv2 is not None:
        try:
            detector = cv2.QRCodeDetector()
            text, _points, _straight = detector.detectAndDecode(frame)
            if text and text.strip():
                values.append(text.strip())
        except Exception:
            pass
    return values


def save_json_atomic(path: Path, data: dict) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(temp, path)


def load_pending() -> dict[str, dict]:
    with PENDING_LOCK:
        if not PENDING_PATH.exists():
            return {}
        try:
            value = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}


def set_pending(slot_key: str, data: dict | None) -> None:
    with PENDING_LOCK:
        jobs = load_pending()
        if data is None:
            jobs.pop(slot_key, None)
        else:
            jobs[slot_key] = data
        if jobs:
            save_json_atomic(PENDING_PATH, jobs)
        else:
            PENDING_PATH.unlink(missing_ok=True)


def check_reachable(router: RouterTarget) -> None:
    try:
        with socket.create_connection((router.ip, router.port), timeout=router.preflight_timeout):
            return
    except OSError as exc:
        raise AppError(f"{router.name} is offline at {router.ip}:{router.port}. Check power, switch/VLAN, IP and Telnet.") from exc


class ServerClient:
    def __init__(self, cfg: VerifyConfig):
        self.base_url = cfg.server_url
        self.api_key = cfg.server_api_key
        self.station_id = cfg.station_id
        self.hostname = socket.gethostname()
        self.timeout = cfg.server_timeout

    def client_id(self, router: RouterTarget) -> str:
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
                data = json.loads(exc.read().decode("utf-8")); msg = data.get("error", str(exc))
            except Exception:
                msg = str(exc)
            raise AppError(f"Server rejected request: {msg}") from exc
        except (error.URLError, TimeoutError, OSError) as exc:
            raise AppError(f"Cannot reach central server {self.base_url}: {exc}") from exc

    def health(self) -> dict:
        return self.call("GET", "/api/health")

    def stats(self) -> dict:
        return self.call("GET", "/api/stats")

    def begin(self, router: RouterTarget, request_id: str, mac: str, serial: str, gpon: str, part: str,
              scanned_serial: str = "") -> dict:
        return self.call("POST", "/api/verify/check", {
            "client_id": self.client_id(router), "request_id": request_id, "mac": mac,
            "serial_number": serial, "scanned_serial_number": scanned_serial,
            "gpon_number": gpon, "part_number": part, "router_ip": router.ip,
        })

    def report(self, router: RouterTarget, verification_id: str, status: str, results: dict, detail: str) -> dict:
        return self.call("POST", "/api/verify/result", {
            "client_id": self.client_id(router), "verification_id": verification_id, "status": status,
            "serial_scan_result": results.get("scan", ""),
            "wifi_calibration_result": results.get("wifi", ""),
            "bob_calibration_result": results.get("bob", ""),
            "firmware_result": results.get("firmware", ""),
            "firmware_version": results.get("firmware_version", ""),
            "led_result": results.get("led", ""), "reset_result": results.get("reset", ""),
            "wps_result": results.get("wps", ""), "user_mode_result": results.get("user", ""),
            "detail": f"{router.name} {router.ip} | {detail}",
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


async def open_logged_in(router: RouterTarget, emit):
    if telnetlib3 is None:
        raise AppError("Missing dependency 'telnetlib3'. Run: pip install -r requirements.txt")
    emit(f"Connecting to {router.ip}:{router.port}")
    try:
        reader, writer = await asyncio.wait_for(
            telnetlib3.open_connection(host=router.ip, port=router.port, connect_minwait=0.05), timeout=router.timeout
        )
    except Exception as exc:
        raise AppError(f"Telnet connection failed: {exc}") from exc
    try:
        if router.username:
            text = await read_until(reader, router.login_prompt, router.timeout)
            if text: emit(text.rstrip())
            writer.write(router.username + "\r\n"); emit(f"> Username: {router.username}")
        if router.password:
            text = await read_until(reader, router.password_prompt, router.timeout)
            if text: emit(text.rstrip())
            writer.write(router.password + "\r\n"); emit("> Password: ********")
        if router.command_prompt:
            text = await read_until(reader, router.command_prompt, router.timeout)
            if text: emit(text.rstrip())
        return reader, writer
    except Exception:
        try: writer.close()
        except Exception: pass
        raise


async def close_session(router: RouterTarget, writer) -> None:
    try:
        if router.exit_command:
            writer.write(router.exit_command + "\r\n")
            await asyncio.sleep(0.05)
        writer.close()
    except Exception:
        pass


async def send_commands(router: RouterTarget, commands: list[str], emit, allow_disconnect: bool = False) -> str:
    reader, writer = await open_logged_in(router, emit)
    outputs: list[str] = []
    try:
        for index, cmd in enumerate(commands):
            emit(f"> {cmd}")
            writer.write(cmd + "\r\n")
            await asyncio.sleep(router.command_delay)
            try:
                response = await read_until(reader, router.command_prompt, router.timeout)
            except Exception as exc:
                if allow_disconnect and index == len(commands) - 1:
                    emit(f"Connection ended after final command (allowed): {exc}")
                    break
                raise
            outputs.append(response)
            if response: emit(response.rstrip())
        return "\n".join(outputs)
    finally:
        await close_session(router, writer)


async def read_identity(router: RouterTarget, cfg: VerifyConfig, emit) -> tuple[str, str, str, str]:
    reader, writer = await open_logged_in(router, emit)
    try:
        async def run_block(title: str, commands: list[str]) -> str:
            emit(f"--- {title} ---")
            outputs = []
            for cmd in commands:
                emit(f"> {cmd}"); writer.write(cmd + "\r\n"); await asyncio.sleep(router.command_delay)
                response = await read_until(reader, router.command_prompt, router.timeout)
                outputs.append(response)
                if response: emit(response.rstrip())
            return "\n".join(outputs)
        mac_out = await run_block("READ MAC", cfg.read_mac_commands)
        serial_out = await run_block("READ SERIAL NUMBER", cfg.read_serial_commands)
        gpon_out = await run_block("READ GPON NUMBER", cfg.read_gpon_commands)
        part_out = await run_block("READ PART NUMBER", cfg.read_part_commands) if cfg.read_part_commands else ""
        mac = colon_mac(extract_value(mac_out, cfg.read_mac_regex, cfg.read_mac_group, "MAC"))
        serial = extract_value(serial_out, cfg.read_serial_regex, cfg.read_serial_group, "serial number")
        gpon = extract_value(gpon_out, cfg.read_gpon_regex, cfg.read_gpon_group, "GPON number")
        part = cfg.read_part_static or extract_value(part_out, cfg.read_part_regex, cfg.read_part_group, "part number")
        if cfg.read_part_static:
            emit(f"PART NUMBER: using configured STATIC_VALUE={part}")
        return mac, serial, gpon, part
    finally:
        await close_session(router, writer)


async def run_automated_checks(router: RouterTarget, cfg: VerifyConfig, emit) -> dict:
    reader, writer = await open_logged_in(router, emit)
    try:
        async def run_block(title: str, commands: list[str]) -> str:
            emit(f"--- {title} ---")
            outputs = []
            for cmd in commands:
                emit(f"> {cmd}"); writer.write(cmd + "\r\n"); await asyncio.sleep(router.command_delay)
                response = await read_until(reader, router.command_prompt, router.timeout)
                outputs.append(response)
                if response: emit(response.rstrip())
            return "\n".join(outputs)

        def evaluate(label: str, output: str, pass_regex: str, fail_regex: str) -> bool:
            if fail_regex and re.search(fail_regex, output, re.MULTILINE):
                emit(f"{label}: FAIL pattern detected")
                return False
            if pass_regex and not re.search(pass_regex, output, re.MULTILINE):
                emit(f"{label}: PASS pattern not found")
                return False
            passed = bool(output.strip())
            emit(f"{label}: {'PASS' if passed else 'FAIL - empty response'}")
            return passed

        wifi_output = await run_block("CHECK WI-FI CALIBRATION DATA", cfg.wifi_calibration_commands)
        wifi_pass = evaluate("WI-FI CALIBRATION", wifi_output, cfg.wifi_calibration_pass_regex, cfg.wifi_calibration_fail_regex)
        bob_output = await run_block("CHECK BOB CALIBRATION DATA", cfg.bob_calibration_commands)
        bob_pass = evaluate("BOB CALIBRATION", bob_output, cfg.bob_calibration_pass_regex, cfg.bob_calibration_fail_regex)
        fw_output = await run_block("READ FIRMWARE VERSION", cfg.firmware_commands)
        firmware = extract_value(fw_output, cfg.firmware_regex, cfg.firmware_group, "firmware version")
        fw_pass = True
        if cfg.expected_firmware_versions and firmware.upper() not in {x.upper() for x in cfg.expected_firmware_versions}:
            fw_pass = False
        if cfg.expected_firmware_regex and not re.fullmatch(cfg.expected_firmware_regex, firmware):
            fw_pass = False
        emit(f"FIRMWARE VERSION: {firmware} -> {'PASS' if fw_pass else 'FAIL'}")
        return {"wifi": wifi_pass, "bob": bob_pass, "firmware": fw_pass, "firmware_version": firmware}
    finally:
        await close_session(router, writer)


async def button_test(router: RouterTarget, prepare: list[str], checks: list[str], pass_regex: str,
                      timeout: float, interval: float, emit, disconnect_is_pass: bool = False,
                      passive_monitor: bool = False) -> tuple[bool, str]:
    reader, writer = await open_logged_in(router, emit)
    all_output: list[str] = []
    try:
        for cmd in prepare:
            emit(f"> {cmd}"); writer.write(cmd + "\r\n"); await asyncio.sleep(router.command_delay)
            response = await read_until(reader, router.command_prompt, router.timeout)
            all_output.append(response)
            if response: emit(response.rstrip())
        deadline = time.monotonic() + timeout
        if passive_monitor:
            emit("Passive Telnet log monitoring started; press the physical button now.")
            while time.monotonic() < deadline:
                remaining = max(0.1, deadline - time.monotonic())
                try:
                    response = await asyncio.wait_for(reader.read(4096), timeout=min(max(0.1, interval), remaining))
                except asyncio.TimeoutError:
                    response = ""
                except Exception as exc:
                    if disconnect_is_pass:
                        emit(f"Router disconnected after button action; configured as PASS: {exc}")
                        return True, "\n".join(all_output)
                    raise
                if response:
                    all_output.append(response)
                    emit(response.rstrip())
                    if re.search(pass_regex, "\n".join(all_output), re.MULTILINE):
                        return True, "\n".join(all_output)
            return False, "\n".join(all_output)

        while time.monotonic() < deadline:
            cycle = []
            for cmd in checks:
                emit(f"> {cmd}"); writer.write(cmd + "\r\n"); await asyncio.sleep(router.command_delay)
                try:
                    response = await read_until(reader, router.command_prompt, router.timeout)
                except Exception as exc:
                    if disconnect_is_pass:
                        emit(f"Router disconnected after button action; configured as PASS: {exc}")
                        return True, "\n".join(all_output)
                    raise
                cycle.append(response); all_output.append(response)
                if response: emit(response.rstrip())
            cycle_text = "\n".join(cycle)
            if re.search(pass_regex, cycle_text, re.MULTILINE):
                return True, "\n".join(all_output)
            await asyncio.sleep(max(0.1, interval))
        return False, "\n".join(all_output)
    finally:
        await close_session(router, writer)


class SlotCard:
    ACTION_TEXT = {
        "IDLE": "START", "PASS": "NEXT PCB", "FAIL": "NEXT PCB", "ERROR": "NEXT PCB",
        "SERVER_CHECK_PENDING": "RETRY SERVER", "WAIT_LED": "LED PASS", "READY_RESET": "TEST RESET",
        "READY_WPS": "TEST WPS", "READY_USER_MODE": "SET USER MODE", "REPORT_PENDING": "RETRY REPORT",
    }

    STEP_LABELS = {
        "scan": "SCAN", "data": "DATA", "server": "SERVER", "wifi": "WI-FI", "bob": "BOB",
        "firmware": "FW", "led": "LED", "reset": "RESET", "wps": "WPS", "user": "USER"
    }

    def __init__(self, app, parent, router: RouterTarget, row: int, column: int):
        self.app = app
        self.router = router
        self.state = "DISABLED" if not router.enabled else "IDLE"
        self.request_id = ""
        self.verification_id = ""
        self.desired_status = ""
        self.results = {"scan": "", "wifi": "", "bob": "", "firmware": "", "firmware_version": "", "led": "", "reset": "", "wps": "", "user": ""}
        self.scanned_serial_value = ""
        self.scan_source = ""
        self.mac_value = ""
        self.serial_value = ""
        self.gpon_value = ""
        self.part_value = ""
        self.firmware_value = ""
        self.status_var = tk.StringVar(value="DISABLED" if not router.enabled else "READY")
        self.detail_var = tk.StringVar(value="Disabled in config" if not router.enabled else "Waiting for PCB")
        self.mac_var = tk.StringVar(value="—")
        self.serial_var = tk.StringVar(value="—")
        self.gpon_var = tk.StringVar(value="—")
        self.part_var = tk.StringVar(value="—")
        self.firmware_var = tk.StringVar(value="—")
        self.scan_var = tk.StringVar(value="")
        self.scan_status_var = tk.StringVar(value="SCAN REQUIRED" if app.cfg.serial_scan_enabled and app.cfg.serial_scan_required else "OPTIONAL")
        self.live_log = tk.StringVar(value="No activity yet" if router.enabled else "Slot disabled")
        self.log_history: list[str] = []
        self.check_var = tk.StringVar(value="SCAN —   DATA —   SERVER —   WI-FI —   BOB —   FW —   LED —   RESET —   WPS —   USER —")
        self.step_values = {key: "—" for key in self.STEP_LABELS}
        self.step_widgets = {}

        # A flat dashboard row inspired by the supplied admin UI.
        self.frame = tk.Frame(parent, bg="#FFFFFF", highlightthickness=1,
                              highlightbackground="#E7ECF5", bd=0)
        self.frame.grid(row=row, column=column, sticky="nsew", padx=0, pady=(0, 7))
        self.frame.grid_columnconfigure(2, weight=1)

        badge = tk.Label(self.frame, text=f"{router.slot:02d}", bg="#EDF3FF", fg="#4F70E8",
                         font=("Segoe UI", 9, "bold"), width=3, pady=8)
        badge.grid(row=0, column=0, rowspan=2, padx=(12, 10), pady=10, sticky="ns")

        identity = tk.Frame(self.frame, bg="#FFFFFF")
        identity.grid(row=0, column=1, rowspan=2, sticky="nw", pady=9, padx=(0, 12))
        tk.Label(identity, text=router.name, bg="#FFFFFF", fg="#27364F",
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")
        tk.Label(identity, text=router.ip or "No IP", bg="#FFFFFF", fg="#8C98AD",
                 font=("Consolas", 8)).pack(anchor="w", pady=(2, 0))

        info = tk.Frame(self.frame, bg="#FFFFFF")
        info.grid(row=0, column=2, sticky="ew", pady=(9, 2))
        for idx, (label, var) in enumerate((("MAC", self.mac_var), ("SERIAL", self.serial_var), ("GPON", self.gpon_var), ("PART", self.part_var), ("FW", self.firmware_var))):
            block = tk.Frame(info, bg="#FFFFFF")
            block.grid(row=0, column=idx, sticky="w", padx=(0, 18))
            tk.Label(block, text=label, bg="#FFFFFF", fg="#A1AABD",
                     font=("Segoe UI", 7, "bold")).pack(anchor="w")
            tk.Label(block, textvariable=var, bg="#FFFFFF", fg="#28354B",
                     font=("Consolas", 8, "bold")).pack(anchor="w")

        steps = tk.Frame(self.frame, bg="#FFFFFF")
        steps.grid(row=1, column=2, sticky="w", pady=(2, 8))
        for idx, (key, label) in enumerate(self.STEP_LABELS.items()):
            pill = tk.Label(steps, text=f"{label}  —", bg="#F4F6FA", fg="#8D98AA",
                            font=("Segoe UI", 6, "bold"), padx=5, pady=3)
            step_row = 0 if idx < 5 else 1
            step_col = idx if idx < 5 else idx - 5
            pill.grid(row=step_row, column=step_col, padx=(0, 4), pady=(0, 3) if step_row == 0 else 0)
            self.step_widgets[key] = pill

        controls = tk.Frame(self.frame, bg="#FFFFFF")
        controls.grid(row=0, column=3, rowspan=2, padx=(8, 12), pady=10, sticky="e")
        self.status_label = tk.Label(controls, textvariable=self.status_var, bg="#EDF3FF", fg="#4F70E8",
                                     font=("Segoe UI", 8, "bold"), padx=9, pady=4)
        self.status_label.pack(anchor="e", pady=(0, 6))
        self.action = tk.Button(controls, text=self.ACTION_TEXT.get(self.state, "START"), command=self.clicked,
                                bg="#4F70E8", fg="#FFFFFF", activebackground="#3C5DD9",
                                activeforeground="#FFFFFF", relief="flat", bd=0, cursor="hand2",
                                font=("Segoe UI", 8, "bold"), padx=12, pady=6, width=13)
        self.action.pack(anchor="e")
        self.log_button = tk.Button(controls, text="VIEW LOG", command=lambda: self.app.show_router_log(self),
                                    bg="#F2F5FA", fg="#68758A", activebackground="#E7EDF8",
                                    activeforeground="#4F70E8", relief="flat", bd=0, cursor="hand2",
                                    font=("Segoe UI", 7, "bold"), padx=10, pady=4, width=13)
        self.log_button.pack(anchor="e", pady=(5, 0))
        self.reject = tk.Button(controls, text="FAIL PCB", command=self.reject_clicked,
                                bg="#FFF0F0", fg="#C43C4A", activebackground="#FFE5E7",
                                relief="flat", bd=0, cursor="hand2", font=("Segoe UI", 8, "bold"),
                                padx=10, pady=5, width=13)
        self.reject.pack(anchor="e", pady=(5, 0))
        self.reject.pack_forget()

        scan_row = tk.Frame(self.frame, bg="#FFFFFF")
        scan_row.grid(row=2, column=1, columnspan=3, sticky="ew", padx=(0, 12), pady=(1, 4))
        scan_row.grid_columnconfigure(2, weight=1)
        tk.Label(scan_row, text="LABEL SERIAL", bg="#FFFFFF", fg="#A1AABD",
                 font=("Segoe UI", 7, "bold")).grid(row=0, column=0, sticky="w", padx=(0, 7))
        self.scan_entry = tk.Entry(scan_row, textvariable=self.scan_var, bg="#F8FAFD", fg="#263247",
                                   insertbackground="#263247", relief="flat", highlightthickness=1,
                                   highlightbackground="#E1E7F0", highlightcolor="#4F70E8",
                                   font=("Consolas", 8, "bold"), width=24)
        self.scan_entry.grid(row=0, column=1, sticky="w")
        self.scan_entry.bind("<Return>", lambda _event: self.manual_scan_clicked())
        self.scan_status_label = tk.Label(scan_row, textvariable=self.scan_status_var, bg="#FFF7E6", fg="#B77A12",
                                          font=("Segoe UI", 7, "bold"), padx=7, pady=3)
        self.scan_status_label.grid(row=0, column=2, sticky="w", padx=(7, 4))
        self.scan_set_button = tk.Button(scan_row, text="SET / USB SCAN", command=self.manual_scan_clicked,
                                         bg="#EDF3FF", fg="#4F70E8", relief="flat", bd=0, cursor="hand2",
                                         font=("Segoe UI", 7, "bold"), padx=8, pady=4)
        self.scan_set_button.grid(row=0, column=3, padx=(4, 4))
        self.scan_camera_button = tk.Button(scan_row, text="CAMERA", command=self.camera_scan_clicked,
                                            bg="#E8F7FB", fg="#2489A7", relief="flat", bd=0, cursor="hand2",
                                            font=("Segoe UI", 7, "bold"), padx=8, pady=4)
        self.scan_camera_button.grid(row=0, column=4, padx=(0, 4))
        self.scan_clear_button = tk.Button(scan_row, text="CLEAR", command=self.clear_scanned_serial,
                                           bg="#F4F6FA", fg="#7C879A", relief="flat", bd=0, cursor="hand2",
                                           font=("Segoe UI", 7, "bold"), padx=7, pady=4)
        self.scan_clear_button.grid(row=0, column=5)

        self.detail_label = tk.Label(self.frame, textvariable=self.detail_var, bg="#FFFFFF", fg="#7E8A9D",
                                     font=("Segoe UI", 7), anchor="w")
        self.detail_label.grid(row=3, column=1, columnspan=3, sticky="ew", padx=(0, 12), pady=(0, 3))
        live_row = tk.Frame(self.frame, bg="#F8FAFD", highlightthickness=1, highlightbackground="#EEF2F7")
        live_row.grid(row=4, column=1, columnspan=3, sticky="ew", padx=(0, 12), pady=(0, 8))
        live_row.grid_columnconfigure(1, weight=1)
        tk.Label(live_row, text="LIVE", bg="#F8FAFD", fg="#4F70E8",
                 font=("Segoe UI", 6, "bold"), padx=6).grid(row=0, column=0, sticky="w")
        tk.Label(live_row, textvariable=self.live_log, bg="#F8FAFD", fg="#6D798D",
                 font=("Consolas", 7), anchor="w").grid(row=0, column=1, sticky="ew", padx=(2, 6), pady=4)

        if not app.cfg.serial_scan_enabled:
            self.scan_entry.configure(state="disabled")
            self.scan_set_button.configure(state="disabled")
            self.scan_camera_button.configure(state="disabled")
            self.scan_clear_button.configure(state="disabled")
            self.scan_status_var.set("SCAN OFF")
            self.scan_status_label.configure(bg="#F0F2F5", fg="#9AA3B1")
            self.set_check("scan", "✓")
        if not router.enabled:
            self.action.configure(state="disabled", bg="#C8CED8")
            self.scan_entry.configure(state="disabled")
            self.scan_set_button.configure(state="disabled")
            self.scan_camera_button.configure(state="disabled")
            self.scan_clear_button.configure(state="disabled")
            self._apply_status_style("DISABLED")

    def add_log(self, text: str):
        stamp = time.strftime("%H:%M:%S")
        line = f"[{stamp}] {text}"
        self.log_history.append(line)
        if len(self.log_history) > 1000:
            self.log_history = self.log_history[-1000:]
        compact = " ".join(str(text).split())
        self.live_log.set(compact[:125] + ("…" if len(compact) > 125 else ""))

    def manual_scan_clicked(self):
        self.app.apply_serial_scan(self, self.scan_var.get(), "MANUAL/USB")

    def camera_scan_clicked(self):
        self.app.start_camera_for_card(self)

    def set_scanned_serial(self, value: str, source: str):
        self.scanned_serial_value = value
        self.scan_source = source
        self.scan_var.set(value)
        self.scan_status_var.set(f"SCANNED · {source}")
        self.scan_status_label.configure(bg="#E9F8F0", fg="#159260")
        self.results["scan"] = "PASS"
        self.set_check("scan", "✓")

    def clear_scanned_serial(self):
        if self.state not in {"IDLE", "PASS", "FAIL", "ERROR"}:
            messagebox.showwarning("Scan locked", "The serial scan cannot be cleared during an active verification cycle.")
            return
        self.scanned_serial_value = ""
        self.scan_source = ""
        self.scan_var.set("")
        self.results["scan"] = ""
        self.set_check("scan", "—")
        self.scan_status_var.set("SCAN REQUIRED" if self.app.cfg.serial_scan_required else "OPTIONAL")
        self.scan_status_label.configure(bg="#FFF7E6", fg="#B77A12")
        self.detail_var.set("Scan or type the serial number printed on the label, then start verification.")

    def clicked(self):
        self.app.handle_action(self)

    def reject_clicked(self):
        self.app.reject_slot(self)

    def reset_cycle(self, keep_scan: bool = True):
        saved_scan = self.scanned_serial_value if keep_scan else ""
        saved_source = self.scan_source if keep_scan else ""
        self.request_id = str(uuid.uuid4())
        self.verification_id = ""
        self.desired_status = ""
        self.results = {"scan": "PASS" if saved_scan else ("PASS" if not self.app.cfg.serial_scan_enabled else ""), "wifi": "", "bob": "", "firmware": "", "firmware_version": "", "led": "", "reset": "", "wps": "", "user": ""}
        self.scanned_serial_value = saved_scan
        self.scan_source = saved_source
        self.mac_value = self.serial_value = self.gpon_value = self.part_value = self.firmware_value = ""
        self.mac_var.set("—")
        self.serial_var.set("—")
        self.gpon_var.set("—")
        self.part_var.set("—")
        self.firmware_var.set("—")
        self.step_values = {key: "—" for key in self.STEP_LABELS}
        if saved_scan or not self.app.cfg.serial_scan_enabled:
            self.step_values["scan"] = "✓"
        for key in self.STEP_LABELS:
            self._render_step(key)
        self._sync_check_var()

    def set_identity(self, mac: str, serial: str, gpon: str, part: str):
        self.mac_value = mac
        self.serial_value = serial
        self.gpon_value = gpon
        self.part_value = part
        self.mac_var.set(mac)
        self.serial_var.set(serial)
        self.gpon_var.set(gpon)
        self.part_var.set(part)

    def set_firmware(self, version: str):
        self.firmware_value = version
        self.firmware_var.set(version or "—")

    def _sync_check_var(self):
        self.check_var.set("   ".join(f"{self.STEP_LABELS[k]} {self.step_values[k]}" for k in self.STEP_LABELS))

    def _render_step(self, key: str):
        mark = self.step_values.get(key, "—")
        widget = self.step_widgets.get(key)
        if not widget:
            return
        if mark == "✓":
            bg, fg = "#E9F8F0", "#159260"
        elif mark == "✕":
            bg, fg = "#FFF0F0", "#C43C4A"
        elif mark == "…":
            bg, fg = "#FFF7E6", "#B77A12"
        else:
            bg, fg = "#F4F6FA", "#8D98AA"
        widget.configure(text=f"{self.STEP_LABELS[key]}  {mark}", bg=bg, fg=fg)

    def set_check(self, name: str, result: str):
        if name not in self.step_values:
            return
        self.step_values[name] = result
        self._render_step(name)
        self._sync_check_var()

    def _apply_status_style(self, state: str):
        if state == "PASS":
            bg, fg = "#E9F8F0", "#159260"
        elif state in {"FAIL", "ERROR", "REPORT_PENDING"}:
            bg, fg = "#FFF0F0", "#C43C4A"
        elif state in {"WAIT_LED", "READY_RESET", "READY_WPS", "READY_USER_MODE"}:
            bg, fg = "#FFF7E6", "#B77A12"
        elif state == "DISABLED":
            bg, fg = "#F0F2F5", "#9AA3B1"
        elif state in {"DATA_RUNNING", "AUTO_CHECK_RUNNING", "LED_STARTING", "RESET_RUNNING", "WPS_RUNNING", "USER_MODE_RUNNING", "REPORTING", "SERVER_CHECK_PENDING"}:
            bg, fg = "#E8F5FF", "#2587C7"
        else:
            bg, fg = "#EDF3FF", "#4F70E8"
        self.status_label.configure(bg=bg, fg=fg)

    def set_state(self, state: str, detail: str = "", status_text: str | None = None):
        self.state = state
        label = status_text or state.replace("_", " ")
        if state == "IDLE":
            label = "READY"
        self.status_var.set(label)
        if detail:
            self.detail_var.set(detail)
        busy = state in {"DATA_RUNNING", "AUTO_CHECK_RUNNING", "LED_STARTING", "RESET_RUNNING", "WPS_RUNNING", "USER_MODE_RUNNING", "REPORTING"}
        disabled = busy or state == "DISABLED"
        self.action.configure(
            state="disabled" if disabled else "normal",
            text=self.ACTION_TEXT.get(state, "WORKING..." if busy else "START"),
            bg="#C8CED8" if disabled else "#4F70E8",
        )
        if state in {"WAIT_LED", "READY_RESET", "READY_WPS", "READY_USER_MODE"}:
            if not self.reject.winfo_ismapped():
                self.reject.pack(anchor="e", pady=(5, 0))
        else:
            self.reject.pack_forget()
        self._apply_status_style(state)

    def pending_payload(self) -> dict:
        return {
            "request_id": self.request_id, "verification_id": self.verification_id,
            "state": self.state, "desired_status": self.desired_status,
            "mac": self.mac_value, "serial": self.serial_value, "scanned_serial": self.scanned_serial_value,
            "scan_source": self.scan_source, "gpon": self.gpon_value, "part": self.part_value,
            "firmware": self.firmware_value,
            "results": self.results, "router_ip": self.router.ip,
        }

    def restore(self, data: dict):
        self.request_id = str(data.get("request_id", str(uuid.uuid4())))
        self.verification_id = str(data.get("verification_id", ""))
        self.desired_status = str(data.get("desired_status", ""))
        self.results = dict(data.get("results", {}))
        scanned = str(data.get("scanned_serial", ""))
        if scanned:
            self.set_scanned_serial(scanned, str(data.get("scan_source", "RECOVERED")) or "RECOVERED")
        self.set_identity(str(data.get("mac", "")), str(data.get("serial", "")), str(data.get("gpon", "")), str(data.get("part", "")))
        self.set_firmware(str(data.get("firmware", "")))
        for key in ["scan", "data", "server", "wifi", "bob", "firmware", "led", "reset", "wps", "user"]:
            val = self.results.get(key, "")
            if val == "PASS":
                self.set_check(key, "✓")
            elif val == "FAIL":
                self.set_check(key, "✕")
        state = str(data.get("state", "REPORT_PENDING"))
        if not self.verification_id:
            safe_state = "SERVER_CHECK_PENDING" if self.mac_value and self.serial_value and self.gpon_value and self.part_value else "IDLE"
        elif state in {"DATA_RUNNING", "AUTO_CHECK_RUNNING", "LED_STARTING", "SERVER_CHECK_PENDING", "WAIT_LED"}:
            safe_state = "SERVER_CHECK_PENDING"
        elif state == "RESET_RUNNING":
            safe_state = "READY_RESET"
        elif state == "WPS_RUNNING":
            safe_state = "READY_WPS"
        elif state == "USER_MODE_RUNNING":
            safe_state = "READY_USER_MODE"
        elif state in {"REPORTING", "REPORT_PENDING"}:
            safe_state = "REPORT_PENDING"
        else:
            safe_state = state if state in self.ACTION_TEXT else "REPORT_PENDING"
        self.set_state(safe_state, "Recovered unfinished verification. Continue the saved stage safely.", "RECOVERY")


class VerifierApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ETE Solutions India | 8-Router Calibration & Quality Verification Station")
        # Fit the application to the operator's display instead of forcing a 1500x940
        # window.  This is important on the common 1366x768 production monitors.
        screen_w = max(1024, self.winfo_screenwidth())
        screen_h = max(700, self.winfo_screenheight())
        win_w = min(1500, max(1040, screen_w - 40))
        win_h = min(940, max(650, screen_h - 80))
        pos_x = max(0, (screen_w - win_w) // 2)
        pos_y = max(0, (screen_h - win_h) // 2)
        self.geometry(f"{win_w}x{win_h}+{pos_x}+{pos_y}")
        self.minsize(min(1040, max(900, screen_w - 100)), min(650, max(600, screen_h - 120)))
        self.configure(bg="#DDE6F5")
        self.events: queue.Queue = queue.Queue(); self.cfg=load_config(CONFIG_PATH); self.server=ServerClient(self.cfg)
        self.cards: dict[str,SlotCard]={}; self.brand_logo=None
        self.camera_stop = threading.Event(); self.camera_running = False; self.camera_target_slot = ""
        self.server_var=tk.StringVar(value="CHECKING"); self.local_pass=tk.StringVar(value="0"); self.local_fail=tk.StringVar(value="0"); self.active_var=tk.StringVar(value="0"); self.server_verified=tk.StringVar(value="0")
        self._load_assets(); self._style(); self._build(); self._restore_pending()
        self.after(100,self._poll_events); self.after(300,self.test_server); self.protocol("WM_DELETE_WINDOW",self._on_close)

    def _load_assets(self):
        p=BASE_DIR/"assets"/"ete_logo.png"
        if p.exists():
            try: self.brand_logo=tk.PhotoImage(file=str(p)); self.iconphoto(True,self.brand_logo)
            except tk.TclError: self.brand_logo=None

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
        s.configure("Treeview", rowheight=30, font=("Segoe UI", 9), background="#FFFFFF",
                    fieldbackground="#FFFFFF", foreground="#39445A", borderwidth=0)
        s.configure("Treeview.Heading", font=("Segoe UI", 8, "bold"), background="#F7F9FC",
                    foreground="#7E899D", relief="flat")

    def _build(self):
        # Dashboard shell: fixed navigation rail + content area, following the supplied visual reference.
        shell = tk.Frame(self, bg="#DDE6F5")
        shell.pack(fill="both", expand=True, padx=28, pady=22)
        shell.grid_rowconfigure(0, weight=1)
        shell.grid_columnconfigure(1, weight=1)

        sidebar = tk.Frame(shell, bg="#FFFFFF", width=190, highlightthickness=1,
                           highlightbackground="#D8E0EE")
        sidebar.grid(row=0, column=0, sticky="nsw")
        sidebar.grid_propagate(False)

        main = tk.Frame(shell, bg="#F5F7FB", highlightthickness=1, highlightbackground="#D8E0EE")
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_rowconfigure(1, weight=1)
        main.grid_columnconfigure(0, weight=1)

        # Sidebar branding
        brand = tk.Frame(sidebar, bg="#FFFFFF")
        brand.pack(fill="x", padx=18, pady=(18, 14))
        if self.brand_logo:
            tk.Label(brand, image=self.brand_logo, bg="#FFFFFF").pack(anchor="w")
        else:
            tk.Label(brand, text="ETE", bg="#4F70E8", fg="#FFFFFF",
                     font=("Segoe UI", 13, "bold"), padx=9, pady=6).pack(anchor="w")
        tk.Label(brand, text="QUALITY STATION", bg="#FFFFFF", fg="#A0A9B8",
                 font=("Segoe UI", 7, "bold")).pack(anchor="w", pady=(5, 0))

        tk.Frame(sidebar, bg="#EFF2F7", height=1).pack(fill="x", padx=16)
        tk.Label(sidebar, text="MENU", bg="#FFFFFF", fg="#A7AFBD",
                 font=("Segoe UI", 7, "bold")).pack(anchor="w", padx=20, pady=(18, 8))

        self.nav_buttons = {}
        def nav_button(key, label, command):
            b = tk.Button(sidebar, text=label, command=command, anchor="w",
                          bg="#FFFFFF", fg="#78849A", activebackground="#EDF3FF",
                          activeforeground="#4F70E8", relief="flat", bd=0, cursor="hand2",
                          font=("Segoe UI", 9), padx=18, pady=9)
            b.pack(fill="x", padx=8, pady=1)
            self.nav_buttons[key] = b
            return b

        nav_button("dashboard", "▣   Dashboard", lambda: self._show_page("dashboard"))
        nav_button("log", "≡   Process Log", lambda: self._show_page("log"))
        nav_button("server", "●   Test Server", self.test_server)

        tk.Label(sidebar, text="CONFIGURATION", bg="#FFFFFF", fg="#A7AFBD",
                 font=("Segoe UI", 7, "bold")).pack(anchor="w", padx=20, pady=(20, 8))
        nav_button("config", "⚙   Open Config", self.open_config)
        nav_button("reload", "↻   Reload Config", self.reload_config)

        spacer = tk.Frame(sidebar, bg="#FFFFFF")
        spacer.pack(fill="both", expand=True)
        station_box = tk.Frame(sidebar, bg="#EDF3FF")
        station_box.pack(fill="x", padx=14, pady=14)
        tk.Label(station_box, text="STATION", bg="#EDF3FF", fg="#8B99AF",
                 font=("Segoe UI", 7, "bold")).pack(anchor="w", padx=12, pady=(10, 1))
        tk.Label(station_box, text=self.cfg.station_id, bg="#EDF3FF", fg="#3D5FCA",
                 font=("Segoe UI", 8, "bold"), wraplength=145, justify="left").pack(anchor="w", padx=12)
        tk.Label(station_box, text=f"Verifier {APP_VERSION}", bg="#EDF3FF", fg="#9AA6BA",
                 font=("Segoe UI", 7)).pack(anchor="w", padx=12, pady=(2, 10))

        # Header
        header = tk.Frame(main, bg="#FFFFFF", height=72)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        header.grid_columnconfigure(1, weight=1)
        title_wrap = tk.Frame(header, bg="#FFFFFF")
        title_wrap.grid(row=0, column=0, sticky="w", padx=22, pady=13)
        tk.Label(title_wrap, text="Router Calibration & Quality Verification", bg="#FFFFFF", fg="#29364B",
                 font=("Segoe UI", 15, "bold")).pack(anchor="w")
        tk.Label(title_wrap, text="Identity · Wi-Fi/BOB calibration · firmware · LED/buttons · 8 router stations", bg="#FFFFFF", fg="#97A1B3",
                 font=("Segoe UI", 8)).pack(anchor="w", pady=(1, 0))

        header_right = tk.Frame(header, bg="#FFFFFF")
        header_right.grid(row=0, column=2, sticky="e", padx=18)
        server_chip = tk.Frame(header_right, bg="#F6F8FC", highlightthickness=1, highlightbackground="#E9EDF4")
        server_chip.pack(side="left", padx=(0, 9))
        tk.Label(server_chip, text="SERVER", bg="#F6F8FC", fg="#A0A9B9",
                 font=("Segoe UI", 7, "bold")).pack(side="left", padx=(10, 5), pady=8)
        self.header_server = tk.Label(server_chip, textvariable=self.server_var, bg="#F6F8FC", fg="#4F70E8",
                                      font=("Segoe UI", 8, "bold"))
        self.header_server.pack(side="left", padx=(0, 10), pady=8)
        self.camera_button = tk.Button(header_right, text="AUTO CAMERA", command=self.toggle_auto_camera,
                                       bg="#E8F7FB", fg="#2489A7", activebackground="#D8F1F7",
                                       activeforeground="#1C7690", relief="flat", bd=0, cursor="hand2",
                                       font=("Segoe UI", 8, "bold"), padx=14, pady=8)
        self.camera_button.pack(side="left", padx=(0, 8))
        self.start_all_button = tk.Button(header_right, text="START ALL", command=self.start_all, bg="#4F70E8", fg="#FFFFFF",
                                          activebackground="#3D5FD6", activeforeground="#FFFFFF", relief="flat", bd=0,
                                          cursor="hand2", font=("Segoe UI", 8, "bold"), padx=18, pady=8)
        self.start_all_button.pack(side="left")

        # Page host
        self.page_host = tk.Frame(main, bg="#F5F7FB")
        self.page_host.grid(row=1, column=0, sticky="nsew")
        self.page_host.grid_rowconfigure(0, weight=1)
        self.page_host.grid_columnconfigure(0, weight=1)

        self.dashboard_page = tk.Frame(self.page_host, bg="#F5F7FB")
        self.log_page = tk.Frame(self.page_host, bg="#F5F7FB")
        for page in (self.dashboard_page, self.log_page):
            page.grid(row=0, column=0, sticky="nsew")

        # DASHBOARD PAGE
        dash = self.dashboard_page
        dash.grid_columnconfigure(0, weight=1)
        dash.grid_rowconfigure(2, weight=1)

        metrics = tk.Frame(dash, bg="#F5F7FB")
        metrics.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 10))
        for i in range(5):
            metrics.grid_columnconfigure(i, weight=1)

        metric_specs = [
            ("SERVER STATUS", self.server_var, "#4F70E8", "S"),
            ("ACTIVE TESTS", self.active_var, "#2BAEC7", "A"),
            ("LOCAL PASS", self.local_pass, "#2AB879", "P"),
            ("LOCAL FAIL", self.local_fail, "#E06873", "F"),
            ("SERVER VERIFIED", self.server_verified, "#7F7DEB", "V"),
        ]
        for i, (label, var, accent, icon_text) in enumerate(metric_specs):
            card = tk.Frame(metrics, bg="#FFFFFF", highlightthickness=1, highlightbackground="#E8ECF3")
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 5, 0 if i == 4 else 5))
            top = tk.Frame(card, bg="#FFFFFF")
            top.pack(fill="x", padx=12, pady=(10, 2))
            tk.Label(top, text=label, bg="#FFFFFF", fg="#A3ACBB", font=("Segoe UI", 7, "bold")).pack(side="left")
            tk.Label(top, text=icon_text, bg=accent, fg="#FFFFFF", font=("Segoe UI", 7, "bold"),
                     width=2, pady=2).pack(side="right")
            tk.Label(card, textvariable=var, bg="#FFFFFF", fg="#263247",
                     font=("Segoe UI", 16, "bold")).pack(anchor="w", padx=12, pady=(0, 10))

        # Section header / helper bar
        helper = tk.Frame(dash, bg="#F5F7FB")
        helper.grid(row=1, column=0, sticky="ew", padx=18, pady=(2, 8))
        helper.grid_columnconfigure(0, weight=1)
        tk.Label(helper, text="Router Verification Progress", bg="#F5F7FB", fg="#2E3A50",
                 font=("Segoe UI", 11, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(helper, text="Scan label serial → Read router identity → Server → Wi-Fi/BOB → Firmware → LED → Reset → WPS → User Mode",
                 bg="#F5F7FB", fg="#98A2B4", font=("Segoe UI", 8)).grid(row=1, column=0, sticky="w", pady=(2, 0))
        tk.Button(helper, text="Refresh Server", command=self.test_server, bg="#FFFFFF", fg="#617089",
                  activebackground="#EDF2FA", relief="flat", bd=0, cursor="hand2",
                  font=("Segoe UI", 8, "bold"), padx=11, pady=6).grid(row=0, column=1, rowspan=2, sticky="e")

        # Responsive router area. The previous fixed two-column layout could be clipped
        # or appear unresponsive on 1366x768 production monitors because the four large
        # router rows had no scrolling and the right-hand controls could fall outside
        # the visible area. This canvas scrolls vertically and automatically stacks the
        # two router panels when the available width is too small.
        router_host = tk.Frame(dash, bg="#F5F7FB")
        router_host.grid(row=2, column=0, sticky="nsew", padx=18, pady=(0, 16))
        router_host.grid_rowconfigure(0, weight=1)
        router_host.grid_columnconfigure(0, weight=1)

        self.router_canvas = tk.Canvas(router_host, bg="#F5F7FB", highlightthickness=0, bd=0)
        router_scrollbar = tk.Scrollbar(router_host, orient="vertical", command=self.router_canvas.yview)
        self.router_canvas.configure(yscrollcommand=router_scrollbar.set)
        self.router_canvas.grid(row=0, column=0, sticky="nsew")
        router_scrollbar.grid(row=0, column=1, sticky="ns", padx=(6, 0))

        body = tk.Frame(self.router_canvas, bg="#F5F7FB")
        self.router_canvas_window = self.router_canvas.create_window((0, 0), window=body, anchor="nw")
        self.router_body = body
        self.router_panel_frames = []

        def update_scrollregion(_event=None):
            self.router_canvas.configure(scrollregion=self.router_canvas.bbox("all"))

        def update_router_layout(event=None):
            width = event.width if event is not None else self.router_canvas.winfo_width()
            width = max(1, int(width))
            self.router_canvas.itemconfigure(self.router_canvas_window, width=width)
            stacked = width < 1060
            if getattr(self, "_router_panels_stacked", None) == stacked and self.router_panel_frames:
                update_scrollregion()
                return
            self._router_panels_stacked = stacked
            if stacked:
                body.grid_columnconfigure(0, weight=1)
                body.grid_columnconfigure(1, weight=0)
                for idx, panel in enumerate(self.router_panel_frames):
                    panel.grid_configure(row=idx, column=0, sticky="nsew", padx=0,
                                         pady=(0, 10 if idx == 0 else 0))
            else:
                body.grid_columnconfigure(0, weight=1, uniform="routerpanels")
                body.grid_columnconfigure(1, weight=1, uniform="routerpanels")
                for idx, panel in enumerate(self.router_panel_frames):
                    panel.grid_configure(row=0, column=idx, sticky="nsew",
                                         padx=(0, 6) if idx == 0 else (6, 0), pady=0)
            update_scrollregion()

        def mousewheel(event):
            if getattr(event, "delta", 0):
                self.router_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            elif getattr(event, "num", 0) == 4:
                self.router_canvas.yview_scroll(-1, "units")
            elif getattr(event, "num", 0) == 5:
                self.router_canvas.yview_scroll(1, "units")
            return "break"

        def bind_router_wheel(_event=None):
            self.bind_all("<MouseWheel>", mousewheel)
            self.bind_all("<Button-4>", mousewheel)
            self.bind_all("<Button-5>", mousewheel)

        def unbind_router_wheel(_event=None):
            self.unbind_all("<MouseWheel>")
            self.unbind_all("<Button-4>")
            self.unbind_all("<Button-5>")

        body.bind("<Configure>", update_scrollregion)
        self.router_canvas.bind("<Configure>", update_router_layout)
        self.router_canvas.bind("<Enter>", bind_router_wheel)
        self.router_canvas.bind("<Leave>", unbind_router_wheel)
        body.bind("<Enter>", bind_router_wheel)
        body.bind("<Leave>", unbind_router_wheel)

        # Two logical panels, four router rows each. They stay side-by-side on a wide
        # monitor and become one full-width panel after another on smaller screens.
        panels = []
        for col, title in enumerate(("Routers 01–04", "Routers 05–08")):
            panel = tk.Frame(body, bg="#FFFFFF", highlightthickness=1, highlightbackground="#E6EAF1")
            panel.grid(row=0, column=col, sticky="nsew", padx=(0, 6) if col == 0 else (6, 0))
            panel.grid_columnconfigure(0, weight=1)
            tk.Label(panel, text=title, bg="#FFFFFF", fg="#344159", font=("Segoe UI", 9, "bold")).grid(
                row=0, column=0, sticky="w", padx=14, pady=(11, 7))
            tk.Frame(panel, bg="#EFF2F7", height=1).grid(row=1, column=0, sticky="ew")
            holder = tk.Frame(panel, bg="#FFFFFF")
            holder.grid(row=2, column=0, sticky="nsew", padx=10, pady=10)
            holder.grid_columnconfigure(0, weight=1)
            panel.grid_rowconfigure(2, weight=1)
            self.router_panel_frames.append(panel)
            panels.append(holder)

        for idx, router in enumerate(self.cfg.routers):
            panel_idx = 0 if idx < 4 else 1
            row_idx = idx if idx < 4 else idx - 4
            card = SlotCard(self, panels[panel_idx], router, row_idx, 0)
            self.cards[router.key] = card

        # Apply a first responsive pass after all cards exist.
        self.after_idle(update_router_layout)

        # PROCESS LOG PAGE
        self.log_page.grid_columnconfigure(0, weight=1)
        self.log_page.grid_rowconfigure(1, weight=1)
        log_head = tk.Frame(self.log_page, bg="#F5F7FB")
        log_head.grid(row=0, column=0, sticky="ew", padx=18, pady=(18, 8))
        log_head.grid_columnconfigure(0, weight=1)
        tk.Label(log_head, text="Process Log", bg="#F5F7FB", fg="#2E3A50",
                 font=("Segoe UI", 14, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(log_head, text="Telnet commands, central server responses and test events",
                 bg="#F5F7FB", fg="#98A2B4", font=("Segoe UI", 8)).grid(row=1, column=0, sticky="w", pady=(2, 0))
        tk.Button(log_head, text="CLEAR LOG", command=lambda: self.log.delete("1.0", "end"),
                  bg="#FFFFFF", fg="#617089", relief="flat", bd=0, cursor="hand2",
                  font=("Segoe UI", 8, "bold"), padx=12, pady=6).grid(row=0, column=1, rowspan=2, sticky="e")

        log_card = tk.Frame(self.log_page, bg="#FFFFFF", highlightthickness=1, highlightbackground="#E6EAF1")
        log_card.grid(row=1, column=0, sticky="nsew", padx=18, pady=(0, 18))
        log_card.grid_rowconfigure(0, weight=1)
        log_card.grid_columnconfigure(0, weight=1)
        self.log = scrolledtext.ScrolledText(log_card, font=("Consolas", 9), bg="#FBFCFE", fg="#465269",
                                             insertbackground="#465269", relief="flat", borderwidth=0,
                                             padx=12, pady=12)
        self.log.grid(row=0, column=0, sticky="nsew")
        self.log.configure(state="disabled")

        self._show_page("dashboard")

    def _show_page(self, page: str):
        target = self.dashboard_page if page == "dashboard" else self.log_page
        target.tkraise()
        for key, button in getattr(self, "nav_buttons", {}).items():
            active = (key == page)
            button.configure(bg="#EDF3FF" if active else "#FFFFFF",
                             fg="#4F70E8" if active else "#78849A",
                             font=("Segoe UI", 9, "bold" if active else "normal"))

    def log_line(self, slot: str, text: str):
        self.log.configure(state="normal")
        self.log.insert("end", f"[{time.strftime('%H:%M:%S')}] [{slot}] {text}\n")
        self.log.see("end")
        self.log.configure(state="disabled")
        card = self.cards.get(slot)
        if card:
            card.add_log(text)

    def show_router_log(self, card: SlotCard):
        win = tk.Toplevel(self)
        win.title(f"{card.router.name} | Live Verification Log")
        win.geometry("940x580")
        win.minsize(720, 440)
        win.configure(bg="#F5F7FB")
        head = tk.Frame(win, bg="#FFFFFF", highlightthickness=1, highlightbackground="#E5EAF2")
        head.pack(fill="x", padx=14, pady=(14, 8))
        tk.Label(head, text=f"{card.router.name}  ·  {card.router.ip}", bg="#FFFFFF", fg="#29364B",
                 font=("Segoe UI", 13, "bold")).pack(side="left", padx=14, pady=12)
        tk.Label(head, textvariable=card.status_var, bg="#EDF3FF", fg="#4F70E8",
                 font=("Segoe UI", 9, "bold"), padx=12, pady=6).pack(side="right", padx=14, pady=10)
        viewer = scrolledtext.ScrolledText(win, font=("Consolas", 9), bg="#FFFFFF", fg="#465269",
                                           relief="flat", padx=12, pady=12, wrap="word")
        viewer.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        viewer.insert("1.0", "\n".join(card.log_history) or "No log entries for this router yet.")
        viewer.configure(state="disabled")

    def emit(self, slot: str, text: str): self.events.put(("log",slot,text))

    def apply_serial_scan(self, card: SlotCard, raw_value: str, source: str = "MANUAL/USB", quiet: bool = False) -> bool:
        if not self.cfg.serial_scan_enabled:
            if not quiet:
                messagebox.showinfo("Serial scan disabled", "Enable [SERIAL_SCAN] in config.ini to use serial label scanning.")
            return False
        if card.state not in {"IDLE", "PASS", "FAIL", "ERROR"}:
            if not quiet:
                messagebox.showwarning("Router busy", f"{card.router.name} is in an active verification stage. Finish or fail the cycle before replacing its serial scan.")
            return False
        try:
            serial = extract_scanned_serial(raw_value, self.cfg)
            if self.cfg.serial_scan_duplicate_block:
                for other in self.cards.values():
                    if other is card or not other.scanned_serial_value:
                        continue
                    if serials_equal(serial, other.scanned_serial_value, self.cfg.serial_scan_case_sensitive):
                        raise AppError(f"Serial {serial} is already assigned to {other.router.name} ({other.router.ip})")
        except Exception as exc:
            if not quiet:
                messagebox.showerror("Invalid serial scan", str(exc))
            self.log_line(card.router.key, f"SERIAL SCAN REJECTED: {exc}")
            return False

        # A new scan after a completed cycle prepares the slot for the next PCB.
        if card.state in {"PASS", "FAIL", "ERROR"}:
            card.reset_cycle(keep_scan=False)
            card.set_state("IDLE", "New label serial captured. Ready to start verification.", "READY")
        card.set_scanned_serial(serial, source)
        card.detail_var.set("Label serial captured. Start verification when the matching PCB is connected.")
        self.log_line(card.router.key, f"SERIAL SCAN {source}: {serial}")
        self._save_card(card)
        if self.cfg.serial_scan_auto_start:
            self.after(150, lambda c=card: self.start_data(c))
        return True

    def start_camera_for_card(self, card: SlotCard):
        if not self.cfg.serial_scan_enabled:
            messagebox.showinfo("Serial scan disabled", "Enable [SERIAL_SCAN] in config.ini to use the camera scanner.")
            return
        if self.camera_running:
            messagebox.showwarning("Camera busy", "The automatic camera scanner is already running. Stop it before starting a per-router scan.")
            return
        self.camera_stop.clear()
        self.camera_running = True
        self.camera_target_slot = card.router.key
        self.camera_button.configure(text="CAMERA BUSY", state="disabled")
        card.scan_status_var.set("CAMERA SCANNING")
        card.scan_status_label.configure(bg="#E8F5FF", fg="#2587C7")
        self.log_line(card.router.key, "Camera scanner opened. Hold the serial barcode or QR code in front of the camera.")
        threading.Thread(target=self._camera_worker, args=(card.router.key, False), daemon=True).start()

    def toggle_auto_camera(self):
        if self.camera_running:
            self.camera_stop.set()
            self.camera_button.configure(text="STOPPING...", state="disabled")
            return
        if not self.cfg.serial_scan_enabled:
            messagebox.showinfo("Serial scan disabled", "Enable [SERIAL_SCAN] in config.ini to use the camera scanner.")
            return
        if not any(c.router.enabled and c.state in {"IDLE", "PASS", "FAIL", "ERROR"} for c in self.cards.values()):
            messagebox.showinfo("No scan slots", "No enabled router slot is currently available for serial scanning.")
            return
        self.camera_stop.clear()
        self.camera_running = True
        self.camera_target_slot = ""
        self.camera_button.configure(text="STOP CAMERA", bg="#FFF0F0", fg="#C43C4A", state="normal")
        self.log_line("CAMERA", "Automatic serial camera started. Each new code will be assigned to the next available router slot.")
        threading.Thread(target=self._camera_worker, args=("", True), daemon=True).start()

    def _camera_worker(self, target_slot: str, continuous: bool):
        window_name = "ETE Solutions India - Serial Scanner"
        try:
            if cv2 is None:
                raise AppError("Camera scanning requires opencv-python. Run: pip install -r requirements.txt")
            capture_backend = cv2.CAP_DSHOW if os.name == "nt" and hasattr(cv2, "CAP_DSHOW") else 0
            cap = cv2.VideoCapture(self.cfg.serial_scan_camera_index, capture_backend) if capture_backend else cv2.VideoCapture(self.cfg.serial_scan_camera_index)
            if not cap.isOpened():
                raise AppError(f"Could not open camera index {self.cfg.serial_scan_camera_index}")
            start_time = time.monotonic()
            last_candidate = ""
            stable_frames = 0
            last_emitted = ""
            last_emitted_at = 0.0
            while not self.camera_stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    raise AppError("Camera frame could not be read")
                decoded = decode_camera_frame(frame)
                candidate = decoded[0].strip() if decoded else ""
                if candidate and candidate == last_candidate:
                    stable_frames += 1
                elif candidate:
                    last_candidate = candidate
                    stable_frames = 1
                else:
                    last_candidate = ""
                    stable_frames = 0

                caption = "AUTO: scan labels in router slot order" if continuous else f"Scan serial for {target_slot}"
                cv2.putText(frame, caption, (18, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2)
                cv2.putText(frame, "ESC/Q to close", (18, 58), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)
                if candidate:
                    cv2.putText(frame, candidate[:60], (18, 88), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
                cv2.imshow(window_name, frame)
                key = cv2.waitKey(1) & 0xFF
                if key in (27, ord("q"), ord("Q")):
                    break

                now = time.monotonic()
                if candidate and stable_frames >= self.cfg.serial_scan_confirm_frames:
                    if candidate != last_emitted or now - last_emitted_at > 2.0:
                        self.events.put(("camera_code", target_slot, (candidate, "CAMERA AUTO" if continuous else "CAMERA")))
                        last_emitted = candidate
                        last_emitted_at = now
                        stable_frames = 0
                        if continuous:
                            start_time = now
                        if not continuous:
                            break
                if not continuous and self.cfg.serial_scan_camera_timeout > 0 and now - start_time >= self.cfg.serial_scan_camera_timeout:
                    raise AppError("Camera scan timed out before a valid serial code was detected")
                if continuous and self.cfg.serial_scan_camera_timeout > 0 and now - start_time >= self.cfg.serial_scan_camera_timeout:
                    # Continuous mode timeout is inactivity/session safety; operator can restart it.
                    break
        except Exception as exc:
            self.events.put(("camera_error", target_slot, str(exc)))
        finally:
            try:
                cap.release()  # type: ignore[name-defined]
            except Exception:
                pass
            if cv2 is not None:
                try:
                    cv2.destroyWindow(window_name)
                except Exception:
                    pass
            self.events.put(("camera_stopped", target_slot, ""))

    def _next_camera_card(self) -> SlotCard | None:
        for router in self.cfg.routers:
            card = self.cards.get(router.key)
            if not card or not router.enabled or card.state not in {"IDLE", "PASS", "FAIL", "ERROR"}:
                continue
            if not card.scanned_serial_value:
                return card
        return None

    def _on_close(self):
        self.camera_stop.set()
        self.destroy()

    def _restore_pending(self):
        for key,data in load_pending().items():
            if key in self.cards: self.cards[key].restore(data)
        self._update_metrics()

    def _save_card(self, card: SlotCard): set_pending(card.router.key,card.pending_payload())

    def handle_action(self, card: SlotCard):
        if card.state in {"IDLE","PASS","FAIL","ERROR"}: self.start_data(card)
        elif card.state=="SERVER_CHECK_PENDING": self.retry_server(card)
        elif card.state=="WAIT_LED": self.led_pass(card)
        elif card.state=="READY_RESET": self.start_reset(card)
        elif card.state=="READY_WPS": self.start_wps(card)
        elif card.state=="READY_USER_MODE": self.start_user_mode(card)
        elif card.state=="REPORT_PENDING": self.retry_report(card)

    def start_all(self):
        started = 0
        missing_scan = 0
        for card in self.cards.values():
            if not card.router.enabled or card.state not in {"IDLE", "PASS", "FAIL", "ERROR"}:
                continue
            if self.cfg.serial_scan_enabled and self.cfg.serial_scan_required and not card.scanned_serial_value:
                missing_scan += 1
                card.set_state("IDLE", "Serial label scan is required before verification.", "SCAN REQUIRED")
                continue
            self.start_data(card)
            started += 1
        if started == 0 and missing_scan:
            messagebox.showwarning("Serial scan required", "Scan or enter the label serial number for each router before pressing START ALL.")

    def start_data(self, card: SlotCard):
        if self.cfg.serial_scan_enabled and self.cfg.serial_scan_required and not card.scanned_serial_value:
            card.set_state("IDLE", "Scan or type the serial number printed on the router label first.", "SCAN REQUIRED")
            card.scan_entry.focus_set()
            return
        card.reset_cycle(keep_scan=True)
        if card.scanned_serial_value or not self.cfg.serial_scan_enabled:
            card.results["scan"] = "PASS"
            card.set_check("scan", "✓")
        card.set_state("DATA_RUNNING", "Connecting and reading MAC, serial, GPON and part number...", "READING")
        card.set_check("data", "…")
        self._save_card(card)
        self._update_metrics()
        self.log_line(card.router.key, f"Verification cycle started. Label serial={card.scanned_serial_value or 'NOT USED'}")
        threading.Thread(target=self._data_worker, args=(card,), daemon=True).start()

    def _validate_identity(self, mac: str, serial: str, gpon: str, part: str):
        validate_mac(mac)
        if self.cfg.serial_valid_regex and not re.fullmatch(self.cfg.serial_valid_regex,serial): raise AppError(f"Serial number format failed validation: {serial}")
        if self.cfg.gpon_valid_regex and not re.fullmatch(self.cfg.gpon_valid_regex,gpon): raise AppError(f"GPON number format failed validation: {gpon}")
        if self.cfg.part_valid_regex and not re.fullmatch(self.cfg.part_valid_regex,part): raise AppError(f"Part number format failed validation: {part}")
        if self.cfg.expected_part_numbers and part.upper() not in {x.upper() for x in self.cfg.expected_part_numbers}: raise AppError(f"Unexpected part number {part}; expected one of {self.cfg.expected_part_numbers}")
        if self.cfg.expected_part_regex and not re.fullmatch(self.cfg.expected_part_regex,part): raise AppError(f"Part number {part} does not match EXPECTED_PART_NUMBER_REGEX")

    def _data_worker(self, card: SlotCard):
        try:
            check_reachable(card.router)
            mac,serial,gpon,part=asyncio.run(read_identity(card.router,self.cfg,lambda m:self.emit(card.router.key,m)))
            self._validate_identity(mac,serial,gpon,part)
            scan_match = True
            if self.cfg.serial_scan_enabled and card.scanned_serial_value:
                scan_match = serials_equal(card.scanned_serial_value, serial, self.cfg.serial_scan_case_sensitive)
            elif self.cfg.serial_scan_enabled and self.cfg.serial_scan_required:
                scan_match = False
            if self.cfg.serial_scan_enabled:
                card.results["scan"] = "PASS" if scan_match else "FAIL"
            self.events.put(("identity",card.router.key,(mac,serial,gpon,part,scan_match)))
            self._server_check_worker(card,mac,serial,gpon,part)
        except Exception as exc:
            self.events.put(("technical_error",card.router.key,str(exc)))

    def _server_check_worker(self, card: SlotCard, mac: str | None=None, serial: str | None=None, gpon: str | None=None, part: str | None=None):
        mac=mac or card.mac_value; serial=serial or card.serial_value; gpon=gpon or card.gpon_value; part=part or card.part_value
        try:
            result=self.server.begin(card.router,card.request_id,mac,serial,gpon,part,card.scanned_serial_value)
        except Exception as exc:
            self.events.put(("server_pending",card.router.key,str(exc)))
            return
        card.verification_id = str(result.get("verification_id", ""))
        if str(result.get("status", "")).upper() in {"PASS", "FAIL", "ERROR"}:
            self.events.put(("already_final", card.router.key, (str(result.get("status")).upper(), "Server already contains the final result for this recovered request.")))
            return
        set_pending(card.router.key, {
            "request_id": card.request_id, "verification_id": card.verification_id,
            "state": "SERVER_CHECK_PENDING", "desired_status": "",
            "mac": mac, "serial": serial, "scanned_serial": card.scanned_serial_value,
            "scan_source": card.scan_source, "gpon": gpon, "part": part, "firmware": card.firmware_value,
            "results": dict(card.results),
            "router_ip": card.router.ip,
        })
        self.events.put(("server_check",card.router.key,result))
        if result.get("allowed"):
            try:
                auto = asyncio.run(run_automated_checks(card.router, self.cfg, lambda m:self.emit(card.router.key,m)))
                self.events.put(("auto_checks", card.router.key, auto))
                if not (auto["wifi"] and auto["bob"] and auto["firmware"]):
                    failed = [name for name in ("wifi", "bob", "firmware") if not auto[name]]
                    self._report_worker(card, "FAIL", "Automated checks failed: " + ", ".join(failed), forced_results={
                        "wifi": "PASS" if auto["wifi"] else "FAIL",
                        "bob": "PASS" if auto["bob"] else "FAIL",
                        "firmware": "PASS" if auto["firmware"] else "FAIL",
                        "firmware_version": auto["firmware_version"],
                    })
                    return
                asyncio.run(send_commands(card.router,self.cfg.led_start_commands,lambda m:self.emit(card.router.key,m)))
                self.events.put(("led_ready",card.router.key,self.cfg.led_instruction))
            except Exception as exc:
                self.events.put(("verified_error",card.router.key,f"Calibration/firmware/LED test start failed: {exc}"))
        else:
            reasons=", ".join(result.get("reasons",[])) or "Server validation failed"
            self._report_worker(card,"FAIL",f"Central MAC validation failed: {reasons}",forced_results={"server":"FAIL"})

    def retry_server(self, card: SlotCard):
        card.set_state("DATA_RUNNING","Retrying central server validation...","SERVER"); self._update_metrics()
        threading.Thread(target=self._server_check_worker,args=(card,),daemon=True).start()

    def led_pass(self, card: SlotCard):
        card.results["led"]="PASS"; card.set_check("led","✓"); card.set_state("LED_STARTING","Turning off LED test mode...","LED PASS"); self._save_card(card); self._update_metrics()
        threading.Thread(target=self._led_stop_worker,args=(card,),daemon=True).start()

    def _led_stop_worker(self, card: SlotCard):
        try:
            if self.cfg.led_stop_commands: asyncio.run(send_commands(card.router,self.cfg.led_stop_commands,lambda m:self.emit(card.router.key,m)))
            self.events.put(("ready_reset",card.router.key,self.cfg.reset_instruction))
        except Exception as exc: self.events.put(("verified_error",card.router.key,f"LED cleanup failed: {exc}"))

    def start_reset(self, card: SlotCard):
        card.set_state("RESET_RUNNING",self.cfg.reset_instruction,"PRESS RESET NOW"); card.set_check("reset","…"); self._save_card(card); self._update_metrics()
        threading.Thread(target=self._button_worker,args=(card,"reset"),daemon=True).start()

    def start_wps(self, card: SlotCard):
        card.set_state("WPS_RUNNING",self.cfg.wps_instruction,"PRESS WPS NOW"); card.set_check("wps","…"); self._save_card(card); self._update_metrics()
        threading.Thread(target=self._button_worker,args=(card,"wps"),daemon=True).start()

    def _button_worker(self, card: SlotCard, kind: str):
        try:
            if kind=="reset": prepare,checks,regex,timeout,interval,disconnect_pass,passive=self.cfg.reset_prepare_commands,self.cfg.reset_check_commands,self.cfg.reset_pass_regex,self.cfg.reset_timeout,self.cfg.reset_poll_interval,self.cfg.reset_disconnect_is_pass,self.cfg.reset_passive_monitor
            else: prepare,checks,regex,timeout,interval,disconnect_pass,passive=self.cfg.wps_prepare_commands,self.cfg.wps_check_commands,self.cfg.wps_pass_regex,self.cfg.wps_timeout,self.cfg.wps_poll_interval,self.cfg.wps_disconnect_is_pass,self.cfg.wps_passive_monitor
            passed,_=asyncio.run(button_test(card.router,prepare,checks,regex,timeout,interval,lambda m:self.emit(card.router.key,m),disconnect_pass,passive))
            self.events.put(("button_result",card.router.key,(kind,passed)))
        except Exception as exc: self.events.put(("verified_error",card.router.key,f"{kind.upper()} test error: {exc}"))

    def start_user_mode(self, card: SlotCard):
        card.set_state("USER_MODE_RUNNING","Applying final user-mode commands...","FINALIZING"); card.set_check("user","…"); self._save_card(card); self._update_metrics()
        threading.Thread(target=self._user_worker,args=(card,),daemon=True).start()

    def _user_worker(self, card: SlotCard):
        try:
            output=asyncio.run(send_commands(card.router,self.cfg.user_mode_commands,lambda m:self.emit(card.router.key,m),allow_disconnect=self.cfg.user_mode_allow_disconnect))
            if self.cfg.user_mode_pass_regex and not re.search(self.cfg.user_mode_pass_regex,output,re.MULTILINE):
                raise AppError("User mode PASS_REGEX was not found in response")
            if self.cfg.user_mode_verify_commands:
                delay = max(0.0, self.cfg.user_mode_reconnect_delay)
                if delay:
                    self.emit(card.router.key, f"Waiting {delay:.0f}s for router reboot before user-mode confirmation...")
                    time.sleep(delay)
                deadline = time.monotonic() + max(1.0, self.cfg.user_mode_verify_timeout)
                last_error = None
                verify_output = ""
                while time.monotonic() < deadline:
                    try:
                        verify_output = asyncio.run(send_commands(card.router,self.cfg.user_mode_verify_commands,lambda m:self.emit(card.router.key,m)))
                        last_error = None
                        break
                    except Exception as exc:
                        last_error = exc
                        time.sleep(2.0)
                if last_error is not None:
                    raise AppError(f"Could not reconnect for user-mode confirmation: {last_error}")
                if self.cfg.user_mode_verify_pass_regex and not re.search(self.cfg.user_mode_verify_pass_regex,verify_output,re.MULTILINE):
                    raise AppError("User mode VERIFY_PASS_REGEX was not found after reboot")
            self.events.put(("user_pass",card.router.key,"User mode applied and confirmed"))
            self._report_worker(card,"PASS","Identity, Wi-Fi calibration, BOB calibration, firmware and physical tests passed; user mode applied and confirmed", forced_results={"user":"PASS"})
        except Exception as exc: self.events.put(("verified_error",card.router.key,f"User mode failed: {exc}"))

    def reject_slot(self, card: SlotCard):
        stage = card.state
        reason=f"Operator failed PCB during {stage.replace('_',' ')}"
        if stage=="WAIT_LED": card.results["led"]="FAIL"; card.set_check("led","✕")
        elif stage=="READY_RESET": card.results["reset"]="FAIL"; card.set_check("reset","✕")
        elif stage=="READY_WPS": card.results["wps"]="FAIL"; card.set_check("wps","✕")
        elif stage=="READY_USER_MODE": card.results["user"]="FAIL"; card.set_check("user","✕")
        card.set_state("REPORTING","Cleaning test mode and reporting FAIL to server...","REPORTING"); self._save_card(card); self._update_metrics()
        threading.Thread(target=self._reject_worker,args=(card,stage,reason),daemon=True).start()

    def _reject_worker(self, card: SlotCard, stage: str, reason: str):
        try:
            if stage == "WAIT_LED" and self.cfg.led_stop_commands:
                asyncio.run(send_commands(card.router,self.cfg.led_stop_commands,lambda m:self.emit(card.router.key,m)))
        except Exception as exc:
            reason += f" | LED cleanup error: {exc}"
        self._report_worker(card,"FAIL",reason)

    def _report_worker(self, card: SlotCard, status: str, detail: str, forced_results: dict | None=None):
        card.desired_status = status
        set_pending(card.router.key, card.pending_payload())
        try:
            results=dict(card.results)
            if forced_results: results.update(forced_results)
            if not card.verification_id:
                self.events.put(("final",card.router.key,(status,detail))); return
            response=self.server.report(card.router,card.verification_id,status,results,detail)
            self.events.put(("reported",card.router.key,(status,detail,response)))
        except Exception as exc:
            card.desired_status=status
            self.events.put(("report_pending",card.router.key,(detail,str(exc))))

    def retry_report(self, card: SlotCard):
        card.set_state("REPORTING","Retrying final report to central server...","REPORTING"); self._update_metrics()
        threading.Thread(target=self._report_worker,args=(card,card.desired_status or "ERROR","Retry of interrupted/final report"),daemon=True).start()

    def _technical_after_server(self, card: SlotCard, message: str):
        if card.verification_id:
            card.set_state("REPORTING","Reporting technical ERROR to server...","ERROR"); self._save_card(card)
            threading.Thread(target=self._report_worker,args=(card,"ERROR",message),daemon=True).start()
        else:
            card.set_check("data","✕"); card.set_state("ERROR",message,"ERROR"); set_pending(card.router.key,None)

    def _poll_events(self):
        try:
            while True:
                event,slot,payload=self.events.get_nowait()
                if event=="log": self.log_line(slot,payload); continue
                if event=="server_status":
                    status,detail,stats=payload; self.server_var.set(status); self.server_verified.set(str(stats.get("verify_pass",0))); self.log_line("SERVER",str(detail)); continue
                if event=="camera_code":
                    raw_value, source = payload
                    card = self.cards.get(slot) if slot else self._next_camera_card()
                    if card:
                        accepted = self.apply_serial_scan(card, raw_value, source, quiet=True)
                        if not accepted:
                            self.log_line("CAMERA", f"Rejected camera value: {raw_value}")
                    else:
                        self.log_line("CAMERA", "All available router slots already have serial scans. Camera stopped.")
                        self.camera_stop.set()
                    continue
                if event=="camera_error":
                    target = self.cards.get(slot)
                    if target:
                        target.scan_status_var.set("CAMERA ERROR")
                        target.scan_status_label.configure(bg="#FFF0F0", fg="#C43C4A")
                        self.log_line(slot, f"CAMERA ERROR: {payload}")
                    else:
                        self.log_line("CAMERA", f"CAMERA ERROR: {payload}")
                    messagebox.showerror("Camera scanner", str(payload))
                    continue
                if event=="camera_stopped":
                    self.camera_running = False
                    self.camera_target_slot = ""
                    self.camera_button.configure(text="AUTO CAMERA", state="normal", bg="#E8F7FB", fg="#2489A7")
                    target = self.cards.get(slot)
                    if target and not target.scanned_serial_value:
                        target.scan_status_var.set("SCAN REQUIRED" if self.cfg.serial_scan_required else "OPTIONAL")
                        target.scan_status_label.configure(bg="#FFF7E6", fg="#B77A12")
                    continue
                card=self.cards.get(slot)
                if not card: continue
                if event=="identity":
                    mac,serial,gpon,part,scan_match=payload
                    card.set_identity(mac,serial,gpon,part)
                    card.results["data"]="PASS"; card.set_check("data","✓")
                    if self.cfg.serial_scan_enabled:
                        card.results["scan"]="PASS" if scan_match else "FAIL"
                        card.set_check("scan","✓" if scan_match else "✕")
                        if scan_match:
                            self.log_line(slot, f"SERIAL MATCH PASS: label={card.scanned_serial_value or 'OPTIONAL'} router={serial}")
                        else:
                            self.log_line(slot, f"SERIAL MATCH FAIL: label={card.scanned_serial_value or 'MISSING'} router={serial}")
                    card.set_state("DATA_RUNNING","Identity read. Checking central server...","SERVER CHECK"); self._save_card(card)
                elif event=="server_check":
                    card.verification_id=payload.get("verification_id","")
                    if payload.get("allowed"):
                        card.results["server"]="PASS"; card.set_check("server","✓")
                        if payload.get("serial_scan_result") == "PASS":
                            card.results["scan"]="PASS"; card.set_check("scan","✓")
                        card.set_state("AUTO_CHECK_RUNNING","Writer identity and scanned serial match the server. Checking Wi-Fi calibration, BOB calibration and firmware...","AUTO CHECK")
                    else:
                        reasons = ", ".join(payload.get("reasons", [])) or "Server validation failed"
                        card.results["server"]="FAIL"; card.set_check("server","✕")
                        if "scan" in reasons.lower() or "scanned serial" in reasons.lower():
                            card.results["scan"]="FAIL"; card.set_check("scan","✕")
                        card.set_state("REPORTING",f"Central server rejected verification: {reasons}","SERVER FAIL")
                    self._save_card(card)
                elif event=="auto_checks":
                    card.results["wifi"]="PASS" if payload["wifi"] else "FAIL"; card.set_check("wifi","✓" if payload["wifi"] else "✕")
                    card.results["bob"]="PASS" if payload["bob"] else "FAIL"; card.set_check("bob","✓" if payload["bob"] else "✕")
                    card.results["firmware"]="PASS" if payload["firmware"] else "FAIL"; card.set_check("firmware","✓" if payload["firmware"] else "✕")
                    card.results["firmware_version"]=payload["firmware_version"]; card.set_firmware(payload["firmware_version"])
                    if payload["wifi"] and payload["bob"] and payload["firmware"]:
                        card.set_state("LED_STARTING","Automated calibration and firmware checks passed. Starting LED test...","LED TEST")
                    else:
                        card.set_state("REPORTING","Automated calibration or firmware check failed. Reporting FAIL...","FAIL")
                    self._save_card(card)
                elif event=="led_ready":
                    card.set_state("WAIT_LED",payload,"CHECK LEDS"); self._save_card(card)
                elif event=="ready_reset":
                    card.set_state("READY_RESET",payload,"READY RESET"); self._save_card(card)
                elif event=="button_result":
                    kind,passed=payload
                    if passed:
                        card.results[kind]="PASS"; card.set_check(kind,"✓")
                        if kind=="reset": card.set_state("READY_WPS",self.cfg.wps_instruction,"READY WPS")
                        else: card.set_state("READY_USER_MODE","All checks passed. Apply final user mode.","READY FINAL")
                        self._save_card(card)
                    else:
                        card.results[kind]="FAIL"; card.set_check(kind,"✕"); card.set_state("REPORTING",f"{kind.upper()} button was not detected. Reporting FAIL...","FAIL")
                        self._save_card(card); threading.Thread(target=self._report_worker,args=(card,"FAIL",f"{kind.upper()} button not detected within configured timeout"),daemon=True).start()
                elif event=="user_pass":
                    card.results["user"]="PASS"; card.set_check("user","✓"); self._save_card(card)
                elif event=="server_pending":
                    card.set_state("SERVER_CHECK_PENDING",f"Server unavailable: {payload}. Identity is saved; retry will use the same request ID.","SERVER OFFLINE"); self._save_card(card)
                elif event in {"technical_error","verified_error"}:
                    self._technical_after_server(card,payload)
                elif event=="reported":
                    status,detail,response=payload; card.set_state(status,detail,status); set_pending(card.router.key,None)
                    self.log_line(slot, f"FINAL RESULT: {status} | {detail}")
                    if response.get("stats"): self.server_verified.set(str(response["stats"].get("verify_pass",0)))
                elif event=="already_final":
                    status,detail=payload; card.set_state(status,detail,status); set_pending(card.router.key,None); self.log_line(slot, f"FINAL RESULT: {status} | {detail}")
                elif event=="final":
                    status,detail=payload; card.set_state(status,detail,status); set_pending(card.router.key,None); self.log_line(slot, f"FINAL RESULT: {status} | {detail}")
                elif event=="report_pending":
                    detail,error_text=payload; card.set_state("REPORT_PENDING",f"Result saved locally but server report failed: {error_text}","REPORT PENDING"); card.desired_status=card.desired_status or "ERROR"; self._save_card(card)
                self._update_metrics()
        except queue.Empty:
            pass
        self.after(100,self._poll_events)

    def _update_metrics(self):
        states=[c.state for c in self.cards.values()]
        self.local_pass.set(str(states.count("PASS"))); self.local_fail.set(str(states.count("FAIL")+states.count("ERROR")))
        active=sum(1 for s in states if s not in {"IDLE","PASS","FAIL","ERROR","DISABLED"}); self.active_var.set(str(active))

    def test_server(self):
        self.server_var.set("CHECKING")
        def work():
            try:
                health=self.server.health(); stats=self.server.stats(); self.events.put(("server_status","",("ONLINE",health,stats)))
            except Exception as exc: self.events.put(("server_status","",("OFFLINE",str(exc),{})))
        threading.Thread(target=work,daemon=True).start()

    def open_config(self):
        try:
            if os.name=="nt": os.startfile(CONFIG_PATH)  # type: ignore[attr-defined]
            else: subprocess.Popen(["xdg-open",str(CONFIG_PATH)])
        except Exception as exc: messagebox.showerror("Open config",str(exc))

    def reload_config(self):
        if any(c.state not in {"IDLE","PASS","FAIL","ERROR","DISABLED"} for c in self.cards.values()):
            messagebox.showwarning("Reload blocked","Finish or report all active verification jobs before reloading config."); return
        try:
            new_cfg=load_config(CONFIG_PATH)
            if [(r.ip,r.enabled) for r in new_cfg.routers] != [(r.ip,r.enabled) for r in self.cfg.routers]:
                messagebox.showinfo("Restart required","Router slot/IP changes were loaded, but restart the application to rebuild the 8-slot screen.")
            same_layout = [(r.ip,r.enabled) for r in new_cfg.routers] == [(r.ip,r.enabled) for r in self.cfg.routers]
            if same_layout:
                by_slot = {r.slot: r for r in new_cfg.routers}
                for card in self.cards.values():
                    card.router = by_slot[card.router.slot]
            self.cfg=new_cfg; self.server=ServerClient(new_cfg); messagebox.showinfo("Config reloaded","Commands, regex patterns, credentials and server settings were reloaded." if same_layout else "Command/server settings were reloaded. Restart is required for router slot/IP changes.")
        except Exception as exc: messagebox.showerror("Config error",str(exc))


if __name__ == "__main__":
    try:
        VerifierApp().mainloop()
    except Exception as exc:
        try:
            root=tk.Tk(); root.withdraw(); messagebox.showerror("ETE Router Verifier",str(exc)); root.destroy()
        except Exception:
            print(exc)
