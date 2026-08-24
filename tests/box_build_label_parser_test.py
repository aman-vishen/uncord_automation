from __future__ import annotations
import importlib.util
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('box_app',ROOT/'box_build'/'app.py')
mod=importlib.util.module_from_spec(spec); assert spec and spec.loader; spec.loader.exec_module(mod)

samples=[
    ('14:D6:7C:00:00:08,SN00000002,UNCO00000002','14:D6:7C:00:00:08','SN00000002','UNCO00000002'),
    ('MAC=14:D6:7C:00:00:08;SERIAL=SN00000002;GPON=UNCO00000002','14:D6:7C:00:00:08','SN00000002','UNCO00000002'),
    ('{"mac":"14D67C000008","serial":"SN00000002","gpon":"UNCO00000002"}','14:D6:7C:00:00:08','SN00000002','UNCO00000002'),
]
for raw,mac,serial,gpon in samples:
    p=mod.parse_label(raw)
    assert p['mac']==mac,(raw,p)
    assert p['serial_number']==serial,(raw,p)
    assert p['gpon_number']==gpon,(raw,p)
print('Box Build label parser test PASS')
