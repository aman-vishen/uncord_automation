"""End-to-end test for Box Build identity linking and cloud MES stage visibility."""
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
        mes = load("mes_box_test", ROOT / "mes_dashboard" / "app.py")
        cloud = ThreadingHTTPServer(("127.0.0.1", 0), mes.Handler)
        threading.Thread(target=cloud.serve_forever, daemon=True).start()
        cloud_url = f"http://127.0.0.1:{cloud.server_address[1]}"

        server = load("server_box_test", ROOT / "server" / "server.py")
        db = server.MacDatabase(Path(td) / "factory.db")
        db.import_identities([{
            "mac": "14:D6:7C:00:00:08",
            "serial_number": "SN00000002",
            "gpon_number": "UNCO00000002",
        }])
        # Stages 1-2 are external log-driven stages.
        db.report_stage_log_batch("LOG-COLLECTOR", [
            {"event_id":"wifi-1","stage_name":"WIFI_CALIBRATION","station_id":"WIFI-CAL-01","status":"PASS","mac":"14:D6:7C:00:00:08","serial_number":"SN00000002","gpon_number":"UNCO00000002","completed_at":server.now_iso(),"detail":"pass"},
            {"event_id":"label-1","stage_name":"LABEL_PRINTING","station_id":"LABEL-01","status":"PASS","mac":"14:D6:7C:00:00:08","serial_number":"SN00000002","gpon_number":"UNCO00000002","completed_at":server.now_iso(),"detail":"printed"},
        ], "127.0.0.1")
        # Box Build is stage 3 and happens while the identity row is still AVAILABLE.
        looked = db.lookup_box_build_identity(mac="14:D6:7C:00:00:08", serial_number="SN00000002", gpon_number="UNCO00000002")
        assert looked["pcb_serial_number"] == ""
        bound = db.bind_pcb_serial(
            client_id="BOX-BUILD-01", event_id="scan-1", mac="14:D6:7C:00:00:08",
            serial_number="SN00000002", gpon_number="UNCO00000002",
            pcb_serial_number="PCB-A00001", raw_label="14:D6:7C:00:00:08,SN00000002,UNCO00000002",
            client_ip="127.0.0.1",
        )
        assert bound["pcb_serial_number"] == "PCB-A00001"
        # Retry is idempotent and must not create another stage history row.
        again = db.bind_pcb_serial(
            client_id="BOX-BUILD-01", event_id="scan-2", mac="14:D6:7C:00:00:08",
            serial_number="SN00000002", gpon_number="UNCO00000002",
            pcb_serial_number="PCB-A00001", raw_label="retry",
            client_ip="127.0.0.1",
        )
        assert again["already_bound"] is True

        # MAC Write is stage 4. With the gate ON, the linked PCB allows allocation.
        allocation = db.allocate("WRITER-01/R1", "writer-request", "127.0.0.1", require_pcb_serial=True)
        assert allocation["pcb_serial_number"] == "PCB-A00001"
        db.report_result("WRITER-01/R1", allocation["reservation_id"], "PASS", "ok", "SN00000002", "UNCO00000002")

        with db.connect() as con:
            row = con.execute("SELECT pcb_serial_number,box_build_station FROM mac_pool WHERE mac=?", ("14D67C000008",)).fetchone()
            assert row["pcb_serial_number"] == "PCB-A00001"
            assert con.execute("SELECT COUNT(*) FROM stage_log_history WHERE stage_name='BOX_BUILD'").fetchone()[0] == 1

        # Duplicate PCB cannot be linked to a second identity.
        db.import_identities([{"mac":"14:D6:7C:00:00:10","serial_number":"SN00000003","gpon_number":"UNCO00000003"}])
        try:
            db.allocate("WRITER-01/R2", "writer-request-gated", "127.0.0.1", require_pcb_serial=True)
            raise AssertionError("PCB gate should reject an identity without pcb_serial_number")
        except server.AppError as exc:
            assert "NO PCB SERIAL NUMBER PRESENT" in str(exc)
        # Gate OFF remains available as a configurable legacy/nonstandard mode.
        a2 = db.allocate("WRITER-01/R2", "writer-request-2", "127.0.0.1", require_pcb_serial=False)
        db.report_result("WRITER-01/R2", a2["reservation_id"], "PASS", "ok", "SN00000003", "UNCO00000003")
        try:
            db.bind_pcb_serial(client_id="BOX-BUILD-01", event_id="scan-3", mac=a2["mac"], serial_number="SN00000003", gpon_number="UNCO00000003", pcb_serial_number="PCB-A00001", raw_label="", client_ip="127.0.0.1")
            raise AssertionError("duplicate PCB serial should have been rejected")
        except server.AppError:
            pass

        # Verification after Box Build carries PCB serial into the final MES record.
        check = db.begin_verification(
            client_id="VERIFY-01/R1", request_id="verify-request", mac=allocation["mac"],
            serial_number="SN00000002", scanned_serial_number="SN00000002",
            gpon_number="UNCO00000002", part_number="AX3000", router_ip="192.168.20.101",
            client_ip="127.0.0.1",
        )
        assert check["pcb_serial_number"] == "PCB-A00001"
        db.report_verification(
            client_id="VERIFY-01/R1", verification_id=check["verification_id"], status="PASS",
            serial_scan_result="PASS", wifi_calibration_result="PASS", bob_calibration_result="PASS",
            firmware_result="PASS", firmware_version="V1.0.1", led_result="PASS",
            reset_result="PASS", wps_result="PASS", user_mode_result="PASS", detail="all pass",
        )
        # Add stages 5-6 so all seven process stages show PASS.
        for eid, stage in [
            ("bob-1","BOB_CALIBRATION"), ("coupling-1","WIFI_COUPLING_VOIP"),
        ]:
            db.report_stage_log_batch("LOG-COLLECTOR", [{
                "event_id":eid,"stage_name":stage,"station_id":stage+"-01","status":"PASS",
                "mac":allocation["mac"],"serial_number":"SN00000002","gpon_number":"UNCO00000002",
                "completed_at":server.now_iso(),"detail":"pass",
            }], "127.0.0.1")

        worker = server.CloudSyncWorker(db, {
            "CLOUD_MES_ENABLED": "1", "CLOUD_MES_URL": cloud_url,
            "CLOUD_MES_API_KEY": "shared-test-key", "PLANT_ID": "UNCORD-TEST",
            "CLOUD_SYNC_INTERVAL_SECONDS": "30", "CLOUD_BATCH_SIZE": "100",
            "CLOUD_REQUEST_TIMEOUT_SECONDS": "10",
        })
        status = worker.sync_once()
        assert status["pending"] == 0

        token = base64.b64encode(b"admin:dashboard-test-key").decode()
        req = urllib.request.Request(cloud_url + "/api/dashboard", headers={"Authorization": "Basic " + token})
        dashboard = json.loads(urllib.request.urlopen(req).read())
        assert [row["stage"] for row in dashboard["stages"]] == [
            "1. Wi-Fi Calibration", "2. Label Printing", "3. Box Build", "4. MAC Write",
            "5. BOB Calibration", "6. Wi-Fi Coupling & VoIP", "7. Verification",
        ]
        assert dashboard["stages"][0]["pass"] == 1
        assert any(r.get("pcb_serial_number") == "PCB-A00001" for r in dashboard["recent"])
        req = urllib.request.Request(cloud_url + "/api/traceability?q=PCB-A00001", headers={"Authorization": "Basic " + token})
        trace = json.loads(urllib.request.urlopen(req).read())
        assert trace["found"] is True
        assert trace["identity"]["mac"] == allocation["mac"]
        assert trace["identity"]["pcb_serial_number"] == "PCB-A00001"
        assert any(x["stage"] == "Box Build" for x in trace["history"])
        assert any(x["stage"] == "Verification" for x in trace["history"])
        cloud.shutdown()
        print("Box Build traceability test PASS")


if __name__ == "__main__":
    main()
