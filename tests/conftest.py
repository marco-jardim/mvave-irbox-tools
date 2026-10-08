"""Shared fakes: an in-memory IR Box behind a fake rtmidi. No device I/O."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from irbox import midi
from irbox.device import IRBox
from irbox.protocol import CMD_QUERY, CMD_READ, CMD_WRITE, checksum, unwrap_sysex, wrap_sysex

IN_PORTS = ["Other In", "USB-Midi 0"]
OUT_PORTS = ["Microsoft GS Wavetable Synth 0", "USB-Midi 1"]

SELECT_BASE = 0xE0000000
SAVE_BASE = 0xF0000000
REFRESH = 0xA0000000
NAMES = 0x80000000
CURRENT = 0x20000000


def frame(cmd: int, body: bytes) -> bytes:
    return b"\x00\x59" + bytes([cmd]) + len(body).to_bytes(3, "little") + body + bytes([checksum(body)])


def make_block(i: int) -> bytes:
    """Factory-like preset for slot index ``i``."""
    b = bytearray(8192)
    name = f"Factory {i + 1}".encode()
    b[0 : len(name)] = name
    b[17] = 0xFF if i in (6, 24, 29) else 1  # factory quirk: cab_on 0xFF
    b[18] = 0
    b[19:24] = b"patch"
    b[24:28] = b"CAB\x00"
    b[28:42] = b"\xff" * 14
    b[42] = 2
    b[43] = 70
    for k in range(2048):
        v = ((i + 1) * 1000 + k * 37) % 200001 - 100000
        b[44 + 3 * k : 47 + 3 * k] = (v & 0xFFFFFF).to_bytes(3, "little")
    eq = bytearray(40)
    eq[9:14] = bytes([5] * 5)
    eq[21] = 0xFF
    for k, hz in enumerate((100, 400, 1000, 3000, 8000)):
        eq[22 + 2 * k : 24 + 2 * k] = hz.to_bytes(2, "little")
    eq[32:34] = (200).to_bytes(2, "little")
    eq[34:36] = (6000).to_bytes(2, "little")
    eq[36:38] = (12000).to_bytes(2, "little")
    eq[38:40] = (80).to_bytes(2, "little")
    b[7168:7208] = eq
    b[6188:7168] = bytes([0x5A]) * 980
    return bytes(b)


class FakeIRBoxHW:
    """Emulates the verified behavior of firmware IR-BOX_010."""

    def __init__(self) -> None:
        self.flash = [bytearray(make_block(i)) for i in range(32)]
        self.active = 22
        self.working = bytearray(self.flash[self.active])
        self.requests: list[bytes] = []
        self.ops: list[tuple] = []
        self.nack: set[tuple[int, int]] = set()
        self.ignore_select = False
        self.midi_in: FakeMidiIn | None = None

    def name_table(self) -> bytes:
        return b"".join(bytes(blk[:17]) for blk in self.flash)

    # -- request log helpers ---------------------------------------------
    def frames(self, cmd: int) -> list[tuple[int, int, bytes]]:
        """(type, addr, payload-or-length) of every request with ``cmd``."""
        out = []
        for raw in self.requests:
            if raw[2] != cmd:
                continue
            body = raw[6:-1]
            type_ = body[0]
            addr = int.from_bytes(body[1:5], "little")
            out.append((type_, addr, body[8:] if cmd == CMD_WRITE else body[5:8]))
        return out

    def high_level(self, with_reads: bool = False) -> list[tuple]:
        """Ops with the 173-byte chunks of one write merged (reads dropped by default)."""
        out: list[tuple] = []
        for op in self.ops:
            if op[0] == "read" and not with_reads:
                continue
            if (
                op[0] == "write"
                and out
                and out[-1][0] == "write"
                and out[-1][2] % 173 == 0
                and out[-1][1] + out[-1][2] == op[1]
            ):
                out[-1] = ("write", out[-1][1], out[-1][2] + op[2])
                continue
            out.append(op)
        return out

    # -- protocol --------------------------------------------------------
    def receive(self, message: list[int]) -> None:
        raw = unwrap_sysex(bytes(message))
        self.requests.append(raw)
        cmd = raw[2]
        body = raw[6:-1]
        if cmd == CMD_READ:
            reply = self._read(body)
        elif cmd == CMD_WRITE:
            reply = self._write(body)
        elif cmd == CMD_QUERY:
            reply = frame(CMD_QUERY, b"IR-BOX_010" + bytes(11))
        else:
            return
        assert self.midi_in is not None
        self.midi_in.deliver(wrap_sysex(reply))

    def _read(self, body: bytes) -> bytes:
        type_ = body[0]
        addr = int.from_bytes(body[1:5], "little")
        n = int.from_bytes(body[5:8], "little")
        self.ops.append(("read", type_, addr, n))
        if type_ == 4 and addr == CURRENT and n == 1:
            data = bytes([self.active])
        elif type_ == 5 and addr >= NAMES and addr < NAMES + 544:
            data = (self.name_table() + bytes(n))[:n]  # offset is ignored
        elif type_ == 5 and addr + n <= 8192:
            data = bytes(self.working[addr : addr + n])
        else:
            data = (bytes([checksum(body)]) + bytes(n))[:n]  # stale junk
        return frame(CMD_READ, body[:8] + data)

    def _write(self, body: bytes) -> bytes:
        type_ = body[0]
        addr = int.from_bytes(body[1:5], "little")
        n = int.from_bytes(body[5:8], "little")
        data = body[8 : 8 + n]
        status = 0
        if (type_, addr) in self.nack:
            status = 1
        elif type_ == 4 and 0 <= addr - SELECT_BASE < 32 and data == bytes([addr - SELECT_BASE]):
            self.ops.append(("select", addr - SELECT_BASE))
            if not self.ignore_select:
                self.active = addr - SELECT_BASE
                self.working = bytearray(self.flash[self.active])
        elif type_ == 5 and addr == REFRESH and n == 1:
            self.ops.append(("refresh", data[0]))
        elif type_ == 5 and 0 <= addr - SAVE_BASE < 32 and data == b"\x00":
            self.ops.append(("save", addr - SAVE_BASE))
            self.flash[addr - SAVE_BASE] = bytearray(self.working)
        elif type_ == 5 and addr + n <= 8192:
            self.ops.append(("write", addr, n))
            self.working[addr : addr + n] = data
        else:
            status = 1
        return frame(0x00, bytes([status]))


class FakeMidiIn:
    def __init__(self, dev: FakeIRBoxHW) -> None:
        dev.midi_in = self
        self.callback = None
        self.port: int | None = None

    def get_ports(self) -> list[str]:
        return list(IN_PORTS)

    def ignore_types(self, sysex=True, timing=True, active_sense=True) -> None:
        pass

    def set_callback(self, func, data=None) -> None:
        self.callback = func

    def cancel_callback(self) -> None:
        self.callback = None

    def open_port(self, port=0, name=None) -> None:
        self.port = port

    def close_port(self) -> None:
        self.port = None

    def deliver(self, data: bytes) -> None:
        assert self.callback is not None
        self.callback((list(data), 0.0), None)


class FakeMidiOut:
    def __init__(self, dev: FakeIRBoxHW) -> None:
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
        self.dev.receive(list(message))


@pytest.fixture
def hw(monkeypatch) -> FakeIRBoxHW:
    dev = FakeIRBoxHW()
    monkeypatch.setattr(midi.rtmidi, "MidiIn", lambda: FakeMidiIn(dev))
    monkeypatch.setattr(midi.rtmidi, "MidiOut", lambda: FakeMidiOut(dev))
    return dev


def _no_sleep(_seconds: float) -> None:
    return None


def make_box(progress=None) -> tuple[midi.IRBoxMIDI, IRBox]:
    m = midi.IRBoxMIDI(inter_chunk_delay=0)
    m.open()
    return m, IRBox(m, sleep=_no_sleep, progress=progress)


@pytest.fixture
def box(hw) -> Iterator[IRBox]:
    m, b = make_box()
    try:
        yield b
    finally:
        m.close()


@pytest.fixture
def box_factory(hw):
    """Callable creating additional IRBox instances on the fake device."""
    opened: list[midi.IRBoxMIDI] = []

    def factory(progress=None) -> IRBox:
        m, b = make_box(progress)
        opened.append(m)
        return b

    yield factory
    for m in opened:
        m.close()
