"""MAC Writer v13.9 scan lookup + exact reservation test."""
from __future__ import annotations
import importlib.util
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module

def main() -> None:
    server = load("server_scan_test", ROOT / "server" / "server.py")
    with tempfile.TemporaryDirectory() as td:
        db = server.MacDatabase(Path(td) / "factory.db")
        db.import_identities([
            {"mac":"14:D6:7C:00:00:08","serial_number":"SN-FIRST","gpon_number":"UNCOFIRST"},
            {"mac":"14:D6:7C:00:00:10","serial_number":"SN-SECOND","gpon_number":"UNCOSECOND"},
        ])
        db.bind_pcb_serial(
            client_id="BOX-01", event_id="box-second", mac="14:D6:7C:00:00:10",
            serial_number="SN-SECOND", gpon_number="UNCOSECOND", pcb_serial_number="PCB-SECOND",
            raw_label="second", client_ip="127.0.0.1",
        )

        by_mac = db.lookup_identity_by_scan("14:D6:7C:00:00:10")
        by_serial = db.lookup_identity_by_scan("sn-second")
        by_gpon = db.lookup_identity_by_scan("uncosecond")
        for item in (by_mac, by_serial, by_gpon):
            assert item["mac"] == "14:D6:7C:00:00:10"
            assert item["serial_number"] == "SN-SECOND"
            assert item["gpon_number"] == "UNCOSECOND"
            assert item["pcb_serial_number"] == "PCB-SECOND"
            assert item["state"] == "AVAILABLE"

        # Exact scanned allocation must reserve SECOND even though FIRST is earlier and AVAILABLE.
        allocation = db.allocate_specific(
            "WRITER/R2", "scan-request-2", "127.0.0.1", by_serial["mac"], require_pcb_serial=True
        )
        assert allocation["mac"] == "14:D6:7C:00:00:10"
        with db.connect() as con:
            states = {r["mac"]: r["state"] for r in con.execute("SELECT mac,state FROM mac_pool").fetchall()}
        assert states["14D67C000008"] == "AVAILABLE"
        assert states["14D67C000010"] == "RESERVED"

        try:
            db.lookup_identity_by_scan("DOES-NOT-EXIST")
            raise AssertionError("expected missing scan failure")
        except server.AppError as exc:
            assert "IDENTITY_NOT_FOUND" in str(exc)

    print("MAC Writer scan identity test PASS")

if __name__ == "__main__":
    main()
