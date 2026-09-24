#!/usr/bin/env python3
"""
ETE Solutions India - Central Server v13.72 Final Verification Rework Gate

Run ONCE against the current production server.py.

What it changes:
1) Normal Final Verification is BLOCKED after the same DUT has already PASSed.
2) MAC Rewrite can explicitly authorize a rework cycle through /api/rework/authorize.
3) An authorization remains ACTIVE across Final Verification FAIL/ERROR retries.
4) The authorization is consumed only when that DUT PASSes Final Verification again.
5) MAC pool / writer PASS state is not changed by MAC Rewrite.

The patch is idempotent and writes a backup before modifying server.py.
"""
from __future__ import annotations
import shutil
import sys
from pathlib import Path

MARKER = "# V13_72_FINAL_VERIFICATION_REWORK_GATE"


def fail(message: str) -> None:
    raise RuntimeError(message)


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        fail(f"{label}: expected exactly one match, found {count}. Server source does not match the supported baseline.")
    return text.replace(old, new, 1)


def patch_server(path: Path) -> None:
    if not path.is_file():
        fail(f"server.py not found: {path}")

    original = path.read_text(encoding="utf-8")
    if MARKER in original:
        print("v13.72 rework gate is already installed. No changes made.")
        return

    text = original

    schema_anchor = """                CREATE TABLE IF NOT EXISTS system_settings (
                    setting_key TEXT PRIMARY KEY,
                    setting_value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
"""
    schema_new = """                CREATE TABLE IF NOT EXISTS final_verification_rework (
                    authorization_id TEXT PRIMARY KEY,
                    mac TEXT NOT NULL,
                    serial_number TEXT,
                    gpon_number TEXT,
                    source TEXT NOT NULL DEFAULT 'MAC_REWRITE',
                    client_id TEXT,
                    client_ip TEXT,
                    reason TEXT,
                    status TEXT NOT NULL DEFAULT 'ACTIVE',
                    authorized_at TEXT NOT NULL,
                    consumed_at TEXT,
                    consumed_verification_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_fv_rework_mac_status
                    ON final_verification_rework(mac, status);

                CREATE TABLE IF NOT EXISTS system_settings (
                    setting_key TEXT PRIMARY KEY,
                    setting_value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
"""
    text = replace_once(text, schema_anchor, schema_new, "database schema")

    verification_anchor = "    # ---------- Verification API ----------\n"
    verification_methods = '''    # V13_72_FINAL_VERIFICATION_REWORK_GATE
    # A normal DUT that already PASSed Final Verification is blocked from running
    # Final Verification again. MAC Rewrite can create an explicit authorization.
    # FAIL/ERROR leaves the authorization ACTIVE; only a new FV PASS consumes it.
    def authorize_final_verification_rework(self, *, client_id: str, mac: str,
                                            serial_number: str, gpon_number: str,
                                            source: str, reason: str,
                                            client_ip: str) -> dict:
        client_id = (client_id or "").strip()[:250]
        mac_n = validate_mac(mac)
        serial = (serial_number or "").strip()[:200]
        gpon = (gpon_number or "").strip()[:200]
        source = (source or "MAC_REWRITE").strip()[:100] or "MAC_REWRITE"
        reason = (reason or "MAC Rewrite completed; Final Verification required again.").strip()[:2000]

        if not client_id:
            raise AppError("client_id is required")
        if not serial or not gpon:
            raise AppError("serial_number and gpon_number are required")

        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            pool = con.execute("SELECT * FROM mac_pool WHERE mac=?", (mac_n,)).fetchone()
            if pool is None:
                con.rollback(); raise AppError("MAC_NOT_IN_MASTER_LIST")
            if (pool["state"] or "").upper() != "PASS":
                con.rollback(); raise AppError("MAC_REWRITE_REQUIRES_WRITER_PASS_IDENTITY")
            if (pool["serial_number"] or "").strip().upper() != serial.upper():
                con.rollback(); raise AppError("SERIAL_DOES_NOT_MATCH_WRITER_RECORD")
            if (pool["gpon_number"] or "").strip().upper() != gpon.upper():
                con.rollback(); raise AppError("GPON_DOES_NOT_MATCH_WRITER_RECORD")

            prior_pass = con.execute(
                "SELECT verification_id, completed_at FROM verification_history "
                "WHERE mac=? AND status='PASS' AND UPPER(COALESCE(serial_number,''))=UPPER(?) "
                "ORDER BY id DESC LIMIT 1",
                (mac_n, serial),
            ).fetchone()

            # If the board has never passed FV, no override is required: normal FV is allowed.
            if prior_pass is None:
                con.commit()
                return {
                    "ok": True,
                    "authorization_required": False,
                    "authorization_id": "",
                    "status": "NOT_REQUIRED",
                    "mac": colon_mac(mac_n),
                    "serial_number": serial,
                    "gpon_number": gpon,
                }

            existing = con.execute(
                "SELECT * FROM final_verification_rework "
                "WHERE mac=? AND status='ACTIVE' ORDER BY authorized_at DESC LIMIT 1",
                (mac_n,),
            ).fetchone()
            if existing is not None:
                con.commit()
                return {
                    "ok": True,
                    "authorization_required": True,
                    "authorization_id": existing["authorization_id"],
                    "status": "ACTIVE",
                    "reused": True,
                    "mac": colon_mac(mac_n),
                    "serial_number": serial,
                    "gpon_number": gpon,
                }

            authorization_id = str(uuid.uuid4())
            ts = now_iso()
            con.execute(
                """INSERT INTO final_verification_rework(
                   authorization_id,mac,serial_number,gpon_number,source,client_id,
                   client_ip,reason,status,authorized_at)
                   VALUES(?,?,?,?,?,?,?,?, 'ACTIVE', ?)""",
                (authorization_id, mac_n, serial, gpon, source, client_id,
                 (client_ip or "")[:100], reason, ts),
            )
            con.commit()
            return {
                "ok": True,
                "authorization_required": True,
                "authorization_id": authorization_id,
                "status": "ACTIVE",
                "reused": False,
                "mac": colon_mac(mac_n),
                "serial_number": serial,
                "gpon_number": gpon,
            }

'''
    text = replace_once(
        text,
        verification_anchor,
        verification_methods + verification_anchor,
        "verification methods anchor",
    )

    prior_block = '''            prior_mac_pass = con.execute(
                "SELECT serial_number, client_id, router_ip, completed_at FROM verification_history "
                "WHERE mac=? AND status='PASS' ORDER BY id DESC LIMIT 1", (mac_n,)
            ).fetchone()
            is_retest = False
            if prior_mac_pass is not None:
                if (prior_mac_pass["serial_number"] or "").strip().upper() != serial.upper():
                    reasons.append("MAC_ALREADY_VERIFIED_WITH_DIFFERENT_SERIAL")
                else:
                    is_retest = True
'''
    new_prior_block = '''            prior_mac_pass = con.execute(
                "SELECT serial_number, client_id, router_ip, completed_at FROM verification_history "
                "WHERE mac=? AND status='PASS' ORDER BY id DESC LIMIT 1", (mac_n,)
            ).fetchone()
            is_retest = False
            rework_authorization_id = ""
            if prior_mac_pass is not None:
                if (prior_mac_pass["serial_number"] or "").strip().upper() != serial.upper():
                    reasons.append("MAC_ALREADY_VERIFIED_WITH_DIFFERENT_SERIAL")
                else:
                    active_rework = con.execute(
                        "SELECT authorization_id FROM final_verification_rework "
                        "WHERE mac=? AND status='ACTIVE' "
                        "AND UPPER(COALESCE(serial_number,''))=UPPER(?) "
                        "AND UPPER(COALESCE(gpon_number,''))=UPPER(?) "
                        "ORDER BY authorized_at DESC LIMIT 1",
                        (mac_n, serial, gpon),
                    ).fetchone()
                    if active_rework is None:
                        reasons.append("FINAL_VERIFICATION_ALREADY_PASSED")
                    else:
                        is_retest = True
                        rework_authorization_id = active_rework["authorization_id"]
'''
    text = replace_once(text, prior_block, new_prior_block, "prior FV PASS gate")

    return_anchor = '''                "reused_request": False,
                "is_retest": is_retest,
                "mac": colon_mac(mac_n),
'''
    return_new = '''                "reused_request": False,
                "is_retest": is_retest,
                "authorized_rework": bool(rework_authorization_id),
                "rework_authorization_id": rework_authorization_id,
                "mac": colon_mac(mac_n),
'''
    text = replace_once(text, return_anchor, return_new, "verification response")

    consume_anchor = '''            updated = con.execute("SELECT * FROM verification_history WHERE verification_id=?", (verification_id,)).fetchone()
            self._queue_cloud_event(
'''
    consume_new = '''            # Successful re-verification closes the rework gate. FAIL/ERROR intentionally
            # leave it ACTIVE so the same repaired board can retry Final Verification.
            if status == "PASS":
                con.execute(
                    """UPDATE final_verification_rework
                       SET status='CONSUMED', consumed_at=?, consumed_verification_id=?
                       WHERE mac=? AND status='ACTIVE'
                       AND UPPER(COALESCE(serial_number,''))=UPPER(?)
                       AND UPPER(COALESCE(gpon_number,''))=UPPER(?)""",
                    (ts, verification_id, row["mac"], row["serial_number"] or "", row["gpon_number"] or ""),
                )
            updated = con.execute("SELECT * FROM verification_history WHERE verification_id=?", (verification_id,)).fetchone()
            self._queue_cloud_event(
'''
    text = replace_once(text, consume_anchor, consume_new, "rework authorization consumption")

    api_anchor = '''            elif self.path == "/api/verify/check":
                payload = self.state.db.begin_verification(
'''
    api_new = '''            elif self.path == "/api/rework/authorize":
                payload = self.state.db.authorize_final_verification_rework(
                    client_id=str(body.get("client_id", self.headers.get("X-Station-ID", ""))),
                    mac=str(body.get("mac", "")),
                    serial_number=str(body.get("serial_number", "")),
                    gpon_number=str(body.get("gpon_number", "")),
                    source=str(body.get("source", "MAC_REWRITE")),
                    reason=str(body.get("reason", "")),
                    client_ip=self.client_address[0],
                )
                self._json(200, payload)
            elif self.path == "/api/verify/check":
                payload = self.state.db.begin_verification(
'''
    text = replace_once(text, api_anchor, api_new, "rework API endpoint")

    backup = path.with_name(path.name + ".before_v13_72_rework.bak")
    if not backup.exists():
        shutil.copy2(path, backup)

    path.write_text(text, encoding="utf-8")
    print(f"PATCHED: {path}")
    print(f"BACKUP : {backup}")
    print("Installed rules:")
    print(" - Normal repeat FV after PASS: BLOCKED")
    print(" - MAC Rewrite authorization: ALLOWED")
    print(" - Authorized FV FAIL/ERROR: retry remains allowed")
    print(" - Authorized FV PASS: authorization consumed; repeat blocked again")


def main() -> int:
    if len(sys.argv) >= 2:
        target = Path(sys.argv[1]).expanduser()
    else:
        raw = input("Full path to production server.py: ").strip().strip('"')
        if not raw:
            print("No path supplied.")
            return 2
        target = Path(raw).expanduser()
    try:
        patch_server(target.resolve())
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
