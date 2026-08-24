"""End-to-end local SQLite test for factory server -> cloud MES synchronization."""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        os.environ.pop("DATABASE_URL", None)
        os.environ["CLOUD_SQLITE_PATH"] = str(Path(td) / "cloud.db")
        os.environ["INGEST_API_KEY"] = "shared-test-key"
        os.environ["DASHBOARD_USERNAME"] = "admin"
        os.environ["DASHBOARD_PASSWORD"] = "dashboard-test-key"
        mes = load("mes_test", ROOT / "mes_dashboard" / "app.py")
        cloud = ThreadingHTTPServer(("127.0.0.1", 0), mes.Handler)
        threading.Thread(target=cloud.serve_forever, daemon=True).start()
        cloud_url = f"http://127.0.0.1:{cloud.server_address[1]}"

        server = load("server_test", ROOT / "server" / "server.py")
        db = server.MacDatabase(Path(td) / "factory.db")
        db.import_identities([{
            "mac":"14:D6:7C:00:00:01", "serial_number":"ZTEG00000001", "gpon_number":"ZTEG00000001"
        }])
        # Stages 1-2: Wi-Fi Calibration and Label Printing happen before Box Build.
        db.report_stage_log_batch("LOG-COLLECTOR", [
            {"event_id":"wifi-1","stage_name":"WIFI_CALIBRATION","station_id":"WIFI-CAL-01","status":"PASS","mac":"14:D6:7C:00:00:01","serial_number":"ZTEG00000001","source_file":"wifi.log","source_offset":1,"detail":"RF calibration pass","completed_at":server.now_iso()},
            {"event_id":"label-1","stage_name":"LABEL_PRINTING","station_id":"LABEL-01","status":"PASS","mac":"14:D6:7C:00:00:01","serial_number":"ZTEG00000001","gpon_number":"ZTEG00000001","source_file":"label.csv","source_offset":1,"detail":"printed","completed_at":server.now_iso()},
        ], "127.0.0.1")
        # Stage 3: Box Build links PCB serial while the identity is still AVAILABLE.
        db.bind_pcb_serial(
            client_id="BOX-BUILD-01", event_id="box-1", mac="14:D6:7C:00:00:01",
            serial_number="ZTEG00000001", gpon_number="ZTEG00000001",
            pcb_serial_number="PCB00000001", raw_label="14:D6:7C:00:00:01,ZTEG00000001,ZTEG00000001",
            client_ip="127.0.0.1",
        )
        # Stage 4: MAC Writer gate accepts only because PCB serial is now present.
        allocation = db.allocate("WRITER-01/R1", "writer-request", "127.0.0.1", require_pcb_serial=True)
        assert allocation["pcb_serial_number"] == "PCB00000001"
        db.report_result("WRITER-01/R1", allocation["reservation_id"], "PASS", "ok", "ZTEG00000001", "ZTEG00000001")
        check = db.begin_verification(
            client_id="VERIFY-01/R1", request_id="verify-request", mac=allocation["mac"],
            serial_number="ZTEG00000001", scanned_serial_number="ZTEG00000001",
            gpon_number="ZTEG00000001", part_number="AX3000", router_ip="192.168.20.101",
            client_ip="127.0.0.1",
        )
        db.report_verification(
            client_id="VERIFY-01/R1", verification_id=check["verification_id"], status="PASS",
            serial_scan_result="PASS", wifi_calibration_result="PASS", bob_calibration_result="PASS",
            firmware_result="PASS", firmware_version="V1.0.1", led_result="PASS",
            reset_result="PASS", wps_result="PASS", user_mode_result="PASS", detail="all pass",
        )
        # Stages 5-6: BOB Calibration and Wi-Fi Coupling & VoIP.
        db.report_stage_log_batch("LOG-COLLECTOR", [
            {"event_id":"bob-1","stage_name":"BOB_CALIBRATION","station_id":"BOB-01","status":"PASS","mac":allocation["mac"],"source_file":"bob.jsonl","source_offset":1,"detail":"optical pass","completed_at":server.now_iso()},
            {"event_id":"coupling-1","stage_name":"WIFI_COUPLING_VOIP","station_id":"COUPLING-01","status":"PASS","mac":allocation["mac"],"source_file":"coupling.log","source_offset":1,"detail":"Wi-Fi and VoIP pass","completed_at":server.now_iso()},
        ], "127.0.0.1")
        worker = server.CloudSyncWorker(db, {
            "CLOUD_MES_ENABLED": "1", "CLOUD_MES_URL": cloud_url,
            "CLOUD_MES_API_KEY": "shared-test-key", "PLANT_ID": "UNCORD-TEST",
            "CLOUD_SYNC_INTERVAL_SECONDS": "30", "CLOUD_BATCH_SIZE": "100",
            "CLOUD_REQUEST_TIMEOUT_SECONDS": "10",
        })
        status = worker.sync_once()
        assert status["pending"] == 0 and status["synced"] == 7

        token = base64.b64encode(b"admin:dashboard-test-key").decode()
        req = urllib.request.Request(cloud_url + "/api/dashboard", headers={"Authorization": "Basic " + token})
        dashboard = json.loads(urllib.request.urlopen(req).read())
        assert dashboard["kpi"]["writer_pass"] == 1
        assert dashboard["kpi"]["line_input"] == 1
        assert dashboard["kpi"]["final_pass"] == 1
        assert [row["stage"] for row in dashboard["stages"]] == [
            "1. Wi-Fi Calibration", "2. Label Printing", "3. Box Build",
            "4. MAC Write", "5. BOB Calibration", "6. Wi-Fi Coupling & VoIP", "7. Verification",
        ]
        assert all(row["pass"] == 1 for row in dashboard["stages"])
        cloud.shutdown()
        print("Cloud sync integration test PASS")


if __name__ == "__main__":
    main()
