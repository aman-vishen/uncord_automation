from __future__ import annotations

import configparser
import csv
import io
import json
import mimetypes
import sqlite3
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).resolve().parent
cfg = configparser.ConfigParser()
cfg.read(BASE_DIR / "config.ini", encoding="utf-8")
mes = cfg["MES"]
DB_PATH = (BASE_DIR / mes.get("DATABASE_PATH", "../server/mac_server.db")).resolve()
HOST = mes.get("HOST", "0.0.0.0")
PORT = mes.getint("PORT", 8080)
REFRESH_SECONDS = mes.getint("REFRESH_SECONDS", 10)


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def parse_range(query: dict[str, list[str]]) -> tuple[str, str]:
    today = date.today(); default_start = today - timedelta(days=6)
    start = query.get("start", [default_start.isoformat()])[0]
    end = query.get("end", [today.isoformat()])[0]
    try: s, e = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError: s, e = default_start, today
    if e < s: s, e = e, s
    return s.isoformat(), e.isoformat()


def day_expr(field: str) -> str: return f"substr({field},1,10)"
def safe_rate(num: int, den: int) -> float: return round((num / den * 100.0), 2) if den else 0.0


def dashboard_data(start: str, end: str) -> dict[str, Any]:
    with connect() as con:
        writer = {r["state"]: r["c"] for r in con.execute(
            f"SELECT state,COUNT(*) c FROM mac_pool WHERE completed_at IS NOT NULL AND {day_expr('completed_at')} BETWEEN ? AND ? GROUP BY state", (start,end))}
        verifier = {r["status"]: r["c"] for r in con.execute(
            f"SELECT status,COUNT(*) c FROM verification_history WHERE completed_at IS NOT NULL AND {day_expr('completed_at')} BETWEEN ? AND ? GROUP BY status", (start,end))}
        daily = con.execute(f"""SELECT {day_expr('completed_at')} day,
            SUM(CASE WHEN status='PASS' THEN 1 ELSE 0 END) pass,
            SUM(CASE WHEN status IN ('FAIL','ERROR') THEN 1 ELSE 0 END) fail,
            COUNT(*) total FROM verification_history WHERE completed_at IS NOT NULL
            AND {day_expr('completed_at')} BETWEEN ? AND ? GROUP BY day ORDER BY day""", (start,end)).fetchall()
        stages=[]
        for label,col in [("Server MAC","server_check"),("Serial Scan","serial_scan_result"),("Wi-Fi Calibration","wifi_calibration_result"),("BOB Calibration","bob_calibration_result"),("Firmware","firmware_result"),("LED","led_result"),("Reset","reset_result"),("WPS","wps_result"),("User Mode","user_mode_result")]:
            row=con.execute(f"""SELECT SUM(CASE WHEN {col}='PASS' THEN 1 ELSE 0 END)p,
                SUM(CASE WHEN {col} IN ('FAIL','ERROR') THEN 1 ELSE 0 END)f,
                SUM(CASE WHEN COALESCE({col},'') NOT IN ('','SKIP','PENDING') THEN 1 ELSE 0 END)tested
                FROM verification_history WHERE completed_at IS NOT NULL AND {day_expr('completed_at')} BETWEEN ? AND ?""",(start,end)).fetchone()
            p,f,t=int(row['p'] or 0),int(row['f'] or 0),int(row['tested'] or 0)
            stages.append({'stage':label,'pass':p,'fail':f,'tested':t,'yield':safe_rate(p,t)})
        stations=con.execute(f"""SELECT client_id,COUNT(*) total,
            SUM(CASE WHEN status='PASS' THEN 1 ELSE 0 END) pass,
            SUM(CASE WHEN status IN ('FAIL','ERROR') THEN 1 ELSE 0 END) fail,
            MAX(completed_at) last_result FROM verification_history WHERE completed_at IS NOT NULL
            AND {day_expr('completed_at')} BETWEEN ? AND ? GROUP BY client_id ORDER BY total DESC""",(start,end)).fetchall()
        recent=con.execute("""SELECT completed_at,mac,serial_number,gpon_number,part_number,client_id,router_ip,status,
            wifi_calibration_result,bob_calibration_result,firmware_version,detail FROM verification_history
            WHERE completed_at IS NOT NULL ORDER BY id DESC LIMIT 100""").fetchall()
        pool={r['state']:r['c'] for r in con.execute("SELECT state,COUNT(*) c FROM mac_pool GROUP BY state")}
    wp=int(writer.get('PASS',0)); wf=int(writer.get('FAIL',0))+int(writer.get('ERROR',0)); vp=int(verifier.get('PASS',0)); vf=int(verifier.get('FAIL',0))+int(verifier.get('ERROR',0)); vt=vp+vf
    return {'range':{'start':start,'end':end},'kpi':{'production_volume':vt,'final_pass':vp,'final_fail':vf,'final_yield':safe_rate(vp,vt),'writer_pass':wp,'writer_fail':wf,'available_macs':int(pool.get('AVAILABLE',0)),'reserved_macs':int(pool.get('RESERVED',0))},'daily':[dict(r) for r in daily],'stages':stages,'stations':[dict(r)|{'yield':safe_rate(int(r['pass'] or 0),int(r['total'] or 0))} for r in stations],'recent':[dict(r) for r in recent],'refresh_seconds':REFRESH_SECONDS}


