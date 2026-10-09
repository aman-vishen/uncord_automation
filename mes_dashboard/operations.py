"""Read-only production analytics. Ingestion remains owned by app.py."""
from __future__ import annotations

import json
import math
import threading
import time as clock
from contextlib import closing
from decimal import Decimal
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

PLANT_ZONE = ZoneInfo("Asia/Kolkata")
STAGES = {
    "WIFI_CALIBRATION": "Wi-Fi calibration",
    "LABEL_PRINTING": "Label + PCB link",
    "IDENTITY_WRITER_RESULT": "MAC write + firmware",
    "BOB_CALIBRATION": "BOB calibration",
    "WIFI_COUPLING_VOIP": "Wi-Fi coupling & VoIP",
    "QUALITY_VERIFICATION_RESULT": "Final verification",
    "BOX_BUILD": "Box build",
    "GIFT_BOX_LABEL": "Gift box label",
    "MASTER_CARTON_LABEL": "Master carton label",
}


def percent(passed: int, tested: int) -> float | None:
    return round(100 * passed / tested, 2) if tested else None


def iso(value: Any) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value or "")


def ranges(query: dict[str, list[str]]) -> tuple[date, date]:
    today = datetime.now(PLANT_ZONE).date()
    try:
        start = date.fromisoformat(query.get("start", [(today - timedelta(days=6)).isoformat()])[0])
        end = date.fromisoformat(query.get("end", [today.isoformat()])[0])
    except ValueError as exc:
        raise ValueError("Choose valid From and To dates") from exc
    if end < start:
        raise ValueError("To date must be on or after From date")
    if (end - start).days > 366:
        raise ValueError("Choose a period of 367 days or fewer")
    return start, end


def integer(query: dict[str, list[str]], key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(query.get(key, [str(default)])[0])))
    except ValueError as exc:
        raise ValueError(f"Invalid {key}") from exc


