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
        writer_rows = con.execute(
            f"""SELECT completed_at,'MAC Write' stage,mac,serial_number,gpon_number,'' part_number,
                client_id,'' router_ip,state status,'' source_file,result_detail detail
                FROM mac_pool WHERE completed_at IS NOT NULL AND {day_expr('completed_at')} BETWEEN ? AND ?""",
            (start, end),
        ).fetchall()
        verifier_rows = con.execute(
            f"""SELECT completed_at,'Verification' stage,mac,serial_number,gpon_number,part_number,
                client_id,router_ip,status,'' source_file,detail
                FROM verification_history WHERE completed_at IS NOT NULL AND {day_expr('completed_at')} BETWEEN ? AND ?""",
            (start, end),
        ).fetchall()
        stage_rows = con.execute(
            f"""SELECT completed_at,stage_name stage,mac,serial_number,gpon_number,part_number,
                station_id client_id,'' router_ip,status,source_file,detail
                FROM stage_log_history WHERE {day_expr('completed_at')} BETWEEN ? AND ?""",
            (start, end),
        ).fetchall()
        pool = {r['state']: r['c'] for r in con.execute("SELECT state,COUNT(*) c FROM mac_pool GROUP BY state")}

    all_rows = [dict(r) for r in writer_rows] + [dict(r) for r in stage_rows] + [dict(r) for r in verifier_rows]
    display = {
        'WIFI_CALIBRATION': 'Wi-Fi Calibration', 'LABEL_PRINTING': 'Label Printing',
        'BOB_CALIBRATION': 'BOB Calibration', 'WIFI_COUPLING_VOIP': 'Wi-Fi Coupling & VoIP',
    }
    for row in all_rows: row['stage'] = display.get(row['stage'], row['stage'])
    ordered = ['MAC Write','Wi-Fi Calibration','Label Printing','BOB Calibration','Wi-Fi Coupling & VoIP','Verification']
    stages=[]
    for idx,label in enumerate(ordered,1):
        rows=[r for r in all_rows if r['stage']==label]
        p=sum(r['status']=='PASS' for r in rows); f=sum(r['status'] in ('FAIL','ERROR') for r in rows); t=p+f
        stages.append({'stage':f'{idx}. {label}','pass':p,'fail':f,'tested':t,'yield':safe_rate(p,t)})
    writer_total=stages[0]['tested']; verifier_total=stages[-1]['tested']; vp=stages[-1]['pass']; vf=stages[-1]['fail']
    daily_map={}
    for r in all_rows:
        if r['stage']!='Verification': continue
        day=str(r['completed_at'])[:10]; item=daily_map.setdefault(day,{'day':day,'pass':0,'fail':0,'total':0})
        item['total']+=1; item['pass' if r['status']=='PASS' else 'fail']+=1
    station_map={}
    for r in all_rows:
        station=r['client_id'] or 'UNKNOWN'; item=station_map.setdefault(station,{'client_id':station,'total':0,'pass':0,'fail':0,'last_result':''})
        item['total']+=1; item['pass' if r['status']=='PASS' else 'fail']+=1; item['last_result']=max(item['last_result'],str(r['completed_at']))
    stations=[]
    for item in station_map.values(): item['yield']=safe_rate(item['pass'],item['total']); stations.append(item)
    recent=sorted(all_rows,key=lambda r:str(r['completed_at']),reverse=True)[:200]
    return {'range':{'start':start,'end':end},'kpi':{'production_volume':verifier_total,'line_input':writer_total,'final_pass':vp,'final_fail':vf,'final_yield':safe_rate(vp,verifier_total),'writer_pass':stages[0]['pass'],'writer_fail':stages[0]['fail'],'wip':max(0,writer_total-verifier_total),'available_macs':int(pool.get('AVAILABLE',0)),'reserved_macs':int(pool.get('RESERVED',0))},'daily':[daily_map[k] for k in sorted(daily_map)],'stages':stages,'stations':sorted(stations,key=lambda x:(-x['total'],x['client_id'])),'recent':recent,'refresh_seconds':REFRESH_SECONDS}


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
            if path in {'/export/verification.csv','/export/production.csv'}:
                s,e=parse_range(query)
                data=dashboard_data(s,e); rows=data['recent']
                out=io.StringIO(); w=csv.DictWriter(out,fieldnames=['completed_at','stage','mac','serial_number','gpon_number','part_number','client_id','router_ip','source_file','status','detail'])
                w.writeheader(); w.writerows(rows)
                return self.send_bytes(out.getvalue().encode(),'text/csv; charset=utf-8',headers={'Content-Disposition':f'attachment; filename=production_{s}_to_{e}.csv'})
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
