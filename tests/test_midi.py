"""MIDI transport logic against an in-memory fake of rtmidi. No device I/O."""

import json

import pytest

from irbox import midi
from irbox.protocol import (
    CMD_QUERY,
    CMD_READ,
    ForbiddenOperation,
    build_query,
    build_read,
    checksum,
    unwrap_sysex,
    wrap_sysex,
)

IN_PORTS = ["Some Other In", "USB-Midi 0", "USB-Midi 1"]
OUT_PORTS = ["Microsoft GS Wavetable Synth 0", "USB-Midi 2", "USB-Midi 3"]


def _frame(cmd: int, body: bytes) -> bytes:
    return b"\x00\x59" + bytes([cmd]) + len(body).to_bytes(3, "little") + body + bytes([checksum(body)])


class FakeDevice:
    """Answers READ and QUERY frames the way the protocol docs describe."""

    def __init__(self) -> None:
        self.requests: list[bytes] = []
        self.midi_in: "FakeMidiIn | None" = None
        self.fragment = False
        self.silent = False
        self.addr_skew = 0

    def receive(self, message: list[int]) -> None:
        raw = unwrap_sysex(bytes(message))
        self.requests.append(raw)
        if self.silent:
            return
        assert self.midi_in is not None
        if raw[2] == CMD_READ:
            addr = int.from_bytes(raw[7:11], "little")
            n = int.from_bytes(raw[11:14], "little")
            data = bytes((addr + i) & 0xFF for i in range(n))
            echo = raw[6:7] + (addr + self.addr_skew).to_bytes(4, "little") + raw[11:14]
            reply = _frame(CMD_READ, echo + data)
        elif raw[2] == CMD_QUERY:
            reply = _frame(CMD_QUERY, b"IR-BOX".ljust(16, b"\x00") + b"\x01\x02")
        else:
            reply = _frame(0x00, b"\x00")
        wire = wrap_sysex(reply)
        self.midi_in.deliver(bytes([0xC0, 0x01]))  # unrelated program change
        if self.fragment:
            self.midi_in.deliver(wire[:10])
            self.midi_in.deliver(wire[10:])
        else:
            self.midi_in.deliver(wire)


class FakeMidiIn:
    def __init__(self, dev: FakeDevice) -> None:
        dev.midi_in = self
        self.callback = None
        self.port: int | None = None
        self.ignore: dict[str, bool] = {}

    def get_ports(self) -> list[str]:
        return list(IN_PORTS)

    def ignore_types(self, sysex=True, timing=True, active_sense=True) -> None:
        self.ignore = {"sysex": sysex, "timing": timing, "active_sense": active_sense}

    def set_callback(self, func, data=None) -> None:
        self.callback = func

    def cancel_callback(self) -> None:
        self.callback = None

    def open_port(self, port=0, name=None) -> None:
        self.port = port

    def close_port(self) -> None:
        self.port = None

    def deliver(self, data: bytes) -> None:
        assert self.callback is not None and self.port is not None
        self.callback((list(data), 0.0), None)


class FakeMidiOut:
    def __init__(self, dev: FakeDevice) -> None:
        self.dev = dev
        self.port: int | None = None

    def get_ports(self) -> list[str]:
        return list(OUT_PORTS)

    def open_port(self, port=0, name=None) -> None:
        self.port = port

    def close_port(self) -> None:
        self.port = None

    def send_message(self, message) -> None:
        assert self.port is not None
        assert all(isinstance(b, int) for b in message)
        self.dev.receive(message)


@pytest.fixture
def device(monkeypatch):
    dev = FakeDevice()
    monkeypatch.setattr(midi.rtmidi, "MidiIn", lambda: FakeMidiIn(dev))
    monkeypatch.setattr(midi.rtmidi, "MidiOut", lambda: FakeMidiOut(dev))
    return dev


def test_resolve_port():
    names = ["Other", "USB-Midi [0]", "USB-Midi [1]"]
    assert midi.resolve_port(names, None) == 1
    assert midi.resolve_port(names, "2") == 2
    assert midi.resolve_port(names, "midi [1") == 2
    with pytest.raises(RuntimeError):
        midi.resolve_port(names, "9")
    with pytest.raises(RuntimeError):
        midi.resolve_port(["Other"], None)


def test_list_ports(device):
    assert midi.list_ports() == (IN_PORTS, OUT_PORTS)


def test_open_and_close(device):
    with midi.IRBoxMIDI() as m:
        m.open()
        fake_in = device.midi_in
        assert (m.in_name, m.out_name) == ("USB-Midi 0", "USB-Midi 2")
        assert fake_in.ignore["sysex"] is False
        assert fake_in.port == 1
    assert fake_in.port is None and fake_in.callback is None


def test_query(device):
    with midi.IRBoxMIDI() as m:
        m.open("1", "midi 3")
        reply = m.query(timeout=1.0)
    assert reply.name == "IR-BOX"
    assert reply.rest == b"\x01\x02"
    assert reply.checksum_ok
    assert device.requests == [build_query()]


@pytest.mark.parametrize("fragment", [False, True])
def test_read_chunked(device, fragment):
    device.fragment = fragment
    base = 0x80000000
    with midi.IRBoxMIDI(chunk=128, inter_chunk_delay=0) as m:
        m.open()
        data = m.read(5, base, 300)
    assert data == bytes((base + i) & 0xFF for i in range(300))
    assert device.requests == [
        build_read(5, base, 128),
        build_read(5, base + 128, 128),
        build_read(5, base + 256, 44),
    ]


def test_read_echo_mismatch(device):
    device.addr_skew = 1
    with midi.IRBoxMIDI() as m:
        m.open()
        with pytest.raises(RuntimeError, match="echo mismatch"):
            m.read(4, 0x20000000, 1)


def test_timeout(device):
    device.silent = True
    with midi.IRBoxMIDI() as m:
        m.open()
        with pytest.raises(TimeoutError):
            m.read(4, 0x20000000, 1, timeout=0.05)


def test_erase_refused(device):
    with midi.IRBoxMIDI() as m:
        m.open()
        with pytest.raises(ForbiddenOperation):
            m.transact(bytes.fromhex("005921010000" "00" "ff"), None)
    assert device.requests == []


def test_trace(device, tmp_path):
    path = tmp_path / "t.jsonl"
    with midi.IRBoxMIDI(path) as m:
        m.open()
        m.query(timeout=1.0)
    recs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    tx = [r for r in recs if r["dir"] == "TX"]
    rx = [r for r in recs if r["dir"] == "RX" and "sysex" in r]
    assert tx[0]["sysex"] == wrap_sysex(build_query()).hex()
    assert tx[0]["decoded"] == build_query().hex()
    assert rx[0]["decoded"].startswith("005911")
    assert any(r.get("midi") == "c001" for r in recs)
    assert all("t" in r for r in recs)
