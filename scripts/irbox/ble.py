"""bleak-based transport for the IR Box. Read-only by policy."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic

from .protocol import (
    FORBIDDEN_COMMANDS,
    NOTIFY_CHAR_UUID,
    READ_CHUNK,
    WRITE_CHAR_UUID,
    ForbiddenOperation,
    Frame,
    FrameParser,
    ReadResponse,
    build_read,
    guard_frame,
    iter_read_chunks,
)

__all__ = [
    "FORBIDDEN_COMMANDS",
    "FORBIDDEN_UUID_PREFIXES",
    "ForbiddenOperation",
    "IRBoxBLE",
    "ScanResult",
    "guard_frame",
    "guard_uuid",
    "scan",
]

log = logging.getLogger("irbox.ble")

# Safety denylist: GATT characteristics this toolkit must never use. The
# command denylist (FORBIDDEN_COMMANDS / guard_frame) lives in protocol.py and
# is shared with the MIDI transport.
FORBIDDEN_UUID_PREFIXES = ("0000ae00", "0000ae01", "0000ae02")

CHUNK_TIMEOUT_S = 3.0
INTER_CHUNK_DELAY_S = 0.2
NOTIFY_SETTLE_S = 3.0


def guard_uuid(uuid: str) -> None:
    if uuid.lower().startswith(FORBIDDEN_UUID_PREFIXES):
        raise ForbiddenOperation(f"refusing to touch characteristic {uuid}")


@dataclass
class ScanResult:
    name: str | None
    address: str
    rssi: int | None
    service_uuids: list[str] = field(default_factory=list)


async def scan(timeout: float = 5.0, show_all: bool = False) -> list[ScanResult]:
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    results: list[ScanResult] = []
    for device, adv in found.values():
        name = device.name or adv.local_name
        if not show_all and not (name and "ir-box" in name.lower()):
            continue
        results.append(
            ScanResult(name, device.address, adv.rssi, list(adv.service_uuids))
        )
    results.sort(key=lambda r: -(r.rssi if r.rssi is not None else -999))
    return results


class IRBoxBLE:
    def __init__(self, trace_path: str | Path | None = None) -> None:
        self._client: BleakClient | None = None
        self._parser = FrameParser()
        self._queue: asyncio.Queue[Frame] = asyncio.Queue()
        self._trace = (
            open(trace_path, "a", encoding="utf-8") if trace_path else None
        )
        self._with_response = True

    # -- tracing ---------------------------------------------------------
    def _log_trace(self, direction: str, data: bytes | None, note: str = "") -> None:
        if self._trace is None:
            return
        rec: dict[str, object] = {"t": time.time(), "dir": direction}
        if data is not None:
            rec["hex"] = data.hex()
        if note:
            rec["note"] = note
        self._trace.write(json.dumps(rec) + "\n")
        self._trace.flush()

    # -- connection ------------------------------------------------------
    async def connect(
        self,
        address: str | None = None,
        name: str | None = None,
        scan_timeout: float = 5.0,
    ) -> None:
        if address is None:
            matches = await scan(scan_timeout)
            if name:
                matches = [m for m in matches if m.name and name.lower() in m.name.lower()]
            if not matches:
                raise RuntimeError("no matching IR-BOX device found")
            address = matches[0].address
        client = BleakClient(address)
        await client.connect()
        self._client = client
        mtu = getattr(client, "mtu_size", None)
        log.info("connected to %s, MTU=%s", address, mtu)
        self._log_trace("EVT", None, f"connected {address} mtu={mtu}")

        guard_uuid(WRITE_CHAR_UUID)
        guard_uuid(NOTIFY_CHAR_UUID)
        char = client.services.get_characteristic(WRITE_CHAR_UUID)
        if char is None:
            raise RuntimeError("write characteristic ae41 not found")
        self._with_response = not self._supports_no_response(char)
        mode = "with response" if self._with_response else "without response"
        log.info("writing %s", mode)
        self._log_trace("EVT", None, f"write mode: {mode}")

        await client.start_notify(NOTIFY_CHAR_UUID, self._on_notify)
        await asyncio.sleep(NOTIFY_SETTLE_S)

    @staticmethod
    def _supports_no_response(char: BleakGATTCharacteristic) -> bool:
        return "write-without-response" in char.properties

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
            self._client = None
        if self._trace is not None:
            self._trace.close()
            self._trace = None

    async def __aenter__(self) -> IRBoxBLE:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.disconnect()

    # -- I/O -------------------------------------------------------------
    def _on_notify(self, _char: object, data: bytearray) -> None:
        raw = bytes(data)
        self._log_trace("RX", raw)
        for frame in self._parser.feed(raw):
            self._queue.put_nowait(frame)

    async def _send(self, frame: bytes) -> None:
        guard_frame(frame)
        if self._client is None:
            raise RuntimeError("not connected")
        self._log_trace("TX", frame)
        await self._client.write_gatt_char(
            WRITE_CHAR_UUID, frame, response=self._with_response
        )

    async def read(
        self, type_: int, addr: int, length: int, chunk: int = READ_CHUNK
    ) -> bytes:
        if chunk <= 0:
            raise ValueError("chunk must be positive")
        out = bytearray()
        first = True
        for c_addr, c_len in iter_read_chunks(addr, length, chunk):
            if not first:
                await asyncio.sleep(INTER_CHUNK_DELAY_S)
            first = False
            out += await self._read_chunk(type_, c_addr, c_len)
        return bytes(out)

    async def _read_chunk(self, type_: int, addr: int, length: int) -> bytes:
        while not self._queue.empty():
            self._queue.get_nowait()
        await self._send(build_read(type_, addr, length))
        try:
            async with asyncio.timeout(CHUNK_TIMEOUT_S):
                while True:
                    frame = await self._queue.get()
                    if not isinstance(frame, ReadResponse):
                        log.warning("ignoring non-read frame: %s", frame.raw.hex())
                        continue
                    if (frame.type, frame.addr, frame.length) != (type_, addr, length):
                        raise RuntimeError(
                            f"read echo mismatch: got type={frame.type} "
                            f"addr=0x{frame.addr:x} len={frame.length}"
                        )
                    return frame.data
        except TimeoutError as e:
            raise TimeoutError(
                f"no read response for type={type_} addr=0x{addr:x} len={length}"
            ) from e
