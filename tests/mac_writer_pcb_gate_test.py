"""Database-level test for configurable PCB serial gate before MAC Write."""
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
    server = load("server_gate_test", ROOT / "server" / "server.py")
    with tempfile.TemporaryDirectory() as td:
        db = server.MacDatabase(Path(td) / "factory.db")
        db.import_identities([
            {"mac":"14:D6:7C:00:00:08","serial_number":"SN00000001","gpon_number":"UNCO00000001"},
            {"mac":"14:D6:7C:00:00:10","serial_number":"SN00000002","gpon_number":"UNCO00000002"},
        ])

        # Gate ON must stop at the first AVAILABLE row and must NOT skip to a later row.
        try:
            db.allocate("WRITER/R1", "req-blocked", "127.0.0.1", require_pcb_serial=True)
            raise AssertionError("Expected PCB gate rejection")
        except server.AppError as exc:
            assert "NO PCB SERIAL NUMBER PRESENT" in str(exc)
        with db.connect() as con:
            states = [r[0] for r in con.execute("SELECT state FROM mac_pool ORDER BY seq").fetchall()]
            assert states == ["AVAILABLE", "AVAILABLE"], states

        # Box Build links the first identity; gate ON now permits exactly that row.
        db.bind_pcb_serial(
            client_id="BOX-BUILD-01", event_id="box-1", mac="14:D6:7C:00:00:08",
            serial_number="SN00000001", gpon_number="UNCO00000001", pcb_serial_number="PCB00001",
            raw_label="14:D6:7C:00:00:08,SN00000001,UNCO00000001", client_ip="127.0.0.1",
        )
        a1 = db.allocate("WRITER/R1", "req-ok", "127.0.0.1", require_pcb_serial=True)
        assert a1["mac"] == "14:D6:7C:00:00:08"
        assert a1["pcb_serial_number"] == "PCB00001"

        # Gate OFF permits the next row even though no Box Build link exists.
        a2 = db.allocate("WRITER/R2", "req-gate-off", "127.0.0.1", require_pcb_serial=False)
        assert a2["mac"] == "14:D6:7C:00:00:10"
        assert a2["pcb_serial_number"] == ""

    print("MAC Writer PCB gate test PASS")

if __name__ == "__main__":
    main()
