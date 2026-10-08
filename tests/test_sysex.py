"""USB-MIDI SysEx packing. Golden vectors from pferreir/cuvave-midi unit tests."""

import random

import pytest

from irbox.protocol import (
    CMD_QUERY,
    FrameParser,
    OtherFrame,
    QueryReply,
    ReadResponse,
    SysExAssembler,
    WriteAck,
    build_query,
    build_read,
    build_write,
    checksum,
    decode7,
    encode7,
    frame_length,
    trim_frame,
    unwrap_sysex,
    wrap_sysex,
)

WRITE_RAW = bytes.fromhex("00 59 22 09 00 00 05 09 00 00 80 01 00 00 01 6F")
WRITE_PACKED = bytes.fromhex("00 32 09 49 00 00 40 02 09 00 00 00 18 00 00 00 01 5E 01")
QUERY_RAW = bytes.fromhex("00 59 11 00 00 00 FF")
QUERY_PACKED = bytes.fromhex("00 32 45 00 00 00 40 7F 00")
INIT_RAW = bytes.fromhex("00 59 00 00 00 00 FF")
INIT_PACKED = bytes.fromhex("00 32 01 00 00 00 40 7F 00")


def _frame(cmd: int, body: bytes) -> bytes:
    return b"\x00\x59" + bytes([cmd]) + len(body).to_bytes(3, "little") + body + bytes([checksum(body)])


# -- golden vectors -----------------------------------------------------------


def test_encode7_write_vector():
    assert WRITE_RAW == build_write(5, 0x80000009, b"\x01")
    assert encode7(WRITE_RAW) == WRITE_PACKED
    assert decode7(WRITE_PACKED) == WRITE_RAW


def test_query_vector():
    assert build_query() == QUERY_RAW
    assert encode7(QUERY_RAW) == QUERY_PACKED
    assert encode7(QUERY_RAW, flush_zero=False) == QUERY_PACKED[:-1]
    assert wrap_sysex(QUERY_RAW) == bytes.fromhex("F0") + QUERY_PACKED + bytes.fromhex("F7")


def test_query_captured_without_flush_unwraps():
    assert unwrap_sysex(bytes.fromhex("F0 00 32 45 00 00 00 40 7F F7")) == QUERY_RAW
    assert unwrap_sysex(wrap_sysex(QUERY_RAW)) == QUERY_RAW


def test_init_vector():
    assert encode7(INIT_RAW) == INIT_PACKED
    assert unwrap_sysex(wrap_sysex(INIT_RAW)) == INIT_RAW


def test_wire_write_vector_unwraps():
    raw = unwrap_sysex(bytes.fromhex("f000320949000040021b00000018000000013a01f7"))
    assert raw == build_write(5, 0x8000001B, b"\x01")
    assert raw[2] == 0x22
    assert raw[6] == 5
    assert int.from_bytes(raw[7:11], "little") == 0x8000001B
    assert int.from_bytes(raw[11:14], "little") == 1
    assert raw[14:-1] == b"\x01"
    assert raw[-1] == checksum(raw[6:-1])


# -- properties ---------------------------------------------------------------


def test_roundtrip_random_lengths():
    rng = random.Random(0x4353)
    for n in range(301):
        raw = bytes(rng.randrange(256) for _ in range(n))
        packed = encode7(raw)
        assert len(packed) == n + n // 7 + 1
        assert all(b <= 0x7F for b in packed)
        assert decode7(packed) == raw
        short = encode7(raw, flush_zero=False)
        assert len(short) == (8 * n + 6) // 7
        assert decode7(short) == raw
        assert packed.startswith(short)


@pytest.mark.parametrize("fill", [0x00, 0xFF, 0x80, 0x01])
def test_roundtrip_constant_patterns(fill):
    for n in range(0, 50):
        raw = bytes([fill]) * n
        assert decode7(encode7(raw)) == raw
        assert decode7(encode7(raw, flush_zero=False)) == raw


def test_decode7_rejects_8bit_input():
    with pytest.raises(ValueError):
        decode7(b"\x00\x80")


# -- trailing zero pitfall ----------------------------------------------------


ZERO_CS_FRAMES = [
    _frame(CMD_QUERY, b"\xff"),  # 8 bytes
    _frame(CMD_QUERY, b"\xff" + b"\x00" * 6),  # 14 bytes: exact 7-bit boundary
    build_read(0xFF, 0, 0),  # 15 bytes
    _frame(0x23, bytes.fromhex("04 00000020 010000") + b"\xda"),  # read reply
]


