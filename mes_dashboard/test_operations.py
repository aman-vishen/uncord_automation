"""Regression checks for analytics semantics and the original ingest protocol."""
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

TEMP = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
os.environ["DATABASE_URL"] = ""
os.environ["CLOUD_SQLITE_PATH"] = str(Path(TEMP.name) / "test.db")
sys.path.insert(0, str(Path(__file__).parent))
app = importlib.import_module("app")


def event(identifier, status="PASS", mac="AA:BB:CC:DD:EE:01", stamp="2026-10-09T03:00:00Z", stage="QUALITY_VERIFICATION_RESULT", plant="P1", **extra):
    raw = dict(event_id=identifier, event_type=stage if stage.endswith("RESULT") else "STAGE_LOG_RESULT",
               stage_name=stage, plant_id=plant, station_id="Station 01", status=status,
               mac=mac, completed_at=stamp, model="Router A", serial_number="Serial " + mac, **extra)
    return raw


class OperationsTests(unittest.TestCase):
    def setUp(self):
        with app.db.connect() as con:
            con.execute("DELETE FROM production_events")
            con.commit()
        self.filters = {"start": ["2026-10-09"], "end": ["2026-10-09"]}
        app.operations.invalidate()

    def insert(self, *events):
        accepted, duplicates, rejected = app.db.insert_events(events)
        self.assertFalse(rejected)
        app.operations.invalidate()
        return accepted, duplicates

    def test_supported_stages_preserve_ingest_and_duplicates(self):
        events = [event("stage-"+stage, stage=stage) for stage in ["WIFI_CALIBRATION", "LABEL_PRINTING", "GIFT_BOX_LABEL", "MASTER_CARTON_LABEL", "BOB_CALIBRATION", "WIFI_COUPLING_VOIP", "BOX_BUILD"]]
        accepted, _ = self.insert(*events)
        self.assertEqual(len(accepted), 7)
        _, duplicates = self.insert(*events)
        self.assertEqual(len(duplicates), 7)
        _, _, rejected = app.db.insert_events([event("invalid", stage="UNSUPPORTED")])
        self.assertEqual(rejected[0]["error"], "Unsupported or missing stage_name")
        summary = app.operations.summary(self.filters, 0)
        self.assertEqual(len(summary["stages"]), 9)
        self.assertEqual(sum(r["attempts"] for r in summary["stages"]), 7)

    def test_attempts_first_pass_and_latest_unit_results(self):
        self.insert(event("first", "FAIL"), event("retry", stamp="2026-10-09T04:00:00Z"),
                    event("second-product", mac="AA:BB:CC:DD:EE:02"),
                    event("unlinked", mac=""))
        result = app.operations.summary(self.filters, 0)
        self.assertEqual(result["totals"]["attempts"], 4)
        self.assertEqual(result["totals"]["unlinked"], 1)
        m = result["metrics"]
        self.assertEqual(m["good_units"], 2)
        self.assertEqual(m["tested_units"], 2)
        self.assertEqual(m["first_units"], 2)
        self.assertEqual(m["final_first_pass_yield"], 50)
        self.assertEqual(m["retested_units"], 1)
        self.assertEqual(m["retest_rate"], 50)
        self.assertEqual(m["extra_attempts"], 1)

    def test_first_attempt_is_ranked_before_date_filter(self):
        self.insert(event("old-first", "FAIL", stamp="2026-10-08T03:00:00Z"), event("new-pass"))
        result = app.operations.summary(self.filters, 0)
        self.assertEqual(result["metrics"]["good_units"], 1)
        self.assertEqual(result["metrics"]["first_units"], 0)
        self.assertIsNone(result["metrics"]["final_first_pass_yield"])

    def test_first_attempt_retains_subsecond_order(self):
        self.insert(event("z-first", "FAIL", stamp="2026-10-09T03:00:00.100Z"),
                    event("a-retry", stamp="2026-10-09T03:00:00.200Z"))
        result = app.operations.summary(self.filters, 0)
        self.assertEqual(result["metrics"]["final_first_pass_yield"], 0)
        self.assertEqual(result["metrics"]["good_units"], 1)

    def test_latest_failure_is_not_good_output_and_plants_are_separate(self):
        self.insert(event("pass"), event("fail-later", "ERROR", stamp="2026-10-09T05:00:00Z"), event("same-mac-other-plant", plant="P2"))
        result = app.operations.summary(self.filters, 0)
        self.assertEqual(result["metrics"]["good_units"], 1)
        self.assertEqual(result["metrics"]["latest_failed_units"], 1)
        query = dict(self.filters, plant=["P1"])
        self.assertEqual(app.operations.summary(query, 0)["metrics"]["good_units"], 0)

    def test_ist_boundaries_and_normalized_mac(self):
        self.insert(event("before", stamp="2026-10-08T18:29:59Z", mac="AA:BB:CC:DD:EE:03"),
                    event("start", "FAIL", stamp="2026-10-08T18:30:00Z"),
                    event("last", stamp="2026-10-09T18:29:59Z", mac="aabbccddee01"),
                    event("after", stamp="2026-10-09T18:30:00Z", mac="AA:BB:CC:DD:EE:04"))
        result = app.operations.summary(self.filters, 0)
        self.assertEqual(result["totals"]["attempts"], 2)
        self.assertEqual(result["metrics"]["tested_units"], 1)
        self.assertEqual(result["metrics"]["good_units"], 1)
        self.assertEqual(result["daily"][0]["day"], "2026-10-09")
        self.assertEqual(result["daily"][0]["attempts"], 2)

    def test_search_beyond_300_and_pagination(self):
        events = [event(f"row-{i:04}", stage="BOX_BUILD", source_offset=i, detail="needle old record" if i==0 else "normal") for i in range(351)]
        self.insert(*events)
        found = app.operations.records(dict(self.filters, q=["needle"]))
        self.assertEqual(found["total"], 1)
        self.assertEqual(found["rows"][0]["event_id"], "row-0000")
        found = app.operations.records(dict(self.filters, page=["2"], page_size=["100"]))
        self.assertEqual(found["total"], 351)
        self.assertEqual(len(found["rows"]), 100)
        self.assertEqual(found["pages"], 4)
        literal = app.operations.records(dict(self.filters, q=["%_"]))
        self.assertEqual(literal["total"], 0)
        injection = app.operations.records(dict(self.filters, q=["' OR 1=1 --"]))
        self.assertEqual(injection["total"], 0)

    def test_failed_filter_includes_error(self):
        self.insert(event("pass"), event("fail", "FAIL"), event("error", "ERROR"))
        found = app.operations.records(dict(self.filters, status=["FAILED"]))
        self.assertEqual(found["total"], 2)

    def test_unit_drilldown_matches_latest_outcome_not_all_pass_attempts(self):
        self.insert(event("pass"), event("fail-later", "FAIL", stamp="2026-10-09T05:00:00Z"),
                    event("good", mac="AA:BB:CC:DD:EE:02"),
                    event("unlinked-pass", mac=""))
        good = app.operations.records(dict(self.filters, cohort=["latest_good"]))
        failed = app.operations.records(dict(self.filters, cohort=["latest_failed"]))
        self.assertEqual([row["event_id"] for row in good["rows"]], ["good"])
        self.assertEqual([row["event_id"] for row in failed["rows"]], ["fail-later"])

    def test_passport_requires_ambiguity_resolution(self):
        self.insert(event("one", plant="P1"), event("two", plant="P2"))
        result = app.operations.passport("AA:BB:CC:DD:EE:01")
        self.assertTrue(result["ambiguous"])
        result = app.operations.passport("AA:BB:CC:DD:EE:01", plant="P1", mac="AABBCCDDEE01")
        self.assertFalse(result["ambiguous"])
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["identity"]["plant_id"], "P1")

    def test_empty_data_is_unknown_and_history_has_continuous_days(self):
        query = {"start": ["2026-10-07"], "end": ["2026-10-09"]}
        result = app.operations.summary(query, 0)
        self.assertEqual(len(result["daily"]), 3)
        self.assertIsNone(result["metrics"]["final_first_pass_yield"])
        self.assertIsNone(result["metrics"]["wip"])
        self.assertTrue(all(r["yield_rate"] is None for r in result["stages"]))
        self.assertIn("No production", result["attention"][0]["title"])

    def test_invalid_date_range(self):
        with self.assertRaises(ValueError):
            app.operations.records({"start": ["2026-10-09"], "end": ["2026-10-08"]})


if __name__ == "__main__":
    unittest.main()
