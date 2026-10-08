"""python-rtmidi transport for the IR Box (USB-MIDI SysEx).

This module only builds READ and QUERY frames itself; writes are passed in
through :meth:`IRBoxMIDI.transact` (by :mod:`irbox.device`, which restricts
them to an allowlist). Denylisted commands (erase 0x21) are refused here by
``guard_frame``.

Every ``00 59`` frame is sent as ``F0 <encode7(frame)> F7`` (see
``protocol.wrap_sysex``). Replies arrive on an rtmidi callback thread, are
reassembled into complete SysEx messages and handed to the caller through a
thread-safe queue; decoding happens in the caller's thread so errors surface
as exceptions there.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from pathlib import Path
from typing import TextIO

import rtmidi

from .protocol import (
    CMD_QUERY,
    CMD_READ,
    FORBIDDEN_COMMANDS,
    ForbiddenOperation,
    Frame,
    FrameParser,
    QueryReply,
    ReadResponse,
    SysExAssembler,
    build_query,
    build_read,
    decode7,
    guard_frame,
    iter_read_chunks,
    unwrap_sysex,
    wrap_sysex,
)

__all__ = [
    "CHUNK_TIMEOUT_S",
    "DEFAULT_PORT_MATCH",
    "FORBIDDEN_COMMANDS",
    "INTER_CHUNK_DELAY_S",
    "MIDI_READ_CHUNK",
    "ForbiddenOperation",
    "IRBoxMIDI",
    "guard_frame",
    "list_ports",
    "resolve_port",
]

log = logging.getLogger("irbox.midi")

DEFAULT_PORT_MATCH = "USB-Midi"
# Chosen by this toolkit (not taken from any app): small chunks keep each
# reply SysEx well under the 1024-byte WinMM/RtMidi input buffer.
MIDI_READ_CHUNK = 128
CHUNK_TIMEOUT_S = 2.0
INTER_CHUNK_DELAY_S = 0.02


def list_ports() -> tuple[list[str], list[str]]:
    """Return ``(input_names, output_names)`` as reported by rtmidi."""
    midi_in = rtmidi.MidiIn()
    midi_out = rtmidi.MidiOut()
    return list(midi_in.get_ports()), list(midi_out.get_ports())


def resolve_port(
    names: list[str], spec: str | None, default: str = DEFAULT_PORT_MATCH
) -> int:
    """Pick a port index from ``names``.

    ``spec`` may be a decimal index or a case-insensitive name substring; when
    None, the first port containing ``default`` is used.
    """
    if spec is not None and spec.strip().isdigit():
        idx = int(spec)
        if not 0 <= idx < len(names):
            raise RuntimeError(f"MIDI port index {idx} out of range; available: {names}")
        return idx
    pattern = default if spec is None else spec
    for i, name in enumerate(names):
        if pattern.lower() in name.lower():
            return i
    raise RuntimeError(f"no MIDI port matching {pattern!r}; available: {names}")


class IRBoxMIDI:
    def __init__(
        self,
        trace_path: str | Path | None = None,
        chunk: int = MIDI_READ_CHUNK,
        chunk_timeout: float = CHUNK_TIMEOUT_S,
        inter_chunk_delay: float = INTER_CHUNK_DELAY_S,
    ) -> None:
        if chunk <= 0:
            raise ValueError("chunk must be positive")
        self.chunk = chunk
        self.chunk_timeout = chunk_timeout
        self.inter_chunk_delay = inter_chunk_delay
        self.in_name: str | None = None
        self.out_name: str | None = None
        self._in: rtmidi.MidiIn | None = None
        self._out: rtmidi.MidiOut | None = None
        self._queue: queue.Queue[bytes] = queue.Queue()
        self._assembler = SysExAssembler()
        self._parser = FrameParser()
        self._trace_lock = threading.Lock()
        self._trace: TextIO | None = (
            open(trace_path, "a", encoding="utf-8") if trace_path else None
        )

    # -- tracing ---------------------------------------------------------
    def _log_trace(
        self,
        direction: str,
        sysex: bytes | None = None,
        decoded: bytes | None = None,
        note: str = "",
        midi: bytes | None = None,
    ) -> None:
        if self._trace is None:
            return
        rec: dict[str, object] = {"t": time.time(), "dir": direction}
        if sysex is not None:
            rec["sysex"] = sysex.hex()
        if decoded is not None:
            rec["decoded"] = decoded.hex()
        if midi is not None:
            rec["midi"] = midi.hex()
        if note:
            rec["note"] = note
        with self._trace_lock:
            self._trace.write(json.dumps(rec) + "\n")
            self._trace.flush()

    # -- connection ------------------------------------------------------
    def open(self, in_spec: str | None = None, out_spec: str | None = None) -> None:
        """Open input and output ports by index or name substring."""
        midi_in = rtmidi.MidiIn()
        midi_out = rtmidi.MidiOut()
        in_names = list(midi_in.get_ports())
        out_names = list(midi_out.get_ports())
        in_idx = resolve_port(in_names, in_spec)
        out_idx = resolve_port(out_names, out_spec)
        midi_in.ignore_types(sysex=False, timing=True, active_sense=True)
        midi_in.set_callback(self._on_message)
        midi_in.open_port(in_idx)
        self._in = midi_in
        midi_out.open_port(out_idx)
        self._out = midi_out
        self.in_name = in_names[in_idx]
        self.out_name = out_names[out_idx]
        log.info("MIDI in: %s, out: %s", self.in_name, self.out_name)
        self._log_trace("EVT", note=f"opened in={self.in_name!r} out={self.out_name!r}")

    def close(self) -> None:
        if self._in is not None:
            self._in.cancel_callback()
            self._in.close_port()
            self._in = None
        if self._out is not None:
            self._out.close_port()
            self._out = None
        if self._trace is not None:
            with self._trace_lock:
                self._trace.close()
                self._trace = None

    def __enter__(self) -> IRBoxMIDI:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def errors(self) -> list[str]:
        """Reassembly and frame-parsing problems seen so far."""
        return self._assembler.errors + self._parser.errors

    # -- I/O -------------------------------------------------------------
    def _on_message(self, event: tuple[list[int], float], _data: object = None) -> None:
        """rtmidi callback (runs on rtmidi's thread)."""
        data = bytes(event[0])
        if not data:
            return
        n_errors = len(self._assembler.errors)
        complete = self._assembler.feed(data)
        for err in self._assembler.errors[n_errors:]:
            log.warning("MIDI input: %s", err)
            self._log_trace("RX", note=err)
        if not complete and data[0] != 0xF0 and not self._assembler.in_progress:
            self._log_trace("RX", midi=data, note="non-SysEx message ignored")
        for msg in complete:
            # The assembler guarantees 7-bit content, so decode7 cannot fail.
            self._log_trace("RX", sysex=msg, decoded=decode7(msg[1:-1]))
            self._queue.put(msg)

    def _send(self, raw: bytes) -> None:
        guard_frame(raw)
        if self._out is None:
            raise RuntimeError("MIDI output not open")
        wire = wrap_sysex(raw)
        self._log_trace("TX", sysex=wire, decoded=raw)
        self._out.send_message(list(wire))

    def _drain(self) -> None:
        while True:
            try:
                msg = self._queue.get_nowait()
            except queue.Empty:
                break
            log.warning("discarding stale SysEx: %s", msg.hex())
        self._parser.reset()

    def transact(
        self, raw_frame: bytes, expect_cmd: int | None, timeout: float | None = None
    ) -> Frame:
        """Send one raw ``00 59`` frame and return the first matching reply.

        ``expect_cmd`` filters replies by command byte (None accepts any).
        Denylisted commands raise :class:`ForbiddenOperation` before sending.
        """
        guard_frame(raw_frame)
        limit = self.chunk_timeout if timeout is None else timeout
        self._drain()
        self._send(raw_frame)
        deadline = time.monotonic() + limit
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                msg = self._queue.get(timeout=remaining)
            except queue.Empty as e:
                detail = f"; errors: {self.errors[-3:]}" if self.errors else ""
                raise TimeoutError(
                    f"no reply to cmd 0x{raw_frame[2]:02x} within {limit} s{detail}"
                ) from e
            for frame in self._parser.feed(unwrap_sysex(msg)):
                if expect_cmd is None or frame.raw[2] == expect_cmd:
                    return frame
                log.warning("ignoring frame: %s", frame.raw.hex())

    def query(self, timeout: float | None = None) -> QueryReply:
        """Send the 0x11 query and return the parsed reply."""
        frame = self.transact(build_query(), CMD_QUERY, timeout)
        if not isinstance(frame, QueryReply):
            raise RuntimeError(f"unexpected reply to query: {frame.raw.hex()}")
        return frame

    def read(
        self,
        type_: int,
        addr: int,
        length: int,
        chunk: int | None = None,
        timeout: float | None = None,
        delay: float | None = None,
    ) -> bytes:
        """Chunked READ (0x23); every chunk echo is verified."""
        size = self.chunk if chunk is None else chunk
        if size <= 0:
            raise ValueError("chunk must be positive")
        pause = self.inter_chunk_delay if delay is None else delay
        out = bytearray()
        for i, (c_addr, c_len) in enumerate(iter_read_chunks(addr, length, size)):
            if i:
                time.sleep(pause)
            frame = self.transact(build_read(type_, c_addr, c_len), CMD_READ, timeout)
            if not isinstance(frame, ReadResponse):
                raise RuntimeError(f"unexpected reply to read: {frame.raw.hex()}")
            if (frame.type, frame.addr, frame.length) != (type_, c_addr, c_len):
                raise RuntimeError(
                    f"read echo mismatch: got type={frame.type} "
                    f"addr=0x{frame.addr:x} len={frame.length}"
                )
            out += frame.data
        return bytes(out)
