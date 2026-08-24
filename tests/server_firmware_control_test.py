import hashlib
import importlib.util
import tempfile
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1] / 'server' / 'server.py'
spec = importlib.util.spec_from_file_location('server_mod_fw_test', SERVER)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

with tempfile.TemporaryDirectory() as td:
    db = mod.MacDatabase(Path(td) / 'fw.db')
    fw = Path(td) / 'firmware.bin'
    fw.write_bytes(b'firmware-test-' * 1000)
    cfg = db.select_firmware_file(fw)
    assert cfg['available'] is True
    assert cfg['enabled'] is False
    assert cfg['sha256'] == hashlib.sha256(fw.read_bytes()).hexdigest()
    db.set_firmware_enabled(True)
    assert db.firmware_config()['enabled'] is True
    db.set_firmware_enabled(False)
    assert db.firmware_config()['enabled'] is False
print('Server firmware control test PASS')
