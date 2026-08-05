"""Parser and durable-queue test for the production stage log collector."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path
from queue import Queue

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    collector = load("collector_test", ROOT / "stage_log_collector" / "app.py")
    engine = collector.CollectorEngine(Queue())
    samples = {
        "WIFI_CALIBRATION": "wifi_calibration.log",
        "LABEL_PRINTING": "label_printing.csv",
        "BOB_CALIBRATION": "bob_calibration.jsonl",
        "WIFI_COUPLING_VOIP": "wifi_coupling_voip.log",
    }
    for cfg in engine.stages:
        line = (ROOT / "stage_log_collector" / "sample_logs" / samples[cfg.key]).read_text(encoding="utf-8").splitlines()[0]
        parsed = collector.StageParser(cfg).parse(line)
        assert parsed and parsed["status"] == "PASS", (cfg.key, parsed)
        assert parsed["mac"] == "14:D6:7C:00:00:01"

    with tempfile.TemporaryDirectory() as td:
        db = collector.CollectorDatabase(Path(td) / "collector.db")
        payload = {
            "event_id": "collector-event-1", "stage_name": "LABEL_PRINTING",
            "station_id": "LABEL-01", "status": "PASS", "mac": "14:D6:7C:00:00:01",
            "serial_number": "ZTEG00000001", "gpon_number": "ZTEG00000001",
            "completed_at": collector.now_iso(), "source_file": "label.csv", "source_offset": 10,
            "raw_record": "sample", "detail": "printed",
        }
        assert db.queue_event(payload)
        assert not db.queue_event(payload)
        assert len(db.pending(10)) == 1
        db.mark_sent([payload["event_id"]])
        assert db.stats()["overall"]["SENT"] == 1

        # File-tail integration: BEGIN initializes at zero, the next poll queues both lines.
        log_path = Path(td) / "wifi.log"
        log_path.write_text((ROOT / "stage_log_collector" / "sample_logs" / "wifi_calibration.log").read_text(), encoding="utf-8")
        cfg = next(x for x in engine.stages if x.key == "WIFI_CALIBRATION")
        cfg.log_path = log_path
        cfg.start_position = "BEGIN"
        engine.db = collector.CollectorDatabase(Path(td) / "tail.db")
        engine._read_stage(cfg)
        engine._read_stage(cfg)
        pending = engine.db.pending(10)
        assert len(pending) == 2
        assert {row["status"] for row in pending} == {"PASS", "FAIL"}
    print("Stage log collector test PASS")


if __name__ == "__main__":
    main()
