"""Convert arbitrary WAV impulse responses to the IR Box format. Pure, no device I/O.

The pedal plays exactly 2048 signed 24-bit samples at 44100 Hz (46.4 ms).
:func:`convert` reads a WAV file (PCM 8/16/24/32-bit or float 32/64-bit,
any channel count), picks or mixes a channel, resamples it to 44100 Hz with a
Kaiser-windowed sinc interpolator, cuts it to 2048 samples (with a
raised-cosine fade-out only when real content was cut), normalizes the peak
and encodes it to signed 24-bit integers.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from .model import (
    IR_OFFSET,
    IR_RATE,
    IR_SAMPLES,
    PRESET_LEN,
    S24_MAX,
    S24_MIN,
    decode_s24le,
)
from .wav import WavInfo, parse_wav, write_wav_24bit_mono

__all__ = [
    "CHANNEL_MODES",
    "DEFAULT_FADE",
    "IR_DURATION_MS",
    "NORMALIZE_MODES",
    "ConvertResult",
    "convert",
    "convert_array",
    "decode_wav",
    "export_wav",
    "resample",
    "samples_from_preset",
]

CHANNEL_MODES = ("mix", "left", "right")
NORMALIZE_MODES = ("peak", "none")
DEFAULT_FADE = 128
IR_DURATION_MS = IR_SAMPLES * 1000.0 / IR_RATE  # 46.44 ms

# Float full scale: 1.0 corresponds to 2**23 in the 24-bit integer domain,
# matching how integer WAV files are decoded (v / 2**(bits-1)).
_SCALE = float(1 << 23)
# Peak normalization targets the largest positive code so 0 dB never clips.
_NORM_FULL_SCALE = S24_MAX / _SCALE

# Leading samples quieter than this (relative to the peak) are "silence".
SILENCE_THRESHOLD_DB = -60.0

# Resampler design: zero crossings per side, passband edge (fraction of the
# lower Nyquist frequency) and Kaiser window beta (~ -90 dB stopband).
RESAMPLE_ZEROS = 32
RESAMPLE_ROLLOFF = 0.95
RESAMPLE_BETA = 8.6

FloatArray = NDArray[np.float64]


@dataclass(frozen=True)
class ConvertResult:
    samples: list[int]  # exactly 2048 signed 24-bit integers
    source_rate: int
    source_frames: int
    source_channels: int
    truncated: bool
    peak_dbfs: float  # output peak relative to 24-bit full scale (-inf if silent)
    warnings: list[str] = field(default_factory=list)
    channel: str = "mix"
    trimmed_frames: int = 0  # leading source frames removed by trim_leading_silence
    clipped: int = 0  # output samples clipped to the 24-bit range

    @property
    def source_duration_ms(self) -> float:
        return self.source_frames * 1000.0 / self.source_rate

    @property
    def used_duration_ms(self) -> float:
        return IR_DURATION_MS


# -- WAV decoding -------------------------------------------------------------


def decode_wav(info: WavInfo) -> FloatArray:
    """Decode WAV sample bytes to a ``(frames, channels)`` float64 array.

    Integer formats are scaled by ``1 / 2**(bits-1)`` (8-bit is unsigned).
    """
    n, ch = info.frames, info.channels
    raw = np.frombuffer(info.data, dtype=np.uint8)
    if info.is_float:
        dtype = "<f4" if info.bits == 32 else "<f8"
        out = np.frombuffer(info.data, dtype=dtype).astype(np.float64)
    elif info.bits == 8:
        out = (raw.astype(np.float64) - 128.0) / 128.0
    elif info.bits == 16:
        out = np.frombuffer(info.data, dtype="<i2").astype(np.float64) / 32768.0
    elif info.bits == 24:
        b = raw.reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = np.where(v >= 1 << 23, v - (1 << 24), v)
        out = v.astype(np.float64) / _SCALE
    elif info.bits == 32:
        out = np.frombuffer(info.data, dtype="<i4").astype(np.float64) / float(1 << 31)
    else:
        raise ValueError(f"unsupported PCM width {info.bits}")
    out = out.reshape(n, ch)
    if not np.all(np.isfinite(out)):
        raise ValueError("WAV contains non-finite (NaN/Inf) samples")
    return out


# -- resampling ---------------------------------------------------------------


def resample(
    x: Sequence[float] | FloatArray,
    src_rate: int,
    dst_rate: int,
    n_out: int | None = None,
    zeros: int = RESAMPLE_ZEROS,
    rolloff: float = RESAMPLE_ROLLOFF,
    beta: float = RESAMPLE_BETA,
) -> FloatArray:
    """Band-limited resampling of a 1-D signal by any (non-integer) ratio.

    Output sample ``n`` is the input evaluated at ``t = n * src / dst`` input
    samples through a Kaiser-windowed sinc low-pass whose cutoff is
    ``rolloff`` times the lower of the two Nyquist frequencies. Samples outside
    the input are treated as zero. ``n_out`` defaults to the full length
    ``ceil(len(x) * dst / src)``; only the requested outputs are computed.
    """
    if src_rate <= 0 or dst_rate <= 0:
        raise ValueError("sample rates must be positive")
    if zeros < 1:
        raise ValueError("zeros must be >= 1")
    if not 0.0 < rolloff <= 1.0:
        raise ValueError("rolloff must be in (0, 1]")
    sig = np.asarray(x, dtype=np.float64)
    if sig.ndim != 1:
        raise ValueError("resample expects a 1-D signal")
    if n_out is None:
        n_out = math.ceil(len(sig) * dst_rate / src_rate)
    if n_out < 0:
        raise ValueError("n_out must be >= 0")
    out = np.zeros(n_out, dtype=np.float64)
    if n_out == 0 or len(sig) == 0:
        return out
    if src_rate == dst_rate:
        m = min(n_out, len(sig))
        out[:m] = sig[:m]
        return out
    step = src_rate / dst_rate  # input samples per output sample
    cutoff = rolloff * min(1.0, dst_rate / src_rate)  # relative to input Nyquist
    half = zeros / cutoff  # kernel half-width in input samples
    taps = int(math.ceil(2.0 * half)) + 2
    i0_beta = float(np.i0(beta))
    offsets = np.arange(taps, dtype=np.int64)
    block = max(1, (1 << 20) // taps)
    last = len(sig) - 1
    for start in range(0, n_out, block):
        stop = min(n_out, start + block)
        centers = np.arange(start, stop, dtype=np.float64) * step
        k = np.ceil(centers - half).astype(np.int64)[:, None] + offsets[None, :]
        t = centers[:, None] - k
        u = t / half
        inside = (np.abs(u) <= 1.0) & (k >= 0) & (k <= last)
        window = np.i0(beta * np.sqrt(np.clip(1.0 - u * u, 0.0, None))) / i0_beta
        h = np.where(inside, cutoff * np.sinc(cutoff * t) * window, 0.0)
        out[start:stop] = np.sum(h * sig[np.clip(k, 0, last)], axis=1)
    return out


# -- conversion ---------------------------------------------------------------


def _select_channel(frames: FloatArray, channel: str) -> FloatArray:
    n_ch = frames.shape[1]
    if channel == "mix":
        return frames.mean(axis=1)
    if channel == "left":
        return frames[:, 0].copy()
    if channel == "right":
        if n_ch < 2:
            raise ValueError("--channel right needs a file with at least 2 channels; this one is mono")
        return frames[:, 1].copy()
    raise ValueError(f"unknown channel mode {channel!r}; choose from {', '.join(CHANNEL_MODES)}")


def _fade_out(length: int) -> FloatArray:
    """Raised-cosine gain ramp from ~1 down to exactly 0 over ``length`` samples."""
    i = np.arange(1, length + 1, dtype=np.float64)
    return 0.5 * (1.0 + np.cos(np.pi * i / length))


def convert_array(
    frames: FloatArray,
    source_rate: int,
    channel: str = "mix",
    normalize: str = "peak",
    target_peak_db: float = 0.0,
    gain_db: float = 0.0,
    fade_samples: int = DEFAULT_FADE,
    trim_leading_silence: bool = False,
) -> ConvertResult:
    """Convert a ``(frames, channels)`` float array (1.0 = full scale)."""
    if normalize not in NORMALIZE_MODES:
        raise ValueError(f"unknown normalize mode {normalize!r}; choose from {', '.join(NORMALIZE_MODES)}")
    for label, value in (("target peak", target_peak_db), ("gain", gain_db)):
        if not math.isfinite(value):
            raise ValueError(f"{label} must be a finite number of dB")
    if not 0 <= fade_samples <= IR_SAMPLES:
        raise ValueError(f"fade must be between 0 and {IR_SAMPLES} samples, got {fade_samples}")
    if source_rate <= 0:
        raise ValueError("source rate must be positive")
    arr = np.asarray(frames, dtype=np.float64)
    if arr.ndim == 1:
        arr = arr[:, None]
    if arr.ndim != 2 or arr.shape[1] < 1:
        raise ValueError("frames must be a (frames, channels) array")
    n_frames, n_ch = arr.shape
    warnings: list[str] = []
    sig = _select_channel(arr, channel)

    trimmed = 0
    if trim_leading_silence and sig.size:
        peak_in = float(np.max(np.abs(sig)))
        if peak_in > 0:
            threshold = peak_in * 10.0 ** (SILENCE_THRESHOLD_DB / 20.0)
            trimmed = int(np.argmax(np.abs(sig) >= threshold))
            sig = sig[trimmed:]

    # Input samples after this index lie beyond the last kept output sample.
    step = source_rate / IR_RATE
    cut = math.floor((IR_SAMPLES - 1) * step) + 1
    tail = sig[cut:]
    truncated = bool(tail.size and np.any(tail != 0.0))
    if truncated:
        total = float(np.sum(sig * sig))
        lost = float(np.sum(tail * tail))
        pct = 100.0 * lost / total if total > 0 else 0.0
        warnings.append(
            f"IR is longer than {IR_DURATION_MS:.1f} ms; it was cut to {IR_SAMPLES} samples "
            f"({pct:.2f}% of its energy discarded)"
        )

    out = resample(sig, source_rate, IR_RATE, n_out=IR_SAMPLES)
    if truncated and fade_samples > 0:
        out[IR_SAMPLES - fade_samples :] *= _fade_out(fade_samples)

    peak = float(np.max(np.abs(out))) if out.size else 0.0
    if peak == 0.0:
        warnings.append("input is silent; the IR is all zeros")
    elif normalize == "peak":
        out *= (_NORM_FULL_SCALE * 10.0 ** (target_peak_db / 20.0)) / peak
    if gain_db != 0.0:
        out *= 10.0 ** (gain_db / 20.0)

    ints = np.rint(out * _SCALE)
    clipped = int(np.count_nonzero((ints > S24_MAX) | (ints < S24_MIN)))
    if clipped:
        warnings.append(f"{clipped} samples clipped to the 24-bit range; lower --peak-db or --gain-db")
    samples = [int(v) for v in np.clip(ints, S24_MIN, S24_MAX).astype(np.int64)]
    peak_out = max((abs(s) for s in samples), default=0)
    peak_dbfs = 20.0 * math.log10(peak_out / S24_MAX) if peak_out else float("-inf")
    return ConvertResult(
        samples=samples,
        source_rate=source_rate,
        source_frames=n_frames,
        source_channels=n_ch,
        truncated=truncated,
        peak_dbfs=peak_dbfs,
        warnings=warnings,
        channel=channel,
        trimmed_frames=trimmed,
        clipped=clipped,
    )


def convert(
    path: str | Path,
    channel: str = "mix",
    normalize: str = "peak",
    target_peak_db: float = 0.0,
    gain_db: float = 0.0,
    fade_samples: int = DEFAULT_FADE,
    trim_leading_silence: bool = False,
) -> ConvertResult:
    """Read a WAV file and convert it to 2048 signed 24-bit samples at 44100 Hz.

    ``target_peak_db`` 0.0 puts the peak at full scale, like the factory IRs.
    ``gain_db`` is applied after normalization.
    """
    info = parse_wav(Path(path).read_bytes())
    if info.frames == 0:
        raise ValueError(f"{path}: WAV has no sample frames")
    return convert_array(
        decode_wav(info),
        info.rate,
        channel=channel,
        normalize=normalize,
        target_peak_db=target_peak_db,
        gain_db=gain_db,
        fade_samples=fade_samples,
        trim_leading_silence=trim_leading_silence,
    )


def export_wav(samples24: Sequence[int], path: str | Path) -> None:
    """Write samples as a 24-bit mono 44100 Hz WAV."""
    write_wav_24bit_mono(path, samples24, IR_RATE)


def samples_from_preset(block: bytes) -> list[int]:
    """Extract the 2048 IR samples from an 8192-byte preset block."""
    if len(block) != PRESET_LEN:
        raise ValueError(f"preset must be {PRESET_LEN} bytes, got {len(block)}")
    return decode_s24le(bytes(block[IR_OFFSET : IR_OFFSET + IR_SAMPLES * 3]))
