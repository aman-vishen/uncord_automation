import csv
import hashlib
import json
import queue
import re
import shutil
import socket
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error as urlerror, request as urlrequest
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "server_config.txt"
DB_PATH = BASE_DIR / "mac_server.db"
FIRMWARE_DIR = BASE_DIR / "firmware"

BRAND_DARK = "#185890"
BRAND_BLUE = "#2898D0"
BRAND_DEEP = "#12466F"
BRAND_BG = "#F2F8FC"
BRAND_CARD = "#FFFFFF"
BRAND_TEXT = "#17324A"
BRAND_MUTED = "#5C7387"


class AppError(Exception):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


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


def normalize_mac(value: str) -> str:
    return re.sub(r"[^0-9A-Fa-f]", "", value).upper()


def validate_mac(value: str) -> str:
    mac = normalize_mac(value)
    if len(mac) != 12 or not re.fullmatch(r"[0-9A-F]{12}", mac):
        raise AppError(f"Invalid MAC: {value}")
    return mac


def colon_mac(value: str) -> str:
    n = validate_mac(value)
    return ":".join(n[i:i + 2] for i in range(0, 12, 2))


def parse_identity_list_file(path: Path) -> list[dict[str, str]]:
    """Read identity rows while preserving MAC + serial + GPON from the SAME input line.

    Preferred format (CSV, TSV, or whitespace-separated):
        MAC,SERIAL_NUMBER,GPON_SERIAL_NUMBER
        14:D6:7C:00:00:01,SN00000001,ZTEG00000001

    A header row is optional. Header aliases such as SERIAL, SN, GPON, GPON_SN and
    GPON_SERIAL_NUMBER are accepted. Legacy MAC-only rows are still accepted so an
    old queue can be imported, but the Writer can be configured to require all three.
    """
    if not path.exists():
        raise AppError(f"Identity list not found: {path}")

    records: list[dict[str, str]] = []
    seen_macs: set[str] = set()

    def clean(v: str) -> str:
        return (v or "").strip().strip('"').strip("'")

    def add(mac_raw: str, serial_raw: str = "", gpon_raw: str = "", line_no: int = 0) -> None:
        mac_raw, serial, gpon = clean(mac_raw), clean(serial_raw), clean(gpon_raw)
        if not mac_raw or mac_raw.startswith("#") or mac_raw.startswith(";"):
            return
        try:
            mac = validate_mac(mac_raw)
        except AppError as exc:
            raise AppError(f"Line {line_no}: {exc}") from exc
        if mac in seen_macs:
            raise AppError(f"Line {line_no}: duplicate MAC in input list: {colon_mac(mac)}")
        seen_macs.add(mac)
        records.append({"mac": mac, "serial_number": serial, "gpon_number": gpon, "line_no": str(line_no)})

    lines = path.read_text(encoding="utf-8-sig").splitlines()
    meaningful = [(i, line) for i, line in enumerate(lines, 1) if line.strip() and not line.lstrip().startswith(("#", ";"))]
    if not meaningful:
        raise AppError("The selected identity list is empty.")

    first_text = meaningful[0][1]
    delimiter = "," if "," in first_text else ("\t" if "\t" in first_text else None)
    header_map = None

    def split_line(text: str) -> list[str]:
        if delimiter == ",":
            return next(csv.reader([text]))
        if delimiter == "\t":
            return text.split("\t")
        return re.split(r"\s+", text.strip())

    first_cells = [clean(x) for x in split_line(first_text)]
    normalized_headers = [re.sub(r"[^A-Z0-9]", "", x.upper()) for x in first_cells]
    header_tokens = {"MAC", "MACADDRESS", "SERIAL", "SERIALNUMBER", "SN", "GPON", "GPONSN", "GPONSERIAL", "GPONSERIALNUMBER", "GPONNUMBER"}
    if any(x in header_tokens for x in normalized_headers):
        def find_idx(candidates):
            for idx, value in enumerate(normalized_headers):
                if value in candidates:
                    return idx
            return None
        header_map = {
            "mac": find_idx({"MAC", "MACADDRESS"}),
            "serial": find_idx({"SERIAL", "SERIALNUMBER", "SN"}),
            "gpon": find_idx({"GPON", "GPONSN", "GPONSERIAL", "GPONSERIALNUMBER", "GPONNUMBER"}),
        }
        if header_map["mac"] is None:
            raise AppError("Identity-list header is missing a MAC column.")
        meaningful = meaningful[1:]

    for line_no, text in meaningful:
        cells = [clean(x) for x in split_line(text)]
        if not cells:
            continue
        if header_map:
            def at(idx):
                return cells[idx] if idx is not None and idx < len(cells) else ""
            add(at(header_map["mac"]), at(header_map["serial"]), at(header_map["gpon"]), line_no)
        else:
            add(cells[0], cells[1] if len(cells) > 1 else "", cells[2] if len(cells) > 2 else "", line_no)

    if not records:
        raise AppError("No identity rows found in the selected file.")
    return records


def parse_mac_list_file(path: Path) -> list[str]:
    # Backward-compatible helper used by older integrations/tests.
    return [row["mac"] for row in parse_identity_list_file(path)]


def guess_lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        sock.close()


