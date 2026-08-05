import asyncio
import json
import os
import queue
import re
import socket
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

APP_VERSION = "11.0-ETE-UNCORD-COMMANDS"
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.txt"
COMMANDS_PATH = BASE_DIR / "commands.txt"
PENDING_PATH = BASE_DIR / "pending_jobs.json"
MAX_ROUTERS = 8
PENDING_LOCK = threading.RLock()

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
    seen_ips: set[str] = set()
    for slot in range(1, count + 1):
        enabled = cfg.get(f"ROUTER_{slot}_ENABLED", "1").strip().lower() not in {"0", "no", "false", "off"}
        ip = cfg.get(f"ROUTER_{slot}_IP", "").strip()
        name = cfg.get(f"ROUTER_{slot}_NAME", f"Router {slot}").strip() or f"Router {slot}"
        if enabled and not ip:
            raise AppError(f"ROUTER_{slot}_IP is required because slot {slot} is enabled")
        if enabled:
            try:
                socket.inet_aton(ip)
            except OSError as exc:
                raise AppError(f"ROUTER_{slot}_IP is not a valid IPv4 address: {ip}") from exc
            if ip in seen_ips:
                raise AppError(f"Duplicate router IP in config: {ip}")
            seen_ips.add(ip)
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
    if not path.exists():
        raise AppError(f"Commands file not found: {path}")
    sections = {"WRITE": [], "VERIFY": [], "FINALIZE": []}
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
    mac = allocation.get("mac", "")
    seq = int(allocation.get("seq", 0))
    base = identifier_formats(mac, seq=seq)
    serial = render_template(cfg.get("SERIAL_TEMPLATE", "SN{SEQ_PAD10}"), base, "SERIAL")
    base["SERIAL"] = serial
    gpon = render_template(cfg.get("GPON_TEMPLATE", "UNCD{SEQ_HEX8}"), base, "GPON")
    serial_regex = cfg.get("SERIAL_VALID_REGEX", r"^[A-Za-z0-9._/-]{4,64}$")
    gpon_regex = cfg.get("GPON_VALID_REGEX", r"^[A-Za-z0-9._/-]{4,64}$")
    if serial_regex and not re.fullmatch(serial_regex, serial):
        raise AppError(f"Generated serial number failed SERIAL_VALID_REGEX: {serial}")
    if gpon_regex and not re.fullmatch(gpon_regex, gpon):
        raise AppError(f"Generated GPON number failed GPON_VALID_REGEX: {gpon}")
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
    try:
        with socket.create_connection((router.ip, router.port), timeout=router.preflight_timeout):
            return
    except OSError as exc:
        raise AppError(
            f"{router.name} is not reachable at {router.ip}:{router.port}. "
            "No MAC was allocated. Check PCB power, switch/VLAN, IP address, and Telnet service."
        ) from exc


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

    def allocate(self, router: RouterTarget, request_id: str) -> dict:
        return self.call("POST", "/api/allocate", {
            "client_id": self._client_id(router),
            "request_id": request_id,
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


async def read_until(reader, prompt: str, timeout: float) -> str:
    if not prompt:
        await asyncio.sleep(0.2)
        chunks = []
        while True:
            try:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=0.15)
            except asyncio.TimeoutError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        return "".join(chunks)
    try:
        return await asyncio.wait_for(reader.readuntil(prompt), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise AppError(f"Timed out waiting for prompt {prompt!r}") from exc


async def telnet_session(router: RouterTarget, mac: str, serial: str, gpon: str, seq: int,
                         write_cmds: list[str], verify_cmds: list[str], finalize_cmds: list[str], emit):
    if telnetlib3 is None:
        raise AppError("Missing dependency 'telnetlib3'. Run: pip install -r requirements.txt")

    emit(f"Connecting to {router.name} at {router.ip}:{router.port} ...")
    try:
        reader, writer = await asyncio.wait_for(
            telnetlib3.open_connection(host=router.ip, port=router.port, connect_minwait=0.05),
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
        "connect": "CONNECT",
        "reserve": "IDENTITY",
        "write": "WRITE",
        "verify": "VERIFY",
        "report": "RPT",
    }

    def __init__(self, app, parent, router: RouterTarget, row: int, column: int):
        self.app = app
        self.router = router
        self.running = False
        self.status = tk.StringVar(value="DISABLED" if not router.enabled else "READY")
        self.mac = tk.StringVar(value="—")
        self.serial = tk.StringVar(value="—")
        self.gpon = tk.StringVar(value="—")
        self.detail = tk.StringVar(value="Disabled in config" if not router.enabled else "Waiting for PCB")
        self.live_log = tk.StringVar(value="No activity yet" if router.enabled else "Slot disabled")
        self.log_history: list[str] = []
        self.step_values = {key: "—" for key in self.STEP_LABELS}
        self.step_widgets: dict[str, tk.Label] = {}

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

        identity_values = tk.Frame(self.frame, bg="#FFFFFF")
        identity_values.grid(row=0, column=2, sticky="w", pady=(9, 2))
        for idx, (label, var) in enumerate((("MAC", self.mac), ("SERIAL", self.serial), ("GPON", self.gpon))):
            block = tk.Frame(identity_values, bg="#FFFFFF")
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
            pill.grid(row=0, column=idx, padx=(0, 4))
            self.step_widgets[key] = pill

        controls = tk.Frame(self.frame, bg="#FFFFFF")
        controls.grid(row=0, column=3, rowspan=2, padx=(8, 12), pady=10, sticky="e")
        self.status_label = tk.Label(controls, textvariable=self.status, bg="#EDF3FF", fg="#4F70E8",
                                     font=("Segoe UI", 8, "bold"), padx=9, pady=4)
        self.status_label.pack(anchor="e", pady=(0, 6))
        self.button = tk.Button(controls, text="PROGRAM PCB", command=self._clicked,
                                bg="#4F70E8", fg="#FFFFFF", activebackground="#3C5DD9",
                                activeforeground="#FFFFFF", relief="flat", bd=0, cursor="hand2",
                                font=("Segoe UI", 8, "bold"), padx=10, pady=6, width=13)
        self.button.pack(anchor="e")
        self.log_button = tk.Button(controls, text="VIEW LOG", command=lambda: self.app.show_router_log(self),
                                    bg="#F2F5FA", fg="#68758A", activebackground="#E7EDF8",
                                    activeforeground="#4F70E8", relief="flat", bd=0, cursor="hand2",
                                    font=("Segoe UI", 7, "bold"), padx=10, pady=4, width=13)
        self.log_button.pack(anchor="e", pady=(5, 0))

        self.detail_label = tk.Label(self.frame, textvariable=self.detail, bg="#FFFFFF", fg="#7E8A9D",
                                     font=("Segoe UI", 7), anchor="w", wraplength=500, justify="left")
        self.detail_label.grid(row=2, column=1, columnspan=3, sticky="ew", padx=(0, 12), pady=(0, 3))
        live_row = tk.Frame(self.frame, bg="#F8FAFD", highlightthickness=1, highlightbackground="#EEF2F7")
        live_row.grid(row=3, column=1, columnspan=3, sticky="ew", padx=(0, 12), pady=(0, 8))
        live_row.grid_columnconfigure(1, weight=1)
        tk.Label(live_row, text="LIVE", bg="#F8FAFD", fg="#4F70E8",
                 font=("Segoe UI", 6, "bold"), padx=6).grid(row=0, column=0, sticky="w")
        self.live_log_label = tk.Label(live_row, textvariable=self.live_log, bg="#F8FAFD", fg="#6D798D",
                                       font=("Consolas", 7), anchor="w")
        self.live_log_label.grid(row=0, column=1, sticky="ew", padx=(2, 6), pady=4)

        if not router.enabled:
            self.button.configure(state="disabled", bg="#C8CED8")
            self._apply_status_style("DISABLED")

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
            bg, fg = "#E9F8F0", "#159260"
        elif mark == "✕":
            bg, fg = "#FFF0F0", "#C43C4A"
        elif mark == "…":
            bg, fg = "#FFF7E6", "#B77A12"
        else:
            bg, fg = "#F4F6FA", "#8D98AA"
        widget.configure(text=f"{self.STEP_LABELS[name]}  {mark}", bg=bg, fg=fg)

    def _apply_status_style(self, state: str):
        if state == "PASS":
            bg, fg = "#E9F8F0", "#159260"
        elif state in {"FAIL", "ERROR", "RECOVERY", "OFFLINE"}:
            bg, fg = "#FFF0F0", "#C43C4A"
        elif state == "PROGRAMMING":
            bg, fg = "#E8F5FF", "#248BC3"
        elif state == "DISABLED":
            bg, fg = "#F0F2F5", "#9AA3B1"
        else:
            bg, fg = "#EDF3FF", "#4F70E8"
        self.status_label.configure(bg=bg, fg=fg)

    def set_state(self, status: str, detail: str | None = None, mac: str | None = None,
                  serial: str | None = None, gpon: str | None = None):
        self.status.set(status)
        if detail is not None:
            self.detail.set(detail)
        if mac is not None:
            self.mac.set(mac)
        if serial is not None:
            self.serial.set(serial)
        if gpon is not None:
            self.gpon.set(gpon)
        self._apply_status_style(status)


class ClientApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ETE Solutions India | 8-Router Identity Programming Station")
        self.geometry("1500x940")
        self.minsize(1180, 760)
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

        try:
            cfg = self._cfg()
            self.routers = parse_router_targets(cfg)
            self.station_var.set(ServerClient(cfg).station_id)
        except Exception as exc:
            messagebox.showerror("Configuration error", str(exc))
            self.routers = []

        self.brand_logo = None
        self._load_brand_assets()
        self._style()
        self._build()
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

        brand = tk.Frame(sidebar, bg="#FFFFFF")
        brand.pack(fill="x", padx=18, pady=(18, 14))
        if self.brand_logo:
            tk.Label(brand, image=self.brand_logo, bg="#FFFFFF").pack(anchor="w")
        else:
            tk.Label(brand, text="ETE", bg="#4F70E8", fg="#FFFFFF",
                     font=("Segoe UI", 13, "bold"), padx=9, pady=6).pack(anchor="w")
        tk.Label(brand, text="MAC WRITER", bg="#FFFFFF", fg="#A0A9B8",
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
        nav_button("server", "●   Refresh Server", self._check_server_async)

        tk.Label(sidebar, text="CONFIGURATION", bg="#FFFFFF", fg="#A7AFBD",
                 font=("Segoe UI", 7, "bold")).pack(anchor="w", padx=20, pady=(20, 8))
        nav_button("config", "⚙   Open Config", self._open_config)
        nav_button("commands", "⌘   Open Commands", self._open_commands)

        spacer = tk.Frame(sidebar, bg="#FFFFFF")
        spacer.pack(fill="both", expand=True)
        station_box = tk.Frame(sidebar, bg="#EDF3FF")
        station_box.pack(fill="x", padx=14, pady=14)
        tk.Label(station_box, text="STATION", bg="#EDF3FF", fg="#8B99AF",
                 font=("Segoe UI", 7, "bold")).pack(anchor="w", padx=12, pady=(10, 1))
        tk.Label(station_box, textvariable=self.station_var, bg="#EDF3FF", fg="#3D5FCA",
                 font=("Segoe UI", 8, "bold"), wraplength=145, justify="left").pack(anchor="w", padx=12)
        tk.Label(station_box, text=f"Writer {APP_VERSION}", bg="#EDF3FF", fg="#9AA6BA",
                 font=("Segoe UI", 7)).pack(anchor="w", padx=12, pady=(2, 10))

        header = tk.Frame(main, bg="#FFFFFF", height=72)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        header.grid_columnconfigure(1, weight=1)
        title_wrap = tk.Frame(header, bg="#FFFFFF")
        title_wrap.grid(row=0, column=0, sticky="w", padx=22, pady=13)
        tk.Label(title_wrap, text="Router Identity Programming", bg="#FFFFFF", fg="#29364B",
                 font=("Segoe UI", 15, "bold")).pack(anchor="w")
        tk.Label(title_wrap, text="Central allocation · MAC, Serial and GPON programming · 8 parallel stations",
                 bg="#FFFFFF", fg="#97A1B3", font=("Segoe UI", 8)).pack(anchor="w", pady=(1, 0))

        header_right = tk.Frame(header, bg="#FFFFFF")
        header_right.grid(row=0, column=2, sticky="e", padx=18)
        server_chip = tk.Frame(header_right, bg="#F6F8FC", highlightthickness=1, highlightbackground="#E9EDF4")
        server_chip.pack(side="left", padx=(0, 9))
        tk.Label(server_chip, text="SERVER", bg="#F6F8FC", fg="#A0A9B9",
                 font=("Segoe UI", 7, "bold")).pack(side="left", padx=(10, 5), pady=8)
        self.header_server = tk.Label(server_chip, textvariable=self.server_var, bg="#F6F8FC", fg="#4F70E8",
                                      font=("Segoe UI", 8, "bold"))
        self.header_server.pack(side="left", padx=(0, 10), pady=8)
        self.all_btn = tk.Button(header_right, text="PROGRAM ALL", command=self.start_all,
                                 bg="#4F70E8", fg="#FFFFFF", activebackground="#3D5FD6",
                                 activeforeground="#FFFFFF", relief="flat", bd=0, cursor="hand2",
                                 font=("Segoe UI", 8, "bold"), padx=18, pady=8)
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
        metrics.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 10))
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
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 4, 0 if i == 6 else 4))
            top = tk.Frame(card, bg="#FFFFFF")
            top.pack(fill="x", padx=10, pady=(9, 2))
            tk.Label(top, text=label, bg="#FFFFFF", fg="#A3ACBB", font=("Segoe UI", 7, "bold")).pack(side="left")
            tk.Label(top, text=icon_text, bg=accent, fg="#FFFFFF", font=("Segoe UI", 7, "bold"),
                     width=2, pady=2).pack(side="right")
            tk.Label(card, textvariable=var, bg="#FFFFFF", fg="#263247",
                     font=("Segoe UI", 14, "bold")).pack(anchor="w", padx=10, pady=(0, 9))

        helper = tk.Frame(dash, bg="#F5F7FB")
        helper.grid(row=1, column=0, sticky="ew", padx=18, pady=(2, 8))
        helper.grid_columnconfigure(0, weight=1)
        tk.Label(helper, text="Router Programming Progress", bg="#F5F7FB", fg="#2E3A50",
                 font=("Segoe UI", 11, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(helper, text="Each router advances through Connect → Identity Allocation → Write → Verify → Server Report",
                 bg="#F5F7FB", fg="#98A2B4", font=("Segoe UI", 8)).grid(row=1, column=0, sticky="w", pady=(2, 0))
        tk.Button(helper, text="Refresh Server", command=self._check_server_async, bg="#FFFFFF", fg="#617089",
                  activebackground="#EDF2FA", relief="flat", bd=0, cursor="hand2",
                  font=("Segoe UI", 8, "bold"), padx=11, pady=6).grid(row=0, column=1, rowspan=2, sticky="e")

        body = tk.Frame(dash, bg="#F5F7FB")
        body.grid(row=2, column=0, sticky="nsew", padx=18, pady=(0, 16))
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

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
            panels.append(holder)

        for idx, router in enumerate(self.routers):
            panel_idx = 0 if idx < 4 else 1
            row_idx = idx if idx < 4 else idx - 4
            card = SlotCard(self, panels[panel_idx], router, row_idx, 0)
            self.cards[router.key] = card

        self.log_page.grid_columnconfigure(0, weight=1)
        self.log_page.grid_rowconfigure(1, weight=1)
        log_head = tk.Frame(self.log_page, bg="#F5F7FB")
        log_head.grid(row=0, column=0, sticky="ew", padx=18, pady=(18, 8))
        log_head.grid_columnconfigure(0, weight=1)
        tk.Label(log_head, text="Process Log", bg="#F5F7FB", fg="#2E3A50",
                 font=("Segoe UI", 14, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(log_head, text="Telnet commands, MAC/Serial/GPON assignments and central server responses",
                 bg="#F5F7FB", fg="#98A2B4", font=("Segoe UI", 8)).grid(row=1, column=0, sticky="w", pady=(2, 0))
        tk.Button(log_head, text="CLEAR LOG", command=self._clear_log,
                  bg="#FFFFFF", fg="#617089", relief="flat", bd=0, cursor="hand2",
                  font=("Segoe UI", 8, "bold"), padx=12, pady=6).grid(row=0, column=1, rowspan=2, sticky="e")

        log_card = tk.Frame(self.log_page, bg="#FFFFFF", highlightthickness=1, highlightbackground="#E6EAF1")
        log_card.grid(row=1, column=0, sticky="nsew", padx=18, pady=(0, 18))
        log_card.grid_rowconfigure(0, weight=1)
        log_card.grid_columnconfigure(0, weight=1)
        self.log = scrolledtext.ScrolledText(log_card, font=("Consolas", 9), bg="#FBFCFE", fg="#465269",
                                             insertbackground="#465269", relief="flat", borderwidth=0,
                                             padx=12, pady=12, wrap="word")
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

    def start_all(self):
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

    def start_router(self, router: RouterTarget, quiet: bool = False) -> bool:
        if not router.enabled or router.key in self.running_slots:
            return False
        if self.has_pending(router.key):
            if not quiet:
                self.resolve_pending(router)
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
            "request_id": request_id,
            "state": "REQUESTING",
            "created_at": time.time(),
        })
        self.running_slots.add(router.key)
        self.active_var.set(str(len(self.running_slots)))
        card = self.cards[router.key]
        card.running = True
        card.reset_steps()
        card.set_step("connect", "…")
        card.set_state("PROGRAMMING", "Checking router connection before identity allocation", "ALLOCATING...", "—", "—")
        card.button.configure(state="disabled", text="PROGRAMMING...")
        self._append(router.key, f"Cycle started for {router.name} {router.ip}")
        threading.Thread(
            target=self._cycle_worker,
            args=(router, client, request_id, cfg, write_cmds, verify_cmds, finalize_cmds),
            daemon=True,
        ).start()
        return True

    def _cycle_worker(self, router: RouterTarget, client: ServerClient, request_id: str, cfg, write_cmds, verify_cmds, finalize_cmds):
        def emit(msg):
            self.events.put(("log", (router.key, msg)))
        reservation_id = None
        mac = None
        serial = ""
        gpon = ""
        try:
            emit(f"Pre-checking Telnet connection to {router.ip}:{router.port} ...")
            try:
                check_router_reachable(router)
            except Exception as exc:
                set_pending_job(router.key, None)
                self.events.put(("router_offline", (router.key, str(exc))))
                return
            self.events.put(("preflight_ok", (router.key,)))
            emit("Router connection pre-check passed. Requesting MAC from server.")
            alloc = client.allocate(router, request_id)
            reservation_id = alloc["reservation_id"]
            mac = alloc["mac"]
            serial, gpon, seq = build_identifiers(alloc, cfg)
            alloc.update({"serial_number": serial, "gpon_number": gpon, "seq": seq})
            set_pending_job(router.key, {
                "slot": router.slot,
                "router_name": router.name,
                "router_ip": router.ip,
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

            passed, checks = asyncio.run(telnet_session(router, mac, serial, gpon, seq, write_cmds, verify_cmds, finalize_cmds, emit))
            self.events.put(("verify_result", (router.key, passed, checks)))
            status = "PASS" if passed else "FAIL"
            detail = "MAC, serial and GPON verified successfully" if passed else f"Identifier verification failed: {checks}"
            report = client.report(router, reservation_id, status, detail, serial, gpon)
            set_pending_job(router.key, None)
            self.events.put(("done", (router.key, mac, serial, gpon, status, report)))
        except Exception as exc:
            error_text = str(exc)
            if reservation_id:
                try:
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
            request_id = pending.get("request_id")
            if not request_id:
                raise AppError("Pending job has no request_id")
            alloc = client.allocate(router, request_id)
            serial, gpon, seq = build_identifiers(alloc, cfg)
            pending.update({
                "reservation_id": alloc["reservation_id"],
                "mac": alloc["mac"],
                "serial_number": serial,
                "gpon_number": gpon,
                "seq": seq,
                "state": "RESERVED",
            })
            set_pending_job(router.key, pending)
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
            args=(router, client, pending, write_cmds, verify_cmds, finalize_cmds),
            daemon=True,
        ).start()

    def _resume_worker(self, router, client, pending, write_cmds, verify_cmds, finalize_cmds):
        def emit(msg):
            self.events.put(("log", (router.key, msg)))
        mac = pending["mac"]
        serial = pending.get("serial_number", "")
        gpon = pending.get("gpon_number", "")
        seq = int(pending.get("seq", 0))
        reservation_id = pending["reservation_id"]
        try:
            passed, checks = asyncio.run(telnet_session(router, mac, serial, gpon, seq, write_cmds, verify_cmds, finalize_cmds, emit))
            self.events.put(("verify_result", (router.key, passed, checks)))
            status = "PASS" if passed else "FAIL"
            report = client.report(router, reservation_id, status, "Recovered interrupted cycle", serial, gpon)
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
        card.button.configure(state="normal", text="PROGRAM PCB")
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
                card.button.configure(state="normal", text="PROGRAM NEXT PCB")
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
                elif kind == "preflight_ok":
                    slot_key, = payload
                    card = self.cards[slot_key]
                    card.set_step("connect", "✓")
                    card.set_step("reserve", "…")
                    card.set_state("PROGRAMMING", "Router online; requesting unique identity from central server", "ALLOCATING...", "—", "—")
                elif kind == "allocated":
                    slot_key, alloc = payload
                    card = self.cards[slot_key]
                    card.set_step("reserve", "✓")
                    card.set_step("write", "…")
                    card.set_state("PROGRAMMING", "Identity reserved; writing MAC, serial and GPON over Telnet",
                                   alloc["mac"], alloc.get("serial_number", "—"), alloc.get("gpon_number", "—"))
                    self._set_stats(alloc.get("stats", {}))
                    self._append(slot_key, f"Reserved MAC={alloc['mac']} SERIAL={alloc.get('serial_number','')} GPON={alloc.get('gpon_number','')} reservation={alloc['reservation_id']}")
                elif kind == "telnet_start":
                    slot_key, = payload
                    self.cards[slot_key].set_step("write", "…")
                elif kind == "verify_result":
                    slot_key, passed, checks = payload
                    card = self.cards[slot_key]
                    card.set_step("write", "✓")
                    card.set_step("verify", "✓" if passed else "✕")
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
                        card.set_state("PASS", "MAC, serial and GPON written and verified. Replace PCB when ready.", mac, serial, gpon)
                    elif status == "FAIL":
                        card.set_step("verify", "✕")
                        card.set_state("FAIL", "Identifier verification failed. This MAC remains blocked.", mac, serial, gpon)
                    else:
                        if card.step_values.get("verify") == "—":
                            card.set_step("write", "✕")
                        card.set_state("ERROR", "Programming error. This identity remains blocked.", mac, serial, gpon)
                    self._append(slot_key, f"CYCLE {status}: {mac}")
                    self._finish_slot(slot_key)
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
