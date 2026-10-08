"""Parsers and encoders for the preset block and name table. Pure, no I/O.

Preset block layout (8192 bytes, type 5 working copy at offset 0):

* ``0x00`` name[17] NUL-padded ASCII, ``0x11`` cab_on, ``0x12`` eq_on,
  ``0x13`` ``b"patch"`` (24-byte header);
* ``0x18`` IR block, 6164 bytes: ``b"CAB\\0"``, cab name[14], type (2),
  level (the volume byte at ``0x2B``), then 2048 signed 24-bit little-endian
  samples at 44100 Hz;
* ``0x1C00`` EQ block, 40 bytes (see :data:`EQ_BANDS`).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass

NAME_SLOTS = 32
NAME_STRIDE = 17
NAME_TABLE_LEN = NAME_SLOTS * NAME_STRIDE  # 544
NAME_MAX_CHARS = 16
PRESET_LEN = 8192

HEADER_LEN = 24
CAB_ON_OFFSET = 17
EQ_ON_OFFSET = 18
PATCH_MAGIC = b"patch"
PATCH_MAGIC_OFFSET = 19

IR_BLOCK_OFFSET = 24  # 0x18
CAB_MAGIC = b"CAB\x00"
CAB_NAME_LEN = 14
CAB_TYPE_IR = 0x02
LEVEL_OFFSET = 43  # 0x2B, also the volume control
IR_OFFSET = 44
IR_SAMPLES = 2048
IR_RATE = 44100
IR_END = IR_OFFSET + IR_SAMPLES * 3  # 6188
IR_BLOCK_LEN = IR_END - IR_BLOCK_OFFSET  # 6164

EQ_OFFSET = 7168  # 0x1C00
EQ_LEN = 40
EQ_END = EQ_OFFSET + EQ_LEN  # 7208

VOLUME_MAX = 127

S24_MIN = -(1 << 23)
S24_MAX = (1 << 23) - 1

# Ranges of the working copy that define a preset (header, IR block, EQ).
PRESET_RANGES: tuple[tuple[int, int], ...] = (
    (0, HEADER_LEN),
    (IR_BLOCK_OFFSET, IR_END),
    (EQ_OFFSET, EQ_END),
)


def _ascii(raw: bytes) -> str:
    """Text up to the first NUL, keeping printable ASCII (32-126) only."""
    end = raw.find(b"\x00")
    if end >= 0:
        raw = raw[:end]
    return "".join(chr(b) for b in raw if 32 <= b <= 126)


def name_from_field(raw: bytes) -> str | None:
    """Decode a NUL-padded name field; None when empty."""
    return _ascii(raw) or None


def parse_name_table(data: bytes) -> list[str | None]:
    if len(data) != NAME_TABLE_LEN:
        raise ValueError(f"name table must be {NAME_TABLE_LEN} bytes, got {len(data)}")
    names: list[str | None] = []
    for i in range(NAME_SLOTS):
        names.append(name_from_field(data[i * NAME_STRIDE : (i + 1) * NAME_STRIDE]))
    return names


def encode_name(name: str) -> bytes:
    """Validate a preset name and return its 17-byte NUL-padded field.

    Names are 1-16 printable ASCII characters (0x20-0x7E).
    """
    if not name:
        raise ValueError("name must not be empty")
    if len(name) > NAME_MAX_CHARS:
        raise ValueError(f"name {name!r} is longer than {NAME_MAX_CHARS} characters")
    bad = [c for c in name if not 32 <= ord(c) <= 126]
    if bad:
        raise ValueError(f"name {name!r} contains non-printable-ASCII characters: {bad!r}")
    return name.encode("ascii").ljust(NAME_STRIDE, b"\x00")


def build_header(name_field: bytes, cab_on: int, eq_on: int) -> bytes:
    """Build the 24-byte preset header ``name[17] cab_on eq_on 'patch'``."""
    if len(name_field) != NAME_STRIDE:
        raise ValueError(f"name field must be {NAME_STRIDE} bytes, got {len(name_field)}")
    for label, value in (("cab_on", cab_on), ("eq_on", eq_on)):
        if not 0 <= value <= 0xFF:
            raise ValueError(f"{label} must be a byte, got {value}")
    return bytes(name_field) + bytes([cab_on, eq_on]) + PATCH_MAGIC


def encode_s24le(samples: Sequence[int]) -> bytes:
    """Encode signed 24-bit integers as little-endian 3-byte words."""
    out = bytearray()
    for i, s in enumerate(samples):
        v = int(s)
        if not S24_MIN <= v <= S24_MAX:
            raise ValueError(f"sample {i} = {v} is outside the signed 24-bit range")
        out += (v & 0xFFFFFF).to_bytes(3, "little")
    return bytes(out)


def decode_s24le(data: bytes) -> list[int]:
    """Decode little-endian signed 24-bit words."""
    if len(data) % 3:
        raise ValueError(f"24-bit data length {len(data)} is not a multiple of 3")
    return [int.from_bytes(data[i : i + 3], "little", signed=True) for i in range(0, len(data), 3)]


def build_ir_block(
    samples: Sequence[int], level: int, cab_name: bytes | None = None
) -> bytes:
    """Build the 6164-byte IR block written at offset 0x18.

    ``level`` must be the current volume byte (0x2B lies inside the block).
    ``cab_name`` (<= 14 bytes) defaults to 14 NUL bytes, like the official app.
    """
    if len(samples) != IR_SAMPLES:
        raise ValueError(f"IR must have exactly {IR_SAMPLES} samples, got {len(samples)}")
    if not 0 <= level <= 0xFF:
        raise ValueError(f"level must be a byte, got {level}")
    name = b"" if cab_name is None else bytes(cab_name)
    if len(name) > CAB_NAME_LEN:
        raise ValueError(f"cab name must be at most {CAB_NAME_LEN} bytes, got {len(name)}")
    block = (
        CAB_MAGIC
        + name.ljust(CAB_NAME_LEN, b"\x00")
        + bytes([CAB_TYPE_IR, level])
        + encode_s24le(samples)
    )
    assert len(block) == IR_BLOCK_LEN
    return block


def _i8(b: int) -> int:
    return b - 256 if b >= 128 else b


def _u16(data: bytes, off: int) -> int:
    return int.from_bytes(data[off : off + 2], "little")


def _s24(data: bytes, off: int) -> int:
    return int.from_bytes(data[off : off + 3], "little", signed=True)


# -- EQ ---------------------------------------------------------------------

EQ_GAIN_MAX_DB = 12.0
EQ_Q_MIN = 0.1
EQ_Q_MAX = 16.0
EQ_FREQ_MIN = 20
EQ_FREQ_MAX = 20000


@dataclass(frozen=True)
class EqBand:
    """Byte offsets of one EQ band, relative to the start of the EQ block."""

    name: str
    enable: int
    freq: int
    gain: int | None = None
    q: int | None = None


EQ_BANDS: dict[str, EqBand] = {
    "hpf": EqBand("hpf", enable=0, freq=38),
    "lowshelf": EqBand("lowshelf", enable=1, freq=32, gain=19),
    **{
        f"peak{k + 1}": EqBand(f"peak{k + 1}", enable=2 + k, freq=22 + 2 * k, gain=14 + k, q=9 + k)
        for k in range(5)
    },
    "highshelf": EqBand("highshelf", enable=7, freq=34, gain=20),
    "lpf": EqBand("lpf", enable=8, freq=36),
}


def _finite(label: str, value: float) -> float:
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(f"{label} must be a finite number, got {value!r}")
    return v


def encode_eq_gain(db: float) -> int:
    """Gain in dB -> stored byte (int8, tenths of a dB). Range +-12.0 dB."""
    v = _finite("gain", db)
    if not -EQ_GAIN_MAX_DB <= v <= EQ_GAIN_MAX_DB:
        raise ValueError(f"gain {db} dB is outside -{EQ_GAIN_MAX_DB}..+{EQ_GAIN_MAX_DB} dB")
    return round(v * 10) & 0xFF


def decode_eq_gain(b: int) -> float:
    return _i8(b) / 10.0


def encode_eq_q(q: float) -> int:
    """Q factor -> stored byte (tenths). Range 0.1-16.0 (1-160)."""
    v = _finite("Q", q)
    if not EQ_Q_MIN <= v <= EQ_Q_MAX:
        raise ValueError(f"Q {q} is outside {EQ_Q_MIN}..{EQ_Q_MAX}")
    return max(1, min(160, round(v * 10)))


def decode_eq_q(b: int) -> float:
    return b / 10.0


def encode_eq_freq(hz: int) -> bytes:
    """Frequency in Hz -> u16 little-endian. Range 20-20000 Hz."""
    if isinstance(hz, bool) or not isinstance(hz, int):
        raise ValueError(f"frequency must be an integer number of Hz, got {hz!r}")
    if not EQ_FREQ_MIN <= hz <= EQ_FREQ_MAX:
        raise ValueError(f"frequency {hz} Hz is outside {EQ_FREQ_MIN}..{EQ_FREQ_MAX} Hz")
    return hz.to_bytes(2, "little")


def update_eq_block(
    block: bytes,
    band: str,
    *,
    freq: int | None = None,
    gain_db: float | None = None,
    q: float | None = None,
    enable: bool | None = None,
) -> bytes:
    """Return a copy of the 40-byte EQ block with one band changed."""
    if len(block) != EQ_LEN:
        raise ValueError(f"EQ block must be {EQ_LEN} bytes, got {len(block)}")
    spec = EQ_BANDS.get(band)
    if spec is None:
        raise ValueError(f"unknown EQ band {band!r}; choose from {', '.join(EQ_BANDS)}")
    out = bytearray(block)
    if freq is not None:
        out[spec.freq : spec.freq + 2] = encode_eq_freq(freq)
    if gain_db is not None:
        if spec.gain is None:
            raise ValueError(f"band {band} has no gain parameter")
        out[spec.gain] = encode_eq_gain(gain_db)
    if q is not None:
        if spec.q is None:
            raise ValueError(f"band {band} has no Q parameter (only peak1..peak5 do)")
        out[spec.q] = encode_eq_q(q)
    if enable is not None:
        out[spec.enable] = 1 if enable else 0
    return bytes(out)


@dataclass(frozen=True)
class EqBandState:
    band: str
    enabled: bool
    freq_hz: int
    gain_db: float | None  # None for bands without gain (hpf, lpf)
    q: float | None  # None for bands without Q (all but peak1..peak5)


def describe_eq_band(block: bytes, band: str) -> EqBandState:
    """Decoded settings of one band of a 40-byte EQ block."""
    if len(block) != EQ_LEN:
        raise ValueError(f"EQ block must be {EQ_LEN} bytes, got {len(block)}")
    spec = EQ_BANDS.get(band)
    if spec is None:
        raise ValueError(f"unknown EQ band {band!r}; choose from {', '.join(EQ_BANDS)}")
    return EqBandState(
        band=band,
        enabled=block[spec.enable] != 0,
        freq_hz=_u16(block, spec.freq),
        gain_db=None if spec.gain is None else decode_eq_gain(block[spec.gain]),
        q=None if spec.q is None else decode_eq_q(block[spec.q]),
    )


@dataclass
class EqSettings:
    enable: list[int]  # 9 flags: hpf, lowshelf, peak1..5, highshelf, lpf
    peak_q: list[int]  # 5, tenths (1-160)
    peak_db: list[int]  # 5, signed tenths of a dB
    ls_db: int
    hs_db: int
    peak_hz: list[int]  # 5
    ls_hz: int
    hs_hz: int
    lp_hz: int
    hp_hz: int


def parse_eq(block: bytes) -> EqSettings:
    if len(block) != EQ_LEN:
        raise ValueError(f"EQ block must be {EQ_LEN} bytes, got {len(block)}")
    return EqSettings(
        enable=list(block[0:9]),
        peak_q=list(block[9:14]),
        peak_db=[_i8(b) for b in block[14:19]],
        ls_db=_i8(block[19]),
        hs_db=_i8(block[20]),
        peak_hz=[_u16(block, 22 + 2 * i) for i in range(5)],
        ls_hz=_u16(block, 32),
        hs_hz=_u16(block, 34),
        lp_hz=_u16(block, 36),
        hp_hz=_u16(block, 38),
    )


@dataclass
class Preset:
    name: str
    cab_on: int  # nonzero means on (factory slots use 0xFF on some presets)
    eq_on: int
    magic: bytes
    cad_magic: bytes
    cad_name: str
    cad_type: int
    level: int
    ir_samples: list[int]
    eq: EqSettings
    unknown_ir_to_eq: bytes  # bytes 6188-7167
    unknown_tail: bytes  # bytes 7208-8191
    eq_reserved: int  # byte 7189

    @property
    def magic_ok(self) -> bool:
        return self.magic == PATCH_MAGIC and self.cad_magic == CAB_MAGIC


def parse_preset(data: bytes) -> Preset:
    if len(data) != PRESET_LEN:
        raise ValueError(f"preset must be {PRESET_LEN} bytes, got {len(data)}")
    return Preset(
        name=_ascii(data[0:17]),
        cab_on=data[CAB_ON_OFFSET],
        eq_on=data[EQ_ON_OFFSET],
        magic=bytes(data[19:24]),
        cad_magic=bytes(data[24:28]),
        cad_name=_ascii(data[28:42]),
        cad_type=data[42],
        level=data[LEVEL_OFFSET],
        ir_samples=[_s24(data, IR_OFFSET + 3 * i) for i in range(IR_SAMPLES)],
        eq=parse_eq(bytes(data[EQ_OFFSET:EQ_END])),
        unknown_ir_to_eq=bytes(data[IR_END:EQ_OFFSET]),
        unknown_tail=bytes(data[EQ_END:PRESET_LEN]),
        eq_reserved=data[EQ_OFFSET + 21],
    )


def preset_to_dict(p: Preset) -> dict[str, object]:
    d = asdict(p)
    d["magic"] = p.magic.hex()
    d["cad_magic"] = p.cad_magic.hex()
    d["magic_ok"] = p.magic_ok
    d["unknown_ir_to_eq"] = p.unknown_ir_to_eq.hex()
    d["unknown_tail"] = p.unknown_tail.hex()
    return d


def preset_ranges_equal(a: bytes, b: bytes) -> bool:
    """True when header, IR block and EQ block of two preset blocks match."""
    return all(a[lo:hi] == b[lo:hi] for lo, hi in PRESET_RANGES)


def preset_range_diffs(a: bytes, b: bytes) -> list[int]:
    """Offsets inside :data:`PRESET_RANGES` where the two blocks differ."""
    out: list[int] = []
    for lo, hi in PRESET_RANGES:
        out.extend(i for i in range(lo, hi) if a[i : i + 1] != b[i : i + 1])
    return out


def check_preset_block(data: bytes) -> None:
    """Raise ValueError unless ``data`` looks like a complete preset block."""
    if len(data) != PRESET_LEN:
        raise ValueError(f"preset must be {PRESET_LEN} bytes, got {len(data)}")
    if data[PATCH_MAGIC_OFFSET:HEADER_LEN] != PATCH_MAGIC:
        raise ValueError("header magic 'patch' missing at offset 19")
    if data[IR_BLOCK_OFFSET : IR_BLOCK_OFFSET + 4] != CAB_MAGIC:
        raise ValueError("IR block magic 'CAB\\0' missing at offset 24")
