"""Minimal stdlib WAV reading/writing."""

from __future__ import annotations

import struct
import wave
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_S24_MIN = -(1 << 23)
_S24_MAX = (1 << 23) - 1


def write_wav_24bit_mono(
    path: str | Path, samples: Sequence[int], rate: int = 44100
) -> None:
    """Write signed 24-bit integer samples as a mono PCM WAV."""
    buf = bytearray()
    for s in samples:
        s = max(_S24_MIN, min(_S24_MAX, int(s)))
        buf += (s & 0xFFFFFF).to_bytes(3, "little")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(3)
        w.setframerate(rate)
        w.writeframes(bytes(buf))


@dataclass
class WavData:
    rate: int
    channels: int
    bits: int
    is_float: bool
    samples: list[list[float]]  # per channel, floats in [-1, 1)


@dataclass(frozen=True)
class WavInfo:
    """Format of a WAV file plus its raw sample bytes (whole frames only)."""

    rate: int
    channels: int
    bits: int
    is_float: bool
    data: bytes

    @property
    def width(self) -> int:
        return self.bits // 8

    @property
    def frames(self) -> int:
        return len(self.data) // (self.width * self.channels)


def parse_wav(blob: bytes) -> WavInfo:
    """Parse a RIFF/WAVE byte string (PCM 8/16/24/32-bit, float 32/64-bit)."""
    if len(blob) < 12 or blob[0:4] != b"RIFF" or blob[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    fmt: tuple[int, int, int, int] | None = None
    data: bytes | None = None
    pos = 12
    while pos + 8 <= len(blob):
        cid = blob[pos : pos + 4]
        size = struct.unpack_from("<I", blob, pos + 4)[0]
        body = blob[pos + 8 : pos + 8 + size]
        if cid == b"fmt ":
            if len(body) < 16:
                raise ValueError("fmt chunk too short")
            tag, ch, rate, _, _, bits = struct.unpack_from("<HHIIHH", body)
            if tag == 0xFFFE:
                if len(body) < 26:
                    raise ValueError("extensible fmt chunk too short")
                tag = struct.unpack_from("<H", body, 24)[0]
            fmt = (tag, ch, rate, bits)
        elif cid == b"data":
            data = body
        pos += 8 + size + (size & 1)
    if fmt is None or data is None:
        raise ValueError("missing fmt or data chunk")
    tag, ch, rate, bits = fmt
    if ch < 1:
        raise ValueError("invalid channel count")
    if rate < 1:
        raise ValueError("invalid sample rate")
    if tag not in (1, 3):
        raise ValueError(f"unsupported WAV format tag {tag}")
    is_float = tag == 3
    if is_float and bits not in (32, 64):
        raise ValueError(f"unsupported float width {bits}")
    if not is_float and bits not in (8, 16, 24, 32):
        raise ValueError(f"unsupported PCM width {bits}")
    frame_size = (bits // 8) * ch
    usable = (len(data) // frame_size) * frame_size
    return WavInfo(rate, ch, bits, is_float, bytes(data[:usable]))


def read_wav(path: str | Path) -> WavData:
    info = parse_wav(Path(path).read_bytes())
    width = info.width
    frame_size = width * info.channels
    channels: list[list[float]] = [[] for _ in range(info.channels)]
    for i in range(info.frames):
        for c in range(info.channels):
            off = i * frame_size + c * width
            channels[c].append(_decode(info.data, off, info.bits, info.is_float))
    return WavData(info.rate, info.channels, info.bits, info.is_float, channels)


def _decode(data: bytes, off: int, bits: int, is_float: bool) -> float:
    if is_float:
        if bits == 32:
            return struct.unpack_from("<f", data, off)[0]
        if bits == 64:
            return struct.unpack_from("<d", data, off)[0]
        raise ValueError(f"unsupported float width {bits}")
    if bits == 8:
        return (data[off] - 128) / 128.0
    if bits in (16, 24, 32):
        n = bits // 8
        v = int.from_bytes(data[off : off + n], "little", signed=True)
        return v / float(1 << (bits - 1))
    raise ValueError(f"unsupported PCM width {bits}")
