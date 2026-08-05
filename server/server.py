import csv
import json
import re
import socket
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "server_config.txt"
DB_PATH = BASE_DIR / "mac_server.db"

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


def parse_mac_list_file(path: Path) -> list[str]:
    if not path.exists():
        raise AppError(f"MAC list not found: {path}")
    result: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        value = value.strip().strip('"').strip("'")
        if not value or value.startswith("#") or value.startswith(";"):
            return
        try:
            mac = validate_mac(value)
        except AppError:
            return
        if mac not in seen:
            seen.add(mac)
            result.append(mac)

    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.reader(f):
                for cell in row:
                    add(cell)
    else:
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith(";"):
                continue
            add(re.split(r"[\s,]+", line, maxsplit=1)[0])
    if not result:
        raise AppError("No valid MAC addresses found in the selected file.")
    return result


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
                """
            )
            # Automatic schema migration for databases created by older releases.
            existing = {
                "mac_pool": {row[1] for row in con.execute("PRAGMA table_info(mac_pool)")},
                "verification_history": {row[1] for row in con.execute("PRAGMA table_info(verification_history)")},
            }
            migrations = {
                "mac_pool": {
                    "serial_number": "TEXT",
                    "gpon_number": "TEXT",
                },
                "verification_history": {
                    "scanned_serial_number": "TEXT",
                    "serial_scan_result": "TEXT",
                    "gpon_number": "TEXT",
                    "wifi_calibration_result": "TEXT",
                    "bob_calibration_result": "TEXT",
                    "firmware_result": "TEXT",
                    "firmware_version": "TEXT",
                },
            }
            for table, columns in migrations.items():
                for column, declaration in columns.items():
                    if column not in existing[table]:
                        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_mac_pool_serial_unique ON mac_pool(UPPER(serial_number)) WHERE serial_number IS NOT NULL AND serial_number<>''")
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_mac_pool_gpon_unique ON mac_pool(UPPER(gpon_number)) WHERE gpon_number IS NOT NULL AND gpon_number<>''")
            con.execute("CREATE INDEX IF NOT EXISTS idx_verify_gpon_status ON verification_history(gpon_number, status)")

    def import_macs(self, macs: list[str]) -> dict[str, int]:
        normalized = [validate_mac(m) for m in macs]
        added = 0
        skipped = 0
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            next_seq = con.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM mac_pool").fetchone()[0]
            for mac in normalized:
                cur = con.execute(
                    "INSERT OR IGNORE INTO mac_pool(mac, seq, state, added_at, updated_at) VALUES (?, ?, 'AVAILABLE', ?, ?)",
                    (mac, next_seq, now_iso(), now_iso()),
                )
                if cur.rowcount == 1:
                    added += 1
                    next_seq += 1
                else:
                    skipped += 1
            con.commit()
        return {"added": added, "skipped": skipped, "input": len(normalized)}

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

    # ---------- Existing MAC writer API ----------
    def allocate(self, client_id: str, request_id: str, client_ip: str) -> dict:
        client_id = client_id.strip()
        request_id = request_id.strip()
        if not client_id or not request_id:
            raise AppError("client_id and request_id are required")
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute("SELECT * FROM mac_pool WHERE request_id=?", (request_id,)).fetchone()
            if existing is not None:
                if existing["client_id"] != client_id:
                    con.rollback()
                    raise AppError("request_id already belongs to another client")
                con.commit()
                return self._reservation_payload(existing, True)
            row = con.execute("SELECT mac, seq FROM mac_pool WHERE state='AVAILABLE' ORDER BY seq LIMIT 1").fetchone()
            if row is None:
                con.rollback()
                raise LookupError("MAC queue is empty")
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
        }

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
                con.commit()
            except sqlite3.IntegrityError as exc:
                con.rollback()
                raise AppError("Serial number or GPON number is already assigned to another MAC") from exc
            return {"ok": True, "already_final": False, "state": status, "mac": colon_mac(row["mac"]),
                    "serial_number": serial, "gpon_number": gpon}

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
                   gpon_number, part_number, client_id, client_ip, router_ip, status, pool_state, server_check,
                   started_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'STARTED', ?, ?, ?, ?)""",
                (verification_id, request_id, mac_n, serial, scanned_serial,
                 "PASS" if scanned_serial and scanned_serial.upper() == serial.upper() else ("FAIL" if scanned_serial else ""),
                 gpon, part, client_id, client_ip, router_ip, pool_state, server_check, ts, ts),
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
            "gpon_number": row["gpon_number"] or "", "part_number": row["part_number"] or "", "pool_state": row["pool_state"] or "",
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
            rows = con.execute("SELECT seq,mac,state,serial_number,gpon_number,client_id,reserved_at,completed_at FROM mac_pool ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        return [{"seq": r["seq"], "mac": colon_mac(r["mac"]), "state": r["state"],
                 "serial": r["serial_number"] or "", "gpon": r["gpon_number"] or "",
                 "client_id": r["client_id"] or "", "reserved_at": r["reserved_at"] or "",
                 "completed_at": r["completed_at"] or ""} for r in rows]

    def recent_verifications(self, limit: int = 300) -> list[dict]:
        with self.connect() as con:
            rows = con.execute(
                """SELECT id,mac,serial_number,gpon_number,part_number,firmware_version,status,pool_state,client_id,router_ip,completed_at,updated_at
                   FROM verification_history ORDER BY id DESC LIMIT ?""", (limit,)
            ).fetchall()
        return [{"id": r["id"], "mac": colon_mac(r["mac"]), "serial": r["serial_number"] or "",
                 "gpon": r["gpon_number"] or "", "part": r["part_number"] or "", "firmware": r["firmware_version"] or "", "status": r["status"], "pool_state": r["pool_state"] or "",
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
            w.writerow(["SEQ","MAC","STATE","SERIAL_NUMBER","GPON_NUMBER","ADDED_AT","REQUEST_ID","RESERVATION_ID","CLIENT_ID","CLIENT_IP","RESERVED_AT","COMPLETED_AT","RESULT_DETAIL","UPDATED_AT"])
            for r in rows:
                w.writerow([r["seq"], colon_mac(r["mac"]), r["state"], r["serial_number"], r["gpon_number"], r["added_at"], r["request_id"], r["reservation_id"], r["client_id"], r["client_ip"], r["reserved_at"], r["completed_at"], r["result_detail"], r["updated_at"]])

    def export_verification_csv(self, path: Path) -> None:
        with self.connect() as con:
            rows = con.execute("SELECT * FROM verification_history ORDER BY id").fetchall()
        with path.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ID","VERIFICATION_ID","REQUEST_ID","MAC","ROUTER_SERIAL_NUMBER","SCANNED_SERIAL_NUMBER","SERIAL_SCAN_RESULT","GPON_NUMBER","PART_NUMBER","CLIENT_ID","CLIENT_IP","ROUTER_IP","STATUS","POOL_STATE","SERVER_CHECK","WIFI_CAL","BOB_CAL","FIRMWARE_RESULT","FIRMWARE_VERSION","LED","RESET","WPS","USER_MODE","DETAIL","STARTED_AT","COMPLETED_AT","UPDATED_AT"])
            for r in rows:
                w.writerow([r["id"], r["verification_id"], r["request_id"], colon_mac(r["mac"]), r["serial_number"], r["scanned_serial_number"], r["serial_scan_result"], r["gpon_number"], r["part_number"], r["client_id"], r["client_ip"], r["router_ip"], r["status"], r["pool_state"], r["server_check"], r["wifi_calibration_result"], r["bob_calibration_result"], r["firmware_result"], r["firmware_version"], r["led_result"], r["reset_result"], r["wps_result"], r["user_mode_result"], r["detail"], r["started_at"], r["completed_at"], r["updated_at"]])


class ApiState:
    def __init__(self, db: MacDatabase, api_key: str):
        self.db = db
        self.api_key = api_key


class MacApiHandler(BaseHTTPRequestHandler):
    server_version = "ETEProductionServer/8.0"

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
                self._json(200, {"ok": True, "server_time": now_iso(), "version": "8.0"})
            elif self.path == "/api/stats":
                self._json(200, {"ok": True, **self.state.db.stats()})
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
                    payload = self.state.db.allocate(str(body.get("client_id", "")), str(body.get("request_id", "")), self.client_address[0])
                except LookupError as exc:
                    self._json(409, {"ok": False, "error": str(exc), "code": "QUEUE_EMPTY"}); return
                self._json(200, {"ok": True, **payload, "stats": self.state.db.stats()})
            elif self.path == "/api/result":
                payload = self.state.db.report_result(
                    str(body.get("client_id", "")), str(body.get("reservation_id", "")),
                    str(body.get("status", "")), str(body.get("detail", "")),
                    str(body.get("serial_number", "")), str(body.get("gpon_number", "")),
                )
                self._json(200, {**payload, "stats": self.state.db.stats()})
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
        self.brand_logo = None
        self.status_var = tk.StringVar(value="STOPPED")
        self.endpoint_var = tk.StringVar(value="")
        self.vars = {k: tk.StringVar(value="0") for k in ["total","available","reserved","pass","verify_pass","verify_fail","verify_started","clients"]}
        self._load_brand_assets(); self._style(); self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(1000, self._refresh_loop)
        self._start_server()

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
        root.columnconfigure(0, weight=1); root.rowconfigure(3, weight=1)
        head = ttk.Frame(root, style="App.TFrame"); head.grid(row=0,column=0,sticky="ew",pady=(0,12)); head.columnconfigure(1,weight=1)
        if self.brand_logo: ttk.Label(head,image=self.brand_logo,background=BRAND_BG).grid(row=0,column=0,rowspan=3,padx=(0,14))
        b = ttk.Frame(head,style="App.TFrame"); b.grid(row=0,column=1,rowspan=3,sticky="w")
        ttk.Label(b,text="ETE SOLUTIONS INDIA",style="BrandName.TLabel").grid(row=0,column=0,sticky="w")
        ttk.Label(b,text="Central MAC & Quality Verification Server",style="Header.TLabel").grid(row=1,column=0,sticky="w")
        ttk.Label(b,text="MAC allocation, used-MAC validation and duplicate-safe final verification",style="Sub.TLabel").grid(row=2,column=0,sticky="w")

        bar = ttk.Frame(root,style="Card.TFrame",padding=14); bar.grid(row=1,column=0,sticky="ew"); bar.columnconfigure(1,weight=1)
        ttk.Label(bar,text="SERVER",style="CardTitle.TLabel").grid(row=0,column=0,sticky="w")
        ttk.Label(bar,textvariable=self.status_var,style="Metric.TLabel").grid(row=1,column=0,sticky="w",padx=(0,20))
        ttk.Label(bar,text="CLIENT URL",style="CardTitle.TLabel").grid(row=0,column=1,sticky="w")
        ttk.Label(bar,textvariable=self.endpoint_var,style="Endpoint.TLabel").grid(row=1,column=1,sticky="w")
        ttk.Button(bar,text="Import MAC List",style="Primary.TButton",command=self._import).grid(row=0,column=2,rowspan=2,padx=4)
        ttk.Button(bar,text="Export MAC",command=self._export_mac).grid(row=0,column=3,rowspan=2,padx=4)
        ttk.Button(bar,text="Export Verification",command=self._export_verify).grid(row=0,column=4,rowspan=2,padx=4)

        metrics = ttk.Frame(root,style="App.TFrame"); metrics.grid(row=2,column=0,sticky="ew",pady=10)
        items=[("MAC TOTAL","total"),("AVAILABLE","available"),("RESERVED","reserved"),("MAC WRITE PASS","pass"),("VERIFY PASS","verify_pass"),("VERIFY FAIL","verify_fail"),("IN PROGRESS","verify_started"),("CLIENTS","clients")]
        for i,(label,key) in enumerate(items):
            metrics.columnconfigure(i,weight=1)
            c=ttk.Frame(metrics,style="Card.TFrame",padding=10); c.grid(row=0,column=i,sticky="nsew",padx=3)
            ttk.Label(c,text=label,style="CardTitle.TLabel").pack(anchor="w"); ttk.Label(c,textvariable=self.vars[key],style="Metric.TLabel").pack(anchor="w")

        tabs=ttk.Notebook(root); tabs.grid(row=3,column=0,sticky="nsew")
        mac_tab=ttk.Frame(tabs,padding=8); ver_tab=ttk.Frame(tabs,padding=8); cli_tab=ttk.Frame(tabs,padding=8)
        tabs.add(mac_tab,text="MAC Ledger"); tabs.add(ver_tab,text="Quality Verification"); tabs.add(cli_tab,text="Clients")
        for tab in (mac_tab,ver_tab,cli_tab): tab.rowconfigure(0,weight=1); tab.columnconfigure(0,weight=1)
        self.mac_table=ttk.Treeview(mac_tab,columns=("seq","mac","state","client","reserved","completed"),show="headings")
        for col,text,w in [("seq","#",55),("mac","MAC",160),("state","STATUS",95),("client","WRITER STATION",220),("reserved","RESERVED",180),("completed","COMPLETED",180)]: self.mac_table.heading(col,text=text); self.mac_table.column(col,width=w,anchor="w")
        self.mac_table.grid(row=0,column=0,sticky="nsew"); mac_sb=ttk.Scrollbar(mac_tab,orient="vertical",command=self.mac_table.yview); mac_sb.grid(row=0,column=1,sticky="ns"); self.mac_table.configure(yscrollcommand=mac_sb.set)
        self.ver_table=ttk.Treeview(ver_tab,columns=("id","mac","serial","part","status","pool","client","router","time"),show="headings")
        for col,text,w in [("id","#",50),("mac","MAC",145),("serial","SERIAL",150),("part","PART NUMBER",130),("status","STATUS",90),("pool","WRITE STATE",90),("client","VERIFY STATION",170),("router","ROUTER IP",115),("time","TIME",180)]: self.ver_table.heading(col,text=text); self.ver_table.column(col,width=w,anchor="w")
        self.ver_table.grid(row=0,column=0,sticky="nsew"); ver_sb=ttk.Scrollbar(ver_tab,orient="vertical",command=self.ver_table.yview); ver_sb.grid(row=0,column=1,sticky="ns"); self.ver_table.configure(yscrollcommand=ver_sb.set)
        self.client_table=ttk.Treeview(cli_tab,columns=("id","host","ip","seen","version"),show="headings")
        for col,text,w in [("id","STATION ID",260),("host","HOSTNAME",180),("ip","IP",125),("seen","LAST SEEN",220),("version","VERSION",120)]: self.client_table.heading(col,text=text); self.client_table.column(col,width=w,anchor="w")
        self.client_table.grid(row=0,column=0,sticky="nsew"); cli_sb=ttk.Scrollbar(cli_tab,orient="vertical",command=self.client_table.yview); cli_sb.grid(row=0,column=1,sticky="ns"); self.client_table.configure(yscrollcommand=cli_sb.set)

    def _start_server(self):
        try:
            cfg=parse_key_value_file(CONFIG_PATH); port=int(cfg.get("PORT","8765")); host=cfg.get("BIND","0.0.0.0")
            self.httpd=ThreadingHTTPServer((host,port),MacApiHandler); self.httpd.daemon_threads=True
            self.httpd.api_state=ApiState(self.db,cfg.get("API_KEY","change-me"))  # type: ignore[attr-defined]
            threading.Thread(target=self.httpd.serve_forever,daemon=True).start(); self.status_var.set("RUNNING"); self.endpoint_var.set(f"http://{guess_lan_ip()}:{port}")
        except Exception as exc:
            self.status_var.set("ERROR"); self.endpoint_var.set(str(exc)); messagebox.showerror("Server start failed",str(exc))

    def _import(self):
        p=filedialog.askopenfilename(title="Import MAC list",filetypes=[("MAC list","*.txt *.csv"),("All files","*.*")])
        if not p: return
        try:
            r=self.db.import_macs(parse_mac_list_file(Path(p))); messagebox.showinfo("Import complete",f"Added: {r['added']}\nSkipped existing: {r['skipped']}\n\nExisting records are never reset or reused."); self._refresh()
        except Exception as exc: messagebox.showerror("Import failed",str(exc))

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
        for t in (self.mac_table,self.ver_table,self.client_table):
            for item in t.get_children(): t.delete(item)
        for r in reversed(self.db.recent_rows()): self.mac_table.insert("","end",values=(r["seq"],r["mac"],r["state"],r["client_id"],r["reserved_at"],r["completed_at"]))
        for r in reversed(self.db.recent_verifications()): self.ver_table.insert("","end",values=(r["id"],r["mac"],r["serial"],r["part"],r["status"],r["pool_state"],r["client"],r["router_ip"],r["time"]))
        for c in clients: self.client_table.insert("","end",values=(c["client_id"],c["hostname"],c["last_ip"],c["last_seen"],c["app_version"]))

    def _refresh_loop(self):
        try: self._refresh()
        finally: self.after(1500,self._refresh_loop)

    def _close(self):
        if self.httpd: self.httpd.shutdown(); self.httpd.server_close()
        self.destroy()


if __name__ == "__main__":
    ServerApp().mainloop()