class MacDatabase:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=15.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS mac_pool (
                    mac TEXT PRIMARY KEY,
                    seq INTEGER NOT NULL UNIQUE,
                    state TEXT NOT NULL DEFAULT 'AVAILABLE',
                    added_at TEXT NOT NULL,
                    request_id TEXT UNIQUE,
                    reservation_id TEXT UNIQUE,
                    client_id TEXT,
                    client_ip TEXT,
                    reserved_at TEXT,
                    completed_at TEXT,
                    result_detail TEXT,
                    serial_number TEXT,
                    gpon_number TEXT,
                    pcb_serial_number TEXT,
                    box_build_station TEXT,
                    box_build_at TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_mac_pool_state_seq ON mac_pool(state, seq);

                CREATE TABLE IF NOT EXISTS clients (
                    client_id TEXT PRIMARY KEY,
                    hostname TEXT,
                    last_ip TEXT,
                    last_seen TEXT NOT NULL,
                    app_version TEXT
                );

                CREATE TABLE IF NOT EXISTS verification_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    verification_id TEXT NOT NULL UNIQUE,
                    request_id TEXT NOT NULL UNIQUE,
                    mac TEXT NOT NULL,
                    serial_number TEXT,
                    scanned_serial_number TEXT,
                    serial_scan_result TEXT,
                    gpon_number TEXT,
                    pcb_serial_number TEXT,
                    part_number TEXT,
                    client_id TEXT NOT NULL,
                    client_ip TEXT,
                    router_ip TEXT,
                    status TEXT NOT NULL,
                    pool_state TEXT,
                    server_check TEXT,
                    wifi_calibration_result TEXT,
                    bob_calibration_result TEXT,
                    firmware_result TEXT,
                    firmware_version TEXT,
                    led_result TEXT,
                    reset_result TEXT,
                    wps_result TEXT,
                    user_mode_result TEXT,
                    detail TEXT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_verify_mac_status ON verification_history(mac, status);
                CREATE INDEX IF NOT EXISTS idx_verify_serial_status ON verification_history(serial_number, status);
                CREATE INDEX IF NOT EXISTS idx_verify_updated ON verification_history(updated_at);

                CREATE TABLE IF NOT EXISTS cloud_sync_queue (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    synced_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_cloud_sync_status_created
                    ON cloud_sync_queue(status, created_at);

                CREATE TABLE IF NOT EXISTS stage_log_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    stage_name TEXT NOT NULL,
                    station_id TEXT NOT NULL,
                    client_ip TEXT,
                    source_file TEXT,
                    source_offset INTEGER,
                    status TEXT NOT NULL,
                    mac TEXT,
                    serial_number TEXT,
                    gpon_number TEXT,
                    pcb_serial_number TEXT,
                    part_number TEXT,
                    detail TEXT,
                    raw_record TEXT,
                    completed_at TEXT NOT NULL,
                    received_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_stage_log_stage_completed
                    ON stage_log_history(stage_name, completed_at);
                CREATE INDEX IF NOT EXISTS idx_stage_log_mac ON stage_log_history(mac);
                CREATE INDEX IF NOT EXISTS idx_stage_log_serial ON stage_log_history(serial_number);

                CREATE TABLE IF NOT EXISTS system_settings (
                    setting_key TEXT PRIMARY KEY,
                    setting_value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            # Automatic schema migration for databases created by older releases.
            existing = {
                "mac_pool": {row[1] for row in con.execute("PRAGMA table_info(mac_pool)")},
                "verification_history": {row[1] for row in con.execute("PRAGMA table_info(verification_history)")},
                "stage_log_history": {row[1] for row in con.execute("PRAGMA table_info(stage_log_history)")},
            }
            migrations = {
                "mac_pool": {
                    "serial_number": "TEXT",
                    "gpon_number": "TEXT",
                    "pcb_serial_number": "TEXT",
                    "box_build_station": "TEXT",
                    "box_build_at": "TEXT",
                },
                "verification_history": {
                    "scanned_serial_number": "TEXT",
                    "serial_scan_result": "TEXT",
                    "gpon_number": "TEXT",
                    "pcb_serial_number": "TEXT",
                    "wifi_calibration_result": "TEXT",
                    "bob_calibration_result": "TEXT",
                    "firmware_result": "TEXT",
                    "firmware_version": "TEXT",
                },
                "stage_log_history": {
                    "pcb_serial_number": "TEXT",
                },
            }
            for table, columns in migrations.items():
                for column, declaration in columns.items():
                    if column not in existing[table]:
                        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_mac_pool_serial_unique ON mac_pool(UPPER(serial_number)) WHERE serial_number IS NOT NULL AND serial_number<>''")
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_mac_pool_gpon_unique ON mac_pool(UPPER(gpon_number)) WHERE gpon_number IS NOT NULL AND gpon_number<>''")
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_mac_pool_pcb_serial_unique ON mac_pool(UPPER(pcb_serial_number)) WHERE pcb_serial_number IS NOT NULL AND pcb_serial_number<>''")
            con.execute("CREATE INDEX IF NOT EXISTS idx_verify_gpon_status ON verification_history(gpon_number, status)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_stage_log_pcb_serial ON stage_log_history(pcb_serial_number)")
            FIRMWARE_DIR.mkdir(parents=True, exist_ok=True)
            try:
                startup_cfg = parse_key_value_file(CONFIG_PATH)
            except Exception:
                startup_cfg = {}
            defaults = {
                "firmware_enabled": startup_cfg.get("FIRMWARE_UPDATE_ENABLED", "0"),
                "firmware_file": startup_cfg.get("FIRMWARE_FILE", ""),
                "firmware_sha256": "",
                "firmware_size": "0",
            }
            for key, value in defaults.items():
                con.execute(
                    "INSERT OR IGNORE INTO system_settings(setting_key,setting_value,updated_at) VALUES(?,?,?)",
                    (key, str(value), now_iso()),
                )
            # If a release ships with a configured firmware image, preselect and hash it
            # without ever forcing the ON/OFF switch to ON. Existing user-selected
            # firmware in mac_server.db is preserved.
            configured_fw = Path(startup_cfg.get("FIRMWARE_FILE", "")).name
            if configured_fw:
                current_row = con.execute(
                    "SELECT setting_value FROM system_settings WHERE setting_key='firmware_file'"
                ).fetchone()
                current_fw = str(current_row[0] or "") if current_row else ""
                if not current_fw or current_fw == configured_fw:
                    fw_path = FIRMWARE_DIR / configured_fw
                    if fw_path.is_file():
                        digest = hashlib.sha256()
                        fw_size = 0
                        with fw_path.open("rb") as fh:
                            while True:
                                chunk = fh.read(1024 * 1024)
                                if not chunk:
                                    break
                                digest.update(chunk)
                                fw_size += len(chunk)
                        for fw_key, fw_value in (
                            ("firmware_file", configured_fw),
                            ("firmware_sha256", digest.hexdigest()),
                            ("firmware_size", str(fw_size)),
                        ):
                            con.execute(
                                "INSERT INTO system_settings(setting_key,setting_value,updated_at) VALUES(?,?,?) "
                                "ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value, updated_at=excluded.updated_at",
                                (fw_key, fw_value, now_iso()),
                            )
            # Backfill completed local records into the durable cloud queue. INSERT OR IGNORE
            # makes this safe on every startup and enables upgrades without losing history.
            completed_writer_rows = con.execute("SELECT * FROM mac_pool WHERE state IN ('PASS','FAIL','ERROR')").fetchall()
            for row in completed_writer_rows:
                self._queue_cloud_event(
                    con, f"writer:{row['reservation_id'] or row['mac']}:{row['state']}",
                    "IDENTITY_WRITER_RESULT", self._writer_cloud_payload(row),
                )
            completed_verify_rows = con.execute("SELECT * FROM verification_history WHERE status IN ('PASS','FAIL','ERROR')").fetchall()
            for row in completed_verify_rows:
                self._queue_cloud_event(
                    con, f"verify:{row['verification_id']}:{row['status']}",
                    "QUALITY_VERIFICATION_RESULT", self._verification_cloud_payload(row),
                )

    def get_setting(self, key: str, default: str = "") -> str:
        with self.connect() as con:
            row = con.execute("SELECT setting_value FROM system_settings WHERE setting_key=?", (key,)).fetchone()
            return str(row[0]) if row is not None else default

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as con:
            con.execute(
                "INSERT INTO system_settings(setting_key,setting_value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value, updated_at=excluded.updated_at",
                (key, str(value), now_iso()),
            )

    def firmware_config(self) -> dict:
        enabled_raw = self.get_setting("firmware_enabled", "0")
        enabled = enabled_raw.strip().lower() not in {"0", "no", "false", "off", ""}
        file_name = Path(self.get_setting("firmware_file", "")).name
        sha256 = self.get_setting("firmware_sha256", "").strip().lower()
        try:
            size = int(self.get_setting("firmware_size", "0") or 0)
        except ValueError:
            size = 0
        path = (FIRMWARE_DIR / file_name) if file_name else None
        available = bool(path and path.is_file() and sha256)
        if available and size <= 0:
            size = path.stat().st_size
        return {
            "enabled": enabled,
            "available": available,
            "file_name": file_name,
            "sha256": sha256,
            "size": size,
            "download_path": "/api/firmware/download",
        }

    def set_firmware_enabled(self, enabled: bool) -> dict:
        if enabled:
            cfg = self.firmware_config()
            if not cfg.get("available"):
                raise AppError("Select a firmware file on the server before enabling firmware update.")
        self.set_setting("firmware_enabled", "1" if enabled else "0")
        return self.firmware_config()

    def select_firmware_file(self, source: Path) -> dict:
        if not source.is_file():
            raise AppError(f"Firmware file not found: {source}")
        FIRMWARE_DIR.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", source.name) or "firmware.bin"
        destination = FIRMWARE_DIR / safe_name
        try:
            if source.resolve() != destination.resolve():
                shutil.copy2(source, destination)
        except OSError as exc:
            raise AppError(f"Could not copy firmware into server storage: {exc}") from exc
        digest = hashlib.sha256()
        size = 0
        with destination.open("rb") as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
        self.set_setting("firmware_file", safe_name)
        self.set_setting("firmware_sha256", digest.hexdigest())
        self.set_setting("firmware_size", str(size))
        return self.firmware_config()

    def import_identities(self, records: list[dict[str, str]]) -> dict[str, int]:
        """Bulk-import MAC/SERIAL/GPON identity rows without one SELECT per field per row.

        Large production lists (for example 40,000 identities) used to execute several
        SQLite lookups for every row. Besides being slow, the GUI called this method on
        Tk's event thread and Windows displayed "Not Responding" while the transaction
        was running. This implementation validates the list in memory, stages it in a
        temporary table, performs set-based conflict checks, and uses executemany for
        inserts/updates. The whole import remains a single transaction.
        """
        if not records:
            return {"added": 0, "updated": 0, "skipped": 0, "input": 0}

        prepared: list[tuple[int, str, str, str, str]] = []
        serial_seen: dict[str, tuple[str, str]] = {}
        gpon_seen: dict[str, tuple[str, str]] = {}
        for order, rec in enumerate(records):
            mac = validate_mac(rec.get("mac", ""))
            serial = (rec.get("serial_number") or "").strip()[:200]
            gpon = (rec.get("gpon_number") or "").strip()[:200]
            line_no = str(rec.get("line_no", "?"))
            if serial:
                key = serial.upper()
                prior = serial_seen.get(key)
                if prior and prior[0] != mac:
                    raise AppError(f"Line {line_no}: serial number {serial} is also used by {colon_mac(prior[0])} (line {prior[1]})")
                serial_seen[key] = (mac, line_no)
            if gpon:
                key = gpon.upper()
                prior = gpon_seen.get(key)
                if prior and prior[0] != mac:
                    raise AppError(f"Line {line_no}: GPON serial {gpon} is also used by {colon_mac(prior[0])} (line {prior[1]})")
                gpon_seen[key] = (mac, line_no)
            prepared.append((order, mac, serial, gpon, line_no))

        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                con.execute("DROP TABLE IF EXISTS temp.tmp_identity_import")
                con.execute("""CREATE TEMP TABLE tmp_identity_import(
                    input_order INTEGER PRIMARY KEY,
                    mac TEXT NOT NULL UNIQUE,
                    serial TEXT NOT NULL,
                    gpon TEXT NOT NULL,
                    line_no TEXT NOT NULL
                )""")
                con.executemany(
                    "INSERT INTO tmp_identity_import(input_order,mac,serial,gpon,line_no) VALUES (?,?,?,?,?)",
                    prepared,
                )

                # Check only the Serial/GPON values present in this import. Chunked IN
                # lookups use the UPPER(...) unique indexes and avoid a large join that can
                # become very slow when re-importing tens of thousands of existing rows.
                def check_existing_identifier(kind: str, seen: dict[str, tuple[str, str]]) -> None:
                    if not seen:
                        return
                    column = "serial_number" if kind == "serial" else "gpon_number"
                    label = "serial number" if kind == "serial" else "GPON serial"
                    keys = list(seen.keys())
                    for start in range(0, len(keys), 400):
                        chunk = keys[start:start + 400]
                        placeholders = ",".join("?" for _ in chunk)
                        rows = con.execute(
                            f"SELECT mac,{column} value FROM mac_pool WHERE UPPER({column}) IN ({placeholders})",
                            chunk,
                        ).fetchall()
                        for row in rows:
                            key = (row["value"] or "").upper()
                            expected = seen.get(key)
                            if expected and expected[0] != row["mac"]:
                                raise AppError(
                                    f"Line {expected[1]}: {label} {row['value']} is already assigned to {colon_mac(row['mac'])}"
                                )

                check_existing_identifier("serial", serial_seen)
                check_existing_identifier("gpon", gpon_seen)

                existing_rows = con.execute(
                    """SELECT m.mac,m.state,m.serial_number,m.gpon_number,t.serial,t.gpon,t.line_no,t.input_order
                       FROM tmp_identity_import t JOIN mac_pool m ON m.mac=t.mac"""
                ).fetchall()
                existing = {r["mac"]: r for r in existing_rows}

                inserts: list[tuple] = []
                updates: list[tuple] = []
                skipped = 0
                ts = now_iso()
                next_seq = con.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM mac_pool").fetchone()[0]

                for _, mac, serial, gpon, line_no in prepared:
                    row = existing.get(mac)
                    if row is None:
                        inserts.append((mac, next_seq, ts, serial or None, gpon or None, ts))
                        next_seq += 1
                        continue

                    old_serial = (row["serial_number"] or "").strip()
                    old_gpon = (row["gpon_number"] or "").strip()
                    if row["state"] == "AVAILABLE":
                        new_serial = serial or old_serial
                        new_gpon = gpon or old_gpon
                        if new_serial == old_serial and new_gpon == old_gpon:
                            skipped += 1
                        else:
                            updates.append((new_serial or None, new_gpon or None, ts, mac))
                    else:
                        # Never modify a reserved or completed production identity.
                        if serial and old_serial.upper() != serial.upper():
                            raise AppError(f"Line {line_no}: {colon_mac(mac)} is {row['state']} and cannot be changed to serial {serial}")
                        if gpon and old_gpon.upper() != gpon.upper():
                            raise AppError(f"Line {line_no}: {colon_mac(mac)} is {row['state']} and cannot be changed to GPON serial {gpon}")
                        skipped += 1

                if inserts:
                    con.executemany(
                        "INSERT INTO mac_pool(mac,seq,state,added_at,serial_number,gpon_number,updated_at) VALUES (?,?,'AVAILABLE',?,?,?,?)",
                        inserts,
                    )
                if updates:
                    con.executemany(
                        "UPDATE mac_pool SET serial_number=?, gpon_number=?, updated_at=? WHERE mac=? AND state='AVAILABLE'",
                        updates,
                    )
                con.commit()
            except Exception:
                con.rollback()
                raise

        return {"added": len(inserts), "updated": len(updates), "skipped": skipped, "input": len(records)}

    def import_macs(self, macs: list[str]) -> dict[str, int]:
        # Backward compatibility for callers that still submit MAC-only data.
        return self.import_identities([{"mac": m, "serial_number": "", "gpon_number": ""} for m in macs])

    def touch_client(self, client_id: str, hostname: str, ip: str, app_version: str) -> None:
        client_id = (client_id or "UNKNOWN").strip()[:100]
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO clients(client_id, hostname, last_ip, last_seen, app_version)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(client_id) DO UPDATE SET hostname=excluded.hostname,
                    last_ip=excluded.last_ip, last_seen=excluded.last_seen, app_version=excluded.app_version
                """,
                (client_id, (hostname or "")[:100], (ip or "")[:64], now_iso(), (app_version or "")[:32]),
            )

    # ---------- Cloud MES synchronization ----------
    def _queue_cloud_event(self, con: sqlite3.Connection, event_id: str, event_type: str, payload: dict) -> None:
        ts = now_iso()
        body = dict(payload)
        body["event_id"] = event_id
        body["event_type"] = event_type
        con.execute(
            """INSERT OR IGNORE INTO cloud_sync_queue(
               event_id,event_type,payload_json,status,attempts,created_at,updated_at)
               VALUES (?,?,?,'PENDING',0,?,?)""",
            (event_id, event_type, json.dumps(body, separators=(",", ":"), ensure_ascii=False), ts, ts),
        )

    def _writer_cloud_payload(self, row: sqlite3.Row, plant_id: str = "") -> dict:
        client_id = row["client_id"] or ""
        slot = ""
        if "/R" in client_id.upper():
            try:
                slot = int(client_id.upper().rsplit("/R", 1)[1])
            except (ValueError, IndexError):
                slot = ""
        return {
            "plant_id": plant_id,
            "station_id": client_id,
            "router_slot": slot,
            "router_ip": "",
            "mac": colon_mac(row["mac"]),
            "serial_number": row["serial_number"] or "",
            "gpon_number": row["gpon_number"] or "",
            "pcb_serial_number": row["pcb_serial_number"] or "",
            "part_number": "",
            "status": row["state"],
            "writer_state": row["state"],
            "detail": row["result_detail"] or "",
            "completed_at": row["completed_at"] or row["updated_at"] or now_iso(),
            "source_request_id": row["request_id"] or "",
            "source_reservation_id": row["reservation_id"] or "",
        }

    def _verification_cloud_payload(self, row: sqlite3.Row, plant_id: str = "") -> dict:
        client_id = row["client_id"] or ""
        slot = ""
        if "/R" in client_id.upper():
            try:
                slot = int(client_id.upper().rsplit("/R", 1)[1])
            except (ValueError, IndexError):
                slot = ""
        return {
            "plant_id": plant_id,
            "station_id": client_id,
            "router_slot": slot,
            "router_ip": row["router_ip"] or "",
            "mac": colon_mac(row["mac"]),
            "serial_number": row["serial_number"] or "",
            "scanned_serial_number": row["scanned_serial_number"] or "",
            "gpon_number": row["gpon_number"] or "",
            "pcb_serial_number": row["pcb_serial_number"] or "",
            "part_number": row["part_number"] or "",
            "status": row["status"],
            "writer_state": row["pool_state"] or "",
            "server_check": row["server_check"] or "",
            "serial_scan_result": row["serial_scan_result"] or "",
            "wifi_calibration_result": row["wifi_calibration_result"] or "",
            "bob_calibration_result": row["bob_calibration_result"] or "",
            "firmware_result": row["firmware_result"] or "",
            "firmware_version": row["firmware_version"] or "",
            "led_result": row["led_result"] or "",
            "reset_result": row["reset_result"] or "",
            "wps_result": row["wps_result"] or "",
            "user_mode_result": row["user_mode_result"] or "",
            "detail": row["detail"] or "",
            "completed_at": row["completed_at"] or row["updated_at"] or now_iso(),
            "source_request_id": row["request_id"] or "",
            "source_verification_id": row["verification_id"] or "",
        }

    def pending_cloud_events(self, limit: int = 100, plant_id: str = "") -> list[dict]:
        limit = max(1, min(int(limit), 500))
        with self.connect() as con:
            rows = con.execute(
                "SELECT event_id,payload_json FROM cloud_sync_queue WHERE status='PENDING' ORDER BY created_at LIMIT ?",
                (limit,),
            ).fetchall()
        events: list[dict] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            payload["plant_id"] = plant_id or payload.get("plant_id", "")
            events.append(payload)
        return events

    def mark_cloud_synced(self, event_ids: list[str]) -> None:
        ids = [str(x) for x in event_ids if x]
        if not ids:
            return
        ts = now_iso()
        with self.connect() as con:
            con.executemany(
                "UPDATE cloud_sync_queue SET status='SYNCED',synced_at=?,updated_at=?,last_error=NULL WHERE event_id=?",
                [(ts, ts, event_id) for event_id in ids],
            )

    def mark_cloud_error(self, event_ids: list[str], error_text: str) -> None:
        ids = [str(x) for x in event_ids if x]
        if not ids:
            return
        ts = now_iso()
        message = (error_text or "Cloud synchronization failed")[:2000]
        with self.connect() as con:
            con.executemany(
                "UPDATE cloud_sync_queue SET attempts=attempts+1,last_error=?,updated_at=? WHERE event_id=?",
                [(message, ts, event_id) for event_id in ids],
            )

    def cloud_sync_stats(self) -> dict:
        with self.connect() as con:
            rows = con.execute("SELECT status,COUNT(*) c FROM cloud_sync_queue GROUP BY status").fetchall()
            last = con.execute(
                "SELECT synced_at FROM cloud_sync_queue WHERE status='SYNCED' ORDER BY synced_at DESC LIMIT 1"
            ).fetchone()
            failed = con.execute(
                "SELECT COUNT(*) c FROM cloud_sync_queue WHERE status='PENDING' AND attempts>0"
            ).fetchone()["c"]
        counts = {row["status"]: int(row["c"]) for row in rows}
        return {
            "pending": counts.get("PENDING", 0),
            "synced": counts.get("SYNCED", 0),
            "retrying": int(failed or 0),
            "last_synced_at": (last["synced_at"] if last else "") or "",
        }

    def recent_cloud_events(self, limit: int = 200) -> list[dict]:
        with self.connect() as con:
            rows = con.execute(
                """SELECT event_id,event_type,status,attempts,last_error,created_at,synced_at
                   FROM cloud_sync_queue ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def cloud_snapshot_event(self, plant_id: str) -> dict:
        stats = self.stats()
        minute = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d%H%M")
        return {
            "event_id": f"snapshot:{plant_id}:{minute}",
            "event_type": "MAC_POOL_SNAPSHOT",
            "plant_id": plant_id,
            "station_id": "CENTRAL-SERVER",
            "router_slot": "",
            "router_ip": "",
            "mac": "",
            "serial_number": "",
            "gpon_number": "",
            "pcb_serial_number": "",
            "part_number": "",
            "status": "INFO",
            "completed_at": now_iso(),
            "metrics": stats,
        }

    # ---------- External production-stage log ingestion ----------
    ALLOWED_STAGE_NAMES = {
        "WIFI_CALIBRATION",
        "LABEL_PRINTING",
        "BOB_CALIBRATION",
        "WIFI_COUPLING_VOIP",
        "BOX_BUILD",
    }

    def report_stage_log_batch(self, client_id: str, records: list[dict], client_ip: str) -> dict:
        client_id = (client_id or "STAGE-LOG-COLLECTOR").strip()[:150]
        if not isinstance(records, list) or not records:
            raise AppError("records must be a non-empty list")
        if len(records) > 500:
            raise AppError("Maximum 500 stage records per request")
        accepted: list[str] = []
        duplicates: list[str] = []
        rejected: list[dict] = []
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            for raw in records:
                try:
                    if not isinstance(raw, dict):
                        raise AppError("record must be an object")
                    event_id = str(raw.get("event_id", "")).strip()[:250]
                    stage_name = str(raw.get("stage_name", "")).strip().upper()
                    status = str(raw.get("status", "")).strip().upper()
                    if not event_id:
                        raise AppError("event_id is required")
                    if stage_name not in self.ALLOWED_STAGE_NAMES:
                        raise AppError(f"Unsupported stage_name: {stage_name}")
                    if status not in {"PASS", "FAIL", "ERROR"}:
                        raise AppError("status must be PASS, FAIL, or ERROR")
                    mac_text = str(raw.get("mac", "")).strip()
                    mac = validate_mac(mac_text) if mac_text else ""
                    completed_at = str(raw.get("completed_at", "")).strip() or now_iso()
                    station_id = str(raw.get("station_id", "")).strip()[:150] or client_id
                    values = (
                        event_id, stage_name, station_id, (client_ip or "")[:64],
                        str(raw.get("source_file", ""))[:1000], int(raw.get("source_offset", 0) or 0),
                        status, mac, str(raw.get("serial_number", ""))[:250],
                        str(raw.get("gpon_number", ""))[:250], str(raw.get("pcb_serial_number", ""))[:250],
                        str(raw.get("part_number", ""))[:250],
                        str(raw.get("detail", ""))[:8000], str(raw.get("raw_record", ""))[:16000],
                        completed_at, now_iso(),
                    )
                    cur = con.execute(
                        """INSERT OR IGNORE INTO stage_log_history(
                           event_id,stage_name,station_id,client_ip,source_file,source_offset,status,
                           mac,serial_number,gpon_number,pcb_serial_number,part_number,detail,raw_record,completed_at,received_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values,
                    )
                    if cur.rowcount == 0:
                        duplicates.append(event_id)
                        continue
                    accepted.append(event_id)
                    cloud_payload = {
                        "plant_id": "",
                        "station_id": station_id,
                        "router_slot": str(raw.get("router_slot", ""))[:30],
                        "router_ip": str(raw.get("router_ip", ""))[:100],
                        "stage_name": stage_name,
                        "source_file": str(raw.get("source_file", ""))[:1000],
                        "source_offset": int(raw.get("source_offset", 0) or 0),
                        "mac": colon_mac(mac) if mac else "",
                        "serial_number": str(raw.get("serial_number", ""))[:250],
                        "gpon_number": str(raw.get("gpon_number", ""))[:250],
                        "pcb_serial_number": str(raw.get("pcb_serial_number", ""))[:250],
                        "part_number": str(raw.get("part_number", ""))[:250],
                        "status": status,
                        "detail": str(raw.get("detail", ""))[:8000],
                        "raw_log": str(raw.get("raw_record", ""))[:16000],
                        "completed_at": completed_at,
                        "source_event_id": event_id,
                    }
                    self._queue_cloud_event(con, f"stage:{event_id}", "STAGE_LOG_RESULT", cloud_payload)
                except Exception as exc:
                    rejected.append({"event_id": str(raw.get("event_id", ""))[:250] if isinstance(raw, dict) else "", "error": str(exc)})
            con.commit()
        return {"ok": not rejected, "accepted_event_ids": accepted, "duplicate_event_ids": duplicates, "rejected": rejected}

    def stage_log_stats(self) -> dict:
        with self.connect() as con:
            rows = con.execute(
                """SELECT stage_name,status,COUNT(*) AS c FROM stage_log_history
                   GROUP BY stage_name,status ORDER BY stage_name,status"""
            ).fetchall()
            recent = con.execute(
                """SELECT stage_name,station_id,status,mac,serial_number,pcb_serial_number,completed_at
                   FROM stage_log_history ORDER BY id DESC LIMIT 20"""
            ).fetchall()
        by_stage: dict[str, dict[str, int]] = {}
        for row in rows:
            item = by_stage.setdefault(row["stage_name"], {"pass": 0, "fail": 0, "error": 0, "total": 0})
            key = str(row["status"]).lower()
            count = int(row["c"])
            item[key] = count
            item["total"] += count
        return {"stages": by_stage, "recent": [dict(row) for row in recent]}

    # ---------- Existing MAC writer API ----------
    def allocate(self, client_id: str, request_id: str, client_ip: str, require_pcb_serial: bool = False) -> dict:
        client_id = client_id.strip()
        request_id = request_id.strip()
        require_pcb_serial = bool(require_pcb_serial)
        if not client_id or not request_id:
            raise AppError("client_id and request_id are required")
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute("SELECT * FROM mac_pool WHERE request_id=?", (request_id,)).fetchone()
            if existing is not None:
                if existing["client_id"] != client_id:
                    con.rollback()
                    raise AppError("request_id already belongs to another client")
                if require_pcb_serial and not (existing["pcb_serial_number"] or "").strip():
                    con.rollback()
                    raise AppError("NO PCB SERIAL NUMBER PRESENT — Complete Box Build before MAC Write. MAC was not allocated.")
                con.commit()
                return self._reservation_payload(existing, True)

            # Always inspect the first AVAILABLE identity in sequence. When the PCB gate is
            # enabled we deliberately do NOT skip an unlinked row and allocate a later row,
            # because that could associate the wrong pre-built PCB with the wrong identity.
            row = con.execute("SELECT * FROM mac_pool WHERE state='AVAILABLE' ORDER BY seq LIMIT 1").fetchone()
            if row is None:
                con.rollback()
                raise LookupError("MAC queue is empty")
            if require_pcb_serial and not (row["pcb_serial_number"] or "").strip():
                con.rollback()
                raise AppError("NO PCB SERIAL NUMBER PRESENT — Complete Box Build before MAC Write. MAC was not allocated.")

            reservation_id = str(uuid.uuid4())
            ts = now_iso()
            cur = con.execute(
                """UPDATE mac_pool SET state='RESERVED', request_id=?, reservation_id=?, client_id=?,
                   client_ip=?, reserved_at=?, updated_at=? WHERE mac=? AND state='AVAILABLE'""",
                (request_id, reservation_id, client_id, client_ip, ts, ts, row["mac"]),
            )
            if cur.rowcount != 1:
                con.rollback()
                raise AppError("Allocation race detected; retry request")
            allocated = con.execute("SELECT * FROM mac_pool WHERE reservation_id=?", (reservation_id,)).fetchone()
            con.commit()
            return self._reservation_payload(allocated, False)

    def _reservation_payload(self, row: sqlite3.Row, reused: bool) -> dict:
        return {
            "mac": colon_mac(row["mac"]), "mac_normalized": row["mac"], "seq": row["seq"],
            "state": row["state"], "reservation_id": row["reservation_id"], "request_id": row["request_id"],
            "client_id": row["client_id"], "reserved_at": row["reserved_at"], "reused_request": reused,
            "serial_number": row["serial_number"] or "", "gpon_number": row["gpon_number"] or "",
            "pcb_serial_number": row["pcb_serial_number"] or "",
        }

    def lookup_identity_by_scan(self, scan_value: str) -> dict:
        """Resolve one scanned label value as MAC, hardware serial, or GPON serial.

        A barcode scanner normally behaves like a keyboard, so the writer sends the
        exact scanned text here. The lookup is case-insensitive for serial fields and
        also accepts MACs with or without separators. If one token would resolve to
        more than one identity row, programming is blocked instead of guessing.
        """
        raw = (scan_value or "").strip()[:250]
        if not raw:
            raise AppError("SCAN_VALUE_REQUIRED")
        compact = re.sub(r"[^0-9A-Fa-f]", "", raw).upper()
        mac_candidate = compact if len(compact) == 12 and re.fullmatch(r"[0-9A-F]{12}", compact) else "__NO_MAC__"
        with self.connect() as con:
            rows = con.execute(
                """SELECT * FROM mac_pool
                   WHERE mac=? OR UPPER(COALESCE(serial_number,''))=UPPER(?)
                              OR UPPER(COALESCE(gpon_number,''))=UPPER(?)""",
                (mac_candidate, raw, raw),
            ).fetchall()
        # Collapse accidental multi-field matches on the same row, but never guess
        # when the same scanned token points to different identity rows.
        unique = {row["mac"]: row for row in rows}
        if not unique:
            raise AppError("IDENTITY_NOT_FOUND_IN_MASTER_LIST")
        if len(unique) != 1:
            raise AppError("AMBIGUOUS_LABEL_SCAN")
        row = next(iter(unique.values()))
        serial = (row["serial_number"] or "").strip()
        gpon = (row["gpon_number"] or "").strip()
        if not serial or not gpon:
            raise AppError("IDENTITY_ROW_IS_INCOMPLETE")
        matched_by = []
        if row["mac"] == mac_candidate:
            matched_by.append("MAC")
        if serial.upper() == raw.upper():
            matched_by.append("SERIAL")
        if gpon.upper() == raw.upper():
            matched_by.append("GPON")
        return {
            "scan_value": raw,
            "matched_by": matched_by,
            "mac": colon_mac(row["mac"]),
            "serial_number": serial,
            "gpon_number": gpon,
            "pcb_serial_number": row["pcb_serial_number"] or "",
            "box_build_station": row["box_build_station"] or "",
            "box_build_at": row["box_build_at"] or "",
            "state": row["state"],
        }

    def allocate_specific(self, client_id: str, request_id: str, client_ip: str, mac: str,
                          require_pcb_serial: bool = False) -> dict:
        """Atomically reserve the exact identity selected by a label scan."""
        client_id = (client_id or "").strip()
        request_id = (request_id or "").strip()
        mac_n = validate_mac(mac)
        if not client_id or not request_id:
            raise AppError("client_id and request_id are required")
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute("SELECT * FROM mac_pool WHERE request_id=?", (request_id,)).fetchone()
            if existing is not None:
                if existing["client_id"] != client_id:
                    con.rollback(); raise AppError("request_id already belongs to another client")
                if existing["mac"] != mac_n:
                    con.rollback(); raise AppError("request_id already belongs to a different scanned identity")
                if require_pcb_serial and not (existing["pcb_serial_number"] or "").strip():
                    con.rollback(); raise AppError("NO PCB SERIAL NUMBER PRESENT — Complete Box Build before MAC Write. MAC was not allocated.")
                con.commit()
                return self._reservation_payload(existing, True)

            row = con.execute("SELECT * FROM mac_pool WHERE mac=?", (mac_n,)).fetchone()
            if row is None:
                con.rollback(); raise AppError("IDENTITY_NOT_FOUND_IN_MASTER_LIST")
            if row["state"] != "AVAILABLE":
                con.rollback(); raise AppError(f"SCANNED_IDENTITY_NOT_AVAILABLE — current state is {row['state']}")
            if require_pcb_serial and not (row["pcb_serial_number"] or "").strip():
                con.rollback(); raise AppError("NO PCB SERIAL NUMBER PRESENT — Complete Box Build before MAC Write. MAC was not allocated.")

            reservation_id = str(uuid.uuid4())
            ts = now_iso()
            cur = con.execute(
                """UPDATE mac_pool SET state='RESERVED', request_id=?, reservation_id=?, client_id=?,
                   client_ip=?, reserved_at=?, updated_at=? WHERE mac=? AND state='AVAILABLE'""",
                (request_id, reservation_id, client_id, client_ip, ts, ts, mac_n),
            )
            if cur.rowcount != 1:
                con.rollback(); raise AppError("Allocation race detected; rescan label")
            allocated = con.execute("SELECT * FROM mac_pool WHERE reservation_id=?", (reservation_id,)).fetchone()
            con.commit()
            return self._reservation_payload(allocated, False)

    def report_result(self, client_id: str, reservation_id: str, status: str, detail: str,
                      serial_number: str = "", gpon_number: str = "") -> dict:
        status = status.upper().strip()
        if status not in {"PASS", "FAIL", "ERROR"}:
            raise AppError("status must be PASS, FAIL, or ERROR")
        serial = (serial_number or "").strip()[:200]
        gpon = (gpon_number or "").strip()[:200]
        if status == "PASS" and (not serial or not gpon):
            raise AppError("serial_number and gpon_number are required for a PASS writer result")
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM mac_pool WHERE reservation_id=?", (reservation_id,)).fetchone()
            if row is None:
                con.rollback(); raise AppError("Unknown reservation_id")
            if row["client_id"] != client_id:
                con.rollback(); raise AppError("Reservation belongs to another client")
            if row["state"] in {"PASS", "FAIL", "ERROR"}:
                if row["state"] == status:
                    if serial and row["serial_number"] and row["serial_number"].upper() != serial.upper():
                        con.rollback(); raise AppError("Final reservation already contains a different serial number")
                    if gpon and row["gpon_number"] and row["gpon_number"].upper() != gpon.upper():
                        con.rollback(); raise AppError("Final reservation already contains a different GPON number")
                    self._queue_cloud_event(
                        con, f"writer:{reservation_id}:{status}", "IDENTITY_WRITER_RESULT",
                        self._writer_cloud_payload(row),
                    )
                    con.commit()
                    return {"ok": True, "already_final": True, "state": row["state"], "mac": colon_mac(row["mac"]),
                            "serial_number": row["serial_number"] or "", "gpon_number": row["gpon_number"] or ""}
                con.rollback(); raise AppError(f"Reservation is already final as {row['state']}")
            if row["state"] != "RESERVED":
                con.rollback(); raise AppError(f"Reservation is in unexpected state {row['state']}")
            try:
                ts = now_iso()
                con.execute(
                    "UPDATE mac_pool SET state=?, completed_at=?, result_detail=?, serial_number=?, gpon_number=?, updated_at=? WHERE reservation_id=?",
                    (status, ts, (detail or "")[:4000], serial or None, gpon or None, ts, reservation_id),
                )
                updated = con.execute("SELECT * FROM mac_pool WHERE reservation_id=?", (reservation_id,)).fetchone()
                self._queue_cloud_event(
                    con, f"writer:{reservation_id}:{status}", "IDENTITY_WRITER_RESULT",
                    self._writer_cloud_payload(updated),
                )
                con.commit()
            except sqlite3.IntegrityError as exc:
                con.rollback()
                raise AppError("Serial number or GPON number is already assigned to another MAC") from exc
            return {"ok": True, "already_final": False, "state": status, "mac": colon_mac(row["mac"]),
                    "serial_number": serial, "gpon_number": gpon}

    # ---------- Box Build identity-linking API ----------
    def lookup_box_build_identity(self, *, mac: str, serial_number: str = "", gpon_number: str = "") -> dict:
        mac_n = validate_mac(mac)
        serial = (serial_number or "").strip()[:250]
        gpon = (gpon_number or "").strip()[:250]
        with self.connect() as con:
            row = con.execute("SELECT * FROM mac_pool WHERE mac=?", (mac_n,)).fetchone()
        if row is None:
            raise AppError("MAC_NOT_IN_MASTER_LIST")
        expected_serial = (row["serial_number"] or "").strip()
        expected_gpon = (row["gpon_number"] or "").strip()
        if serial and expected_serial and serial.upper() != expected_serial.upper():
            raise AppError("SERIAL_DOES_NOT_MATCH_MAC")
        if gpon and expected_gpon and gpon.upper() != expected_gpon.upper():
            raise AppError("GPON_DOES_NOT_MATCH_MAC")
        if not expected_serial or not expected_gpon:
            raise AppError("IDENTITY_ROW_IS_INCOMPLETE")
        return {
            "mac": colon_mac(row["mac"]),
            "serial_number": expected_serial,
            "gpon_number": expected_gpon,
            "pcb_serial_number": row["pcb_serial_number"] or "",
            "box_build_station": row["box_build_station"] or "",
            "box_build_at": row["box_build_at"] or "",
            "writer_state": row["state"],
        }

    def bind_pcb_serial(self, *, client_id: str, event_id: str, mac: str, serial_number: str,
                        gpon_number: str, pcb_serial_number: str, raw_label: str,
                        client_ip: str) -> dict:
        client_id = (client_id or "BOX-BUILD-01").strip()[:150]
        event_id = (event_id or "").strip()[:250] or str(uuid.uuid4())
        mac_n = validate_mac(mac)
        serial = (serial_number or "").strip()[:250]
        gpon = (gpon_number or "").strip()[:250]
        pcb = (pcb_serial_number or "").strip()[:250]
        if not serial or not gpon:
            raise AppError("Serial number and GPON serial number are required")
        if not pcb:
            raise AppError("PCB serial number is required")
        if len(pcb) < 3:
            raise AppError("PCB serial number is too short")
        ts = now_iso()
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM mac_pool WHERE mac=?", (mac_n,)).fetchone()
            if row is None:
                con.rollback(); raise AppError("MAC_NOT_IN_MASTER_LIST")
            expected_serial = (row["serial_number"] or "").strip()
            expected_gpon = (row["gpon_number"] or "").strip()
            if not expected_serial or not expected_gpon:
                con.rollback(); raise AppError("IDENTITY_ROW_IS_INCOMPLETE")
            if expected_serial.upper() != serial.upper():
                con.rollback(); raise AppError("SERIAL_DOES_NOT_MATCH_MAC")
            if expected_gpon.upper() != gpon.upper():
                con.rollback(); raise AppError("GPON_DOES_NOT_MATCH_MAC")

            other = con.execute(
                "SELECT mac,serial_number,gpon_number FROM mac_pool WHERE UPPER(pcb_serial_number)=UPPER(?) AND mac<>? LIMIT 1",
                (pcb, mac_n),
            ).fetchone()
            if other is not None:
                con.rollback(); raise AppError(
                    f"PCB_SERIAL_ALREADY_LINKED_TO_{colon_mac(other['mac'])}"
                )

            existing_pcb = (row["pcb_serial_number"] or "").strip()
            if existing_pcb and existing_pcb.upper() != pcb.upper():
                con.rollback(); raise AppError("MAC_ALREADY_LINKED_TO_DIFFERENT_PCB_SERIAL")

            already_bound = bool(existing_pcb)
            if not already_bound:
                try:
                    con.execute(
                        "UPDATE mac_pool SET pcb_serial_number=?,box_build_station=?,box_build_at=?,updated_at=? WHERE mac=?",
                        (pcb, client_id, ts, ts, mac_n),
                    )
                except sqlite3.IntegrityError as exc:
                    con.rollback(); raise AppError("PCB serial number is already linked to another identity") from exc
            else:
                # Keep original binding timestamp, but record the current station if it was previously blank.
                con.execute(
                    "UPDATE mac_pool SET box_build_station=COALESCE(NULLIF(box_build_station,''),?),box_build_at=COALESCE(NULLIF(box_build_at,''),?),updated_at=? WHERE mac=?",
                    (client_id, ts, ts, mac_n),
                )

            updated = con.execute("SELECT * FROM mac_pool WHERE mac=?", (mac_n,)).fetchone()
            stage_event_id = "boxbuild:" + uuid.uuid5(uuid.NAMESPACE_URL, f"{mac_n}|{pcb.upper()}").hex
            detail = f"Linked PCB serial {pcb} to production identity"
            cur = con.execute(
                """INSERT OR IGNORE INTO stage_log_history(
                   event_id,stage_name,station_id,client_ip,source_file,source_offset,status,
                   mac,serial_number,gpon_number,pcb_serial_number,part_number,detail,raw_record,completed_at,received_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (stage_event_id, "BOX_BUILD", client_id, (client_ip or "")[:64], "BOX_BUILD_SCANNER", 0,
                 "PASS", mac_n, expected_serial, expected_gpon, pcb, "", detail,
                 (raw_label or "")[:16000], ts, ts),
            )
            # If the request is a retry with a new event_id, the identity remains idempotent and
            # the history row is still safe because the PCB/MAC binding itself is unique.
            cloud_payload = {
                "plant_id": "", "station_id": client_id, "router_slot": "", "router_ip": "",
                "stage_name": "BOX_BUILD", "source_file": "BOX_BUILD_SCANNER", "source_offset": 0,
                "mac": colon_mac(mac_n), "serial_number": expected_serial, "gpon_number": expected_gpon,
                "pcb_serial_number": pcb, "part_number": "", "status": "PASS", "detail": detail,
                "raw_log": (raw_label or "")[:16000], "completed_at": ts, "source_event_id": event_id,
            }
            self._queue_cloud_event(con, stage_event_id, "STAGE_LOG_RESULT", cloud_payload)
            con.commit()
            return {
                "ok": True, "already_bound": already_bound, "history_inserted": cur.rowcount == 1,
                "event_id": event_id, "mac": colon_mac(updated["mac"]),
                "serial_number": updated["serial_number"] or "", "gpon_number": updated["gpon_number"] or "",
                "pcb_serial_number": updated["pcb_serial_number"] or "",
                "box_build_station": updated["box_build_station"] or "", "box_build_at": updated["box_build_at"] or "",
            }

    # ---------- Verification API ----------
    def begin_verification(self, *, client_id: str, request_id: str, mac: str, serial_number: str,
                           scanned_serial_number: str, gpon_number: str, part_number: str,
                           router_ip: str, client_ip: str) -> dict:
        client_id = client_id.strip()
        request_id = request_id.strip()
        mac_n = validate_mac(mac)
        serial = (serial_number or "").strip()[:200]
        scanned_serial = (scanned_serial_number or "").strip()[:200]
        gpon = (gpon_number or "").strip()[:200]
        part = (part_number or "").strip()[:200]
        router_ip = (router_ip or "").strip()[:64]
        if not client_id or not request_id:
            raise AppError("client_id and request_id are required")
        if not serial:
            raise AppError("serial_number is required")
        if not gpon:
            raise AppError("gpon_number is required")
        if not part:
            raise AppError("part_number is required")

        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute("SELECT * FROM verification_history WHERE request_id=?", (request_id,)).fetchone()
            if existing is not None:
                if existing["client_id"] != client_id:
                    con.rollback(); raise AppError("request_id already belongs to another verifier client")
                payload = self._verification_check_payload(con, existing)
                con.commit()
                payload["reused_request"] = True
                return payload

            pool = con.execute("SELECT * FROM mac_pool WHERE mac=?", (mac_n,)).fetchone()
            pool_state = pool["state"] if pool is not None else "NOT_FOUND"
            reasons: list[str] = []
            if scanned_serial and scanned_serial.upper() != serial.upper():
                reasons.append("SCANNED_SERIAL_DOES_NOT_MATCH_ROUTER_SERIAL")
            if pool is None:
                reasons.append("MAC_NOT_IN_MASTER_LIST")
            elif pool_state == "AVAILABLE":
                reasons.append("MAC_NOT_USED")
            elif pool_state != "PASS":
                reasons.append(f"MAC_WRITER_STATE_{pool_state}")
            if pool is not None and pool_state == "PASS":
                writer_serial = (pool["serial_number"] or "").strip()
                writer_gpon = (pool["gpon_number"] or "").strip()
                if not writer_serial:
                    reasons.append("WRITER_SERIAL_NOT_RECORDED")
                elif writer_serial.upper() != serial.upper():
                    reasons.append("SERIAL_DOES_NOT_MATCH_WRITER_RECORD")
                if not writer_gpon:
                    reasons.append("WRITER_GPON_NOT_RECORDED")
                elif writer_gpon.upper() != gpon.upper():
                    reasons.append("GPON_DOES_NOT_MATCH_WRITER_RECORD")

            active_mac = con.execute(
                "SELECT client_id, router_ip, serial_number FROM verification_history WHERE mac=? AND status='STARTED'",
                (mac_n,),
            ).fetchone()
            if active_mac is not None:
                reasons.append("MAC_ALREADY_UNDER_VERIFICATION")

            active_serial = con.execute(
                "SELECT mac, client_id, router_ip FROM verification_history "
                "WHERE UPPER(serial_number)=UPPER(?) AND status='STARTED' AND mac<>? LIMIT 1",
                (serial, mac_n),
            ).fetchone()
            if active_serial is not None:
                reasons.append("SERIAL_ALREADY_UNDER_VERIFICATION")

            prior_mac_pass = con.execute(
                "SELECT serial_number, client_id, router_ip, completed_at FROM verification_history "
                "WHERE mac=? AND status='PASS' ORDER BY id DESC LIMIT 1", (mac_n,)
            ).fetchone()
            is_retest = False
            if prior_mac_pass is not None:
                if (prior_mac_pass["serial_number"] or "").strip().upper() != serial.upper():
                    reasons.append("MAC_ALREADY_VERIFIED_WITH_DIFFERENT_SERIAL")
                else:
                    is_retest = True

            prior_serial_pass = con.execute(
                "SELECT mac, client_id, router_ip, completed_at FROM verification_history "
                "WHERE UPPER(serial_number)=UPPER(?) AND status='PASS' ORDER BY id DESC LIMIT 1", (serial,)
            ).fetchone()
            if prior_serial_pass is not None and prior_serial_pass["mac"] != mac_n:
                reasons.append("SERIAL_ALREADY_VERIFIED_WITH_DIFFERENT_MAC")

            active_gpon = con.execute(
                "SELECT mac, client_id, router_ip FROM verification_history "
                "WHERE UPPER(gpon_number)=UPPER(?) AND status='STARTED' AND mac<>? LIMIT 1",
                (gpon, mac_n),
            ).fetchone()
            if active_gpon is not None:
                reasons.append("GPON_ALREADY_UNDER_VERIFICATION")
            prior_gpon_pass = con.execute(
                "SELECT mac FROM verification_history WHERE UPPER(gpon_number)=UPPER(?) "
                "AND status='PASS' ORDER BY id DESC LIMIT 1", (gpon,)
            ).fetchone()
            if prior_gpon_pass is not None and prior_gpon_pass["mac"] != mac_n:
                reasons.append("GPON_ALREADY_VERIFIED_WITH_DIFFERENT_MAC")

            allowed = not reasons
            verification_id = str(uuid.uuid4())
            ts = now_iso()
            server_check = "PASS" if allowed else ",".join(reasons)
            con.execute(
                """INSERT INTO verification_history(
                   verification_id, request_id, mac, serial_number, scanned_serial_number, serial_scan_result,
                   gpon_number, pcb_serial_number, part_number, client_id, client_ip, router_ip, status, pool_state, server_check,
                   started_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'STARTED', ?, ?, ?, ?)""",
                (verification_id, request_id, mac_n, serial, scanned_serial,
                 "PASS" if scanned_serial and scanned_serial.upper() == serial.upper() else ("FAIL" if scanned_serial else ""),
                 gpon, (pool["pcb_serial_number"] if pool is not None else "") or "", part, client_id, client_ip, router_ip, pool_state, server_check, ts, ts),
            )
            row = con.execute("SELECT * FROM verification_history WHERE verification_id=?", (verification_id,)).fetchone()
            con.commit()
            return {
                "verification_id": verification_id,
                "allowed": allowed,
                "reused_request": False,
                "is_retest": is_retest,
                "mac": colon_mac(mac_n),
                "serial_number": serial,
                "scanned_serial_number": scanned_serial,
                "serial_scan_result": "PASS" if scanned_serial and scanned_serial.upper() == serial.upper() else ("FAIL" if scanned_serial else ""),
                "gpon_number": gpon,
                "pcb_serial_number": (pool["pcb_serial_number"] if pool is not None else "") or "",
                "part_number": part,
                "pool_state": pool_state,
                "in_master_list": pool is not None,
                "in_used_list": pool is not None and pool_state != "AVAILABLE",
                "writer_passed": pool_state == "PASS",
                "reasons": reasons,
                "server_check": server_check,
            }

    def _verification_check_payload(self, con: sqlite3.Connection, row: sqlite3.Row) -> dict:
        reasons = [] if row["server_check"] == "PASS" else [x for x in (row["server_check"] or "").split(",") if x]
        return {
            "verification_id": row["verification_id"], "allowed": row["server_check"] == "PASS",
            "mac": colon_mac(row["mac"]), "serial_number": row["serial_number"] or "",
            "scanned_serial_number": row["scanned_serial_number"] or "", "serial_scan_result": row["serial_scan_result"] or "",
            "gpon_number": row["gpon_number"] or "", "pcb_serial_number": row["pcb_serial_number"] or "",
            "part_number": row["part_number"] or "", "pool_state": row["pool_state"] or "",
            "in_master_list": (row["pool_state"] or "") != "NOT_FOUND",
            "in_used_list": (row["pool_state"] or "") not in {"", "NOT_FOUND", "AVAILABLE"},
            "writer_passed": row["pool_state"] == "PASS", "reasons": reasons,
            "server_check": row["server_check"] or "", "status": row["status"],
        }

    def report_verification(self, *, client_id: str, verification_id: str, status: str,
                            serial_scan_result: str, wifi_calibration_result: str, bob_calibration_result: str,
                            firmware_result: str, firmware_version: str,
                            led_result: str, reset_result: str, wps_result: str,
                            user_mode_result: str, detail: str) -> dict:
        status = status.upper().strip()
        if status not in {"PASS", "FAIL", "ERROR"}:
            raise AppError("verification status must be PASS, FAIL, or ERROR")
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM verification_history WHERE verification_id=?", (verification_id,)).fetchone()
            if row is None:
                con.rollback(); raise AppError("Unknown verification_id")
            if row["client_id"] != client_id:
                con.rollback(); raise AppError("Verification belongs to another client")
            if row["status"] in {"PASS", "FAIL", "ERROR"}:
                if row["status"] == status:
                    self._queue_cloud_event(
                        con, f"verify:{verification_id}:{status}", "QUALITY_VERIFICATION_RESULT",
                        self._verification_cloud_payload(row),
                    )
                    con.commit()
                    return {"ok": True, "already_final": True, "status": status, "mac": colon_mac(row["mac"])}
                con.rollback(); raise AppError(f"Verification is already final as {row['status']}")
            if status == "PASS":
                if row["server_check"] != "PASS" or row["pool_state"] != "PASS":
                    con.rollback(); raise AppError("Cannot report PASS because central MAC validation did not pass")
                stage_results = {
                    "WIFI_CALIBRATION": (wifi_calibration_result or "").upper(),
                    "BOB_CALIBRATION": (bob_calibration_result or "").upper(),
                    "FIRMWARE": (firmware_result or "").upper(),
                    "LED": (led_result or "").upper(), "RESET": (reset_result or "").upper(),
                    "WPS": (wps_result or "").upper(), "USER_MODE": (user_mode_result or "").upper(),
                }
                if (row["scanned_serial_number"] or "").strip():
                    stage_results["SERIAL_SCAN"] = (serial_scan_result or "").upper()
                incomplete = [name for name, value in stage_results.items() if value != "PASS"]
                if incomplete:
                    con.rollback(); raise AppError("Cannot report PASS; required tests not PASS: " + ", ".join(incomplete))
                other_mac = con.execute(
                    "SELECT 1 FROM verification_history WHERE mac=? AND status='PASS' "
                    "AND UPPER(COALESCE(serial_number,''))<>UPPER(?) LIMIT 1",
                    (row["mac"], row["serial_number"] or ""),
                ).fetchone()
                if other_mac is not None:
                    con.rollback(); raise AppError("Cannot report PASS; MAC already verified with another serial number")
                other_serial = con.execute(
                    "SELECT 1 FROM verification_history WHERE UPPER(serial_number)=UPPER(?) AND status='PASS' "
                    "AND mac<>? LIMIT 1",
                    (row["serial_number"] or "", row["mac"]),
                ).fetchone()
                if other_serial is not None:
                    con.rollback(); raise AppError("Cannot report PASS; serial number already verified with another MAC")
                other_gpon = con.execute(
                    "SELECT 1 FROM verification_history WHERE UPPER(gpon_number)=UPPER(?) AND status='PASS' "
                    "AND mac<>? LIMIT 1", (row["gpon_number"] or "", row["mac"]),
                ).fetchone()
                if other_gpon is not None:
                    con.rollback(); raise AppError("Cannot report PASS; GPON number already verified with another MAC")
            ts = now_iso()
            con.execute(
                """UPDATE verification_history SET status=?, serial_scan_result=?, wifi_calibration_result=?, bob_calibration_result=?,
                   firmware_result=?, firmware_version=?, led_result=?, reset_result=?, wps_result=?,
                   user_mode_result=?, detail=?, completed_at=?, updated_at=? WHERE verification_id=?""",
                (status, (serial_scan_result or "")[:50], (wifi_calibration_result or "")[:50], (bob_calibration_result or "")[:50],
                 (firmware_result or "")[:50], (firmware_version or "")[:200], (led_result or "")[:50],
                 (reset_result or "")[:50], (wps_result or "")[:50], (user_mode_result or "")[:50],
                 (detail or "")[:6000], ts, ts, verification_id),
            )
            updated = con.execute("SELECT * FROM verification_history WHERE verification_id=?", (verification_id,)).fetchone()
            self._queue_cloud_event(
                con, f"verify:{verification_id}:{status}", "QUALITY_VERIFICATION_RESULT",
                self._verification_cloud_payload(updated),
            )
            con.commit()
            return {"ok": True, "already_final": False, "status": status, "mac": colon_mac(row["mac"])}

    def stats(self) -> dict[str, int]:
        with self.connect() as con:
            rows = con.execute("SELECT state, COUNT(*) c FROM mac_pool GROUP BY state").fetchall()
            vrows = con.execute("SELECT status, COUNT(*) c FROM verification_history GROUP BY status").fetchall()
        counts = {r["state"]: r["c"] for r in rows}
        vcounts = {r["status"]: r["c"] for r in vrows}
        return {
            "total": sum(counts.values()), "available": counts.get("AVAILABLE", 0),
            "reserved": counts.get("RESERVED", 0), "pass": counts.get("PASS", 0),
            "fail": counts.get("FAIL", 0), "error": counts.get("ERROR", 0),
            "verify_started": vcounts.get("STARTED", 0), "verify_pass": vcounts.get("PASS", 0),
            "verify_fail": vcounts.get("FAIL", 0), "verify_error": vcounts.get("ERROR", 0),
        }

    def recent_rows(self, limit: int = 300) -> list[dict]:
        with self.connect() as con:
            rows = con.execute("SELECT seq,mac,state,serial_number,gpon_number,pcb_serial_number,box_build_station,box_build_at,client_id,reserved_at,completed_at FROM mac_pool ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        return [{"seq": r["seq"], "mac": colon_mac(r["mac"]), "state": r["state"],
                 "serial": r["serial_number"] or "", "gpon": r["gpon_number"] or "",
                 "pcb_serial": r["pcb_serial_number"] or "", "box_build_station": r["box_build_station"] or "",
                 "box_build_at": r["box_build_at"] or "",
                 "client_id": r["client_id"] or "", "reserved_at": r["reserved_at"] or "",
                 "completed_at": r["completed_at"] or ""} for r in rows]

    def recent_verifications(self, limit: int = 300) -> list[dict]:
        with self.connect() as con:
            rows = con.execute(
                """SELECT id,mac,serial_number,gpon_number,pcb_serial_number,part_number,firmware_version,status,pool_state,client_id,router_ip,completed_at,updated_at
                   FROM verification_history ORDER BY id DESC LIMIT ?""", (limit,)
            ).fetchall()
        return [{"id": r["id"], "mac": colon_mac(r["mac"]), "serial": r["serial_number"] or "",
                 "gpon": r["gpon_number"] or "", "pcb_serial": r["pcb_serial_number"] or "",
                 "part": r["part_number"] or "", "firmware": r["firmware_version"] or "", "status": r["status"], "pool_state": r["pool_state"] or "",
                 "client": r["client_id"], "router_ip": r["router_ip"] or "",
                 "time": r["completed_at"] or r["updated_at"] or ""} for r in rows]

    def clients(self) -> list[dict]:
        with self.connect() as con:
            rows = con.execute("SELECT client_id,hostname,last_ip,last_seen,app_version FROM clients ORDER BY last_seen DESC").fetchall()
        return [dict(r) for r in rows]

    def export_mac_csv(self, path: Path) -> None:
        with self.connect() as con:
            rows = con.execute("SELECT * FROM mac_pool ORDER BY seq").fetchall()
        with path.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["SEQ","MAC","STATE","SERIAL_NUMBER","GPON_NUMBER","PCB_SERIAL_NUMBER","BOX_BUILD_STATION","BOX_BUILD_AT","ADDED_AT","REQUEST_ID","RESERVATION_ID","CLIENT_ID","CLIENT_IP","RESERVED_AT","COMPLETED_AT","RESULT_DETAIL","UPDATED_AT"])
            for r in rows:
                w.writerow([r["seq"], colon_mac(r["mac"]), r["state"], r["serial_number"], r["gpon_number"], r["pcb_serial_number"], r["box_build_station"], r["box_build_at"], r["added_at"], r["request_id"], r["reservation_id"], r["client_id"], r["client_ip"], r["reserved_at"], r["completed_at"], r["result_detail"], r["updated_at"]])

    def export_verification_csv(self, path: Path) -> None:
        with self.connect() as con:
            rows = con.execute("SELECT * FROM verification_history ORDER BY id").fetchall()
        with path.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ID","VERIFICATION_ID","REQUEST_ID","MAC","ROUTER_SERIAL_NUMBER","SCANNED_SERIAL_NUMBER","SERIAL_SCAN_RESULT","GPON_NUMBER","PCB_SERIAL_NUMBER","PART_NUMBER","CLIENT_ID","CLIENT_IP","ROUTER_IP","STATUS","POOL_STATE","SERVER_CHECK","WIFI_CAL","BOB_CAL","FIRMWARE_RESULT","FIRMWARE_VERSION","LED","RESET","WPS","USER_MODE","DETAIL","STARTED_AT","COMPLETED_AT","UPDATED_AT"])
            for r in rows:
                w.writerow([r["id"], r["verification_id"], r["request_id"], colon_mac(r["mac"]), r["serial_number"], r["scanned_serial_number"], r["serial_scan_result"], r["gpon_number"], r["pcb_serial_number"], r["part_number"], r["client_id"], r["client_ip"], r["router_ip"], r["status"], r["pool_state"], r["server_check"], r["wifi_calibration_result"], r["bob_calibration_result"], r["firmware_result"], r["firmware_version"], r["led_result"], r["reset_result"], r["wps_result"], r["user_mode_result"], r["detail"], r["started_at"], r["completed_at"], r["updated_at"]])


class CloudSyncWorker:
    """Uploads locally committed production events to the Render MES.

    The worker is intentionally independent of Writer/Verifier operation. A cloud
    outage never blocks local MAC allocation or testing; events remain PENDING
    until a later retry succeeds.
    """
    def __init__(self, db: MacDatabase, config: dict[str, str]):
        self.db = db
        self.enabled = config.get("CLOUD_MES_ENABLED", "0").strip().lower() in {"1", "yes", "true", "on"}
        self.base_url = config.get("CLOUD_MES_URL", "").strip().rstrip("/")
        self.api_key = config.get("CLOUD_MES_API_KEY", "").strip()
        self.plant_id = config.get("PLANT_ID", "UNCORD-FACTORY").strip() or "UNCORD-FACTORY"
        self.interval = max(5, int(config.get("CLOUD_SYNC_INTERVAL_SECONDS", "30") or 30))
        self.batch_size = max(1, min(500, int(config.get("CLOUD_BATCH_SIZE", "100") or 100)))
        self.timeout = max(3, int(config.get("CLOUD_REQUEST_TIMEOUT_SECONDS", "20") or 20))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._state = {
            "state": "DISABLED" if not self.enabled else "WAITING",
            "last_attempt_at": "",
            "last_success_at": "",
            "last_error": "",
            "last_uploaded": 0,
        }

    def start(self) -> None:
        if not self.enabled:
            return
        if not self.base_url or not self.api_key:
            with self._lock:
                self._state.update(state="CONFIG ERROR", last_error="CLOUD_MES_URL and CLOUD_MES_API_KEY are required")
            return
        self._thread = threading.Thread(target=self._run, name="cloud-mes-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def request_sync(self) -> None:
        self._wake.set()

    def status(self) -> dict:
        with self._lock:
            data = dict(self._state)
        data.update(self.db.cloud_sync_stats())
        data["enabled"] = self.enabled
        data["url"] = self.base_url
        data["plant_id"] = self.plant_id
        return data

    def _set_state(self, **values) -> None:
        with self._lock:
            self._state.update(values)

    def _run(self) -> None:
        # Run once immediately, then wait for interval or manual wake-up.
        while not self._stop.is_set():
            self.sync_once()
            self._wake.wait(self.interval)
            self._wake.clear()

    def sync_once(self) -> dict:
        if not self.enabled:
            return self.status()
        pending = self.db.pending_cloud_events(self.batch_size, self.plant_id)
        snapshot = self.db.cloud_snapshot_event(self.plant_id)
        events = pending + [snapshot]
        queue_ids = [event["event_id"] for event in pending]
        self._set_state(state="SYNCING", last_attempt_at=now_iso(), last_error="")
        try:
            body = json.dumps({"events": events}, ensure_ascii=False).encode("utf-8")
            req = urlrequest.Request(
                self.base_url + "/api/v1/production-events/batch",
                data=body,
                method="POST",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "ETE-Factory-Sync/13.9",
                },
            )
            with urlrequest.urlopen(req, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            processed = {str(x) for x in payload.get("processed_event_ids", [])}
            synced = [event_id for event_id in queue_ids if event_id in processed]
            missing = [event_id for event_id in queue_ids if event_id not in processed]
            if synced:
                self.db.mark_cloud_synced(synced)
            if missing:
                self.db.mark_cloud_error(missing, "Cloud response did not acknowledge event")
            self._set_state(
                state="ONLINE",
                last_success_at=now_iso(),
                last_error="" if not missing else f"{len(missing)} event(s) not acknowledged",
                last_uploaded=len(synced),
            )
        except urlerror.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            message = f"HTTP {exc.code}: {detail or exc.reason}"
            self.db.mark_cloud_error(queue_ids, message)
            self._set_state(state="OFFLINE", last_error=message, last_uploaded=0)
        except Exception as exc:
            message = str(exc)[:1000]
            self.db.mark_cloud_error(queue_ids, message)
            self._set_state(state="OFFLINE", last_error=message, last_uploaded=0)
        return self.status()


class ApiState:
    def __init__(self, db: MacDatabase, api_key: str):
        self.db = db
        self.api_key = api_key


class MacApiHandler(BaseHTTPRequestHandler):
    server_version = "ETEProductionServer/13.17"

    def log_message(self, fmt, *args):
        return

    @property
    def state(self) -> ApiState:
        return self.server.api_state  # type: ignore[attr-defined]

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _firmware_file(self, path: Path, file_name: str, sha256: str) -> None:
        if not path.is_file():
            self._json(404, {"ok": False, "error": "Firmware file not found"})
            return
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{file_name}"')
        self.send_header("X-Firmware-SHA256", sha256)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _auth(self) -> bool:
        if self.state.api_key and self.headers.get("X-API-Key", "") != self.state.api_key:
            self._json(401, {"ok": False, "error": "Unauthorized"}); return False
        return True

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads((self.rfile.read(length) if length else b"{}").decode("utf-8"))
        except Exception as exc:
            raise AppError(f"Invalid JSON body: {exc}") from exc

    def _touch(self) -> None:
        client_id = self.headers.get("X-Station-ID", "")
        if client_id:
            self.state.db.touch_client(client_id, self.headers.get("X-Hostname", ""), self.client_address[0], self.headers.get("X-App-Version", ""))

    def do_GET(self):
        if not self._auth(): return
        self._touch()
        try:
            if self.path == "/api/health":
                self._json(200, {"ok": True, "server_time": now_iso(), "version": "13.17"})
            elif self.path == "/api/firmware/config":
                self._json(200, {"ok": True, **self.state.db.firmware_config()})
            elif self.path == "/api/firmware/download":
                firmware = self.state.db.firmware_config()
                if not firmware.get("available"):
                    self._json(404, {"ok": False, "error": "No firmware file is selected on the central server"})
                else:
                    fw_path = FIRMWARE_DIR / str(firmware["file_name"])
                    self._firmware_file(fw_path, str(firmware["file_name"]), str(firmware["sha256"]))
            elif self.path == "/api/stats":
                self._json(200, {"ok": True, **self.state.db.stats()})
            elif self.path == "/api/cloud-sync":
                self._json(200, {"ok": True, **self.state.db.cloud_sync_stats()})
            elif self.path == "/api/stage-log/stats":
                self._json(200, {"ok": True, **self.state.db.stage_log_stats()})
            else:
                self._json(404, {"ok": False, "error": "Not found"})
        except Exception as exc:
            self._json(500, {"ok": False, "error": str(exc)})

    def do_POST(self):
        if not self._auth(): return
        self._touch()
        try:
            body = self._body()
            if self.path == "/api/allocate":
                try:
                    payload = self.state.db.allocate(
                        str(body.get("client_id", "")), str(body.get("request_id", "")), self.client_address[0],
                        require_pcb_serial=str(body.get("require_pcb_serial", "0")).strip().lower() in {"1", "true", "yes", "on"},
                    )
                except LookupError as exc:
                    self._json(409, {"ok": False, "error": str(exc), "code": "QUEUE_EMPTY"}); return
                self._json(200, {"ok": True, **payload, "stats": self.state.db.stats()})
            elif self.path == "/api/identity/lookup":
                payload = self.state.db.lookup_identity_by_scan(str(body.get("scan_value", "")))
                self._json(200, {"ok": True, **payload})
            elif self.path == "/api/allocate-specific":
                payload = self.state.db.allocate_specific(
                    str(body.get("client_id", "")), str(body.get("request_id", "")), self.client_address[0],
                    str(body.get("mac", "")),
                    require_pcb_serial=str(body.get("require_pcb_serial", "0")).strip().lower() in {"1", "true", "yes", "on"},
                )
                self._json(200, {"ok": True, **payload, "stats": self.state.db.stats()})
            elif self.path == "/api/result":
                payload = self.state.db.report_result(
                    str(body.get("client_id", "")), str(body.get("reservation_id", "")),
                    str(body.get("status", "")), str(body.get("detail", "")),
                    str(body.get("serial_number", "")), str(body.get("gpon_number", "")),
                )
                self._json(200, {**payload, "stats": self.state.db.stats()})
            elif self.path == "/api/box-build/lookup":
                payload = self.state.db.lookup_box_build_identity(
                    mac=str(body.get("mac", "")),
                    serial_number=str(body.get("serial_number", "")),
                    gpon_number=str(body.get("gpon_number", "")),
                )
                self._json(200, {"ok": True, **payload})
            elif self.path == "/api/box-build/bind":
                payload = self.state.db.bind_pcb_serial(
                    client_id=str(body.get("client_id", self.headers.get("X-Station-ID", ""))),
                    event_id=str(body.get("event_id", "")), mac=str(body.get("mac", "")),
                    serial_number=str(body.get("serial_number", "")), gpon_number=str(body.get("gpon_number", "")),
                    pcb_serial_number=str(body.get("pcb_serial_number", "")), raw_label=str(body.get("raw_label", "")),
                    client_ip=self.client_address[0],
                )
                self._json(200, payload)
            elif self.path == "/api/verify/check":
                payload = self.state.db.begin_verification(
                    client_id=str(body.get("client_id", "")), request_id=str(body.get("request_id", "")),
                    mac=str(body.get("mac", "")), serial_number=str(body.get("serial_number", "")),
                    scanned_serial_number=str(body.get("scanned_serial_number", "")),
                    gpon_number=str(body.get("gpon_number", "")), part_number=str(body.get("part_number", "")), router_ip=str(body.get("router_ip", "")),
                    client_ip=self.client_address[0],
                )
                self._json(200, {"ok": True, **payload, "stats": self.state.db.stats()})
            elif self.path == "/api/verify/result":
                payload = self.state.db.report_verification(
                    client_id=str(body.get("client_id", "")), verification_id=str(body.get("verification_id", "")),
                    status=str(body.get("status", "")),
                    serial_scan_result=str(body.get("serial_scan_result", "")),
                    wifi_calibration_result=str(body.get("wifi_calibration_result", "")),
                    bob_calibration_result=str(body.get("bob_calibration_result", "")),
                    firmware_result=str(body.get("firmware_result", "")),
                    firmware_version=str(body.get("firmware_version", "")),
                    led_result=str(body.get("led_result", "")), reset_result=str(body.get("reset_result", "")),
                    wps_result=str(body.get("wps_result", "")),
                    user_mode_result=str(body.get("user_mode_result", "")), detail=str(body.get("detail", "")),
                )
                self._json(200, {**payload, "stats": self.state.db.stats()})
            elif self.path in {"/api/stage-log/report", "/api/stage-log/batch"}:
                records = body.get("records")
                if records is None:
                    records = [body]
                payload = self.state.db.report_stage_log_batch(
                    client_id=str(body.get("client_id", self.headers.get("X-Station-ID", ""))),
                    records=records,
                    client_ip=self.client_address[0],
                )
                self._json(200 if not payload.get("rejected") else 207, payload)
            else:
                self._json(404, {"ok": False, "error": "Not found"})
        except AppError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self._json(500, {"ok": False, "error": str(exc)})


class ServerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ETE Solutions India | Central Production Server")
        self.geometry("1320x820")
        self.minsize(1050, 680)
        self.configure(bg=BRAND_BG)
        self.db = MacDatabase(DB_PATH)
        self.httpd = None
        self.cloud_worker: CloudSyncWorker | None = None
        self.brand_logo = None
        self.status_var = tk.StringVar(value="STOPPED")
        self.endpoint_var = tk.StringVar(value="")
        self.cloud_status_var = tk.StringVar(value="DISABLED")
        self.cloud_url_var = tk.StringVar(value="")
        self.vars = {k: tk.StringVar(value="0") for k in ["total","available","reserved","pass","verify_pass","verify_fail","verify_started","clients","cloud_pending","cloud_synced"]}
        self.import_status_var = tk.StringVar(value="READY")
        self.firmware_status_var = tk.StringVar(value="OFF")
        self.firmware_file_var = tk.StringVar(value="No firmware selected")
        self.import_queue: queue.Queue = queue.Queue()
        self.import_thread = None
        self._load_brand_assets(); self._style(); self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(1000, self._refresh_loop)
        self._start_server()
        self._refresh_firmware_ui()

    def _load_brand_assets(self):
        path = BASE_DIR / "assets" / "ete_logo.png"
        if path.exists():
            try:
                self.brand_logo = tk.PhotoImage(file=str(path)); self.iconphoto(True, self.brand_logo)
            except tk.TclError:
                self.brand_logo = None

    def _style(self):
        s = ttk.Style(self)
        try: s.theme_use("clam")
        except tk.TclError: pass
        s.configure("App.TFrame", background=BRAND_BG); s.configure("Card.TFrame", background=BRAND_CARD)
        s.configure("Header.TLabel", background=BRAND_BG, foreground=BRAND_DARK, font=("Segoe UI",20,"bold"))
        s.configure("Sub.TLabel", background=BRAND_BG, foreground=BRAND_MUTED, font=("Segoe UI",10))
        s.configure("BrandName.TLabel", background=BRAND_BG, foreground=BRAND_DARK, font=("Segoe UI",11,"bold"))
        s.configure("CardTitle.TLabel", background=BRAND_CARD, foreground=BRAND_DARK, font=("Segoe UI",9,"bold"))
        s.configure("Metric.TLabel", background=BRAND_CARD, foreground=BRAND_TEXT, font=("Segoe UI",18,"bold"))
        s.configure("Endpoint.TLabel", background=BRAND_CARD, foreground=BRAND_BLUE, font=("Consolas",13,"bold"))
        s.configure("Primary.TButton", font=("Segoe UI",10,"bold"), padding=(14,9), background=BRAND_DARK, foreground="#FFFFFF")
        s.map("Primary.TButton", background=[("active",BRAND_BLUE)], foreground=[("!disabled","#FFFFFF")])
        s.configure("Treeview", rowheight=27, font=("Segoe UI",9), background="#FFFFFF", fieldbackground="#FFFFFF", foreground=BRAND_TEXT)
        s.configure("Treeview.Heading", font=("Segoe UI",9,"bold"), background="#EAF5FB", foreground=BRAND_DARK)
        s.map("Treeview", background=[("selected",BRAND_DARK)], foreground=[("selected","#FFFFFF")])

    def _build(self):
        root = ttk.Frame(self, style="App.TFrame", padding=18); root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1); root.rowconfigure(4, weight=1)
        head = ttk.Frame(root, style="App.TFrame"); head.grid(row=0,column=0,sticky="ew",pady=(0,12)); head.columnconfigure(1,weight=1)
        if self.brand_logo: ttk.Label(head,image=self.brand_logo,background=BRAND_BG).grid(row=0,column=0,rowspan=3,padx=(0,14))
        b = ttk.Frame(head,style="App.TFrame"); b.grid(row=0,column=1,rowspan=3,sticky="w")
        ttk.Label(b,text="ETE SOLUTIONS INDIA",style="BrandName.TLabel").grid(row=0,column=0,sticky="w")
        ttk.Label(b,text="Central Production Identity & Quality Server",style="Header.TLabel").grid(row=1,column=0,sticky="w")
        ttk.Label(b,text="Identity allocation, PCB traceability, verification and offline-tolerant cloud MES sync",style="Sub.TLabel").grid(row=2,column=0,sticky="w")

        bar = ttk.Frame(root,style="Card.TFrame",padding=14); bar.grid(row=1,column=0,sticky="ew"); bar.columnconfigure(1,weight=1)
        ttk.Label(bar,text="SERVER",style="CardTitle.TLabel").grid(row=0,column=0,sticky="w")
        ttk.Label(bar,textvariable=self.status_var,style="Metric.TLabel").grid(row=1,column=0,sticky="w",padx=(0,20))
        ttk.Label(bar,text="CLIENT URL",style="CardTitle.TLabel").grid(row=0,column=1,sticky="w")
        ttk.Label(bar,textvariable=self.endpoint_var,style="Endpoint.TLabel").grid(row=1,column=1,sticky="w")
        ttk.Label(bar,text="CLOUD MES",style="CardTitle.TLabel").grid(row=0,column=2,sticky="w",padx=(16,4))
        ttk.Label(bar,textvariable=self.cloud_status_var,style="Metric.TLabel").grid(row=1,column=2,sticky="w",padx=(16,12))
        ttk.Button(bar,text="Sync Now",command=self._sync_now).grid(row=0,column=3,rowspan=2,padx=4)
        self.import_btn = ttk.Button(bar,text="Import Identity List",style="Primary.TButton",command=self._import)
        self.import_btn.grid(row=0,column=4,rowspan=2,padx=4)
        ttk.Button(bar,text="Export MAC",command=self._export_mac).grid(row=0,column=5,rowspan=2,padx=4)
        ttk.Button(bar,text="Export Verification",command=self._export_verify).grid(row=0,column=6,rowspan=2,padx=4)
        ttk.Label(bar,text="IMPORT",style="CardTitle.TLabel").grid(row=0,column=7,sticky="w",padx=(12,4))
        ttk.Label(bar,textvariable=self.import_status_var,style="CardTitle.TLabel").grid(row=1,column=7,sticky="w",padx=(12,4))

        fwbar = ttk.Frame(root, style="Card.TFrame", padding=10); fwbar.grid(row=2,column=0,sticky="ew",pady=(8,0)); fwbar.columnconfigure(2,weight=1)
        ttk.Label(fwbar,text="MAC WRITER FIRMWARE UPDATE",style="CardTitle.TLabel").grid(row=0,column=0,sticky="w",padx=(2,10))
        ttk.Label(fwbar,textvariable=self.firmware_status_var,style="Endpoint.TLabel").grid(row=0,column=1,sticky="w",padx=(0,16))
        ttk.Label(fwbar,textvariable=self.firmware_file_var,style="CardTitle.TLabel").grid(row=0,column=2,sticky="w")
        self.firmware_toggle_btn = ttk.Button(fwbar,text="Enable Firmware",command=self._toggle_firmware)
        self.firmware_toggle_btn.grid(row=0,column=3,padx=4)
        ttk.Button(fwbar,text="Select Firmware File",style="Primary.TButton",command=self._select_firmware).grid(row=0,column=4,padx=(4,2))

        metrics = ttk.Frame(root,style="App.TFrame"); metrics.grid(row=3,column=0,sticky="ew",pady=10)
        items=[("MAC TOTAL","total"),("AVAILABLE","available"),("RESERVED","reserved"),("MAC WRITE PASS","pass"),("VERIFY PASS","verify_pass"),("VERIFY FAIL","verify_fail"),("IN PROGRESS","verify_started"),("CLIENTS","clients"),("CLOUD PENDING","cloud_pending"),("CLOUD SYNCED","cloud_synced")]
        for col in range(5): metrics.columnconfigure(col,weight=1)
        for i,(label,key) in enumerate(items):
            row,col=divmod(i,5)
            c=ttk.Frame(metrics,style="Card.TFrame",padding=10); c.grid(row=row,column=col,sticky="nsew",padx=3,pady=3)
            ttk.Label(c,text=label,style="CardTitle.TLabel").pack(anchor="w"); ttk.Label(c,textvariable=self.vars[key],style="Metric.TLabel").pack(anchor="w")

        tabs=ttk.Notebook(root); tabs.grid(row=4,column=0,sticky="nsew")
        mac_tab=ttk.Frame(tabs,padding=8); ver_tab=ttk.Frame(tabs,padding=8); cli_tab=ttk.Frame(tabs,padding=8); cloud_tab=ttk.Frame(tabs,padding=8)
        tabs.add(mac_tab,text="MAC Ledger"); tabs.add(ver_tab,text="Quality Verification"); tabs.add(cli_tab,text="Clients"); tabs.add(cloud_tab,text="Cloud MES Sync")
        for tab in (mac_tab,ver_tab,cli_tab,cloud_tab): tab.rowconfigure(0,weight=1); tab.columnconfigure(0,weight=1)
        self.mac_table=ttk.Treeview(mac_tab,columns=("seq","mac","serial","gpon","pcb","state","client","box_station","box_time","completed"),show="headings")
        for col,text,w in [("seq","#",55),("mac","MAC",145),("serial","SERIAL NUMBER",145),("gpon","GPON SERIAL",145),("pcb","PCB SERIAL NUMBER",155),("state","STATUS",85),("client","WRITER STATION",150),("box_station","BOX BUILD STATION",150),("box_time","BOX BUILD TIME",170),("completed","WRITE COMPLETED",170)]: self.mac_table.heading(col,text=text); self.mac_table.column(col,width=w,anchor="w")
        self.mac_table.grid(row=0,column=0,sticky="nsew"); mac_sb=ttk.Scrollbar(mac_tab,orient="vertical",command=self.mac_table.yview); mac_sb.grid(row=0,column=1,sticky="ns"); mac_x=ttk.Scrollbar(mac_tab,orient="horizontal",command=self.mac_table.xview); mac_x.grid(row=1,column=0,sticky="ew"); self.mac_table.configure(yscrollcommand=mac_sb.set,xscrollcommand=mac_x.set)
        self.ver_table=ttk.Treeview(ver_tab,columns=("id","mac","serial","pcb","part","status","pool","client","router","time"),show="headings")
        for col,text,w in [("id","#",50),("mac","MAC",140),("serial","SERIAL",140),("pcb","PCB SERIAL",150),("part","PART NUMBER",125),("status","STATUS",85),("pool","WRITE STATE",90),("client","VERIFY STATION",160),("router","ROUTER IP",110),("time","TIME",175)]: self.ver_table.heading(col,text=text); self.ver_table.column(col,width=w,anchor="w")
        self.ver_table.grid(row=0,column=0,sticky="nsew"); ver_sb=ttk.Scrollbar(ver_tab,orient="vertical",command=self.ver_table.yview); ver_sb.grid(row=0,column=1,sticky="ns"); ver_x=ttk.Scrollbar(ver_tab,orient="horizontal",command=self.ver_table.xview); ver_x.grid(row=1,column=0,sticky="ew"); self.ver_table.configure(yscrollcommand=ver_sb.set,xscrollcommand=ver_x.set)
        self.client_table=ttk.Treeview(cli_tab,columns=("id","host","ip","seen","version"),show="headings")
        for col,text,w in [("id","STATION ID",260),("host","HOSTNAME",180),("ip","IP",125),("seen","LAST SEEN",220),("version","VERSION",120)]: self.client_table.heading(col,text=text); self.client_table.column(col,width=w,anchor="w")
        self.client_table.grid(row=0,column=0,sticky="nsew"); cli_sb=ttk.Scrollbar(cli_tab,orient="vertical",command=self.client_table.yview); cli_sb.grid(row=0,column=1,sticky="ns"); self.client_table.configure(yscrollcommand=cli_sb.set)
        self.cloud_table=ttk.Treeview(cloud_tab,columns=("event","type","state","attempts","created","synced","error"),show="headings")
        for col,text,w in [("event","EVENT ID",270),("type","TYPE",210),("state","STATE",90),("attempts","TRIES",60),("created","CREATED",180),("synced","SYNCED",180),("error","LAST ERROR",360)]: self.cloud_table.heading(col,text=text); self.cloud_table.column(col,width=w,anchor="w")
        self.cloud_table.grid(row=0,column=0,sticky="nsew"); cloud_sb=ttk.Scrollbar(cloud_tab,orient="vertical",command=self.cloud_table.yview); cloud_sb.grid(row=0,column=1,sticky="ns"); self.cloud_table.configure(yscrollcommand=cloud_sb.set)

    def _refresh_firmware_ui(self):
        try:
            firmware = self.db.firmware_config()
            enabled = bool(firmware.get("enabled"))
            available = bool(firmware.get("available"))
            if enabled and available:
                self.firmware_status_var.set("ON")
            elif enabled:
                self.firmware_status_var.set("ERROR")
            else:
                self.firmware_status_var.set("OFF")
            file_name = firmware.get("file_name") or "No firmware selected"
            if available:
                mb = float(firmware.get("size", 0)) / (1024 * 1024)
                self.firmware_file_var.set(f"{file_name}  •  {mb:.1f} MB  •  SHA256 {str(firmware.get('sha256',''))[:12]}…")
            else:
                self.firmware_file_var.set(str(file_name))
            self.firmware_toggle_btn.configure(text="Disable Firmware" if enabled else "Enable Firmware")
        except Exception as exc:
            self.firmware_status_var.set("ERROR")
            self.firmware_file_var.set(str(exc))

    def _toggle_firmware(self):
        try:
            current = self.db.firmware_config()
            self.db.set_firmware_enabled(not bool(current.get("enabled")))
            self._refresh_firmware_ui()
        except Exception as exc:
            messagebox.showerror("Firmware update", str(exc))

    def _select_firmware(self):
        path = filedialog.askopenfilename(
            title="Select firmware image to store on the central server",
            filetypes=[("Firmware / binary files", "*.bin *.img *.trx *.tar *.gz *.ubi *.itb *.fw"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            firmware = self.db.select_firmware_file(Path(path))
            self._refresh_firmware_ui()
            messagebox.showinfo(
                "Firmware stored",
                f"Firmware saved on central server:\n{firmware.get('file_name')}\n\n"
                "The firmware update remains controlled by the Enable/Disable button.\n"
                "Add the actual router CLI under [FIRMWARE] in mac_writer/commands.txt before enabling it.",
            )
        except Exception as exc:
            messagebox.showerror("Firmware file", str(exc))

    def _start_server(self):
        try:
            cfg=parse_key_value_file(CONFIG_PATH); port=int(cfg.get("PORT","8765")); host=cfg.get("BIND","0.0.0.0")
            self.httpd=ThreadingHTTPServer((host,port),MacApiHandler); self.httpd.daemon_threads=True
            self.httpd.api_state=ApiState(self.db,cfg.get("API_KEY","change-me"))  # type: ignore[attr-defined]
            threading.Thread(target=self.httpd.serve_forever,daemon=True).start(); self.status_var.set("RUNNING"); self.endpoint_var.set(f"http://{guess_lan_ip()}:{port}")
            self.cloud_worker=CloudSyncWorker(self.db,cfg); self.cloud_worker.start(); self.cloud_url_var.set(self.cloud_worker.base_url)
        except Exception as exc:
            self.status_var.set("ERROR"); self.endpoint_var.set(str(exc)); messagebox.showerror("Server start failed",str(exc))

    def _sync_now(self):
        if not self.cloud_worker or not self.cloud_worker.enabled:
            messagebox.showinfo("Cloud MES", "Cloud synchronization is disabled. Configure CLOUD_MES_* in server_config.txt.")
            return
        self.cloud_worker.request_sync()
        self.cloud_status_var.set("SYNC REQUESTED")

    def _import(self):
        if self.import_thread and self.import_thread.is_alive():
            messagebox.showinfo("Identity import", "An identity list is already being imported.")
            return
        p=filedialog.askopenfilename(title="Import MAC + Serial + GPON identity list",filetypes=[("Identity list","*.txt *.csv *.tsv"),("All files","*.*")])
        if not p:
            return

        self.import_btn.state(["disabled"])
        self.import_status_var.set("IMPORTING…")
        selected_path = Path(p)

        def worker():
            try:
                started = time.monotonic()
                records = parse_identity_list_file(selected_path)
                parse_done = time.monotonic()
                result = self.db.import_identities(records)
                finished = time.monotonic()
                incomplete = sum(1 for x in records if not x.get("serial_number") or not x.get("gpon_number"))
                self.import_queue.put(("ok", result, incomplete, parse_done-started, finished-parse_done))
            except Exception as exc:
                self.import_queue.put(("error", str(exc)))

        self.import_thread = threading.Thread(target=worker, name="IdentityImport", daemon=True)
        self.import_thread.start()
        self.after(100, self._poll_import)

    def _poll_import(self):
        try:
            item = self.import_queue.get_nowait()
        except queue.Empty:
            if self.import_thread and self.import_thread.is_alive():
                self.after(100, self._poll_import)
            return

        self.import_btn.state(["!disabled"])
        if item[0] == "error":
            self.import_status_var.set("FAILED")
            messagebox.showerror("Import failed", item[1])
            return

        _, r, incomplete, parse_seconds, db_seconds = item
        self.import_status_var.set(f"DONE • {r['input']:,} rows")
        msg = (f"Input rows: {r['input']:,}\nAdded: {r['added']:,}\nUpdated available: {r['updated']:,}\n"
               f"Skipped unchanged/final: {r['skipped']:,}\nRows missing Serial/GPON: {incomplete:,}\n\n"
               f"Parse time: {parse_seconds:.1f}s\nDatabase time: {db_seconds:.1f}s\n\n"
               "MAC, Serial Number and GPON Serial Number are kept together as one identity row. "
               "Reserved or completed identities are never overwritten.")
        messagebox.showinfo("Identity import complete", msg)
        self._refresh()

    def _export_mac(self):
        p=filedialog.asksaveasfilename(defaultextension=".csv",initialfile="mac_ledger.csv",filetypes=[("CSV","*.csv")])
        if p:
            try: self.db.export_mac_csv(Path(p)); messagebox.showinfo("Export complete",p)
            except Exception as exc: messagebox.showerror("Export failed",str(exc))

    def _export_verify(self):
        p=filedialog.asksaveasfilename(defaultextension=".csv",initialfile="quality_verification.csv",filetypes=[("CSV","*.csv")])
        if p:
            try: self.db.export_verification_csv(Path(p)); messagebox.showinfo("Export complete",p)
            except Exception as exc: messagebox.showerror("Export failed",str(exc))

    def _refresh(self):
        s=self.db.stats()
        for k in ["total","available","reserved","pass","verify_pass","verify_fail","verify_started"]: self.vars[k].set(str(s[k]))
        clients=self.db.clients(); self.vars["clients"].set(str(len(clients)))
        cloud=self.cloud_worker.status() if self.cloud_worker else {"state":"DISABLED",**self.db.cloud_sync_stats()}
        self.vars["cloud_pending"].set(str(cloud.get("pending",0))); self.vars["cloud_synced"].set(str(cloud.get("synced",0)))
        self.cloud_status_var.set(str(cloud.get("state","DISABLED")))
        for t in (self.mac_table,self.ver_table,self.client_table,self.cloud_table):
            for item in t.get_children(): t.delete(item)
        for r in reversed(self.db.recent_rows()): self.mac_table.insert("","end",values=(r["seq"],r["mac"],r["serial"],r["gpon"],r["pcb_serial"],r["state"],r["client_id"],r["box_build_station"],r["box_build_at"],r["completed_at"]))
        for r in reversed(self.db.recent_verifications()): self.ver_table.insert("","end",values=(r["id"],r["mac"],r["serial"],r["pcb_serial"],r["part"],r["status"],r["pool_state"],r["client"],r["router_ip"],r["time"]))
        for c in clients: self.client_table.insert("","end",values=(c["client_id"],c["hostname"],c["last_ip"],c["last_seen"],c["app_version"]))
        for e in self.db.recent_cloud_events(): self.cloud_table.insert("","end",values=(e["event_id"],e["event_type"],e["status"],e["attempts"],e["created_at"],e["synced_at"] or "",e["last_error"] or ""))

    def _refresh_loop(self):
        try: self._refresh()
        finally: self.after(1500,self._refresh_loop)

    def _close(self):
        if self.cloud_worker: self.cloud_worker.stop()
        if self.httpd: self.httpd.shutdown(); self.httpd.server_close()
        self.destroy()


if __name__ == "__main__":
    ServerApp().mainloop()
