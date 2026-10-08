"""M-VAVE IR Box framing (BLE raw frames and USB-MIDI SysEx). Pure, no I/O.

Frame layout: ``00 59 <cmd> <len:3 LE> <body...> <cs>`` where ``len`` is the
body length and ``cs = ~sum(body) & 0xFF`` (header bytes 0-5 are excluded).

Over USB-MIDI the same frame is sent as ``F0 <encode7(frame)> F7`` where
``encode7`` packs the bytes into an LSB-first stream of 7-bit groups.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

SERVICE_UUID = "0000ae40-0000-1000-8000-00805f9b34fb"
WRITE_CHAR_UUID = "0000ae41-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR_UUID = "0000ae42-0000-1000-8000-00805f9b34fb"

SYNC = b"\x00\x59"
HEADER_LEN = 6  # sync(2) + cmd(1) + len(3)

CMD_ACK = 0x00  # ACK / init frame (layout inferred)
CMD_QUERY = 0x11  # device query; reply layout inferred from the Cube Baby crate
CMD_MIDI_WRITE = 0x15
CMD_ERASE = 0x21  # forbidden in this toolkit
CMD_WRITE = 0x22
CMD_READ = 0x23

SYSEX_START = 0xF0
SYSEX_END = 0xF7

# Length of the NUL-padded ASCII name at the start of a 0x11 query reply.
QUERY_NAME_LEN = 16

# Safety denylist: commands this toolkit must never send, on any transport.
FORBIDDEN_COMMANDS = frozenset({CMD_ERASE})


class ForbiddenOperation(RuntimeError):
    pass


def guard_frame(frame: bytes) -> None:
    """Raise :class:`ForbiddenOperation` if ``frame`` uses a denylisted command."""
    if len(frame) >= 3 and frame[2] in FORBIDDEN_COMMANDS:
        raise ForbiddenOperation(f"refusing to send command 0x{frame[2]:02x}")

TYPE_NOR = 0
TYPE_NAND = 1
TYPE_SD = 2
TYPE_EFF = 3
TYPE_DEV = 4
TYPE_USR = 5
TYPE_RSV1 = 6
TYPE_RSV2 = 7

WRITE_CHUNK = 173
READ_CHUNK = 1000

# Upper bound on a plausible body length; larger values are treated as garbage.
MAX_BODY_LEN = 0x10000


def checksum(data: bytes | bytearray) -> int:
    """Inverted 8-bit sum of ``data``."""
    return (~sum(data)) & 0xFF


def _frame(cmd: int, body: bytes) -> bytes:
    if not 0 <= cmd <= 0xFF:
        raise ValueError("cmd out of range")
    return (
        SYNC
        + bytes([cmd])
        + len(body).to_bytes(3, "little")
        + body
        + bytes([checksum(body)])
    )


def _check_addr_type(type_: int, addr: int) -> None:
    if not 0 <= type_ <= 0xFF:
        raise ValueError("type out of range")
    if not 0 <= addr <= 0xFFFFFFFF:
        raise ValueError("address out of range")


def build_write(type_: int, addr: int, data: bytes) -> bytes:
    """Build a WRITE (0x22) frame."""
    _check_addr_type(type_, addr)
    if len(data) > 0xFFFFFF:
        raise ValueError("data too long")
    body = (
        bytes([type_])
        + addr.to_bytes(4, "little")
        + len(data).to_bytes(3, "little")
        + bytes(data)
    )
    return _frame(CMD_WRITE, body)


def build_read(type_: int, addr: int, length: int) -> bytes:
    """Build a READ (0x23) request frame (always 15 bytes)."""
    _check_addr_type(type_, addr)
    if not 0 <= length <= 0xFFFFFF:
        raise ValueError("length out of range")
    body = bytes([type_]) + addr.to_bytes(4, "little") + length.to_bytes(3, "little")
    return _frame(CMD_READ, body)


def build_query() -> bytes:
    """Build the 0x11 device query frame ``00 59 11 00 00 00 FF``."""
    return _frame(CMD_QUERY, b"")


def frame_length(raw: bytes) -> int | None:
    """Total frame length announced by the header, or None if no header."""
    if len(raw) < HEADER_LEN or raw[:2] != SYNC:
        return None
    return HEADER_LEN + int.from_bytes(raw[3:6], "little") + 1


# -- USB-MIDI SysEx packing --------------------------------------------------


def encode7(raw: bytes, flush_zero: bool = True) -> bytes:
    """Pack ``raw`` into 7-bit bytes as one LSB-first bit stream.

    Bit ``j`` of the input stream is bit ``j % 8`` of ``raw[j // 8]``; output
    byte ``k`` carries stream bits ``7k .. 7k+6``. Leftover bits are flushed in
    a final byte. With ``flush_zero`` (the reference behaviour) a final byte is
    emitted even when no bits are left over, so the output is always
    ``n + n // 7 + 1`` bytes long; without it the length is ``ceil(8n / 7)``.
    """
    out = bytearray()
    accum = 0
    nbits = 0
    for b in raw:
        accum |= b << nbits
        nbits += 8
        while nbits >= 7:
            out.append(accum & 0x7F)
            accum >>= 7
            nbits -= 7
    if nbits > 0 or flush_zero:
        out.append(accum & 0x7F)
    return bytes(out)


def decode7(packed: bytes) -> bytes:
    """Inverse of :func:`encode7`.

    Emits exactly ``(7 * len(packed)) // 8`` bytes; output byte ``k`` is stream
    bits ``8k .. 8k+7``. Leftover padding bits are dropped, but no decoded byte
    is ever dropped, even when it is 0x00 (e.g. a checksum of zero).
    """
    n_raw = (7 * len(packed)) // 8
    out = bytearray()
    accum = 0
    nbits = 0
    for i, s in enumerate(packed):
        if s > 0x7F:
            raise ValueError(f"packed byte {i} is 0x{s:02x}, not 7-bit")
        accum |= s << nbits
        nbits += 7
        # nbits < 15 here, so at most one byte is completed per input byte;
        # after len(packed) inputs exactly n_raw bytes have been emitted.
        if nbits >= 8:
            out.append(accum & 0xFF)
            accum >>= 8
            nbits -= 8
    return bytes(out[:n_raw])


def wrap_sysex(raw: bytes, flush_zero: bool = True) -> bytes:
    """Wrap a raw ``00 59`` frame as a SysEx message ``F0 <encode7> F7``."""
    return bytes([SYSEX_START]) + encode7(raw, flush_zero) + bytes([SYSEX_END])


def trim_frame(raw: bytes) -> bytes:
    """Trim zero padding after a frame, using the header length.

    Raises ``ValueError`` if the header announces more bytes than present.
    Data without a ``00 59`` header, or with non-zero bytes after the
    announced end, is returned unchanged (left to :class:`FrameParser`).
    """
    expected = frame_length(raw)
    if expected is None:
        return raw
    if len(raw) < expected:
        raise ValueError(
            f"truncated frame: header announces {expected} bytes, got {len(raw)}"
        )
    if any(raw[expected:]):
        return raw
    return raw[:expected]


def unwrap_sysex(msg: bytes) -> bytes:
    """Decode a complete SysEx message ``F0 ... F7`` back into a raw frame.

    Accepts payloads with or without the trailing flush byte.
    """
    if len(msg) < 2 or msg[0] != SYSEX_START or msg[-1] != SYSEX_END:
        raise ValueError(f"not a complete SysEx message: {bytes(msg).hex()}")
    return trim_frame(decode7(bytes(msg[1:-1])))


class SysExAssembler:
    """Collects complete ``F0 .. F7`` messages from a MIDI byte stream.

    Real-time bytes (0xF8-0xFF) inside a SysEx are skipped. Any other status
    byte aborts an unterminated SysEx (recorded in ``errors``). Bytes outside
    a SysEx are counted in ``ignored_bytes``.
    """

    def __init__(self) -> None:
        self._buf: bytearray | None = None
        self.errors: list[str] = []
        self.ignored_bytes = 0

    @property
    def in_progress(self) -> bool:
        return self._buf is not None

    def feed(self, data: bytes) -> list[bytes]:
        out: list[bytes] = []
        for b in data:
            if b >= 0xF8:
                continue
            if b == SYSEX_START:
                if self._buf is not None:
                    self.errors.append(
                        f"SysEx restarted before F7; dropped {len(self._buf)} bytes"
                    )
                self._buf = bytearray([b])
            elif b == SYSEX_END:
                if self._buf is None:
                    self.errors.append("stray F7 outside SysEx")
                    self.ignored_bytes += 1
                else:
                    self._buf.append(b)
                    out.append(bytes(self._buf))
                    self._buf = None
            elif b >= 0x80:
                if self._buf is not None:
                    self.errors.append(
                        f"SysEx aborted by status 0x{b:02x}; dropped {len(self._buf)} bytes"
                    )
                    self._buf = None
                self.ignored_bytes += 1
            elif self._buf is not None:
                self._buf.append(b)
            else:
                self.ignored_bytes += 1
        return out


def iter_write_chunks(
    base: int, data: bytes, chunk: int = WRITE_CHUNK
) -> Iterator[tuple[int, bytes]]:
    """Yield ``(address, chunk_bytes)`` pairs for a chunked write."""
    for i in range(0, len(data), chunk):
        yield base + i, bytes(data[i : i + chunk])


def iter_read_chunks(
    base: int, length: int, chunk: int = READ_CHUNK
) -> Iterator[tuple[int, int]]:
    """Yield ``(address, length)`` pairs for a chunked read."""
    for i in range(0, length, chunk):
        yield base + i, min(chunk, length - i)


@dataclass(frozen=True)
class ReadResponse:
    type: int
    addr: int
    length: int
    data: bytes
    raw: bytes


@dataclass(frozen=True)
class WriteAck:
    """ACK / init frame, cmd 0x00 (layout inferred).

    ``status`` is the first body byte (0 means OK), or None for an empty body
    such as the bare init frame ``00 59 00 00 00 00 FF``.
    """

    status: int | None
    raw: bytes

    @property
    def ok(self) -> bool:
        return self.status == 0

    @property
    def body(self) -> bytes:
        return self.raw[HEADER_LEN:-1]

    @property
    def checksum_ok(self) -> bool:
        return self.raw[-1] == checksum(self.body)


@dataclass(frozen=True)
class QueryReply:
    """Reply to the 0x11 query (layout inferred from the Cube Baby crate).

    ``name`` is the 16-byte NUL-padded ASCII field; ``rest`` is everything
    after it (meaning unknown).
    """

    name: str
    rest: bytes
    body: bytes
    raw: bytes

    @property
    def checksum_ok(self) -> bool:
        return self.raw[-1] == checksum(self.body)


@dataclass(frozen=True)
class OtherFrame:
    cmd: int
    body: bytes
    raw: bytes


Frame = ReadResponse | WriteAck | QueryReply | OtherFrame


def parse_query_name(field: bytes) -> str:
    """Decode a NUL-padded ASCII name; non-ASCII bytes become U+FFFD."""
    return field.split(b"\x00", 1)[0].decode("ascii", errors="replace")


class FrameParser:
    """Reassembles frames from arbitrarily fragmented notification data."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self.discarded_bytes = 0
        self.errors: list[str] = []

    def reset(self) -> None:
        self._buf.clear()

    def _drop(self, n: int) -> None:
        self.discarded_bytes += n
        del self._buf[:n]

    def feed(self, data: bytes) -> list[Frame]:
        self._buf += data
        out: list[Frame] = []
        while True:
            idx = self._buf.find(SYNC)
            if idx < 0:
                # Keep a trailing 0x00 which may be the start of a sync.
                keep = 1 if self._buf.endswith(b"\x00") else 0
                self._drop(len(self._buf) - keep)
                break
            if idx > 0:
                self._drop(idx)
            if len(self._buf) < HEADER_LEN:
                break
            cmd = self._buf[2]
            body_len = int.from_bytes(self._buf[3:6], "little")
            if body_len > MAX_BODY_LEN:
                self.errors.append(f"implausible length {body_len}")
                self._drop(1)
                continue
            total = HEADER_LEN + body_len + 1
            if len(self._buf) < total:
                break
            raw = bytes(self._buf[:total])
            body = raw[HEADER_LEN:-1]
            if cmd == CMD_READ:
                frame = self._parse_read(raw, body)
                if frame is None:
                    self._drop(1)
                    continue
                out.append(frame)
            elif cmd == CMD_ACK:
                out.append(WriteAck(body[0] if body else None, raw))
            elif cmd == CMD_QUERY and len(body) >= QUERY_NAME_LEN:
                reply = QueryReply(
                    name=parse_query_name(body[:QUERY_NAME_LEN]),
                    rest=body[QUERY_NAME_LEN:],
                    body=body,
                    raw=raw,
                )
                if not reply.checksum_ok:
                    self.errors.append("query reply checksum mismatch")
                out.append(reply)
            else:
                out.append(OtherFrame(cmd, body, raw))
            del self._buf[:total]
        return out

    def _parse_read(self, raw: bytes, body: bytes) -> ReadResponse | None:
        if len(body) < 8:
            self.errors.append("read response too short")
            return None
        if raw[-1] != checksum(body):
            self.errors.append("read response checksum mismatch")
            return None
        n = int.from_bytes(body[5:8], "little")
        if n != len(body) - 8:
            self.errors.append("read response length echo mismatch")
            return None
        return ReadResponse(
            type=body[0],
            addr=int.from_bytes(body[1:5], "little"),
            length=n,
            data=body[8:],
            raw=raw,
        )
