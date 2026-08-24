"""Ensure prolinecmd clearall is the first router command after login/prompt."""
from __future__ import annotations
import asyncio
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module

class Reader:
    async def readuntil(self, separator):
        assert isinstance(separator, bytes)
        return separator
    async def read(self, n):
        return b""

class Writer:
    def __init__(self): self.writes=[]
    def write(self, value): self.writes.append(value)
    def close(self): pass

class TelnetStub:
    def __init__(self, writer): self.writer=writer
    async def open_connection(self, **kwargs): return Reader(), self.writer

def main() -> None:
    app = load("writer_clear_test", ROOT / "mac_writer" / "app.py")
    writer = Writer()
    app.telnetlib3 = TelnetStub(writer)
    router = app.RouterTarget(
        slot=1, name="DUT 1", ip="192.168.2.1", source_ip="192.168.2.101", enabled=True,
        port=23, username="", password="", login_prompt="login:", password_prompt="Password:",
        command_prompt="#", timeout=1, command_delay=0, preflight_timeout=1,
    )
    asyncio.run(app.telnet_session(
        router, "14:D6:7C:00:00:10", "SN-SECOND", "UNCOSECOND", 2,
        ["prolinecmd macaddr set {MAC_NOSEP}"], ["prolinecmd macaddr get"], [], lambda _m: None,
    ))
    commands = [x.strip() for x in writer.writes if x.strip() and x.strip() != "exit"]
    assert commands[0] == "prolinecmd clearall", commands[:3]
    assert commands[1].startswith("prolinecmd macaddr set"), commands[:3]
    print("MAC Writer clearall order test PASS")

if __name__ == "__main__":
    main()
