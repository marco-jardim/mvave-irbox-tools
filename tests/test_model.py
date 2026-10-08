import json

import pytest

from irbox.model import (
    EQ_LEN,
    PRESET_LEN,
    build_header,
    build_ir_block,
    check_preset_block,
    decode_eq_gain,
    describe_eq_band,
    encode_eq_freq,
    encode_eq_gain,
    encode_eq_q,
    encode_name,
    parse_eq,
    parse_name_table,
    parse_preset,
    preset_to_dict,
    update_eq_block,
)


def test_name_table():
    buf = bytearray(544)
    buf[0:5] = b"Alpha"
    buf[17 * 2 : 17 * 2 + 4] = b"B\x01\xffc"
    buf[17 * 31 : 17 * 31 + 3] = b"Z\x00Q"
    names = parse_name_table(bytes(buf))
    assert len(names) == 32
    assert names[0] == "Alpha"
    assert names[1] is None
    assert names[2] == "Bc"
    assert names[31] == "Z"
    with pytest.raises(ValueError):
        parse_name_table(b"\x00" * 10)


def _synthetic() -> bytes:
    b = bytearray(PRESET_LEN)
    b[0:4] = b"Test"
    b[17] = 1
    b[18] = 0
    b[19:24] = b"patch"
    b[24:28] = b"CAB\x00"
    b[28:31] = b"cab"
    b[42] = 2
    b[43] = 100
    b[44:47] = (1).to_bytes(3, "little")
    b[47:50] = (-1).to_bytes(3, "little", signed=True)
    b[50:53] = (-8388608).to_bytes(3, "little", signed=True)
    b[44 + 2047 * 3 : 44 + 2048 * 3] = (8388607).to_bytes(3, "little")
    b[6188] = 0xAB
    b[7168:7177] = bytes([1, 0, 1, 0, 1, 0, 1, 0, 1])
    b[7177:7182] = bytes([1, 10, 20, 80, 160])
    b[7182:7187] = bytes([0xFF, 5, 0x88, 120, 0])
    b[7187] = 0xFE
    b[7188] = 3
    b[7189] = 9
    for i, hz in enumerate([100, 500, 1000, 5000, 20000]):
        b[7190 + 2 * i : 7192 + 2 * i] = hz.to_bytes(2, "little")
    b[7200:7202] = (200).to_bytes(2, "little")
    b[7202:7204] = (6000).to_bytes(2, "little")
    b[7204:7206] = (12000).to_bytes(2, "little")
    b[7206:7208] = (80).to_bytes(2, "little")
    b[8191] = 0xCD
    return bytes(b)


def test_parse_preset():
    p = parse_preset(_synthetic())
    assert p.name == "Test" and p.cad_name == "cab"
    assert (p.cab_on, p.eq_on, p.cad_type, p.level) == (1, 0, 2, 100)
    assert p.magic_ok
    assert len(p.ir_samples) == 2048
    assert p.ir_samples[:3] == [1, -1, -8388608]
    assert p.ir_samples[-1] == 8388607
    assert p.eq.enable == [1, 0, 1, 0, 1, 0, 1, 0, 1]
    assert p.eq.peak_q == [1, 10, 20, 80, 160]
    assert p.eq.peak_db == [-1, 5, -120, 120, 0]
    assert (p.eq.ls_db, p.eq.hs_db, p.eq_reserved) == (-2, 3, 9)
    assert p.eq.peak_hz == [100, 500, 1000, 5000, 20000]
    assert (p.eq.ls_hz, p.eq.hs_hz, p.eq.lp_hz, p.eq.hp_hz) == (200, 6000, 12000, 80)
    assert len(p.unknown_ir_to_eq) == 980 and p.unknown_ir_to_eq[0] == 0xAB
    assert len(p.unknown_tail) == 984 and p.unknown_tail[-1] == 0xCD


def test_preset_json():
    d = preset_to_dict(parse_preset(_synthetic()))
    s = json.dumps(d)
    back = json.loads(s)
    assert back["magic"] == b"patch".hex()
    assert back["unknown_tail"].endswith("cd")


def test_preset_wrong_size():
    with pytest.raises(ValueError):
        parse_preset(b"\x00" * 100)


