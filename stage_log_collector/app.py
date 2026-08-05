from __future__ import annotations

import configparser
import csv
import hashlib
import io
import json
import os
import re
import socket
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Queue
from urllib import error as urlerror, request as urlrequest
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.ini"
DB_PATH = BASE_DIR / "collector.db"
APP_VERSION = "13.0"

BRAND_DARK = "#185890"
BRAND_BLUE = "#2898D0"
BRAND_DEEP = "#12466F"
BRAND_BG = "#F2F8FC"
BRAND_CARD = "#FFFFFF"
BRAND_TEXT = "#17324A"
BRAND_MUTED = "#5C7387"
GREEN = "#1F9D62"
RED = "#D84949"
AMBER = "#D89519"

STAGE_SECTIONS = [
    ("WIFI_CALIBRATION", "Wi-Fi Calibration"),
    ("LABEL_PRINTING", "Label Printing"),
    ("BOB_CALIBRATION", "BOB Calibration"),
    ("WIFI_COUPLING_VOIP", "Wi-Fi Coupling & VoIP"),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def normalize_mac(value: str) -> str:
    n = re.sub(r"[^0-9A-Fa-f]", "", value or "").upper()
    if not n:
        return ""
    if len(n) != 12 or not re.fullmatch(r"[0-9A-F]{12}", n):
        raise ValueError(f"Invalid MAC: {value}")
    return ":".join(n[i:i + 2] for i in range(0, 12, 2))


def as_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "yes", "true", "on", "enabled"}


def csv_values(value: str) -> set[str]:
    return {x.strip().upper() for x in str(value or "").split(",") if x.strip()}


def parse_timestamp(value: str, fmt: str = "") -> str:
    value = (value or "").strip()
    if not value:
        return now_iso()
    try:
        dt = datetime.strptime(value, fmt) if fmt else datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt.isoformat(timespec="seconds")
    except Exception:
        return now_iso()


@dataclass
class StageConfig:
    key: str
    display_name: str
    enabled: bool
    log_path: Path
    format: str
    start_position: str
    encoding: str
    line_regex: str
    timestamp_format: str
    pass_values: set[str]
    fail_values: set[str]
    error_values: set[str]
    csv_columns: list[str]
    fields: dict[str, str]


