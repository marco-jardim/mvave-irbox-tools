"""High-level synchronous IR Box editor on top of a frame transport (USB-MIDI).

All slot arguments here are 0-based protocol indices (0..31); the pedal
display (and the CLI) shows index + 1.

Memory model (verified on firmware ``IR-BOX_010``):

* type 5 addresses 0..8191 are a RAM *working copy* of the active preset.
  Writes there are audible only after a refresh (``0xA0000000``: 1 = IR,
  cab_on, eq_on; 2 = EQ band parameters; 3 = volume) and are discarded when
  another slot is selected or the pedal is power-cycled, unless saved.
* type 4 ``0xE0000000 + i`` selects slot ``i``; ``0x20000000`` reads the
  active slot (1-byte reads only).
* type 5 ``0xF0000000 + i`` with data ``[0]`` saves the whole working copy to
  flash slot ``i``.
* type 5 ``0x80000000`` is the 544-byte name table. It ignores the address
  offset, so it must be read in a single request.

Writes are restricted to an allowlist (:func:`check_write_target`); the
erase command 0x21 is refused by the transport's ``guard_frame``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .model import (
    CAB_ON_OFFSET,
    EQ_END,
    EQ_LEN,
    EQ_OFFSET,
    EQ_ON_OFFSET,
    HEADER_LEN,
    IR_BLOCK_LEN,
    IR_BLOCK_OFFSET,
    IR_END,
    LEVEL_OFFSET,
    NAME_STRIDE,
    NAME_TABLE_LEN,
    PRESET_LEN,
    VOLUME_MAX,
    build_header,
    build_ir_block,
    check_preset_block,
    encode_name,
    name_from_field,
    parse_name_table,
    preset_range_diffs,
    preset_ranges_equal,
    update_eq_block,
)
from .protocol import (
    CMD_ACK,
    TYPE_DEV,
    TYPE_USR,
    WRITE_CHUNK,
    ForbiddenOperation,
    Frame,
    QueryReply,
    WriteAck,
    build_write,
    iter_write_chunks,
)

__all__ = [
    "ADDR_CURRENT_SLOT",
    "ADDR_NAME_TABLE",
    "ADDR_REFRESH",
    "ADDR_SAVE_BASE",
    "ADDR_SELECT_BASE",
    "DEFAULT_READ_CHUNK",
    "MAX_READ_CHUNK",
    "NUM_SLOTS",
    "REFRESH_EQ",
    "REFRESH_IR",
    "REFRESH_VOLUME",
    "ActiveState",
    "BackupResult",
    "DeviceError",
    "IRBox",
    "Transport",
    "check_slot",
    "check_write_target",
    "load_backup",
]

log = logging.getLogger("irbox.device")

NUM_SLOTS = 32

ADDR_CURRENT_SLOT = 0x20000000  # type 4, 1-byte reads only
ADDR_SELECT_BASE = 0xE0000000  # type 4, + slot index, data [index]
ADDR_NAME_TABLE = 0x80000000  # type 5, 544 bytes, one request
ADDR_REFRESH = 0xA0000000  # type 5, data [mode]
ADDR_SAVE_BASE = 0xF0000000  # type 5, + slot index, data [0]

REFRESH_IR = 1  # IR block, cab_on, eq_on
REFRESH_EQ = 2  # EQ band parameters and enable bits
REFRESH_VOLUME = 3
REFRESH_MODES = (REFRESH_IR, REFRESH_EQ, REFRESH_VOLUME)

DEFAULT_READ_CHUNK = 1000  # verified to work over USB-MIDI
MAX_READ_CHUNK = 1000
SELECT_SETTLE_S = 0.2
SAVE_SETTLE_S = 0.5

BACKUP_FORMAT = "irbox-backup"
BACKUP_VERSION = 1


class DeviceError(RuntimeError):
    """The device answered with an error or with data that failed verification."""


class Transport(Protocol):
    """What :class:`IRBox` needs from a transport (``midi.IRBoxMIDI`` fits)."""

    def read(
        self,
        type_: int,
        addr: int,
        length: int,
        chunk: int | None = None,
        timeout: float | None = None,
        delay: float | None = None,
    ) -> bytes: ...

    def transact(
        self, raw_frame: bytes, expect_cmd: int | None, timeout: float | None = None
    ) -> Frame: ...

    def query(self, timeout: float | None = None) -> QueryReply: ...


def check_slot(slot: int) -> None:
    """Raise ValueError unless ``slot`` is a 0-based slot index (0..31)."""
    if isinstance(slot, bool) or not isinstance(slot, int) or not 0 <= slot < NUM_SLOTS:
        raise ValueError(f"slot index must be 0..{NUM_SLOTS - 1}, got {slot!r}")


def check_write_target(type_: int, addr: int, data: bytes) -> None:
    """Refuse any write outside the verified, non-destructive targets."""
    if not data:
        raise ValueError("refusing an empty write")
    n = len(data)
    if type_ == TYPE_DEV:
        idx = addr - ADDR_SELECT_BASE
        if 0 <= idx < NUM_SLOTS and data == bytes([idx]):
            return
    elif type_ == TYPE_USR:
        if 0 <= addr and addr + n <= PRESET_LEN:
            return
        if addr == ADDR_REFRESH and n == 1 and data[0] in REFRESH_MODES:
            return
        if 0 <= addr - ADDR_SAVE_BASE < NUM_SLOTS and data == b"\x00":
            return
    raise ForbiddenOperation(
        f"refusing to write {n} byte(s) to type {type_} address 0x{addr:08X}: not an allowed target"
    )


@dataclass(frozen=True)
class ActiveState:
    """Active slot and a snapshot of its working copy."""

    slot: int
    working: bytes


@dataclass(frozen=True)
class BackupResult:
    path: Path
    active_slot: int
    names: list[str | None]
    files: list[Path] = field(default_factory=list)


def _slot_file(slot: int) -> str:
    return f"slot_{slot:02d}.bin"


def load_backup(src: str | Path) -> dict[int, bytes]:
    """Load and validate ``slot_NN.bin`` files (0-based NN) from a backup directory.

    Checksums are verified against ``manifest.json`` when present. Backups
    made by ``scripts/probe.py backup`` (no manifest) are accepted too.
    """
    d = Path(src)
    if not d.is_dir():
        raise FileNotFoundError(f"backup directory {d} does not exist")
    sums: dict[int, str] = {}
    manifest = d / "manifest.json"
    if manifest.exists():
        try:
            meta = json.loads(manifest.read_text(encoding="utf-8"))
        except ValueError as e:
            raise ValueError(f"{manifest}: invalid JSON: {e}") from e
        if not isinstance(meta, dict) or meta.get("format") != BACKUP_FORMAT:
            raise ValueError(f"{manifest}: not an {BACKUP_FORMAT} manifest")
        try:
            for entry in meta.get("slots", []):
                sums[int(entry["index"])] = str(entry["sha256"])
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"{manifest}: malformed slot entry: {e!r}") from e
    blocks: dict[int, bytes] = {}
    for slot in range(NUM_SLOTS):
        path = d / _slot_file(slot)
        if not path.exists():
            continue
        data = path.read_bytes()
        try:
            check_preset_block(data)
        except ValueError as e:
            raise ValueError(f"{path}: {e}") from e
        want = sums.get(slot)
        if want is not None and hashlib.sha256(data).hexdigest() != want:
            raise ValueError(f"{path}: SHA-256 does not match manifest.json")
        blocks[slot] = data
    if not blocks:
        raise ValueError(f"no slot_NN.bin files found in {d}")
    return blocks


class IRBox:
    """Synchronous IR Box editor. Slot arguments are 0-based (0..31)."""

    def __init__(
        self,
        transport: Transport,
        *,
        read_chunk: int = DEFAULT_READ_CHUNK,
        timeout: float | None = None,
        select_settle: float = SELECT_SETTLE_S,
        save_settle: float = SAVE_SETTLE_S,
        sleep: Callable[[float], None] = time.sleep,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        if not 1 <= read_chunk <= MAX_READ_CHUNK:
            raise ValueError(f"read chunk must be 1..{MAX_READ_CHUNK} bytes, got {read_chunk}")
        self._t = transport
        self.read_chunk = read_chunk
        self.timeout = timeout
        self.select_settle = select_settle
        self.save_settle = save_settle
        self._sleep = sleep
        self._progress = progress

    def _note(self, msg: str) -> None:
        log.debug(msg)
        if self._progress is not None:
            self._progress(msg)

    # -- reads -----------------------------------------------------------
    def device_name(self) -> str:
        return self._t.query(self.timeout).name

    def active_slot(self) -> int:
        """0-based index of the active slot."""
        data = self._t.read(TYPE_DEV, ADDR_CURRENT_SLOT, 1, chunk=1, timeout=self.timeout)
        if len(data) != 1:
            raise DeviceError(f"active-slot read returned {len(data)} bytes, expected 1")
        if data[0] >= NUM_SLOTS:
            raise DeviceError(f"device reported implausible active slot index {data[0]}")
        return data[0]

    def read_name_table(self) -> bytes:
        """The raw 544-byte name table, read in ONE request (offsets are ignored)."""
        data = self._t.read(
            TYPE_USR, ADDR_NAME_TABLE, NAME_TABLE_LEN, chunk=NAME_TABLE_LEN, timeout=self.timeout
        )
        if len(data) != NAME_TABLE_LEN:
            raise DeviceError(f"name table read returned {len(data)} bytes, expected {NAME_TABLE_LEN}")
        return data

    def names(self) -> list[str | None]:
        """Stored (flash) names of all 32 slots, index 0..31."""
        return parse_name_table(self.read_name_table())

    def read_range(self, addr: int, length: int) -> bytes:
        """Read part of the working copy (type 5, 0..8191)."""
        if addr < 0 or length <= 0 or addr + length > PRESET_LEN:
            raise ValueError(f"range 0x{addr:X}+{length} is outside the {PRESET_LEN}-byte working copy")
        data = self._t.read(TYPE_USR, addr, length, chunk=self.read_chunk, timeout=self.timeout)
        if len(data) != length:
            raise DeviceError(f"read at 0x{addr:X} returned {len(data)} bytes, expected {length}")
        return data

    def read_preset(self) -> bytes:
        """The whole 8192-byte working copy of the active slot."""
        return self.read_range(0, PRESET_LEN)

    def read_header(self) -> bytes:
        return self.read_range(0, HEADER_LEN)

    def read_eq(self) -> bytes:
        return self.read_range(EQ_OFFSET, EQ_LEN)

    def volume(self) -> int:
        return self.read_range(LEVEL_OFFSET, 1)[0]

    # -- writes ----------------------------------------------------------
    def _write_frame(self, type_: int, addr: int, data: bytes) -> None:
        frame = self._t.transact(build_write(type_, addr, data), CMD_ACK, self.timeout)
        if not isinstance(frame, WriteAck):
            raise DeviceError(f"unexpected reply to write at 0x{addr:08X}: {frame.raw.hex(' ')}")
        if not frame.checksum_ok:
            raise DeviceError(f"write ack checksum mismatch at 0x{addr:08X}: {frame.raw.hex(' ')}")
        if frame.status != 0:
            raise DeviceError(
                f"device rejected write of {len(data)} byte(s) to type {type_} address "
                f"0x{addr:08X} (status {frame.status}): {frame.raw.hex(' ')}"
            )

    def write(self, type_: int, addr: int, data: bytes) -> int:
        """Chunked write (173 bytes per frame); every chunk must be acked with status 0.

        Returns the number of frames sent.
        """
        payload = bytes(data)
        check_write_target(type_, addr, payload)
        n = 0
        for c_addr, part in iter_write_chunks(addr, payload, WRITE_CHUNK):
            self._write_frame(type_, c_addr, part)
            n += 1
        return n

    def refresh(self, mode: int) -> None:
        """Apply working-copy changes: 1 = IR/cab_on/eq_on, 2 = EQ bands, 3 = volume."""
        if mode not in REFRESH_MODES:
            raise ValueError(f"refresh mode must be one of {REFRESH_MODES}, got {mode!r}")
        self.write(TYPE_USR, ADDR_REFRESH, bytes([mode]))

    def select(self, slot: int) -> None:
        """Make ``slot`` active (loads it from flash) and verify via readback."""
        check_slot(slot)
        self.write(TYPE_DEV, ADDR_SELECT_BASE + slot, bytes([slot]))
        self._sleep(self.select_settle)
        got = self.active_slot()
        if got != slot:
            raise DeviceError(f"selected slot {slot + 1} but the device reports slot {got + 1}")

    def reload(self, slot: int) -> None:
        """Reload ``slot`` from flash by selecting a neighbor and then ``slot``."""
        check_slot(slot)
        self.select((slot + 1) % NUM_SLOTS)
        self.select(slot)

    def set_volume(self, value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= VOLUME_MAX:
            raise ValueError(f"volume must be an integer 0..{VOLUME_MAX}, got {value!r}")
        self.write(TYPE_USR, LEVEL_OFFSET, bytes([value]))
        self.refresh(REFRESH_VOLUME)

    def set_cab(self, on: bool) -> None:
        self.write(TYPE_USR, CAB_ON_OFFSET, bytes([1 if on else 0]))
        self.refresh(REFRESH_IR)

    def set_eq(self, on: bool) -> None:
        self.write(TYPE_USR, EQ_ON_OFFSET, bytes([1 if on else 0]))
        self.refresh(REFRESH_IR)

    def set_eq_band(
        self,
        band: str,
        *,
        freq: int | None = None,
        gain_db: float | None = None,
        q: float | None = None,
        enable: bool | None = None,
    ) -> bytes:
        """Change one EQ band in the working copy; returns the new 40-byte EQ block."""
        if freq is None and gain_db is None and q is None and enable is None:
            raise ValueError("nothing to change: give a frequency, gain, Q or enable/disable")
        current = self.read_eq()
        new = update_eq_block(current, band, freq=freq, gain_db=gain_db, q=q, enable=enable)
        self.write(TYPE_USR, EQ_OFFSET, new)
        self.refresh(REFRESH_EQ)
        return new

    def load_ir(self, samples24: Sequence[int], name14: bytes | None = None) -> bytes:
        """Write a 2048-sample IR into the working copy (0x18) and apply it (refresh 1).

        The level byte inside the block is set to the current volume. Returns
        the 6164-byte block after verifying it by readback. Nothing is saved.
        """
        block = build_ir_block(samples24, self.volume(), name14)
        self.write(TYPE_USR, IR_BLOCK_OFFSET, block)
        self.refresh(REFRESH_IR)
        back = self.read_range(IR_BLOCK_OFFSET, IR_BLOCK_LEN)
        if back != block:
            bad = sum(1 for x, y in zip(back, block) if x != y)
            raise DeviceError(f"IR readback differs from the upload in {bad} byte(s)")
        return block

    def apply_working_copy(self, block: bytes) -> None:
        """Write header, IR block and EQ of ``block`` to the working copy and apply them.

        Does not save. Verified by readback.
        """
        check_preset_block(block)
        self.write(TYPE_USR, 0, block[0:HEADER_LEN])
        self.write(TYPE_USR, IR_BLOCK_OFFSET, block[IR_BLOCK_OFFSET:IR_END])
        self.write(TYPE_USR, EQ_OFFSET, block[EQ_OFFSET:EQ_END])
        for mode in REFRESH_MODES:
            self.refresh(mode)
        diffs = preset_range_diffs(block, self.read_preset())
        if diffs:
            raise DeviceError(
                f"working-copy readback differs in {len(diffs)} byte(s), first offsets {diffs[:8]}"
            )

    # -- flash -----------------------------------------------------------
    def save(self, slot: int, name: str | None = None, verify: bool = True) -> bytes:
        """Save the working copy (header, IR, EQ) of the active ``slot`` to flash.

        Writes the 24-byte header (with ``name`` if given) then the save word,
        and checks the name table. With ``verify`` the slot is reloaded from
        flash and compared with what was saved. Returns the saved block.
        """
        check_slot(slot)
        name_field = encode_name(name) if name is not None else None
        active = self.active_slot()
        if active != slot:
            raise DeviceError(f"refusing to save: slot {slot + 1} is not active (slot {active + 1} is)")
        head = self.read_header()
        if name_field is None:
            name_field = head[:NAME_STRIDE]
        header = build_header(name_field, head[CAB_ON_OFFSET], head[EQ_ON_OFFSET])
        self.write(TYPE_USR, 0, header)
        expected = self.read_preset()
        if expected[:HEADER_LEN] != header:
            raise DeviceError("header readback differs from what was written; not saving")
        self._note(f"saving slot {slot + 1} to flash")
        self.write(TYPE_USR, ADDR_SAVE_BASE + slot, b"\x00")
        self._sleep(self.save_settle)
        want = name_from_field(name_field)
        stored = self.names()[slot]
        if stored != want:
            raise DeviceError(f"name table shows {stored!r} for slot {slot + 1}, expected {want!r}")
        if verify:
            self.verify_slot(slot, expected)
        return expected

    def verify_slot(self, slot: int, expected: bytes) -> None:
        """Reload ``slot`` from flash and compare header, IR and EQ with ``expected``."""
        self.reload(slot)
        diffs = preset_range_diffs(expected, self.read_preset())
        if diffs:
            raise DeviceError(
                f"slot {slot + 1} differs from the saved data after reload in {len(diffs)} "
                f"byte(s), first offsets {diffs[:8]}"
            )
        self._note(f"verified slot {slot + 1} after reload")

    @contextmanager
    def preserving_active(self) -> Iterator[ActiveState]:
        """Restore the active slot afterwards, including unsaved working-copy edits.

        On normal exit the original slot is re-selected and, if its working
        copy had unsaved changes, they are written back (not saved).
        """
        state = ActiveState(self.active_slot(), self.read_preset())
        try:
            yield state
        finally:
            self.select(state.slot)
        if not preset_ranges_equal(state.working, self.read_preset()):
            self._note(f"re-applying unsaved working-copy changes of slot {state.slot + 1}")
            self.apply_working_copy(state.working)

    def read_slot(self, slot: int) -> bytes:
        """Preset block of ``slot``. For the active slot this is its working copy."""
        check_slot(slot)
        if self.active_slot() == slot:
            return self.read_preset()
        with self.preserving_active():
            self.select(slot)
            return self.read_preset()

    def backup(self, out_dir: str | Path) -> BackupResult:
        """Read all 32 slots from flash into ``out_dir``.

        Writes ``slot_00.bin`` .. ``slot_31.bin`` (0-based index), ``names.bin``,
        ``working_copy.bin`` (active slot before the backup) and
        ``manifest.json``. The active slot and its unsaved edits are restored.
        """
        out = Path(out_dir)
        if out.exists() and (any(out.glob("slot_*.bin")) or (out / "manifest.json").exists()):
            raise FileExistsError(f"{out} already contains a backup; choose another directory")
        out.mkdir(parents=True, exist_ok=True)
        device = self.device_name()
        names_raw = self.read_name_table()
        (out / "names.bin").write_bytes(names_raw)
        files = [out / "names.bin"]
        entries: list[dict[str, object]] = []
        with self.preserving_active() as state:
            (out / "working_copy.bin").write_bytes(state.working)
            files.append(out / "working_copy.bin")
            for slot in range(NUM_SLOTS):
                self.select(slot)
                block = self.read_preset()
                path = out / _slot_file(slot)
                path.write_bytes(block)
                files.append(path)
                name = name_from_field(block[:NAME_STRIDE])
                entries.append(
                    {
                        "index": slot,
                        "slot": slot + 1,
                        "file": path.name,
                        "name": name,
                        "sha256": hashlib.sha256(block).hexdigest(),
                    }
                )
                self._note(f"backed up slot {slot + 1:2d}: {name or '-'}")
            manifest = {
                "format": BACKUP_FORMAT,
                "version": BACKUP_VERSION,
                "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "device": device,
                "slot_numbering": "slot_NN.bin uses the 0-based protocol index; display slot = index + 1",
                "active_index": state.slot,
                "active_slot": state.slot + 1,
                "names_file": "names.bin",
                "working_copy_file": "working_copy.bin",
                "slots": entries,
            }
            (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            files.append(out / "manifest.json")
        return BackupResult(out, state.slot, parse_name_table(names_raw), files)

    def restore(self, src: str | Path, slots: Iterable[int] | None = None) -> list[int]:
        """Write backed-up slots back to flash; returns the restored indices.

        For each slot: select, write header, IR block and EQ, refresh 1/2/3,
        save, then verify by reload + readback. The originally active slot is
        re-selected at the end.
        """
        blocks = load_backup(src)
        targets = sorted(blocks) if slots is None else sorted(set(slots))
        for slot in targets:
            check_slot(slot)
        missing = [s + 1 for s in targets if s not in blocks]
        if missing:
            raise ValueError(f"backup {src} has no data for slot(s) {missing}")
        original = self.active_slot()
        try:
            for slot in targets:
                self._note(f"restoring slot {slot + 1}: {name_from_field(blocks[slot][:NAME_STRIDE]) or '-'}")
                self.select(slot)
                self.apply_working_copy(blocks[slot])
                self.save(slot, None, verify=True)
        finally:
            self.select(original)
        return targets

    def upload_ir(
        self,
        slot: int,
        samples24: Sequence[int],
        name: str | None = None,
        cab_name: bytes | None = None,
    ) -> bytes:
        """Load an IR into ``slot`` (from its flash state), turn the cab on and save.

        ``slot`` stays active afterwards. Returns the saved block.
        """
        check_slot(slot)
        if name is not None:
            encode_name(name)
        build_ir_block(samples24, 0, cab_name)  # validate before touching the device
        if self.active_slot() == slot:
            self.reload(slot)
        else:
            self.select(slot)
        self.load_ir(samples24, cab_name)
        self.set_cab(True)
        return self.save(slot, name, verify=True)

    def rename(self, slot: int, name: str) -> None:
        """Rename ``slot`` in flash, keeping its stored IR and EQ.

        If ``slot`` is active, its unsaved working-copy changes are discarded;
        otherwise the active slot (and its unsaved changes) is restored.
        """
        check_slot(slot)
        encode_name(name)
        if self.active_slot() == slot:
            self.reload(slot)
            self.save(slot, name, verify=True)
            return
        with self.preserving_active():
            self.select(slot)
            self.save(slot, name, verify=True)