def test_cab_on_0xff_factory_quirk_parses():
    b = bytearray(_synthetic())
    b[17] = 0xFF
    p = parse_preset(bytes(b))
    assert p.cab_on == 0xFF and p.magic_ok
    check_preset_block(bytes(b))


def test_eq_value_encoding():
    assert encode_eq_gain(12.0) == 120
    assert encode_eq_gain(-12.0) == 0x88
    assert encode_eq_gain(0.0) == 0
    assert encode_eq_gain(-0.1) == 0xFF
    assert decode_eq_gain(0x88) == -12.0
    for bad in (12.1, -12.5, float("nan")):
        with pytest.raises(ValueError):
            encode_eq_gain(bad)
    assert encode_eq_q(0.5) == 5
    assert encode_eq_q(0.1) == 1
    assert encode_eq_q(16.0) == 160
    for bad in (0.05, 16.1, 0.0):
        with pytest.raises(ValueError):
            encode_eq_q(bad)
    assert encode_eq_freq(1000) == b"\xe8\x03"
    assert encode_eq_freq(20) == b"\x14\x00"
    assert encode_eq_freq(20000) == (20000).to_bytes(2, "little")
    for bad in (19, 20001):
        with pytest.raises(ValueError):
            encode_eq_freq(bad)


def test_eq_band_offsets_match_parser():
    eq = bytes(EQ_LEN)
    for k in range(5):
        eq = update_eq_block(eq, f"peak{k + 1}", freq=100 * (k + 1), gain_db=k - 2.0, q=k + 1.0, enable=True)
    eq = update_eq_block(eq, "hpf", freq=80, enable=True)
    eq = update_eq_block(eq, "lowshelf", freq=200, gain_db=3.5)
    eq = update_eq_block(eq, "highshelf", freq=6000, gain_db=-4.5, enable=True)
    eq = update_eq_block(eq, "lpf", freq=12000)
    s = parse_eq(eq)
    assert s.enable == [1, 0, 1, 1, 1, 1, 1, 1, 0]
    assert s.peak_hz == [100, 200, 300, 400, 500]
    assert s.peak_db == [-20, -10, 0, 10, 20]
    assert s.peak_q == [10, 20, 30, 40, 50]
    assert (s.ls_db, s.hs_db) == (35, -45)
    assert (s.hp_hz, s.ls_hz, s.hs_hz, s.lp_hz) == (80, 200, 6000, 12000)
    d = describe_eq_band(eq, "peak3")
    assert (d.enabled, d.freq_hz, d.gain_db, d.q) == (True, 300, 0.0, 3.0)
    assert describe_eq_band(eq, "lpf").gain_db is None
    with pytest.raises(ValueError):
        update_eq_block(eq, "hpf", gain_db=1.0)
    with pytest.raises(ValueError):
        update_eq_block(eq, "lowshelf", q=1.0)
    with pytest.raises(ValueError):
        update_eq_block(eq, "peak6", freq=100)


def test_names_and_header():
    assert encode_name("Clean 1") == b"Clean 1".ljust(17, b"\x00")
    assert encode_name("x" * 16) == b"x" * 16 + b"\x00"
    for bad in ("", "x" * 17, "caf\u00e9", "tab\there"):
        with pytest.raises(ValueError):
            encode_name(bad)
    h = build_header(encode_name("A"), 1, 0)
    assert h == b"A" + bytes(16) + b"\x01\x00patch"
    assert len(h) == 24


def test_ir_block_layout():
    samples = [0x123456, -1] + [0] * 2046
    blk = build_ir_block(samples, level=70)
    assert len(blk) == 6164
    assert blk[:4] == b"CAB\x00" and blk[4:18] == bytes(14)
    assert (blk[18], blk[19]) == (2, 70)
    assert blk[20:26] == bytes([0x56, 0x34, 0x12, 0xFF, 0xFF, 0xFF])
    assert build_ir_block(samples, 1, b"My cab")[4:18] == b"My cab".ljust(14, b"\x00")
    with pytest.raises(ValueError):
        build_ir_block(samples[:100], 70)
    with pytest.raises(ValueError):
        build_ir_block(samples, 70, b"x" * 15)
