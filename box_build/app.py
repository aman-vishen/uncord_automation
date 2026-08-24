from __future__ import annotations

import configparser
import json
import re
import socket
import threading
import uuid
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from urllib import error as urlerror, request as urlrequest
import tkinter as tk
from tkinter import messagebox, ttk

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.ini"
APP_VERSION = "13.7"

BRAND_DARK = "#185890"
BRAND_BLUE = "#2898D0"
BRAND_BG = "#F2F8FC"
BRAND_CARD = "#FFFFFF"
BRAND_TEXT = "#17324A"
BRAND_MUTED = "#5C7387"
GREEN = "#1F9D62"
RED = "#D84949"
AMBER = "#D89519"


def normalize_mac(value: str) -> str:
    n = re.sub(r"[^0-9A-Fa-f]", "", value or "").upper()
    if len(n) != 12 or not re.fullmatch(r"[0-9A-F]{12}", n):
        raise ValueError(f"Invalid MAC: {value}")
    return ":".join(n[i:i + 2] for i in range(0, 12, 2))


def compact(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", value or "").upper()


def parse_label(raw: str) -> dict[str, str]:
    """Extract MAC/SERIAL/GPON when the scanner payload exposes explicit fields.

    The application also validates against the server record, so a label payload can be
    JSON, key/value text, CSV/pipe/tab/semicolon text, or another string containing the
    three printed values.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("Label scan is empty")
    out = {"mac": "", "serial_number": "", "gpon_number": ""}

    # JSON / QR payloads.
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            aliases = {
                "mac": {"mac", "mac_address", "macaddress"},
                "serial_number": {"serial", "serial_number", "sn", "hwsn"},
                "gpon_number": {"gpon", "gpon_number", "gpon_serial", "gpon_serial_number", "gponsn"},
            }
            lowered = {str(k).strip().lower(): str(v).strip() for k, v in obj.items()}
            for target, keys in aliases.items():
                for key in keys:
                    if key in lowered and lowered[key]:
                        out[target] = lowered[key]
                        break
    except Exception:
        pass

    # Named key/value text: MAC=..., Serial: ..., GPON=...
    patterns = {
        "mac": r"(?i)\bMAC(?:\s*ADDRESS)?\s*[:=]\s*([0-9A-F][0-9A-F:\-.]{10,20})",
        "serial_number": r"(?i)\b(?:SERIAL(?:\s*NUMBER)?|HWSN|SN)\s*[:=]\s*([A-Z0-9_.\-/]+)",
        "gpon_number": r"(?i)\b(?:GPON(?:\s*(?:SERIAL|SN|NUMBER))?|GPONSN)\s*[:=]\s*([A-Z0-9_.\-/]+)",
    }
    for key, pattern in patterns.items():
        if not out[key]:
            m = re.search(pattern, text)
            if m:
                out[key] = m.group(1).strip()

    # Common delimited QR format: MAC,SERIAL,GPON.
    if not (out["mac"] and out["serial_number"] and out["gpon_number"]):
        for sep in (",", "|", "\t", ";"):
            if sep not in text:
                continue
            parts = [x.strip() for x in text.split(sep) if x.strip()]
            if len(parts) >= 3:
                try:
                    candidate_mac = normalize_mac(parts[0])
                except ValueError:
                    continue
                out.setdefault("mac", "")
                if not out["mac"]:
                    out["mac"] = candidate_mac
                if not out["serial_number"]:
                    out["serial_number"] = parts[1]
                if not out["gpon_number"]:
                    out["gpon_number"] = parts[2]
                break

    # Last-resort MAC extraction. Prefer a separated MAC over a bare 12-hex token.
    if not out["mac"]:
        m = re.search(r"(?i)(?<![0-9A-F])(?:[0-9A-F]{2}[:-]){5}[0-9A-F]{2}(?![0-9A-F])", text)
        if not m:
            m = re.search(r"(?i)(?<![0-9A-F])[0-9A-F]{12}(?![0-9A-F])", text)
        if m:
            out["mac"] = normalize_mac(m.group(0))

    if out["mac"]:
        out["mac"] = normalize_mac(out["mac"])
    return out


class ServerClient:
    def __init__(self, cfg: configparser.SectionProxy):
        self.base_url = cfg.get("URL", "http://127.0.0.1:8765").strip().rstrip("/")
        self.api_key = cfg.get("API_KEY", "change-this-key").strip()
        self.station_id = cfg.get("STATION_ID", "BOX-BUILD-01").strip() or "BOX-BUILD-01"
        self.timeout = max(2, cfg.getint("TIMEOUT_SECONDS", 10))

    def post(self, path: str, payload: dict) -> dict:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urlrequest.Request(
            self.base_url + path,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-API-Key": self.api_key,
                "X-Station-ID": self.station_id,
                "X-Hostname": socket.gethostname(),
                "X-App-Version": APP_VERSION,
            },
        )
        try:
            with urlrequest.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urlerror.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8"))
                detail = body.get("error") or str(body)
            except Exception:
                detail = str(exc)
            raise RuntimeError(detail) from exc
        except Exception as exc:
            raise RuntimeError(f"Cannot connect to central server: {exc}") from exc
        if not body.get("ok", False):
            raise RuntimeError(body.get("error", "Server rejected request"))
        return body


class BoxBuildApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ETE Solutions India | Box Build Traceability")
        self.geometry("1180x760")
        self.minsize(940, 650)
        self.configure(bg=BRAND_BG)

        cfg = configparser.ConfigParser()
        cfg.read(CONFIG_PATH, encoding="utf-8")
        if "SERVER" not in cfg:
            raise RuntimeError(f"Missing [SERVER] section in {CONFIG_PATH}")
        self.client = ServerClient(cfg["SERVER"])
        app_cfg = cfg["BOX_BUILD"] if "BOX_BUILD" in cfg else {}
        self.box_build_before_mac = str(app_cfg.get("BOX_BUILD_BEFORE_MAC_WRITE", "1")).strip().lower() not in {"0", "no", "false", "off"}
        # Production order keeps Box Build before MAC Write. In that mode an older
        # v13.5 config containing REQUIRE_MAC_WRITE_PASS=1 must not block Box Build.
        self.require_writer_pass = (not self.box_build_before_mac) and str(app_cfg.get("REQUIRE_MAC_WRITE_PASS", "0")).strip().lower() in {"1", "yes", "true", "on"}
        self.require_all_label_values = str(app_cfg.get("REQUIRE_ALL_LABEL_VALUES", "1")).strip().lower() in {"1", "yes", "true", "on"}
        self.clear_delay_ms = max(0, int(app_cfg.get("SUCCESS_CLEAR_DELAY_MS", "700") or 700))

        self.queue: Queue = Queue()
        self.current: dict[str, str] | None = None
        self.logo = None
        self.status_var = tk.StringVar(value="READY — Scan product label")
        self.server_var = tk.StringVar(value=f"{self.client.station_id}  •  {self.client.base_url}")
        self.label_var = tk.StringVar()
        self.pcb_var = tk.StringVar()
        self.mac_var = tk.StringVar(value="—")
        self.serial_var = tk.StringVar(value="—")
        self.gpon_var = tk.StringVar(value="—")
        self.existing_pcb_var = tk.StringVar(value="—")
        self.count_var = tk.StringVar(value="0")
        self.success_count = 0
        self.busy = False

        self._style()
        self._build()
        self.after(100, self._poll_queue)
        self.after(250, self.label_entry.focus_set)

    def _style(self):
        s = ttk.Style(self)
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        s.configure("App.TFrame", background=BRAND_BG)
        s.configure("Card.TFrame", background=BRAND_CARD)
        s.configure("Title.TLabel", background=BRAND_BG, foreground=BRAND_DARK, font=("Segoe UI", 22, "bold"))
        s.configure("Sub.TLabel", background=BRAND_BG, foreground=BRAND_MUTED, font=("Segoe UI", 10))
        s.configure("CardTitle.TLabel", background=BRAND_CARD, foreground=BRAND_MUTED, font=("Segoe UI", 9, "bold"))
        s.configure("Value.TLabel", background=BRAND_CARD, foreground=BRAND_TEXT, font=("Segoe UI", 16, "bold"))
        s.configure("Big.TEntry", padding=10, font=("Segoe UI", 15))
        s.configure("Primary.TButton", padding=(16, 10), font=("Segoe UI", 10, "bold"), background=BRAND_DARK, foreground="white")
        s.map("Primary.TButton", background=[("active", BRAND_BLUE)])
        s.configure("Treeview", rowheight=28, font=("Segoe UI", 9))
        s.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))

    def _build(self):
        root = ttk.Frame(self, style="App.TFrame", padding=20)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(4, weight=1)

        head = ttk.Frame(root, style="App.TFrame")
        head.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        head.columnconfigure(0, weight=1)
        ttk.Label(head, text="BOX BUILD TRACEABILITY", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(head, text="Scan the finished-product label, then scan the PCB serial number to create a permanent identity link.", style="Sub.TLabel").grid(row=1, column=0, sticky="w", pady=(3, 0))
        ttk.Label(head, textvariable=self.server_var, style="Sub.TLabel").grid(row=0, column=1, rowspan=2, sticky="e")

        scan_card = ttk.Frame(root, style="Card.TFrame", padding=18)
        scan_card.grid(row=1, column=0, sticky="ew")
        scan_card.columnconfigure(1, weight=1)
        ttk.Label(scan_card, text="STEP 1", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(scan_card, text="SCAN PRODUCT LABEL", style="CardTitle.TLabel").grid(row=0, column=1, sticky="w")
        self.label_entry = ttk.Entry(scan_card, textvariable=self.label_var, style="Big.TEntry")
        self.label_entry.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(7, 13))
        self.label_entry.bind("<Return>", lambda _e: self.validate_label())
        ttk.Label(scan_card, text="STEP 2", style="CardTitle.TLabel").grid(row=2, column=0, sticky="w")
        ttk.Label(scan_card, text="SCAN PCB SERIAL NUMBER", style="CardTitle.TLabel").grid(row=2, column=1, sticky="w")
        self.pcb_entry = ttk.Entry(scan_card, textvariable=self.pcb_var, style="Big.TEntry", state="disabled")
        self.pcb_entry.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(7, 0))
        self.pcb_entry.bind("<Return>", lambda _e: self.bind_pcb())

        identity = ttk.Frame(root, style="App.TFrame")
        identity.grid(row=2, column=0, sticky="ew", pady=14)
        for i in range(4):
            identity.columnconfigure(i, weight=1)
        for i, (label, var) in enumerate([
            ("MAC", self.mac_var), ("SERIAL NUMBER", self.serial_var),
            ("GPON SERIAL NUMBER", self.gpon_var), ("PCB SERIAL NUMBER", self.existing_pcb_var),
        ]):
            card = ttk.Frame(identity, style="Card.TFrame", padding=14)
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 5, 0 if i == 3 else 5))
            ttk.Label(card, text=label, style="CardTitle.TLabel").pack(anchor="w")
            ttk.Label(card, textvariable=var, style="Value.TLabel").pack(anchor="w", pady=(5, 0))

        action = ttk.Frame(root, style="Card.TFrame", padding=14)
        action.grid(row=3, column=0, sticky="ew", pady=(0, 14))
        action.columnconfigure(0, weight=1)
        self.status_label = tk.Label(action, textvariable=self.status_var, bg=BRAND_CARD, fg=BRAND_DARK, font=("Segoe UI", 13, "bold"), anchor="w")
        self.status_label.grid(row=0, column=0, sticky="ew")
        ttk.Label(action, text="Successful links", style="CardTitle.TLabel").grid(row=0, column=1, padx=(16, 5))
        ttk.Label(action, textvariable=self.count_var, style="Value.TLabel").grid(row=0, column=2, padx=(0, 16))
        ttk.Button(action, text="Reset", command=self.reset_scan).grid(row=0, column=3)

        history_card = ttk.Frame(root, style="Card.TFrame", padding=12)
        history_card.grid(row=4, column=0, sticky="nsew")
        history_card.rowconfigure(1, weight=1)
        history_card.columnconfigure(0, weight=1)
        ttk.Label(history_card, text="RECENT BOX BUILD LINKS — THIS SESSION", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.table = ttk.Treeview(history_card, columns=("time", "mac", "serial", "gpon", "pcb", "result"), show="headings")
        for col, title, width in [
            ("time", "TIME", 155), ("mac", "MAC", 150), ("serial", "SERIAL", 170),
            ("gpon", "GPON SERIAL", 170), ("pcb", "PCB SERIAL", 190), ("result", "RESULT", 90),
        ]:
            self.table.heading(col, text=title)
            self.table.column(col, width=width, anchor="w")
        self.table.grid(row=1, column=0, sticky="nsew")
        sb = ttk.Scrollbar(history_card, orient="vertical", command=self.table.yview)
        sb.grid(row=1, column=1, sticky="ns")
        self.table.configure(yscrollcommand=sb.set)

    def _set_status(self, text: str, color: str = BRAND_DARK):
        self.status_var.set(text)
        self.status_label.configure(fg=color)

    def _run(self, fn, *args):
        if self.busy:
            return
        self.busy = True
        self.label_entry.configure(state="disabled")
        self.pcb_entry.configure(state="disabled")

        def worker():
            try:
                self.queue.put(("ok", fn(*args)))
            except Exception as exc:
                self.queue.put(("error", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def validate_label(self):
        raw = self.label_var.get().strip()
        if not raw or self.busy:
            return
        try:
            parsed = parse_label(raw)
            if not parsed["mac"]:
                raise ValueError("Could not find a MAC address in the scanned label")
        except Exception as exc:
            self._fail(str(exc))
            return
        self._set_status("Checking label against central identity list…", AMBER)

        def lookup():
            result = self.client.post("/api/box-build/lookup", {
                "client_id": self.client.station_id,
                "mac": parsed["mac"],
                "serial_number": parsed["serial_number"],
                "gpon_number": parsed["gpon_number"],
            })
            expected_serial = result["serial_number"]
            expected_gpon = result["gpon_number"]
            raw_compact = compact(raw)
            # When explicit parser fields are absent, validate that the server's values
            # are physically present in the scanned label payload.
            if self.require_all_label_values:
                if compact(expected_serial) not in raw_compact:
                    raise RuntimeError("Scanned label does not contain the expected Serial Number")
                if compact(expected_gpon) not in raw_compact:
                    raise RuntimeError("Scanned label does not contain the expected GPON Serial Number")
            if self.require_writer_pass and result.get("writer_state") != "PASS":
                raise RuntimeError(f"MAC Write state is {result.get('writer_state')}; Box Build requires PASS")
            result["raw_label"] = raw
            return ("lookup", result)

        self._run(lookup)

    def bind_pcb(self):
        if self.busy or not self.current:
            return
        pcb = self.pcb_var.get().strip()
        if not pcb:
            self._fail("PCB serial number is empty")
            return
        self._set_status("Linking PCB serial to product identity…", AMBER)
        current = dict(self.current)

        def bind():
            result = self.client.post("/api/box-build/bind", {
                "client_id": self.client.station_id,
                "event_id": str(uuid.uuid4()),
                "mac": current["mac"],
                "serial_number": current["serial_number"],
                "gpon_number": current["gpon_number"],
                "pcb_serial_number": pcb,
                "raw_label": current.get("raw_label", ""),
            })
            return ("bind", result)

        self._run(bind)

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                self.busy = False
                if kind == "error":
                    self.label_entry.configure(state="normal")
                    if self.current:
                        self.pcb_entry.configure(state="normal")
                    self._fail(payload)
                    continue
                action, result = payload
                if action == "lookup":
                    self.current = result
                    self.mac_var.set(result["mac"])
                    self.serial_var.set(result["serial_number"])
                    self.gpon_var.set(result["gpon_number"])
                    self.existing_pcb_var.set(result.get("pcb_serial_number") or "Not linked")
                    self.label_entry.configure(state="disabled")
                    self.pcb_entry.configure(state="normal")
                    self.pcb_var.set("")
                    self._set_status("LABEL VALID — Scan PCB serial number", GREEN)
                    self.after(50, self.pcb_entry.focus_set)
                elif action == "bind":
                    self.success_count += 1
                    self.count_var.set(str(self.success_count))
                    self.existing_pcb_var.set(result["pcb_serial_number"])
                    result_text = "ALREADY LINKED" if result.get("already_bound") else "PASS"
                    self.table.insert("", 0, values=(
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"), result["mac"], result["serial_number"],
                        result["gpon_number"], result["pcb_serial_number"], result_text,
                    ))
                    self._set_status(f"PASS — PCB {result['pcb_serial_number']} linked to {result['mac']}", GREEN)
                    self.after(self.clear_delay_ms, self.reset_scan)
        except Empty:
            pass
        self.after(100, self._poll_queue)

    def _fail(self, message: str):
        self._set_status("FAIL — " + str(message), RED)
        try:
            self.bell()
        except Exception:
            pass

    def reset_scan(self):
        self.busy = False
        self.current = None
        self.label_var.set("")
        self.pcb_var.set("")
        self.mac_var.set("—")
        self.serial_var.set("—")
        self.gpon_var.set("—")
        self.existing_pcb_var.set("—")
        self.label_entry.configure(state="normal")
        self.pcb_entry.configure(state="disabled")
        self._set_status("READY — Scan product label", BRAND_DARK)
        self.after(50, self.label_entry.focus_set)


if __name__ == "__main__":
    try:
        BoxBuildApp().mainloop()
    except Exception as exc:
        messagebox.showerror("Box Build startup failed", str(exc))
