from __future__ import annotations

import base64
import csv
import hmac
import io
import json
import mimetypes
import os
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, time as dt_time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
except ImportError:  # SQLite fallback works without third-party packages.
    psycopg = None
    dict_row = None
    Jsonb = None

BASE_DIR = Path(__file__).resolve().parent
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
SQLITE_PATH = Path(os.environ.get("CLOUD_SQLITE_PATH", str(BASE_DIR / "cloud_mes.db"))).resolve()
INGEST_API_KEY = os.environ.get("INGEST_API_KEY", "").strip()
DASHBOARD_USERNAME = os.environ.get("DASHBOARD_USERNAME", "admin").strip()
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "").strip()
REFRESH_SECONDS = max(5, int(os.environ.get("REFRESH_SECONDS", "10")))
MAX_BATCH_SIZE = max(1, min(1000, int(os.environ.get("MAX_BATCH_SIZE", "500"))))
MAX_BODY_BYTES = 5 * 1024 * 1024
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8080"))

EVENT_COLUMNS = [
    "event_id", "event_type", "plant_id", "station_id", "router_slot", "router_ip",
    "mac", "serial_number", "scanned_serial_number", "gpon_number", "part_number",
    "status", "writer_state", "server_check", "serial_scan_result",
    "wifi_calibration_result", "bob_calibration_result", "firmware_result",
    "stage_name", "source_file", "source_offset", "raw_log",
    "firmware_version", "led_result", "reset_result", "wps_result", "user_mode_result",
    "detail", "completed_at", "payload_json",
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


def parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return utc_now()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Invalid completed_at timestamp: {value}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def normalize_text(value: Any, limit: int = 500) -> str:
    return str(value or "").strip()[:limit]


def decode_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


class CloudDatabase:
    def __init__(self) -> None:
        self.postgres = bool(DATABASE_URL)
        if self.postgres and psycopg is None:
            raise RuntimeError("DATABASE_URL is set but psycopg is not installed")
        self.init_schema()

    @property
    def backend_name(self) -> str:
        return "postgresql" if self.postgres else "sqlite"

    def connect(self):
        if self.postgres:
            return psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=False)
        SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(SQLITE_PATH, timeout=20)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        return con

    def init_schema(self) -> None:
        if self.postgres:
            statements = [
                """CREATE TABLE IF NOT EXISTS production_events (
                    event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, plant_id TEXT NOT NULL,
                    station_id TEXT NOT NULL, router_slot TEXT, router_ip TEXT, mac TEXT,
                    serial_number TEXT, scanned_serial_number TEXT, gpon_number TEXT, part_number TEXT,
                    status TEXT NOT NULL, writer_state TEXT, server_check TEXT, serial_scan_result TEXT,
                    wifi_calibration_result TEXT, bob_calibration_result TEXT, firmware_result TEXT,
                    firmware_version TEXT, led_result TEXT, reset_result TEXT, wps_result TEXT,
                    user_mode_result TEXT, detail TEXT, stage_name TEXT, source_file TEXT,
                    source_offset BIGINT, raw_log TEXT, completed_at TIMESTAMPTZ NOT NULL,
                    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), payload_json JSONB NOT NULL
                )""",
                "ALTER TABLE production_events ADD COLUMN IF NOT EXISTS stage_name TEXT",
                "ALTER TABLE production_events ADD COLUMN IF NOT EXISTS source_file TEXT",
                "ALTER TABLE production_events ADD COLUMN IF NOT EXISTS source_offset BIGINT",
                "ALTER TABLE production_events ADD COLUMN IF NOT EXISTS raw_log TEXT",
                "CREATE INDEX IF NOT EXISTS idx_events_completed ON production_events(completed_at DESC)",
                "CREATE INDEX IF NOT EXISTS idx_events_type_completed ON production_events(event_type, completed_at DESC)",
                "CREATE INDEX IF NOT EXISTS idx_events_plant_completed ON production_events(plant_id, completed_at DESC)",
                "CREATE INDEX IF NOT EXISTS idx_events_mac ON production_events(mac)",
                "CREATE INDEX IF NOT EXISTS idx_events_serial ON production_events(serial_number)",
            ]
            with self.connect() as con:
                with con.cursor() as cur:
                    for statement in statements:
                        cur.execute(statement)
                con.commit()
        else:
            schema = """
                CREATE TABLE IF NOT EXISTS production_events (
                    event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, plant_id TEXT NOT NULL,
                    station_id TEXT NOT NULL, router_slot TEXT, router_ip TEXT, mac TEXT,
                    serial_number TEXT, scanned_serial_number TEXT, gpon_number TEXT, part_number TEXT,
                    status TEXT NOT NULL, writer_state TEXT, server_check TEXT, serial_scan_result TEXT,
                    wifi_calibration_result TEXT, bob_calibration_result TEXT, firmware_result TEXT,
                    firmware_version TEXT, led_result TEXT, reset_result TEXT, wps_result TEXT,
                    user_mode_result TEXT, detail TEXT, stage_name TEXT, source_file TEXT,
                    source_offset INTEGER, raw_log TEXT, completed_at TEXT NOT NULL,
                    received_at TEXT NOT NULL, payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_completed ON production_events(completed_at DESC);
                CREATE INDEX IF NOT EXISTS idx_events_type_completed ON production_events(event_type, completed_at DESC);
                CREATE INDEX IF NOT EXISTS idx_events_plant_completed ON production_events(plant_id, completed_at DESC);
                CREATE INDEX IF NOT EXISTS idx_events_mac ON production_events(mac);
                CREATE INDEX IF NOT EXISTS idx_events_serial ON production_events(serial_number);
            """
            with self.connect() as con:
                con.executescript(schema)
                existing = {row[1] for row in con.execute("PRAGMA table_info(production_events)")}
                for column, declaration in {
                    "stage_name": "TEXT", "source_file": "TEXT",
                    "source_offset": "INTEGER", "raw_log": "TEXT",
                }.items():
                    if column not in existing:
                        con.execute(f"ALTER TABLE production_events ADD COLUMN {column} {declaration}")
                con.commit()

    def insert_events(self, events: Iterable[dict[str, Any]]) -> tuple[list[str], list[str], list[dict[str, str]]]:
        accepted: list[str] = []
        duplicates: list[str] = []
        rejected: list[dict[str, str]] = []
        with self.connect() as con:
            cur = con.cursor()
            for raw in events:
                try:
                    event = validate_event(raw, self.postgres)
                except ValueError as exc:
                    rejected.append({
                        "event_id": normalize_text(raw.get("event_id") if isinstance(raw, dict) else "", 200),
                        "error": str(exc),
                    })
                    continue
                values = [event[column] for column in EVENT_COLUMNS]
                if self.postgres:
                    values[-1] = Jsonb(event["payload_object"])
                    placeholders = ",".join(["%s"] * len(EVENT_COLUMNS))
                    sql = f"INSERT INTO production_events ({','.join(EVENT_COLUMNS)}) VALUES ({placeholders}) ON CONFLICT (event_id) DO NOTHING RETURNING event_id"
                    cur.execute(sql, values)
                    if cur.fetchone():
                        accepted.append(event["event_id"])
                    else:
                        duplicates.append(event["event_id"])
                else:
                    placeholders = ",".join(["?"] * len(EVENT_COLUMNS))
                    sql = f"INSERT OR IGNORE INTO production_events ({','.join(EVENT_COLUMNS)},received_at) VALUES ({placeholders},?)"
                    cur.execute(sql, values + [utc_now().isoformat()])
                    if cur.rowcount == 1:
                        accepted.append(event["event_id"])
                    else:
                        duplicates.append(event["event_id"])
            con.commit()
        return accepted, duplicates, rejected

    def events_between(self, start: date, end: date) -> list[dict[str, Any]]:
        start_dt = datetime.combine(start, dt_time.min, tzinfo=timezone.utc)
        end_dt = datetime.combine(end + timedelta(days=1), dt_time.min, tzinfo=timezone.utc)
        return self._query(
            "SELECT * FROM production_events WHERE completed_at>=? AND completed_at<? ORDER BY completed_at",
            (start_dt if self.postgres else start_dt.isoformat(), end_dt if self.postgres else end_dt.isoformat()),
        )

    def recent_events(self, event_type: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(1000, int(limit)))
        if event_type:
            return self._query("SELECT * FROM production_events WHERE event_type=? ORDER BY completed_at DESC LIMIT ?", (event_type, limit))
        return self._query("SELECT * FROM production_events ORDER BY completed_at DESC LIMIT ?", (limit,))

    def latest_snapshot(self) -> dict[str, Any]:
        rows = self.recent_events("MAC_POOL_SNAPSHOT", 1)
        return decode_payload(rows[0].get("payload_json")) if rows else {}

    def count(self) -> int:
        rows = self._query("SELECT COUNT(*) AS c FROM production_events", ())
        return int(rows[0]["c"] if rows else 0)

    def _query(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        with self.connect() as con:
            cur = con.cursor()
            cur.execute(sql.replace("?", "%s") if self.postgres else sql, params)
            rows = cur.fetchall()
        return [dict(row) for row in rows]


def validate_event(raw: Any, postgres: bool) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("Each event must be a JSON object")
    event_id = normalize_text(raw.get("event_id"), 250)
    event_type = normalize_text(raw.get("event_type"), 100).upper()
    plant_id = normalize_text(raw.get("plant_id"), 100)
    station_id = normalize_text(raw.get("station_id"), 150)
    status = normalize_text(raw.get("status"), 30).upper()
    if not event_id:
        raise ValueError("event_id is required")
    if event_type not in {"IDENTITY_WRITER_RESULT", "QUALITY_VERIFICATION_RESULT", "STAGE_LOG_RESULT", "MAC_POOL_SNAPSHOT"}:
        raise ValueError("Unsupported event_type")
    if not plant_id:
        raise ValueError("plant_id is required")
    if not station_id:
        raise ValueError("station_id is required")
    if event_type != "MAC_POOL_SNAPSHOT" and status not in {"PASS", "FAIL", "ERROR"}:
        raise ValueError("status must be PASS, FAIL, or ERROR")
    stage_name = normalize_text(raw.get("stage_name"), 100).upper()
    allowed_stages = {"WIFI_CALIBRATION", "LABEL_PRINTING", "BOB_CALIBRATION", "WIFI_COUPLING_VOIP"}
    if event_type == "STAGE_LOG_RESULT" and stage_name not in allowed_stages:
        raise ValueError("Unsupported or missing stage_name")
    if event_type == "MAC_POOL_SNAPSHOT":
        status = "INFO"
    completed_at = parse_datetime(raw.get("completed_at"))
    payload = dict(raw)
    event = {
        "event_id": event_id, "event_type": event_type, "plant_id": plant_id, "station_id": station_id,
        "router_slot": normalize_text(raw.get("router_slot"), 30), "router_ip": normalize_text(raw.get("router_ip"), 100),
        "mac": normalize_text(raw.get("mac"), 50).upper(), "serial_number": normalize_text(raw.get("serial_number"), 250),
        "scanned_serial_number": normalize_text(raw.get("scanned_serial_number"), 250),
        "gpon_number": normalize_text(raw.get("gpon_number"), 250), "part_number": normalize_text(raw.get("part_number"), 250),
        "status": status, "writer_state": normalize_text(raw.get("writer_state"), 50).upper(),
        "server_check": normalize_text(raw.get("server_check"), 1000),
        "serial_scan_result": normalize_text(raw.get("serial_scan_result"), 50).upper(),
        "wifi_calibration_result": normalize_text(raw.get("wifi_calibration_result"), 50).upper(),
        "bob_calibration_result": normalize_text(raw.get("bob_calibration_result"), 50).upper(),
        "firmware_result": normalize_text(raw.get("firmware_result"), 50).upper(),
        "firmware_version": normalize_text(raw.get("firmware_version"), 250),
        "led_result": normalize_text(raw.get("led_result"), 50).upper(), "reset_result": normalize_text(raw.get("reset_result"), 50).upper(),
        "wps_result": normalize_text(raw.get("wps_result"), 50).upper(), "user_mode_result": normalize_text(raw.get("user_mode_result"), 50).upper(),
        "detail": normalize_text(raw.get("detail"), 8000),
        "stage_name": stage_name,
        "source_file": normalize_text(raw.get("source_file"), 1000),
        "source_offset": int(raw.get("source_offset", 0) or 0),
        "raw_log": normalize_text(raw.get("raw_log"), 16000),
        "completed_at": completed_at if postgres else completed_at.isoformat(),
        "payload_json": json.dumps(payload, ensure_ascii=False), "payload_object": payload,
    }
    return event


def safe_rate(num: int, den: int) -> float:
    return round(num * 100.0 / den, 2) if den else 0.0


def parse_range(query: dict[str, list[str]]) -> tuple[date, date]:
    today = date.today()
    default_start = today - timedelta(days=6)
    try:
        start = date.fromisoformat(query.get("start", [default_start.isoformat()])[0])
        end = date.fromisoformat(query.get("end", [today.isoformat()])[0])
    except ValueError:
        start, end = default_start, today
    if end < start:
        start, end = end, start
    if (end - start).days > 366:
        start = end - timedelta(days=366)
    return start, end


def dashboard_data(start: date, end: date) -> dict[str, Any]:
    events = db.events_between(start, end)
    writer = [e for e in events if e["event_type"] == "IDENTITY_WRITER_RESULT"]
    verifier = [e for e in events if e["event_type"] == "QUALITY_VERIFICATION_RESULT"]
    stage_logs = [e for e in events if e["event_type"] == "STAGE_LOG_RESULT"]

    def result_counts(rows: list[dict[str, Any]]) -> tuple[int, int, int]:
        passed = sum(e["status"] == "PASS" for e in rows)
        failed = sum(e["status"] in {"FAIL", "ERROR"} for e in rows)
        return passed, failed, passed + failed

    writer_pass, writer_fail, writer_total = result_counts(writer)
    final_pass, final_fail, final_total = result_counts(verifier)

    stage_groups: list[tuple[str, list[dict[str, Any]]]] = [
        ("1. MAC Write", writer),
        ("2. Wi-Fi Calibration", [e for e in stage_logs if e.get("stage_name") == "WIFI_CALIBRATION"]),
        ("3. Label Printing", [e for e in stage_logs if e.get("stage_name") == "LABEL_PRINTING"]),
        ("4. BOB Calibration", [e for e in stage_logs if e.get("stage_name") == "BOB_CALIBRATION"]),
        ("5. Wi-Fi Coupling & VoIP", [e for e in stage_logs if e.get("stage_name") == "WIFI_COUPLING_VOIP"]),
        ("6. Verification", verifier),
    ]
    stages = []
    for label, rows in stage_groups:
        passed, failed, tested = result_counts(rows)
        stages.append({
            "stage": label, "pass": passed, "fail": failed, "tested": tested,
            "yield": safe_rate(passed, tested),
        })

    daily_map: dict[str, Counter] = defaultdict(Counter)
    for event in verifier:
        day = as_iso(event["completed_at"])[:10]
        daily_map[day]["total"] += 1
        daily_map[day]["pass" if event["status"] == "PASS" else "fail"] += 1
    daily = [{"day": day, "pass": c["pass"], "fail": c["fail"], "total": c["total"]} for day, c in sorted(daily_map.items())]

    station_map: dict[str, dict[str, Any]] = {}
    for event in [e for e in events if e["event_type"] != "MAC_POOL_SNAPSHOT"]:
        station = event["station_id"] or "UNKNOWN"
        item = station_map.setdefault(station, {"client_id": station, "total": 0, "pass": 0, "fail": 0, "last_result": ""})
        item["total"] += 1
        item["pass" if event["status"] == "PASS" else "fail"] += 1
        item["last_result"] = max(item["last_result"], as_iso(event["completed_at"]))
    stations = sorted(station_map.values(), key=lambda x: (-x["total"], x["client_id"]))
    for item in stations:
        item["yield"] = safe_rate(item["pass"], item["total"])

    def display_stage(e: dict[str, Any]) -> str:
        if e["event_type"] == "IDENTITY_WRITER_RESULT": return "MAC Write"
        if e["event_type"] == "QUALITY_VERIFICATION_RESULT": return "Verification"
        return {
            "WIFI_CALIBRATION": "Wi-Fi Calibration",
            "LABEL_PRINTING": "Label Printing",
            "BOB_CALIBRATION": "BOB Calibration",
            "WIFI_COUPLING_VOIP": "Wi-Fi Coupling & VoIP",
        }.get(e.get("stage_name") or "", e.get("stage_name") or "Production Stage")

    production_events = [e for e in events if e["event_type"] != "MAC_POOL_SNAPSHOT"]
    recent_rows = sorted(production_events, key=lambda e: as_iso(e["completed_at"]), reverse=True)[:200]
    recent = [{
        "completed_at": as_iso(e["completed_at"]), "stage": display_stage(e),
        "mac": e["mac"] or "", "serial_number": e["serial_number"] or "",
        "gpon_number": e["gpon_number"] or "", "part_number": e["part_number"] or "",
        "client_id": e["station_id"] or "", "router_ip": e["router_ip"] or "",
        "source_file": e.get("source_file") or "", "status": e["status"],
        "detail": e["detail"] or "", "event_type": e["event_type"],
    } for e in recent_rows]

    snapshot = db.latest_snapshot()
    metrics = snapshot.get("metrics", {}) if isinstance(snapshot, dict) else {}
    return {
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "kpi": {
            "production_volume": final_total, "line_input": writer_total,
            "final_pass": final_pass, "final_fail": final_fail,
            "final_yield": safe_rate(final_pass, final_total),
            "writer_pass": writer_pass, "writer_fail": writer_fail,
            "wip": max(0, writer_total - final_total),
            "available_macs": int(metrics.get("available", 0) or 0),
            "reserved_macs": int(metrics.get("reserved", 0) or 0),
        },
        "daily": daily, "stages": stages, "stations": stations, "recent": recent,
        "refresh_seconds": REFRESH_SECONDS,
    }


def valid_ingest_auth(header: str) -> bool:
    return bool(INGEST_API_KEY and header.startswith("Bearer ") and hmac.compare_digest(header[7:], INGEST_API_KEY))


def valid_dashboard_auth(header: str) -> bool:
    if not DASHBOARD_PASSWORD:
        return True
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
        username, password = decoded.split(":", 1)
    except Exception:
        return False
    return hmac.compare_digest(username, DASHBOARD_USERNAME) and hmac.compare_digest(password, DASHBOARD_PASSWORD)


class Handler(BaseHTTPRequestHandler):
    server_version = "ETECloudMES/13.0"

    def send_bytes(self, data: bytes, content_type: str, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        self.send_bytes(json.dumps(payload, default=as_iso).encode("utf-8"), "application/json; charset=utf-8", status)

    def require_dashboard(self) -> bool:
        if valid_dashboard_auth(self.headers.get("Authorization", "")):
            return True
        self.send_bytes(b"Authentication required", "text/plain; charset=utf-8", 401, {"WWW-Authenticate": 'Basic realm="ETE MES"'})
        return False

    def json_body(self) -> Any:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            raise ValueError("Invalid request body size")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path == "/api/health":
                return self.send_json({"ok": True, "service": "ETE Cloud MES", "version": "13.0", "database": db.backend_name, "events": db.count()})
            if path == "/api/dashboard":
                if not self.require_dashboard():
                    return
                start, end = parse_range(query)
                return self.send_json(dashboard_data(start, end))
            if path in {"/export/verification.csv", "/export/production.csv"}:
                if not self.require_dashboard():
                    return
                start, end = parse_range(query)
                events = [e for e in db.events_between(start, end) if e["event_type"] != "MAC_POOL_SNAPSHOT"]
                fields = ["completed_at", "event_type", "stage_name", "plant_id", "station_id", "router_slot", "router_ip", "mac", "serial_number", "scanned_serial_number", "gpon_number", "part_number", "status", "source_file", "source_offset", "server_check", "serial_scan_result", "wifi_calibration_result", "bob_calibration_result", "firmware_result", "firmware_version", "led_result", "reset_result", "wps_result", "user_mode_result", "detail", "raw_log", "event_id"]
                out = io.StringIO(); writer = csv.DictWriter(out, fieldnames=fields); writer.writeheader()
                for event in events:
                    row = {key: event.get(key, "") for key in fields}; row["completed_at"] = as_iso(row["completed_at"]); writer.writerow(row)
                return self.send_bytes(out.getvalue().encode("utf-8"), "text/csv; charset=utf-8", headers={"Content-Disposition": f"attachment; filename=production_{start}_to_{end}.csv"})
            if path == "/":
                if not self.require_dashboard():
                    return
                file = BASE_DIR / "templates" / "index.html"
            elif path.startswith("/static/"):
                file = BASE_DIR / path.lstrip("/")
            elif path.startswith("/assets/"):
                file = BASE_DIR / path.lstrip("/")
            else:
                return self.send_json({"ok": False, "error": "Not found"}, 404)
            resolved = file.resolve()
            if not file.exists() or BASE_DIR not in resolved.parents:
                return self.send_json({"ok": False, "error": "Not found"}, 404)
            data = file.read_bytes()
            if file.name == "index.html":
                today = date.today()
                data = data.replace(b"{{ refresh_seconds }}", str(REFRESH_SECONDS).encode())
                data = data.replace(b"{{ start }}", (today - timedelta(days=6)).isoformat().encode())
                data = data.replace(b"{{ end }}", today.isoformat().encode())
            return self.send_bytes(data, mimetypes.guess_type(file.name)[0] or "application/octet-stream")
        except Exception as exc:
            return self.send_json({"ok": False, "error": str(exc)}, 500)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in {"/api/v1/production-events", "/api/v1/production-events/batch"}:
            return self.send_json({"ok": False, "error": "Not found"}, 404)
        if not valid_ingest_auth(self.headers.get("Authorization", "")):
            return self.send_json({"ok": False, "error": "Unauthorized"}, 401)
        try:
            body = self.json_body()
            if path.endswith("/batch"):
                if not isinstance(body, dict) or not isinstance(body.get("events"), list):
                    return self.send_json({"ok": False, "error": "JSON body must contain an events array"}, 400)
                events = body["events"]
            else:
                if not isinstance(body, dict):
                    return self.send_json({"ok": False, "error": "JSON object required"}, 400)
                events = [body]
            if len(events) > MAX_BATCH_SIZE:
                return self.send_json({"ok": False, "error": f"Batch exceeds MAX_BATCH_SIZE={MAX_BATCH_SIZE}"}, 413)
            accepted, duplicates, rejected = db.insert_events(events)
            processed = accepted + duplicates
            return self.send_json({"ok": True, "accepted": len(accepted), "duplicates": len(duplicates), "rejected": rejected, "processed_event_ids": processed}, 200 if not rejected else 207)
        except (ValueError, json.JSONDecodeError) as exc:
            return self.send_json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            return self.send_json({"ok": False, "error": str(exc)}, 500)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")


db = CloudDatabase()

if __name__ == "__main__":
    print(f"ETE Cloud MES v13: http://{HOST}:{PORT}")
    print(f"Database backend: {db.backend_name}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