class Operations:
    def __init__(self, db: Any) -> None:
        self.db = db
        self.postgres = db.postgres
        self._summary_cache: dict[Any, Any] = {}
        self._summary_lock = threading.RLock()
        stamp = "EXTRACT(EPOCH FROM completed_at)" if self.postgres else "(CAST(strftime('%s', completed_at) AS INTEGER) + CAST(substr(strftime('%f', completed_at),4) AS REAL)/1000.0)"
        receipt = "EXTRACT(EPOCH FROM received_at)" if self.postgres else "CAST(strftime('%s', received_at) AS INTEGER)"
        model_fields = ["model", "selected_model", "product_model", "model_name"]
        expressions = [f"NULLIF(TRIM(payload_json->>'{key}'),'')" if self.postgres else f"NULLIF(TRIM(json_extract(payload_json,'$.{key}')),'')" for key in model_fields]
        model = "COALESCE(" + ",".join(expressions + ["NULLIF(TRIM(part_number),'')", "'UNKNOWN'"]) + ")"
        # MAC is the common product link in the existing protocol. Never infer units from event IDs.
        mac = "UPPER(REPLACE(REPLACE(REPLACE(TRIM(COALESCE(mac,'')),':',''),'-',''),' ',''))"
        stage = "CASE WHEN event_type='STAGE_LOG_RESULT' THEN stage_name ELSE event_type END"
        self.base = f"""WITH events AS (
            SELECT production_events.*, {stamp} AS stamp, {receipt} AS receipt_stamp,
                   {model} AS model, {mac} AS unit_key, {stage} AS operation
            FROM production_events WHERE event_type<>'MAC_POOL_SNAPSHOT'
        ) """

    def query(self, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with closing(self.db.connect()) as con:
            with con:
                cur = con.cursor()
                statement = self.base + sql
                cur.execute(statement.replace("?", "%s") if self.postgres else statement, args)
                rows = cur.fetchall()
        return [{key: float(value) if isinstance(value, Decimal) else value for key, value in dict(row).items()} for row in rows]

    def bounds(self, query: dict[str, list[str]]) -> tuple[date, date, int, int]:
        start, end = ranges(query)
        lower = int(datetime.combine(start, time.min, PLANT_ZONE).timestamp())
        upper = int(datetime.combine(end + timedelta(days=1), time.min, PLANT_ZONE).timestamp())
        return start, end, lower, upper

    def filters(self, query: dict[str, list[str]], include_record_filters: bool = False) -> tuple[str, tuple[Any, ...]]:
        _, _, lower, upper = self.bounds(query)
        clauses = ["stamp>=?", "stamp<?"]
        args: list[Any] = [lower, upper]
        for key, column in [("model", "model"), ("plant", "plant_id")]:
            value = query.get(key, [""])[0].strip()[:250]
            if value:
                clauses.append(f"UPPER({column})=UPPER(?)")
                args.append(value)
        if include_record_filters:
            for key, column in [("stage", "operation"), ("status", "status"), ("station", "station_id")]:
                value = query.get(key, [""])[0].strip()[:250]
                if value:
                    if key == "status" and value == "FAILED":
                        clauses.append("status IN ('FAIL','ERROR')")
                    else:
                        clauses.append(f"{column}=?")
                        args.append(value)
            search = query.get("q", [""])[0].strip()[:250]
            if search:
                # Treat %, _ and ! literally in LIKE queries.
                value = "%" + search.upper().replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
                fields = ["mac", "serial_number", "gpon_number", "pcb_serial_number", "model", "station_id", "operation", "source_file", "detail", "event_id"]
                clauses.append("(" + " OR ".join(f"UPPER(COALESCE({field},'')) LIKE ? ESCAPE '!'" for field in fields) + ")")
                args.extend([value] * len(fields))
            cohort = query.get("cohort", [""])[0]
            if cohort in {"latest_good", "latest_failed"}:
                period_where, period_args = self.filters(query)
                result_status = "status='PASS'" if cohort == "latest_good" else "status IN ('FAIL','ERROR')"
                clauses.append("event_id IN (SELECT event_id FROM (SELECT event_id,status,ROW_NUMBER() OVER(PARTITION BY plant_id,unit_key ORDER BY stamp DESC,event_id DESC) AS position FROM events WHERE unit_key<>'' AND operation='QUALITY_VERIFICATION_RESULT' AND " + period_where + ") cohort WHERE position=1 AND " + result_status + ")")
                args.extend(period_args)
        return " AND ".join(clauses), tuple(args)

    def bucket(self, column: str, grain: str) -> str:
        if self.postgres:
            fmt = "YYYY-MM-DD" if grain == "day" else "YYYY-MM-DD HH24:00"
            return f"TO_CHAR({column} AT TIME ZONE 'Asia/Kolkata', '{fmt}')"
        fmt = "%Y-%m-%d" if grain == "day" else "%Y-%m-%d %H:00"
        return f"strftime('{fmt}',{column},'+330 minutes')"

    def summary(self, query: dict[str, list[str]], target_uph: int) -> dict[str, Any]:
        key = (tuple(sorted((k, tuple(v)) for k, v in query.items())), target_uph)
        with self._summary_lock:
            cached = self._summary_cache.get(key)
            if cached and clock.monotonic() - cached[0] < 8:
                return cached[1]
            result = self._summary(query, target_uph)
            if len(self._summary_cache) >= 32:
                self._summary_cache.pop(next(iter(self._summary_cache)))
            self._summary_cache[key] = (clock.monotonic(), result)
            return result

    def invalidate(self) -> None:
        with self._summary_lock:
            self._summary_cache.clear()

    def _summary(self, query: dict[str, list[str]], target_uph: int) -> dict[str, Any]:
        start, end, lower, upper = self.bounds(query)
        where, args = self.filters(query)
        now = datetime.now(timezone.utc)
        totals = self.query(f"""SELECT COUNT(*) AS attempts,
            COALESCE(SUM(CASE WHEN status='PASS' THEN 1 ELSE 0 END),0) AS passed,
            COALESCE(SUM(CASE WHEN status IN ('FAIL','ERROR') THEN 1 ELSE 0 END),0) AS failed,
            COALESCE(SUM(CASE WHEN unit_key='' THEN 1 ELSE 0 END),0) AS unlinked,
            MAX(stamp) AS last_activity FROM events WHERE {where}""", args)[0]
        # Rank globally before filtering dates: FPY excludes later attempts on earlier units.
        ranked = """, ranked AS (SELECT events.*,
            ROW_NUMBER() OVER(PARTITION BY plant_id,unit_key,operation ORDER BY stamp,event_id) AS first_attempt
            FROM events WHERE unit_key<>'' AND stamp IS NOT NULL) """
        first = self.query(ranked + f"""SELECT COUNT(*) AS tested,
            COALESCE(SUM(CASE WHEN status='PASS' THEN 1 ELSE 0 END),0) AS passed
            FROM ranked WHERE first_attempt=1 AND operation='QUALITY_VERIFICATION_RESULT' AND {where}""", args)[0]
        stages = self.query(f"""SELECT operation,COUNT(*) AS attempts,
            SUM(CASE WHEN status='PASS' THEN 1 ELSE 0 END) AS passed,
            SUM(CASE WHEN status IN ('FAIL','ERROR') THEN 1 ELSE 0 END) AS failed,
            MAX(stamp) AS last_activity FROM events WHERE {where} GROUP BY operation""", args)
        stage_first = {r["operation"]: r for r in self.query(ranked + f"""SELECT operation,COUNT(*) AS tested,
            SUM(CASE WHEN status='PASS' THEN 1 ELSE 0 END) AS passed FROM ranked
            WHERE first_attempt=1 AND {where} GROUP BY operation""", args)}
        stage_map = {r["operation"]: r for r in stages}
        complete_stages = []
        for key, label in STAGES.items():
            r = stage_map.get(key, {"attempts": 0, "passed": 0, "failed": 0, "last_activity": None})
            f = stage_first.get(key, {"tested": 0, "passed": 0})
            complete_stages.append(dict(r, key=key, label=label, yield_rate=percent(r["passed"], r["attempts"]),
                first_pass_yield=percent(f["passed"], f["tested"]), first_units=f["tested"]))
        final_scope = f"operation='QUALITY_VERIFICATION_RESULT' AND unit_key<>'' AND {where}"
        latest = ", final AS (SELECT events.*,ROW_NUMBER() OVER(PARTITION BY plant_id,unit_key ORDER BY stamp DESC,event_id DESC) AS latest FROM events WHERE " + final_scope + ") "
        final = self.query(latest + """SELECT COUNT(*) AS tested,
            COALESCE(SUM(CASE WHEN status='PASS' THEN 1 ELSE 0 END),0) AS good,
            COALESCE(SUM(CASE WHEN status IN ('FAIL','ERROR') THEN 1 ELSE 0 END),0) AS failed
            FROM final WHERE latest=1""", args)[0]
        retest = self.query(f"""SELECT COUNT(*) AS units,
            COALESCE(SUM(CASE WHEN attempts>1 THEN 1 ELSE 0 END),0) AS retested,
            COALESCE(SUM(attempts-1),0) AS extra_attempts FROM
            (SELECT plant_id,unit_key,COUNT(*) AS attempts FROM events WHERE {final_scope}
             GROUP BY plant_id,unit_key) counts""", args)[0]
        days = self.query(f"""SELECT {self.bucket('completed_at','day')} AS day, COUNT(*) AS attempts,
            SUM(CASE WHEN status='PASS' THEN 1 ELSE 0 END) AS passed,
            SUM(CASE WHEN status IN ('FAIL','ERROR') THEN 1 ELSE 0 END) AS failed
            FROM events WHERE operation='QUALITY_VERIFICATION_RESULT' AND {where} GROUP BY day ORDER BY day""", args)
        daily_good = {r["day"]: r["good"] for r in self.query(latest + f"""SELECT {self.bucket('completed_at','day')} AS day,COUNT(*) AS good
            FROM final WHERE latest=1 AND status='PASS' GROUP BY day""", args)}
        by_day = {r["day"]: r for r in days}
        daily = []
        for i in range((end-start).days+1):
            day = (start+timedelta(days=i)).isoformat()
            r = by_day.get(day, {"attempts": 0,"passed": 0,"failed": 0})
            daily.append(dict(r,day=day,good=daily_good.get(day,0),observed=bool(r["attempts"])))
        hours = self.query(f"""SELECT {self.bucket('completed_at','hour')} AS hour,COUNT(*) AS attempts,
            SUM(CASE WHEN status='PASS' THEN 1 ELSE 0 END) AS passed,
            SUM(CASE WHEN status IN ('FAIL','ERROR') THEN 1 ELSE 0 END) AS failed
            FROM events WHERE operation='QUALITY_VERIFICATION_RESULT' AND {where} GROUP BY hour ORDER BY hour""",args)
        stations = self.query(f"""SELECT plant_id,station_id,operation,COUNT(*) AS attempts,
            SUM(CASE WHEN status='PASS' THEN 1 ELSE 0 END) AS passed,
            SUM(CASE WHEN status IN ('FAIL','ERROR') THEN 1 ELSE 0 END) AS failed,
            MAX(stamp) AS last_activity FROM events WHERE {where}
            GROUP BY plant_id,station_id,operation ORDER BY attempts DESC""", args)
        for r in stations:
            r["label"] = STAGES.get(r["operation"],r["operation"])
            r["yield_rate"] = percent(r["passed"], r["attempts"])
        # Freshness is global, explicitly independent of the historical filters.
        freshness = self.query("SELECT MAX(receipt_stamp) AS last_received,MAX(stamp) AS last_activity FROM events")[0]
        freshness["receipt_age_seconds"] = max(0,int(now.timestamp()-freshness["last_received"])) if freshness["last_received"] else None
        options = {"models": [r["model"] for r in self.query("SELECT DISTINCT model FROM events ORDER BY model")],
                   "plants": [r["plant_id"] for r in self.query("SELECT DISTINCT plant_id FROM events ORDER BY plant_id")],
                   "stations": [r["station_id"] for r in self.query("SELECT DISTINCT station_id FROM events ORDER BY station_id")]}
        recent = self.records(dict(query, page=["1"],page_size=["8"]))["rows"]
        inventory = self.db.latest_snapshot()
        inventory_metrics = inventory.get("metrics", {}) if isinstance(inventory,dict) else {}
        snapshot_rows = self.db.recent_events("MAC_POOL_SNAPSHOT",1)
        first_fpy = percent(first["passed"],first["tested"])
        attention = []
        if not totals["attempts"]:
            attention.append({"severity":"neutral","title":"No production in this selection","detail":"Widen the dates or reset model and plant filters. No quality assessment is available.","action":"records"})
        if totals["unlinked"]:
            attention.append({"severity":"warning","title":f"{totals['unlinked']:,} attempts have no MAC link","detail":"Visible in attempt totals; excluded from unit counts and first-pass yield. Review identity coverage.","action":"records"})
        if freshness["receipt_age_seconds"] is not None and freshness["receipt_age_seconds"]>900:
            attention.append({"severity":"warning","title":"No recent cloud receipts","detail":"An idle line and delayed uploads both cause this. Check the Central Server sync status.","action":"health"})
        for r in sorted(complete_stages,key=lambda x:x["failed"],reverse=True)[:3]:
            if r["failed"]:
                attention.append({"severity":"warning","title":f"{r['label']}: {r['failed']:,} failed attempts","detail":f"{r['attempts']:,} total attempts in the selected period. Inspect results before assigning a cause.","action":"records","stage":r["key"],"status":"FAIL"})
        return {"range":{"start":start.isoformat(),"end":end.isoformat(),"timezone":"Asia/Kolkata"},
            "generated_at":now.isoformat(),"options":options,"totals":totals,
            "metrics":{"good_units":final["good"],"tested_units":final["tested"],"latest_failed_units":final["failed"],
                "final_first_pass_yield":first_fpy,"first_units":first["tested"],"first_passed":first["passed"],
                "retested_units":retest["retested"],"retest_rate":percent(retest["retested"],retest["units"]),
                "extra_attempts":retest["extra_attempts"],"attempt_pass_rate":percent(totals["passed"],totals["attempts"]),
                "wip":None,"target_uph":target_uph},
            "stages":complete_stages,"daily":daily,"hourly":hours,"stations":stations,
            "freshness":freshness,"attention":attention,"recent":recent,
            "inventory":{"metrics":inventory_metrics,"completed_at":iso(snapshot_rows[0]["completed_at"]) if snapshot_rows else None},
            "definitions":{"good_units":"MAC-linked products whose latest final-verification attempt within this period passed. Each plant + MAC is counted once; this is not a shipment count.",
                "first_pass_yield":"Pass rate of each plant + MAC's earliest recorded final-verification attempt, where that first attempt occurred in this period. Earlier unreported history cannot be inferred.",
                "retest_rate":"MAC-linked products with multiple final-verification attempts inside this period divided by products tested in the period.",
                "wip":"Not calculated: confirmed routes, opening WIP and product state transitions are required."}}

    def records(self, query: dict[str,list[str]]) -> dict[str,Any]:
        where,args = self.filters(query,True)
        page = integer(query,"page",1,1,1000000)
        size = integer(query,"page_size",25,1,100)
        total = self.query(f"SELECT COUNT(*) AS count FROM events WHERE {where}",args)[0]["count"]
        page = min(page,max(1,math.ceil(total/size)))
        sort = query.get("sort",["newest"])[0]
        order = "stamp ASC,event_id ASC" if sort=="oldest" else "stamp DESC,event_id DESC"
        rows = self.query(f"SELECT * FROM events WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",args+(size,(page-1)*size))
        return {"rows":[self.public(r) for r in rows],"total":total,"page":page,"page_size":size,"pages":max(1,math.ceil(total/size))}

    def public(self,row: dict[str,Any],detail: bool=False) -> dict[str,Any]:
        keys = ["event_id","event_type","operation","model","plant_id","station_id","mac","serial_number","gpon_number","pcb_serial_number","status","completed_at","received_at","source_file","detail","firmware_version","router_ip","stamp","receipt_stamp"]
        result = {key:iso(row.get(key)) if key in ("completed_at","received_at") else row.get(key) for key in keys}
        result["stage_label"] = STAGES.get(row.get("operation"),row.get("operation"))
        if detail:
            result["raw_log"] = row.get("raw_log")
            payload = row.get("payload_json")
            result["payload"] = payload if isinstance(payload,dict) else json.loads(payload or "{}")
        return result

    def event(self,event_id: str) -> dict[str,Any]:
        rows = self.query("SELECT * FROM events WHERE event_id=?",(event_id,))
        return {"found":bool(rows),"event":self.public(rows[0],True) if rows else None}

    def passport(self,query: str,plant: str="",mac: str="") -> dict[str,Any]:
        q = query.strip()[:250]
        compact = q.upper().replace(":", "").replace("-", "").replace(" ", "")
        where = "(UPPER(mac)=UPPER(?) OR unit_key=? OR UPPER(serial_number)=UPPER(?) OR UPPER(gpon_number)=UPPER(?) OR UPPER(pcb_serial_number)=UPPER(?))"
        args: tuple[Any,...]=(q,compact,q,q,q)
        if mac:
            where="unit_key=? AND plant_id=?"
            args=(mac.upper().replace(":", "").replace("-", "").replace(" ", ""),plant)
        candidates=self.query(f"SELECT plant_id,unit_key,MAX(stamp) AS latest FROM events WHERE {where} GROUP BY plant_id,unit_key ORDER BY latest DESC LIMIT 51",args)
        if not candidates:
            return {"found":False}
        if len(candidates)>1:
            return {"found":True,"ambiguous":True,"candidates":candidates[:50],"truncated":len(candidates)>50}
        identity=candidates[0]
        if identity["unit_key"]:
            where="plant_id=? AND unit_key=?"
            args=(identity["plant_id"],identity["unit_key"])
        count=self.query(f"SELECT COUNT(*) AS count FROM events WHERE {where}",args)[0]["count"]
        rows=self.query(f"SELECT * FROM events WHERE {where} ORDER BY stamp DESC,event_id DESC LIMIT 1000",args)
        fields={}
        for key in ["mac","serial_number","gpon_number","pcb_serial_number","model","plant_id"]:
            values=self.query(f"SELECT {key} FROM events WHERE {where} AND COALESCE({key},'')<>'' ORDER BY stamp DESC,event_id DESC LIMIT 1",args)
            fields[key]=values[0][key] if values else ""
        latest_rows=self.query(f""", operation_latest AS (
            SELECT events.*,ROW_NUMBER() OVER(PARTITION BY operation ORDER BY stamp DESC,event_id DESC) AS operation_position
            FROM events WHERE {where}) SELECT * FROM operation_latest WHERE operation_position=1""",args)
        last_by_stage={row["operation"]:self.public(row) for row in latest_rows}
        return {"found":True,"ambiguous":False,"identity":fields,"history":[self.public(r) for r in rows],"total":count,"truncated":count>1000,
            "operations":[{"key":key,"label":label,"latest":last_by_stage.get(key)} for key,label in STAGES.items()],
            "identity_linked":bool(identity["unit_key"])}
