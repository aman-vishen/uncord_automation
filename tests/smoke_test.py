"""Offline smoke test for identity allocation and final verification database rules."""
from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    server = load_module("ete_server", ROOT / "server" / "server.py")
    writer = load_module("ete_writer", ROOT / "mac_writer" / "app.py")
    writer_cfg = writer.parse_key_value_file(ROOT / "mac_writer" / "config.txt")

    with tempfile.TemporaryDirectory() as tmp:
        db = server.MacDatabase(Path(tmp) / "test.db")
        db.import_macs(["14-D6-7C-00-00-01", "14-D6-7C-00-00-02"])
        allocation = db.allocate("TEST-WRITER/R1", "request-1", "127.0.0.1")
        serial, gpon, _ = writer.build_identifiers(allocation, writer_cfg)
        db.report_result(
            "TEST-WRITER/R1", allocation["reservation_id"], "PASS", "smoke test", serial, gpon
        )
        check = db.begin_verification(
            client_id="TEST-VERIFY/R1",
            request_id="verify-1",
            mac=allocation["mac"],
            serial_number=serial,
            scanned_serial_number=serial,
            gpon_number=gpon,
            part_number="PN-TEST",
            router_ip="192.168.20.101",
            client_ip="127.0.0.1",
        )
        assert check["allowed"], check["reasons"]
        db.report_verification(
            client_id="TEST-VERIFY/R1",
            verification_id=check["verification_id"],
            status="PASS",
            serial_scan_result="PASS",
            wifi_calibration_result="PASS",
            bob_calibration_result="PASS",
            firmware_result="PASS",
            firmware_version="TEST-1.0",
            led_result="PASS",
            reset_result="PASS",
            wps_result="PASS",
            user_mode_result="PASS",
            detail="smoke test",
        )
        assert db.stats()["verify_pass"] == 1
        stage_result = db.report_stage_log_batch("LOG-01", [{
            "event_id": "smoke-stage-1", "stage_name": "WIFI_CALIBRATION",
            "station_id": "WIFI-CAL-01", "status": "PASS", "mac": allocation["mac"],
            "serial_number": serial, "source_file": "wifi.log", "source_offset": 1,
            "completed_at": server.now_iso(), "detail": "calibration pass",
        }], "127.0.0.1")
        assert stage_result["accepted_event_ids"] == ["smoke-stage-1"]
        assert db.stage_log_stats()["stages"]["WIFI_CALIBRATION"]["pass"] == 1

        allocation2 = db.allocate("TEST-WRITER/R2", "request-2", "127.0.0.1")
        serial2, gpon2, _ = writer.build_identifiers(allocation2, writer_cfg)
        db.report_result("TEST-WRITER/R2", allocation2["reservation_id"], "PASS", "smoke test 2", serial2, gpon2)
        mismatch = db.begin_verification(
            client_id="TEST-VERIFY/R2",
            request_id="verify-2",
            mac=allocation2["mac"],
            serial_number=serial2,
            scanned_serial_number="WRONG-SERIAL",
            gpon_number=gpon2,
            part_number="PN-TEST",
            router_ip="192.168.20.102",
            client_ip="127.0.0.1",
        )
        assert not mismatch["allowed"]
        assert "SCANNED_SERIAL_DOES_NOT_MATCH_ROUTER_SERIAL" in mismatch["reasons"]
    print("Smoke test PASS")


if __name__ == "__main__":
    main()