@pytest.mark.parametrize("raw", ZERO_CS_FRAMES)
@pytest.mark.parametrize("flush", [True, False])
def test_zero_checksum_survives(raw, flush):
    assert raw[-1] == 0x00
    assert raw[-1] == checksum(raw[6:-1])
    wire = wrap_sysex(raw, flush_zero=flush)
    assert wire[-2] == 0x00  # the last packed byte is zero
    assert unwrap_sysex(wire) == raw


def test_zero_checksum_read_reply_parses():
    raw = ZERO_CS_FRAMES[3]
    (f,) = FrameParser().feed(unwrap_sysex(wrap_sysex(raw)))
    assert isinstance(f, ReadResponse)
    assert (f.type, f.addr, f.length, f.data) == (4, 0x20000000, 1, b"\xda")


# -- trimming / validation ----------------------------------------------------


def test_extra_zero_padding_is_trimmed():
    wire = b"\xf0" + encode7(QUERY_RAW) + b"\x00\x00\x00" + b"\xf7"
    assert decode7(wire[1:-1]) != QUERY_RAW  # decodes to extra zero bytes
    assert unwrap_sysex(wire) == QUERY_RAW


def test_truncated_frame_rejected():
    wire = wrap_sysex(WRITE_RAW)
    with pytest.raises(ValueError):
        unwrap_sysex(wire[:-6] + b"\xf7")


def test_unwrap_requires_f0_f7():
    with pytest.raises(ValueError):
        unwrap_sysex(QUERY_PACKED)
    with pytest.raises(ValueError):
        unwrap_sysex(b"\xf0" + QUERY_PACKED)


def test_trim_frame_passthrough():
    assert frame_length(QUERY_RAW) == 7
    assert frame_length(b"\x01\x02") is None
    assert trim_frame(b"\x7e\x01\x02") == b"\x7e\x01\x02"
    two = QUERY_RAW + INIT_RAW  # non-zero trailing data is kept for the parser
    assert trim_frame(two) == two


# -- parsing of decoded payloads ----------------------------------------------


def test_query_reply_parsed():
    body = b"IR-BOX".ljust(16, b"\x00") + bytes([0x01, 0x02, 0xAB])
    raw = _frame(CMD_QUERY, body)
    p = FrameParser()
    (f,) = p.feed(unwrap_sysex(wrap_sysex(raw)))
    assert isinstance(f, QueryReply)
    assert f.name == "IR-BOX"
    assert f.rest == bytes([0x01, 0x02, 0xAB])
    assert f.checksum_ok
    assert not p.errors


def test_query_reply_bad_checksum_flagged():
    body = b"CUBE BABY".ljust(16, b"\x00")
    raw = bytearray(_frame(CMD_QUERY, body))
    raw[-1] ^= 0x01
    p = FrameParser()
    (f,) = p.feed(bytes(raw))
    assert isinstance(f, QueryReply)
    assert f.name == "CUBE BABY" and f.rest == b""
    assert not f.checksum_ok
    assert p.errors


def test_short_query_frame_is_other():
    (f,) = FrameParser().feed(_frame(CMD_QUERY, b"\xaa"))
    assert isinstance(f, OtherFrame) and f.cmd == CMD_QUERY


def test_init_and_ack_frames():
    p = FrameParser()
    init, ack, nak = p.feed(
        unwrap_sysex(wrap_sysex(INIT_RAW))
        + _frame(0x00, b"\x00")
        + _frame(0x00, b"\x05")
    )
    assert isinstance(init, WriteAck) and init.status is None and init.checksum_ok
    assert init.body == b""
    assert isinstance(ack, WriteAck) and ack.status == 0 and ack.ok
    assert isinstance(nak, WriteAck) and nak.status == 5 and not nak.ok


# -- SysEx reassembly ---------------------------------------------------------


def test_assembler_fragmented_with_realtime_and_noise():
    wire = wrap_sysex(WRITE_RAW)
    stream = bytes([0x90, 0x40, 0x7F]) + wire[:5] + b"\xf8" + wire[5:] + bytes([0xB0, 0x07, 0x64])
    a = SysExAssembler()
    out = []
    for i in range(0, len(stream), 3):
        out += a.feed(stream[i : i + 3])
    assert out == [wire]
    assert not a.in_progress
    assert not a.errors
    assert a.ignored_bytes == 6


def test_assembler_aborts_on_status_byte():
    a = SysExAssembler()
    wire = wrap_sysex(QUERY_RAW)
    assert a.feed(b"\xf0\x00\x32\x90\x40\x7f" + wire) == [wire]
    assert a.errors


def test_assembler_whole_messages():
    a = SysExAssembler()
    w1, w2 = wrap_sysex(QUERY_RAW), wrap_sysex(INIT_RAW)
    assert a.feed(w1) == [w1]
    assert a.feed(w2) == [w2]