class CollectorDatabase:
    def __init__(self, path: Path):
        self.path = path
        self._init()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        return con

    def _init(self) -> None:
        with self.connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS file_state (
                    stage_name TEXT PRIMARY KEY,
                    log_path TEXT NOT NULL,
                    byte_offset INTEGER NOT NULL DEFAULT 0,
                    file_identity TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event_queue (
                    event_id TEXT PRIMARY KEY,
                    stage_name TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    sent_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_collector_queue_status ON event_queue(status, created_at);
                """
            )

    def get_file_state(self, stage: StageConfig) -> tuple[int, str] | None:
        with self.connect() as con:
            row = con.execute("SELECT log_path,byte_offset,file_identity FROM file_state WHERE stage_name=?", (stage.key,)).fetchone()
        if not row or row["log_path"] != str(stage.log_path):
            return None
        return int(row["byte_offset"]), str(row["file_identity"] or "")

    def set_offset(self, stage: StageConfig, offset: int, identity: str = "") -> None:
        with self.connect() as con:
            con.execute(
                """INSERT INTO file_state(stage_name,log_path,byte_offset,file_identity,updated_at)
                   VALUES (?,?,?,?,?) ON CONFLICT(stage_name) DO UPDATE SET
                   log_path=excluded.log_path,byte_offset=excluded.byte_offset,
                   file_identity=excluded.file_identity,updated_at=excluded.updated_at""",
                (stage.key, str(stage.log_path), int(offset), identity, now_iso()),
            )

    def queue_event(self, payload: dict) -> bool:
        ts = now_iso()
        with self.connect() as con:
            cur = con.execute(
                """INSERT OR IGNORE INTO event_queue(event_id,stage_name,payload_json,status,attempts,created_at,updated_at)
                   VALUES (?,?,?,'PENDING',0,?,?)""",
                (payload["event_id"], payload["stage_name"], json.dumps(payload, ensure_ascii=False), ts, ts),
            )
            return cur.rowcount == 1

    def pending(self, limit: int) -> list[dict]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT event_id,payload_json FROM event_queue WHERE status='PENDING' ORDER BY created_at LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [json.loads(r["payload_json"]) for r in rows]

    def mark_sent(self, event_ids: list[str]) -> None:
        if not event_ids:
            return
        ts = now_iso()
        with self.connect() as con:
            con.executemany(
                "UPDATE event_queue SET status='SENT',sent_at=?,updated_at=?,last_error=NULL WHERE event_id=?",
                [(ts, ts, x) for x in event_ids],
            )

    def mark_error(self, event_ids: list[str], message: str) -> None:
        if not event_ids:
            return
        ts = now_iso()
        with self.connect() as con:
            con.executemany(
                "UPDATE event_queue SET attempts=attempts+1,last_error=?,updated_at=? WHERE event_id=?",
                [(message[:2000], ts, x) for x in event_ids],
            )

    def stats(self) -> dict:
        with self.connect() as con:
            overall = {r["status"]: int(r["c"]) for r in con.execute("SELECT status,COUNT(*) c FROM event_queue GROUP BY status")}
            rows = con.execute(
                """SELECT stage_name,
                   SUM(CASE WHEN json_extract(payload_json,'$.status')='PASS' THEN 1 ELSE 0 END) pass,
                   SUM(CASE WHEN json_extract(payload_json,'$.status') IN ('FAIL','ERROR') THEN 1 ELSE 0 END) fail,
                   SUM(CASE WHEN status='PENDING' THEN 1 ELSE 0 END) pending,
                   SUM(CASE WHEN status='SENT' THEN 1 ELSE 0 END) sent,
                   MAX(created_at) last_at
                   FROM event_queue GROUP BY stage_name"""
            ).fetchall()
        return {"overall": overall, "stages": {r["stage_name"]: dict(r) for r in rows}}

    def recent(self, limit: int = 200) -> list[dict]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT status,attempts,last_error,created_at,sent_at,payload_json FROM event_queue ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for row in rows:
            p = json.loads(row["payload_json"])
            p.update({"queue_status": row["status"], "attempts": row["attempts"], "last_error": row["last_error"], "sent_at": row["sent_at"]})
            out.append(p)
        return out


class StageParser:
    def __init__(self, cfg: StageConfig):
        self.cfg = cfg
        self.regex = re.compile(cfg.line_regex) if cfg.format == "REGEX" and cfg.line_regex else None

    def parse(self, line: str) -> dict | None:
        line = line.strip("\r\n")
        if not line.strip():
            return None
        fmt = self.cfg.format
        if fmt == "REGEX":
            if not self.regex:
                raise ValueError("LINE_REGEX is empty")
            match = self.regex.search(line)
            if not match:
                return None
            data = {k: (v or "") for k, v in match.groupdict().items()}
        elif fmt == "JSONL":
            obj = json.loads(line)
            data = {name: str(obj.get(source, "")) for name, source in self.cfg.fields.items()}
        elif fmt == "CSV":
            row = next(csv.reader(io.StringIO(line)))
            if not self.cfg.csv_columns:
                raise ValueError("CSV_COLUMNS is empty")
            values = dict(zip(self.cfg.csv_columns, row))
            data = {name: str(values.get(source, "")) for name, source in self.cfg.fields.items()}
        else:
            raise ValueError(f"Unsupported FORMAT: {fmt}")
        result_raw = str(data.get("result", "")).strip().upper()
        if result_raw in self.cfg.pass_values:
            status = "PASS"
        elif result_raw in self.cfg.fail_values:
            status = "FAIL"
        elif result_raw in self.cfg.error_values:
            status = "ERROR"
        else:
            return None
        return {
            "status": status,
            "mac": normalize_mac(data.get("mac", "")),
            "serial_number": str(data.get("serial", "")).strip(),
            "gpon_number": str(data.get("gpon", "")).strip(),
            "part_number": str(data.get("part", "")).strip(),
            "router_ip": str(data.get("router_ip", "")).strip(),
            "router_slot": str(data.get("router_slot", "")).strip(),
            "detail": str(data.get("detail", "")).strip() or line[:4000],
            "completed_at": parse_timestamp(str(data.get("timestamp", "")), self.cfg.timestamp_format),
        }


class CollectorEngine:
    def __init__(self, ui_queue: Queue):
        self.ui_queue = ui_queue
        self.db = CollectorDatabase(DB_PATH)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self._load_config()

    def _load_config(self) -> None:
        cp = configparser.ConfigParser(interpolation=None)
        if not cp.read(CONFIG_PATH, encoding="utf-8"):
            raise RuntimeError(f"Config not found: {CONFIG_PATH}")
        s = cp["SERVER"]
        self.server_url = s.get("URL", "http://127.0.0.1:8765").rstrip("/")
        self.api_key = s.get("API_KEY", "")
        self.station_id = s.get("STATION_ID", socket.gethostname())
        self.poll_seconds = max(0.5, s.getfloat("POLL_SECONDS", 2.0))
        self.batch_size = max(1, min(500, s.getint("BATCH_SIZE", 100)))
        self.timeout = max(2, s.getint("REQUEST_TIMEOUT_SECONDS", 20))
        self.stages: list[StageConfig] = []
        for key, display in STAGE_SECTIONS:
            sec = cp[key]
            fields = {name: sec.get(f"{name.upper()}_FIELD", name) for name in ["timestamp", "mac", "serial", "gpon", "part", "router_ip", "router_slot", "result", "detail"]}
            self.stages.append(StageConfig(
                key=key, display_name=sec.get("DISPLAY_NAME", display), enabled=as_bool(sec.get("ENABLED", "1"), True),
                log_path=Path(os.path.expandvars(sec.get("LOG_PATH", ""))).expanduser(),
                format=sec.get("FORMAT", "REGEX").strip().upper(),
                start_position=sec.get("START_POSITION", "END").strip().upper(),
                encoding=sec.get("ENCODING", "utf-8"), line_regex=sec.get("LINE_REGEX", ""),
                timestamp_format=sec.get("TIMESTAMP_FORMAT", ""),
                pass_values=csv_values(sec.get("PASS_VALUES", "PASS,OK,SUCCESS")),
                fail_values=csv_values(sec.get("FAIL_VALUES", "FAIL,NG")),
                error_values=csv_values(sec.get("ERROR_VALUES", "ERROR,ABORT")),
                csv_columns=[x.strip() for x in sec.get("CSV_COLUMNS", "").split(",") if x.strip()], fields=fields,
            ))

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="stage-log-collector", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def reload(self) -> None:
        self._load_config()

    def emit(self, kind: str, **data) -> None:
        self.ui_queue.put({"kind": kind, **data})

    def _run(self) -> None:
        self.emit("engine", status="RUNNING")
        while not self.stop_event.is_set():
            for stage in list(self.stages):
                if self.stop_event.is_set():
                    break
                if stage.enabled:
                    self._read_stage(stage)
            self.sync_once()
            self.emit("stats", stats=self.db.stats())
            self.stop_event.wait(self.poll_seconds)
        self.emit("engine", status="STOPPED")

    def _read_stage(self, stage: StageConfig) -> None:
        path = stage.log_path
        if not str(path):
            self.emit("stage", stage=stage.key, status="CONFIG", message="LOG_PATH not configured")
            return
        if not path.exists() or not path.is_file():
            self.emit("stage", stage=stage.key, status="WAITING", message=f"Waiting for {path}")
            return
        try:
            stat = path.stat()
            size = stat.st_size
            base_identity = f"{getattr(stat, 'st_dev', 0)}:{getattr(stat, 'st_ino', 0)}"
            state = self.db.get_file_state(stage)
            if state is None:
                offset = size if stage.start_position == "END" else 0
                identity = f"{base_identity}|{time.time_ns()}"
                self.db.set_offset(stage, offset, identity)
                self.emit("log", message=f"{stage.display_name}: initialized at byte {offset}")
                return
            offset, identity = state
            stored_base = identity.split("|", 1)[0] if identity else ""
            if size < offset or (stored_base and stored_base != base_identity):
                offset = 0
                identity = f"{base_identity}|{time.time_ns()}"
                self.emit("log", message=f"{stage.display_name}: log rotated/truncated; restarting at beginning")
            elif not identity:
                identity = f"{base_identity}|{time.time_ns()}"
            parser = StageParser(stage)
            count = 0
            with path.open("rb") as f:
                f.seek(offset)
                while count < 1000:
                    line_start = f.tell()
                    raw = f.readline()
                    if not raw:
                        break
                    if not raw.endswith((b"\n", b"\r")) and f.tell() == size:
                        f.seek(line_start)
                        break
                    next_offset = f.tell()
                    line = raw.decode(stage.encoding, errors="replace")
                    try:
                        record = parser.parse(line)
                        if record:
                            digest = hashlib.sha256(f"{stage.key}|{path.resolve()}|{identity}|{line_start}|".encode() + raw).hexdigest()
                            payload = {
                                "event_id": digest,
                                "stage_name": stage.key,
                                "station_id": self.station_id,
                                "source_file": str(path.resolve()),
                                "source_offset": line_start,
                                "raw_record": line.strip()[:16000],
                                **record,
                            }
                            if self.db.queue_event(payload):
                                count += 1
                                self.emit("record", stage=stage.key, payload=payload)
                    except Exception as exc:
                        self.emit("log", message=f"{stage.display_name}: parse error at byte {line_start}: {exc}")
                    offset = next_offset
            self.db.set_offset(stage, offset, identity)
            self.emit("stage", stage=stage.key, status="MONITORING", message=f"{path.name} · byte {offset}")
        except Exception as exc:
            self.emit("stage", stage=stage.key, status="ERROR", message=str(exc))

    def sync_once(self) -> None:
        events = self.db.pending(self.batch_size)
        if not events:
            self.emit("server", status="ONLINE_IDLE", message="No pending records")
            return
        ids = [e["event_id"] for e in events]
        body = json.dumps({"client_id": self.station_id, "records": events}).encode("utf-8")
        req = urlrequest.Request(
            self.server_url + "/api/stage-log/batch", data=body, method="POST",
            headers={"Content-Type": "application/json", "X-API-Key": self.api_key,
                     "X-Station-ID": self.station_id, "X-Hostname": socket.gethostname(),
                     "X-App-Version": APP_VERSION},
        )
        try:
            with urlrequest.urlopen(req, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
            processed = set(result.get("accepted_event_ids", [])) | set(result.get("duplicate_event_ids", []))
            sent = [x for x in ids if x in processed]
            missing = [x for x in ids if x not in processed]
            self.db.mark_sent(sent)
            if missing:
                self.db.mark_error(missing, "Local server did not acknowledge event")
            self.emit("server", status="ONLINE", message=f"Uploaded {len(sent)} record(s)")
        except urlerror.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            self.db.mark_error(ids, f"HTTP {exc.code}: {detail}")
            self.emit("server", status="OFFLINE", message=f"HTTP {exc.code}")
        except Exception as exc:
            self.db.mark_error(ids, str(exc))
            self.emit("server", status="OFFLINE", message=str(exc))


class CollectorApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ETE Solutions India | Production Stage Log Collector")
        self.geometry("1380x850")
        self.minsize(1100, 680)
        self.configure(bg=BRAND_BG)
        self.ui_queue: Queue = Queue()
        try:
            self.engine = CollectorEngine(self.ui_queue)
        except Exception as exc:
            messagebox.showerror("Configuration error", str(exc))
            raise
        self.stage_vars = {key: {"status": tk.StringVar(value="STOPPED"), "message": tk.StringVar(value=""),
                                 "pass": tk.StringVar(value="0"), "fail": tk.StringVar(value="0"),
                                 "pending": tk.StringVar(value="0"), "sent": tk.StringVar(value="0")}
                           for key, _ in STAGE_SECTIONS}
        self.server_var = tk.StringVar(value="NOT CONNECTED")
        self.engine_var = tk.StringVar(value="STOPPED")
        self.pending_var = tk.StringVar(value="0")
        self.sent_var = tk.StringVar(value="0")
        self.logo = None
        self._load_logo(); self._style(); self._build()
        self.after(200, self._poll_ui)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.engine.start()

    def _load_logo(self):
        try:
            img = tk.PhotoImage(file=str(BASE_DIR / "assets" / "ete_logo.png"))
            factor = max(1, img.width() // 62)
            self.logo = img.subsample(factor, factor)
            self.iconphoto(True, img)
        except Exception:
            self.logo = None

    def _style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Treeview", rowheight=29, background="white", fieldbackground="white", foreground=BRAND_TEXT)
        style.configure("Treeview.Heading", background="#EAF3F9", foreground=BRAND_DEEP, font=("Segoe UI", 10, "bold"))

    def _build(self):
        sidebar = tk.Frame(self, bg=BRAND_DEEP, width=235)
        sidebar.pack(side="left", fill="y"); sidebar.pack_propagate(False)
        brand = tk.Frame(sidebar, bg=BRAND_DEEP); brand.pack(fill="x", padx=20, pady=(24, 20))
        if self.logo: tk.Label(brand, image=self.logo, bg="white", bd=0).pack(anchor="w")
        tk.Label(brand, text="STAGE LOG\nCOLLECTOR", bg=BRAND_DEEP, fg="white", justify="left", font=("Segoe UI", 17, "bold")).pack(anchor="w", pady=(15, 2))
        tk.Label(brand, text="Production data gateway", bg=BRAND_DEEP, fg="#BBD6E8", font=("Segoe UI", 10)).pack(anchor="w")
        for text, command in [("START MONITORING", self.engine.start), ("STOP", self.engine.stop), ("SYNC NOW", lambda: threading.Thread(target=self.engine.sync_once, daemon=True).start()), ("RELOAD CONFIG", self._reload), ("OPEN CONFIG", self._open_config)]:
            tk.Button(sidebar, text=text, command=command, bg=BRAND_BLUE if text in {"START MONITORING", "SYNC NOW"} else "#285B7C", fg="white", activebackground=BRAND_DARK, activeforeground="white", relief="flat", font=("Segoe UI", 10, "bold"), padx=14, pady=11, anchor="w").pack(fill="x", padx=17, pady=4)
        status = tk.Frame(sidebar, bg="#244F70", highlightbackground="#4B7897", highlightthickness=1)
        status.pack(side="bottom", fill="x", padx=17, pady=20)
        tk.Label(status, text="ENGINE", bg="#244F70", fg="#AFC9DA", font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=12, pady=(10, 0))
        tk.Label(status, textvariable=self.engine_var, bg="#244F70", fg="white", font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=12)
        tk.Label(status, textvariable=self.server_var, bg="#244F70", fg="#C9E3F2", wraplength=185, justify="left").pack(anchor="w", padx=12, pady=(5, 12))

        main = tk.Frame(self, bg=BRAND_BG); main.pack(side="left", fill="both", expand=True)
        header = tk.Frame(main, bg=BRAND_BG); header.pack(fill="x", padx=26, pady=(24, 12))
        tk.Label(header, text="Production Stage Log Collector", bg=BRAND_BG, fg=BRAND_TEXT, font=("Segoe UI", 24, "bold")).pack(side="left")
        summary = tk.Frame(header, bg=BRAND_BG); summary.pack(side="right")
        for label, var in [("PENDING", self.pending_var), ("SENT", self.sent_var)]:
            box = tk.Frame(summary, bg="white", highlightbackground="#DCE7EF", highlightthickness=1); box.pack(side="left", padx=5)
            tk.Label(box, text=label, bg="white", fg=BRAND_MUTED, font=("Segoe UI", 8, "bold")).pack(padx=18, pady=(7,0))
            tk.Label(box, textvariable=var, bg="white", fg=BRAND_DEEP, font=("Segoe UI", 18, "bold")).pack(padx=18, pady=(0,7))

        cards = tk.Frame(main, bg=BRAND_BG); cards.pack(fill="x", padx=26)
        cards.grid_columnconfigure((0,1), weight=1, uniform="stage")
        for idx, (key, display) in enumerate(STAGE_SECTIONS):
            card = tk.Frame(cards, bg="white", highlightbackground="#DCE7EF", highlightthickness=1)
            card.grid(row=idx//2, column=idx%2, sticky="nsew", padx=7, pady=7)
            top = tk.Frame(card, bg="white"); top.pack(fill="x", padx=16, pady=(14,8))
            tk.Label(top, text=f"{idx+2}", bg=BRAND_BLUE, fg="white", width=3, font=("Segoe UI", 12, "bold")).pack(side="left")
            tk.Label(top, text=display, bg="white", fg=BRAND_TEXT, font=("Segoe UI", 13, "bold")).pack(side="left", padx=10)
            tk.Label(top, textvariable=self.stage_vars[key]["status"], bg="#EAF3F9", fg=BRAND_DEEP, font=("Segoe UI", 9, "bold"), padx=9, pady=4).pack(side="right")
            tk.Label(card, textvariable=self.stage_vars[key]["message"], bg="white", fg=BRAND_MUTED, anchor="w", wraplength=480).pack(fill="x", padx=16)
            metrics = tk.Frame(card, bg="white"); metrics.pack(fill="x", padx=16, pady=(12,15))
            for label, name in [("PASS","pass"),("FAIL","fail"),("PENDING","pending"),("SENT","sent")]:
                item=tk.Frame(metrics,bg="#F6FAFD"); item.pack(side="left",expand=True,fill="x",padx=3)
                tk.Label(item,text=label,bg="#F6FAFD",fg=BRAND_MUTED,font=("Segoe UI",8,"bold")).pack(pady=(6,0))
                tk.Label(item,textvariable=self.stage_vars[key][name],bg="#F6FAFD",fg=GREEN if name=="pass" else RED if name=="fail" else BRAND_DEEP,font=("Segoe UI",14,"bold")).pack(pady=(0,6))

        log_panel = tk.Frame(main, bg="white", highlightbackground="#DCE7EF", highlightthickness=1)
        log_panel.pack(fill="both", expand=True, padx=33, pady=(8,24))
        tk.Label(log_panel, text="Running log", bg="white", fg=BRAND_TEXT, font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=14, pady=(12,7))
        cols=("time","stage","mac","serial","result","queue","detail")
        self.tree=ttk.Treeview(log_panel,columns=cols,show="headings",height=10)
        widths={"time":155,"stage":170,"mac":145,"serial":150,"result":75,"queue":75,"detail":360}
        for c in cols: self.tree.heading(c,text=c.replace('_',' ').title()); self.tree.column(c,width=widths[c],anchor="w")
        self.tree.pack(fill="both",expand=True,padx=14,pady=(0,14))

    def _poll_ui(self):
        try:
            while True:
                msg=self.ui_queue.get_nowait(); kind=msg.get("kind")
                if kind=="engine": self.engine_var.set(msg["status"])
                elif kind=="server": self.server_var.set(f"LOCAL SERVER: {msg['status']}\n{msg.get('message','')}")
                elif kind=="stage":
                    v=self.stage_vars[msg["stage"]]; v["status"].set(msg["status"]); v["message"].set(msg.get("message", ""))
                elif kind=="record":
                    p=msg["payload"]
                    self.tree.insert("",0,values=(p.get("completed_at",""),p.get("stage_name",""),p.get("mac",""),p.get("serial_number",""),p.get("status",""),"PENDING",p.get("detail","")[:120]))
                    for child in self.tree.get_children()[250:]: self.tree.delete(child)
                elif kind=="stats": self._apply_stats(msg["stats"])
                elif kind=="log":
                    self.tree.insert("",0,values=(now_iso(),"SYSTEM","","","INFO","",msg.get("message","")))
        except Empty: pass
        self.after(250,self._poll_ui)

    def _apply_stats(self, stats):
        overall=stats.get("overall",{}); self.pending_var.set(str(overall.get("PENDING",0))); self.sent_var.set(str(overall.get("SENT",0)))
        for key,_ in STAGE_SECTIONS:
            row=stats.get("stages",{}).get(key,{})
            for name in ["pass","fail","pending","sent"]: self.stage_vars[key][name].set(str(int(row.get(name,0) or 0)))

    def _reload(self):
        try: self.engine.reload(); messagebox.showinfo("Configuration", "Configuration reloaded.")
        except Exception as exc: messagebox.showerror("Configuration", str(exc))

    def _open_config(self):
        try: os.startfile(CONFIG_PATH)  # type: ignore[attr-defined]
        except Exception: messagebox.showinfo("Config file", str(CONFIG_PATH))

    def _close(self):
        self.engine.stop(); self.destroy()


if __name__ == "__main__":
    CollectorApp().mainloop()
