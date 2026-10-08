import struct

import pytest

from irbox.wav import read_wav, write_wav_24bit_mono


def test_roundtrip_24bit(tmp_path):
    samples = [0, 1, -1, 8388607, -8388608, 123456, -654321]
    p = tmp_path / "a.wav"
    write_wav_24bit_mono(p, samples)
    w = read_wav(p)
    assert (w.rate, w.channels, w.bits, w.is_float) == (44100, 1, 24, False)
    assert [round(x * 8388608) for x in w.samples[0]] == samples


def test_clamps(tmp_path):
    p = tmp_path / "c.wav"
    write_wav_24bit_mono(p, [10**9, -(10**9)])
    w = read_wav(p)
    assert [round(x * 8388608) for x in w.samples[0]] == [8388607, -8388608]


def _riff(tag, ch, rate, bits, data):
    fmt = struct.pack("<HHIIHH", tag, ch, rate, rate * ch * bits // 8, ch * bits // 8, bits)
    body = b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", len(body)) + body


def test_16bit_stereo(tmp_path):
    p = tmp_path / "s.wav"
    p.write_bytes(_riff(1, 2, 48000, 16, struct.pack("<4h", 16384, -16384, 0, 32767)))
    w = read_wav(p)
    assert (w.rate, w.channels) == (48000, 2)
    assert w.samples[0] == [0.5, 0.0]
    assert w.samples[1][0] == -0.5


def test_32bit_int_and_float(tmp_path):
    p = tmp_path / "i.wav"
    p.write_bytes(_riff(1, 1, 44100, 32, struct.pack("<2i", 1 << 30, -(1 << 31))))
    assert read_wav(p).samples[0] == [0.5, -1.0]
    q = tmp_path / "f.wav"
    q.write_bytes(_riff(3, 1, 44100, 32, struct.pack("<2f", 0.25, -0.5)))
    w = read_wav(q)
    assert w.is_float and w.samples[0] == [0.25, -0.5]


def test_not_wav(tmp_path):
    p = tmp_path / "x.wav"
    p.write_bytes(b"nope")
    with pytest.raises(ValueError):
        read_wav(p)
