"""WAV -> IR Box conversion (pure, numpy)."""

import math
import struct

import numpy as np
import pytest

from irbox.ir_convert import convert, convert_array, export_wav, resample, samples_from_preset
from irbox.model import IR_OFFSET, PRESET_LEN, decode_s24le, encode_s24le

FS = 1 << 23  # float 1.0 in the 24-bit integer domain
MAX = (1 << 23) - 1


def _riff(tag: int, ch: int, rate: int, bits: int, data: bytes) -> bytes:
    fmt = struct.pack("<HHIIHH", tag, ch, rate, rate * ch * bits // 8, ch * bits // 8, bits)
    body = b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", len(body)) + body


def write_wav(path, frames: np.ndarray, rate: int, fmt: str = "pcm24") -> None:
    """Write a (frames, channels) float array; fmt in pcm8/pcm16/pcm24/pcm32/f32/f64."""
    arr = np.asarray(frames, dtype=np.float64)
    if arr.ndim == 1:
        arr = arr[:, None]
    ch = arr.shape[1]
    flat = arr.reshape(-1)
    if fmt == "f32":
        data, tag, bits = flat.astype("<f4").tobytes(), 3, 32
    elif fmt == "f64":
        data, tag, bits = flat.astype("<f8").tobytes(), 3, 64
    elif fmt == "pcm8":
        data, tag, bits = np.clip(np.rint(flat * 128 + 128), 0, 255).astype(np.uint8).tobytes(), 1, 8
    elif fmt == "pcm16":
        data, tag, bits = np.clip(np.rint(flat * 32768), -32768, 32767).astype("<i2").tobytes(), 1, 16
    elif fmt == "pcm32":
        v = np.clip(np.rint(flat * 2.0**31), -(2**31), 2**31 - 1).astype("<i4")
        data, tag, bits = v.tobytes(), 1, 32
    elif fmt == "pcm24":
        v = np.clip(np.rint(flat * FS), -FS, FS - 1).astype(np.int64)
        data, tag, bits = b"".join((int(x) & 0xFFFFFF).to_bytes(3, "little") for x in v), 1, 24
    else:
        raise AssertionError(fmt)
    path.write_bytes(_riff(tag, ch, rate, bits, data))


def estimate_freq(y: np.ndarray, rate: float) -> float:
    """Frequency from interpolated rising zero crossings."""
    idx = np.nonzero((y[:-1] < 0) & (y[1:] >= 0))[0]
    pos = idx + y[idx] / (y[idx] - y[idx + 1])
    return (len(pos) - 1) * rate / (pos[-1] - pos[0])


def sine(rate: int, freq: float, seconds: float, amp: float = 0.5) -> np.ndarray:
    t = np.arange(int(round(seconds * rate))) / rate
    return amp * np.sin(2 * np.pi * freq * t)


@pytest.mark.parametrize("rate", [22050, 44100, 48000, 88200, 96000])
def test_any_rate_gives_2048_samples(tmp_path, rate):
    rng = np.random.default_rng(rate)
    n = int(0.2 * rate)
    x = rng.standard_normal(n) * np.exp(-np.arange(n) / (0.02 * rate)) * 0.3
    p = tmp_path / "ir.wav"
    write_wav(p, x, rate)
    r = convert(p)
    assert len(r.samples) == 2048
    assert all(isinstance(s, int) and -FS <= s <= MAX for s in r.samples)
    assert (r.source_rate, r.source_frames, r.source_channels) == (rate, n, 1)
    assert r.truncated
    assert max(abs(s) for s in r.samples) == MAX
    assert r.peak_dbfs == pytest.approx(0.0, abs=1e-6)


def test_sine_48k_keeps_frequency(tmp_path):
    p = tmp_path / "sine.wav"
    write_wav(p, sine(48000, 1000.0, 0.2), 48000)
    r = convert(p, normalize="none", fade_samples=0)
    y = np.array(r.samples, dtype=np.float64)
    f = estimate_freq(y[200:1900], 44100)
    assert abs(f - 1000.0) / 1000.0 < 0.01
    assert np.max(np.abs(y[200:1900])) / (0.5 * FS) == pytest.approx(1.0, abs=0.01)


@pytest.mark.parametrize("src", [22050, 32000, 48000, 88200, 96000, 192000])
def test_resample_frequency_and_gain(src):
    x = sine(src, 1000.0, 0.3)
    y = resample(x, src, 44100)
    assert len(y) == math.ceil(len(x) * 44100 / src)
    mid = y[2000:-2000]
    assert abs(estimate_freq(mid, 44100) - 1000.0) / 1000.0 < 0.001
    assert np.sqrt(np.mean(mid**2)) == pytest.approx(0.5 / math.sqrt(2), rel=0.005)


def test_resample_rejects_aliasing():
    # 30 kHz is above the 22.05 kHz output Nyquist frequency: must be filtered out.
    y = resample(sine(96000, 30000.0, 0.2), 96000, 44100)
    rms = np.sqrt(np.mean(y[1000:-1000] ** 2))
    assert rms < 1e-3 * 0.5


def test_resample_identity_and_errors():
    x = np.array([0.1, -0.2, 0.3])
    assert list(resample(x, 44100, 44100, n_out=5)) == [0.1, -0.2, 0.3, 0.0, 0.0]
    assert len(resample([], 48000, 44100, n_out=4)) == 4
    with pytest.raises(ValueError):
        resample(x, 0, 44100)


def test_stereo_channel_modes(tmp_path):
    frames = np.zeros((100, 2))
    frames[0, 0] = 0.5
    frames[1, 1] = -0.25
    p = tmp_path / "st.wav"
    write_wav(p, frames, 44100, "f32")
    mix = convert(p, channel="mix", normalize="none").samples
    left = convert(p, channel="left", normalize="none").samples
    right = convert(p, channel="right", normalize="none").samples
    assert mix[:3] == [FS // 4, -FS // 8, 0]
    assert left[:3] == [FS // 2, 0, 0]
    assert right[:3] == [0, -FS // 4, 0]
    mono = tmp_path / "mono.wav"
    write_wav(mono, frames[:, 0], 44100, "f32")
    with pytest.raises(ValueError, match="mono"):
        convert(mono, channel="right")
    assert convert(mono, channel="left", normalize="none").samples[0] == FS // 2


@pytest.mark.parametrize("fmt", ["pcm8", "pcm16", "pcm24", "pcm32", "f32", "f64"])
def test_input_formats(tmp_path, fmt):
    x = np.zeros(64)
    x[0], x[1] = 0.5, -0.25
    p = tmp_path / f"{fmt}.wav"
    write_wav(p, x, 44100, fmt)
    r = convert(p, normalize="none")
    assert r.samples[:3] == [FS // 2, -FS // 4, 0]
    assert not r.truncated


def test_peak_normalization(tmp_path):
    x = np.zeros(300)
    x[0], x[5] = 0.05, -0.1
    p = tmp_path / "n.wav"
    write_wav(p, x, 44100, "f64")
    r = convert(p)
    assert r.samples[5] == -MAX
    assert abs(r.samples[0] - MAX / 2) <= 1
    assert r.peak_dbfs == pytest.approx(0.0, abs=1e-6)
    r6 = convert(p, target_peak_db=-6.0)
    assert abs(-r6.samples[5] - MAX * 10 ** (-6 / 20)) <= 1
    assert r6.peak_dbfs == pytest.approx(-6.0, abs=1e-3)
    g = convert(p, target_peak_db=-6.0, gain_db=-6.0)
    assert g.peak_dbfs == pytest.approx(-12.0, abs=1e-3)


def test_fade_only_when_truncated(tmp_path):
    long_dc = tmp_path / "long.wav"
    write_wav(long_dc, np.full(4096, 0.5), 44100, "f32")
    r = convert(long_dc, normalize="none", fade_samples=128)
    assert r.truncated
    s = r.samples
    assert s[2047] == 0
    assert s[1919] == FS // 2
    assert all(s[i] > s[i + 1] for i in range(1919, 2047))
    assert convert(long_dc, normalize="none", fade_samples=0).samples[2047] == FS // 2

    zero_tail = np.zeros(4096)
    zero_tail[:2048] = 0.5
    zt = tmp_path / "zt.wav"
    write_wav(zt, zero_tail, 44100, "f32")
    r = convert(zt, normalize="none")
    assert not r.truncated
    assert r.samples[2047] == FS // 2

    short = tmp_path / "short.wav"
    write_wav(short, np.full(1000, 0.5), 44100, "f32")
    r = convert(short, normalize="none")
    assert not r.truncated
    assert r.samples[999] == FS // 2
    assert r.samples[1000:] == [0] * 1048


def test_short_resampled_is_zero_padded(tmp_path):
    p = tmp_path / "s48.wav"
    write_wav(p, np.hanning(1000) * 0.5, 48000)
    r = convert(p)
    assert not r.truncated
    assert r.samples[1000:] == [0] * 1048


def test_clipping(tmp_path):
    p = tmp_path / "c.wav"
    write_wav(p, sine(44100, 500.0, 0.03, amp=0.9), 44100, "f32")
    r = convert(p, normalize="none", gain_db=6.0)
    assert r.clipped > 0
    assert max(r.samples) == MAX and min(r.samples) == -FS
    assert any("clipped" in w for w in r.warnings)


def test_trim_leading_silence(tmp_path):
    x = np.zeros(1000)
    x[300] = 0.5
    x[301:400] = 0.1
    p = tmp_path / "t.wav"
    write_wav(p, x, 44100, "f32")
    r = convert(p, trim_leading_silence=True)
    assert r.trimmed_frames == 300
    assert r.samples[0] == MAX
    assert convert(p).samples[300] == MAX


def test_silent_input(tmp_path):
    p = tmp_path / "z.wav"
    write_wav(p, np.zeros(100), 44100, "pcm16")
    r = convert(p)
    assert r.samples == [0] * 2048
    assert r.peak_dbfs == float("-inf")
    assert any("silent" in w for w in r.warnings)


def test_invalid_options(tmp_path):
    x = np.zeros((10, 1))
    with pytest.raises(ValueError):
        convert_array(x, 44100, normalize="rms")
    with pytest.raises(ValueError):
        convert_array(x, 44100, channel="center")
    with pytest.raises(ValueError):
        convert_array(x, 44100, fade_samples=4096)
    with pytest.raises(ValueError):
        convert_array(x, 44100, gain_db=float("nan"))


def test_non_finite_float_rejected(tmp_path):
    p = tmp_path / "nan.wav"
    write_wav(p, np.array([0.1, np.nan, 0.2]), 44100, "f32")
    with pytest.raises(ValueError, match="non-finite"):
        convert(p)


def test_s24le_encoding():
    assert encode_s24le([0x123456]) == bytes([0x56, 0x34, 0x12])
    assert encode_s24le([-1]) == b"\xff\xff\xff"
    assert encode_s24le([-(1 << 23), (1 << 23) - 1]) == bytes.fromhex("000080ffff7f")
    assert decode_s24le(bytes.fromhex("563412ffffff")) == [0x123456, -1]
    with pytest.raises(ValueError):
        encode_s24le([1 << 23])
    with pytest.raises(ValueError):
        encode_s24le([-(1 << 23) - 1])


def test_export_roundtrip_and_preset_samples(tmp_path):
    samples = [((k * 7919) % (1 << 24)) - (1 << 23) for k in range(2048)]
    p = tmp_path / "x.wav"
    export_wav(samples, p)
    r = convert(p, normalize="none")
    assert r.samples == samples
    block = bytearray(PRESET_LEN)
    block[IR_OFFSET : IR_OFFSET + 6144] = encode_s24le(samples)
    assert samples_from_preset(bytes(block)) == samples
    with pytest.raises(ValueError):
        samples_from_preset(bytes(100))
