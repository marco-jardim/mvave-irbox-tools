import pytest

from irbox.protocol import (
    FrameParser,
    OtherFrame,
    ReadResponse,
    WriteAck,
    build_read,
    build_write,
    checksum,
    iter_read_chunks,
    iter_write_chunks,
)

READ_RESP_SLOT = bytes.fromhex("005923090000" "040000002001000002" "d8")


def test_checksum():
    assert checksum(b"") == 0xFF
    assert checksum(bytes([5, 0x80, 0x20, 0x02])) == 0x58
    assert checksum(bytes([0xFF, 0x01])) == 0xFF


def test_build_read_name_table():
    assert build_read(5, 0x80000000, 544) == bytes.fromhex(
        "00592308000005000000802002" "00" "58"
    )


def test_build_read_current_slot():
    f = build_read(4, 0x20000000, 1)
    assert f == bytes.fromhex("005923080000" "04000000200100" "00" "da")
    assert len(f) == 15


def test_build_write_select_slot():
    assert build_write(4, 0xE0000003, b"\x03") == bytes.fromhex(
        "005922090000" "04" "030000e0" "010000" "03" "14"
    )


def test_build_write_ir_on():
    assert build_write(5, 0x11, b"\x01") == bytes.fromhex(
        "005922090000" "05" "11000000" "010000" "01" "e7"
    )


def test_range_checks():
    with pytest.raises(ValueError):
        build_read(256, 0, 1)
    with pytest.raises(ValueError):
        build_write(5, 1 << 32, b"")


def test_write_chunks():
    data = bytes(range(256)) * 24 + b"\x01" * 20  # 6164 bytes
    chunks = list(iter_write_chunks(0x18, data))
    assert len(data) == 6164
    assert len(chunks) == 36
    assert chunks[0][0] == 0x18
    assert chunks[1][0] == 0x18 + 173
    assert all(len(c) == 173 for _, c in chunks[:-1])
    assert len(chunks[-1][1]) == 6164 - 35 * 173
    assert b"".join(c for _, c in chunks) == data


def test_read_chunks():
    assert list(iter_read_chunks(0, 8192)) == [(i * 1000, 1000) for i in range(8)] + [
        (8000, 192)
    ]
    assert list(iter_read_chunks(0x100, 544)) == [(0x100, 544)]
    assert list(iter_read_chunks(0, 0)) == []


def test_parser_single_read_response():
    (f,) = FrameParser().feed(READ_RESP_SLOT)
    assert isinstance(f, ReadResponse)
    assert (f.type, f.addr, f.length, f.data) == (4, 0x20000000, 1, b"\x02")


def test_parser_fragmented_and_garbage_prefix():
    stream = b"\x13\x00\x37\x00" + READ_RESP_SLOT + READ_RESP_SLOT
    p = FrameParser()
    frames = []
    for i in range(len(stream)):
        frames += p.feed(stream[i : i + 1])
    assert len(frames) == 2
    assert all(isinstance(f, ReadResponse) for f in frames)
    assert p.discarded_bytes == 4


def test_parser_bad_checksum_resyncs():
    bad = bytearray(READ_RESP_SLOT)
    bad[-1] ^= 0xFF
    p = FrameParser()
    frames = p.feed(bytes(bad) + READ_RESP_SLOT)
    assert len(frames) == 1
    assert p.errors


def test_parser_echo_length_mismatch_rejected():
    # length echo says 5 but only 2 data bytes follow
    body = bytes.fromhex("04000000200500" "0001" "02")
    frame = b"\x00\x59\x23" + len(body).to_bytes(3, "little") + body + bytes([checksum(body)])
    p = FrameParser()
    assert p.feed(frame) == []
    assert p.errors


def test_parser_ack_and_other():
    p = FrameParser()
    out = p.feed(bytes.fromhex("005900010000" "00" "ff") + bytes.fromhex("005911010000" "aa" "55"))
    assert isinstance(out[0], WriteAck) and out[0].ok
    assert isinstance(out[1], OtherFrame) and out[1].cmd == 0x11


def test_parser_huge_length_is_garbage():
    p = FrameParser()
    out = p.feed(bytes.fromhex("0059230000ff") + READ_RESP_SLOT)
    assert len(out) == 1