class Handler(BaseHTTPRequestHandler):
    def send_bytes(self, data: bytes, content_type: str, status: int=200, headers: dict[str,str]|None=None):
        self.send_response(status); self.send_header('Content-Type',content_type); self.send_header('Content-Length',str(len(data)))
        for k,v in (headers or {}).items(): self.send_header(k,v)
        self.end_headers(); self.wfile.write(data)
    def send_json(self,obj,status=200): self.send_bytes(json.dumps(obj,default=str).encode(),'application/json; charset=utf-8',status)
    def do_GET(self):
        parsed=urlparse(self.path); query=parse_qs(parsed.query); path=parsed.path
        try:
            if path=='/api/health':
                with connect() as con: con.execute('SELECT 1').fetchone()
                return self.send_json({'ok':True,'database':str(DB_PATH)})
            if path=='/api/dashboard':
                s,e=parse_range(query); return self.send_json(dashboard_data(s,e))
            if path=='/export/verification.csv':
                s,e=parse_range(query)
                with connect() as con: rows=con.execute(f"SELECT * FROM verification_history WHERE completed_at IS NOT NULL AND {day_expr('completed_at')} BETWEEN ? AND ? ORDER BY id",(s,e)).fetchall()
                out=io.StringIO(); w=csv.writer(out)
                if rows: w.writerow(rows[0].keys()); w.writerows([list(r) for r in rows])
                return self.send_bytes(out.getvalue().encode(),'text/csv; charset=utf-8',headers={'Content-Disposition':f'attachment; filename=verification_{s}_to_{e}.csv'})
            if path=='/': file=BASE_DIR/'templates'/'index.html'
            elif path.startswith('/static/'): file=BASE_DIR/path.lstrip('/')
            elif path.startswith('/assets/'): file=BASE_DIR/path.lstrip('/')
            else: return self.send_json({'error':'Not found'},404)
            if not file.exists() or BASE_DIR not in file.resolve().parents: return self.send_json({'error':'Not found'},404)
            data=file.read_bytes()
            if file.name=='index.html': data=data.replace(b'{{ refresh_seconds }}',str(REFRESH_SECONDS).encode()).replace(b'{{ start }}',(date.today()-timedelta(days=6)).isoformat().encode()).replace(b'{{ end }}',date.today().isoformat().encode())
            return self.send_bytes(data,mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
        except Exception as exc: return self.send_json({'ok':False,'error':str(exc)},500)
    def log_message(self,fmt,*args): print(f"[{self.log_date_time_string()}] {fmt%args}")

if __name__=='__main__':
    print(f"ETE MES running at http://{HOST}:{PORT}")
    print(f"Database: {DB_PATH}")
    ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
