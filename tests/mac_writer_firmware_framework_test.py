import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / 'mac_writer' / 'app.py'
CMD = ROOT / 'mac_writer' / 'commands.txt'
FW = ROOT / 'mac_writer' / 'firmware_updater.py'

spec = importlib.util.spec_from_file_location('writer_fw_test', APP)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

write, verify, finalize = mod.parse_commands_file(CMD)
assert write and verify and finalize
source = APP.read_text(encoding='utf-8')
fwsource = FW.read_text(encoding='utf-8')
assert source.index('alloc = client.allocate_specific') < source.index('run_direct_firmware_update(router, local_fw, cfg, emit)')
assert source.index('self.events.put(("verify_result"') < source.index('run_direct_firmware_update(router, local_fw, cfg, emit)')
assert 'effective_finalize_cmds = [] if firmware_enabled else finalize_cmds' in source
assert 'run_direct_firmware_update' in source
assert 'upgrade_router_firmware' in source
assert 'resolve_mcupgrade_executable' not in source
assert 'mcupgrade' not in source.lower()
assert 'sock.bind((source_ip, 0))' in fwsource
assert 'sftp.put' in fwsource
assert 'sysupgrade -T' in fwsource
assert 'upgrade_command = "sysupgrade "' in fwsource
assert 'uptime' in fwsource
print('MAC Writer direct Python firmware framework test PASS')
