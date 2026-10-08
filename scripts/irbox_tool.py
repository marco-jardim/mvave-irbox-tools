"""M-VAVE IR Box editor over USB-MIDI.

Slot numbers are 1..32, as shown on the pedal display. The protocol uses
0-based indices (0..31) internally; this tool converts for you.

Commands by effect:
  read-only      midi-ports, scan (BLE), query, info, dump, backup, ir-export
  working copy   select, volume, cab, eq, eq-band, ir-load
                 (changes are audible at once but NOT saved: they are lost when
                 another slot is selected or the pedal is power-cycled)
  flash          save, rename, ir-upload, restore
                 (persistent; require --yes, otherwise the plan is printed and
                 the tool exits with status 2; an automatic backup of all 32
                 slots is written to backups/<timestamp>/ unless --no-backup)

The erase command and the BLE OTA characteristics are never used, and no
command sends arbitrary frames.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from irbox import ble, midi
from irbox.device import (
    ADDR_CURRENT_SLOT,
    ADDR_NAME_TABLE,
    DEFAULT_READ_CHUNK,
    MAX_READ_CHUNK,
    NUM_SLOTS,
    IRBox,
    load_backup,
)
from irbox.ir_convert import (
    CHANNEL_MODES,
    DEFAULT_FADE,
    IR_DURATION_MS,
    NORMALIZE_MODES,
    ConvertResult,
    convert,
    export_wav,
    samples_from_preset,
)
from irbox.model import (
    CAB_ON_OFFSET,
    EQ_BANDS,
    EQ_END,
    EQ_FREQ_MAX,
    EQ_FREQ_MIN,
    EQ_GAIN_MAX_DB,
    EQ_OFFSET,
    EQ_ON_OFFSET,
    EQ_Q_MAX,
    EQ_Q_MIN,
    IR_RATE,
    IR_SAMPLES,
    NAME_MAX_CHARS,
    NAME_STRIDE,
    NAME_TABLE_LEN,
    PRESET_LEN,
    VOLUME_MAX,
    EqBandState,
    describe_eq_band,
    encode_name,
    name_from_field,
    parse_name_table,
    parse_preset,
    preset_to_dict,
)
from irbox.protocol import TYPE_DEV, TYPE_USR

log = logging.getLogger("irbox_tool")

BACKUP_ROOT = Path("backups")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2

SLOT_HELP = "slot number 1-32 as shown on the pedal display (protocol index = N-1)"


class UsageError(Exception):
    """Invalid combination of options; reported with exit status 2."""


# -- argument types ------------------------------------------------------------


def slot_arg(text: str) -> int:
    """Parse a display slot number 1..32 and return the 0-based protocol index."""
    try:
        n = int(text, 10)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"invalid slot {text!r}; use 1-{NUM_SLOTS}") from e
    if not 1 <= n <= NUM_SLOTS:
        raise argparse.ArgumentTypeError(
            f"slot {n} is out of range; use 1-{NUM_SLOTS} as shown on the pedal display"
        )
    return n - 1


def display_slot(index: int) -> int:
    """0-based protocol index -> slot number shown on the pedal."""
    return index + 1


def _int_range(lo: int, hi: int, what: str) -> Callable[[str], int]:
    def parse(text: str) -> int:
        try:
            v = int(text, 10)
        except ValueError as e:
            raise argparse.ArgumentTypeError(f"invalid {what} {text!r}; expected an integer") from e
        if not lo <= v <= hi:
            raise argparse.ArgumentTypeError(f"{what} {v} is out of range {lo}..{hi}")
        return v

    return parse


def _float_range(lo: float, hi: float, what: str) -> Callable[[str], float]:
    def parse(text: str) -> float:
        try:
            v = float(text)
        except ValueError as e:
            raise argparse.ArgumentTypeError(f"invalid {what} {text!r}; expected a number") from e
        if not math.isfinite(v) or not lo <= v <= hi:
            raise argparse.ArgumentTypeError(f"{what} {text} is out of range {lo}..{hi}")
        return v

    return parse


def _finite_float(text: str) -> float:
    try:
        v = float(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"invalid number {text!r}") from e
    if not math.isfinite(v):
        raise argparse.ArgumentTypeError(f"{text!r} is not a finite number")
    return v


def name_arg(text: str) -> str:
    try:
        encode_name(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e
    return text


volume_arg = _int_range(0, VOLUME_MAX, "volume")
freq_arg = _int_range(EQ_FREQ_MIN, EQ_FREQ_MAX, "frequency (Hz)")
gain_arg = _float_range(-EQ_GAIN_MAX_DB, EQ_GAIN_MAX_DB, "gain (dB)")
q_arg = _float_range(EQ_Q_MIN, EQ_Q_MAX, "Q")
fade_arg = _int_range(0, IR_SAMPLES, "fade length (samples)")
chunk_arg = _int_range(1, MAX_READ_CHUNK, "chunk size")


# -- parser --------------------------------------------------------------------


def _add_flash_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--yes", action="store_true", help="really write flash (without it: print the plan, exit 2)")
    p.add_argument(
        "--no-backup",
        action="store_true",
        help=f"skip the automatic backup of all {NUM_SLOTS} slots to {BACKUP_ROOT}/<timestamp>/",
    )


def _add_convert_options(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group(
        "conversion",
        f"the IR is resampled to {IR_RATE} Hz and cut/zero-padded to {IR_SAMPLES} samples "
        f"({IR_DURATION_MS:.1f} ms), signed 24-bit",
    )
    g.add_argument("--channel", choices=CHANNEL_MODES, default="mix", help="channel to use (default: mix = average)")
    g.add_argument(
        "--normalize", choices=NORMALIZE_MODES, default="peak", help="peak-normalize the IR (default: peak)"
    )
    g.add_argument(
        "--peak-db",
        type=_finite_float,
        default=0.0,
        help="peak level for --normalize peak, in dBFS (default 0.0 = full scale, like the factory IRs)",
    )
    g.add_argument("--gain-db", type=_finite_float, default=0.0, help="extra gain after normalization (default 0)")
    g.add_argument(
        "--fade",
        type=fade_arg,
        default=DEFAULT_FADE,
        help=f"raised-cosine fade-out length in samples, applied only if the IR was cut (default {DEFAULT_FADE})",
    )
    g.add_argument(
        "--trim-silence", action="store_true", help="remove leading samples more than 60 dB below the peak"
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="irbox_tool",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--transport",
        choices=("midi", "ble"),
        default="midi",
        help="transport (default: midi; ble supports only info and dump and is untested)",
    )
    p.add_argument(
        "--midi-in",
        help=f"MIDI input port index or name substring (default: first '{midi.DEFAULT_PORT_MATCH}')",
    )
    p.add_argument(
        "--midi-out",
        help=f"MIDI output port index or name substring (default: first '{midi.DEFAULT_PORT_MATCH}')",
    )
    p.add_argument(
        "--chunk",
        type=chunk_arg,
        help=f"read chunk size in bytes, 1-{MAX_READ_CHUNK} (default {DEFAULT_READ_CHUNK}); "
        f"the name table is always read in one {NAME_TABLE_LEN}-byte request",
    )
    p.add_argument("--address", help="BLE address of the device (ble transport)")
    p.add_argument("--name", dest="ble_name", help="match BLE device by (partial) name (ble transport)")
    p.add_argument("--trace", help="append raw TX/RX frames to this JSONL file")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    # read-only
    sub.add_parser("midi-ports", help="[read-only] list MIDI input and output ports")

    s = sub.add_parser("scan", help="[read-only] scan for IR-BOX devices over BLE")
    s.add_argument("--all", action="store_true", help="list every advertiser")
    s.add_argument("--timeout", type=float, default=5.0)

    q = sub.add_parser("query", help="[read-only] send the 0x11 device query")
    q.add_argument("--timeout", type=float, default=midi.CHUNK_TIMEOUT_S)

    sub.add_parser("info", help="[read-only] active slot, its working copy settings and all slot names")

    d = sub.add_parser("dump", help="[read-only] dump the active slot's working copy to a directory")
    d.add_argument("--out", help="output directory (default: dumps/<timestamp>)")

    b = sub.add_parser(
        "backup",
        help="[read-only] save all 32 slots to DIR (selects each slot, then restores the active one)",
        description="Back up all 32 slots. Files slot_00.bin..slot_31.bin use the 0-based protocol "
        "index (slot_00.bin = display slot 1). The active slot and its unsaved changes are restored.",
    )
    b.add_argument("dir", help="output directory (must not already contain a backup)")

    x = sub.add_parser(
        "ir-export",
        help="[read-only] export a slot's IR as a 24-bit mono 44100 Hz WAV",
        description="Export the IR of a slot. For the active slot, its working copy "
        "(including unsaved changes) is exported; other slots are read from flash "
        "and the active slot is re-selected afterwards.",
    )
    x.add_argument("--slot", type=slot_arg, required=True, help=SLOT_HELP)
    x.add_argument("out", help="output WAV path")

    # working copy
    sel = sub.add_parser(
        "select", help="[working copy] make slot N active (unsaved changes of the current slot are lost)"
    )
    sel.add_argument("slot", type=slot_arg, help=SLOT_HELP)

    v = sub.add_parser("volume", help=f"[working copy] set the preset volume 0-{VOLUME_MAX}")
    v.add_argument("value", type=volume_arg, help=f"0-{VOLUME_MAX} (linear gain)")

    c = sub.add_parser("cab", help="[working copy] switch the cab/IR on or off")
    c.add_argument("state", choices=("on", "off"))

    e = sub.add_parser("eq", help="[working copy] switch the EQ on or off")
    e.add_argument("state", choices=("on", "off"))

    eb = sub.add_parser(
        "eq-band",
        help="[working copy] change one EQ band (shows it when no option is given)",
        description="Change one EQ band of the working copy. A band only acts when the EQ is on "
        "('eq on') and the band is enabled. Q applies to peak1..peak5 only; gain to "
        "lowshelf, peak1..peak5 and highshelf.",
    )
    eb.add_argument("band", choices=tuple(EQ_BANDS))
    eb.add_argument("--freq", type=freq_arg, help=f"frequency in Hz, {EQ_FREQ_MIN}-{EQ_FREQ_MAX}")
    eb.add_argument(
        "--gain", type=gain_arg, help=f"gain in dB, -{EQ_GAIN_MAX_DB}..+{EQ_GAIN_MAX_DB} (0.1 dB steps)"
    )
    eb.add_argument("--q", type=q_arg, help=f"Q factor, {EQ_Q_MIN}-{EQ_Q_MAX} (0.1 steps)")
    en = eb.add_mutually_exclusive_group()
    en.add_argument("--enable", dest="enable", action="store_const", const=True, default=None)
    en.add_argument("--disable", dest="enable", action="store_const", const=False)

    il = sub.add_parser(
        "ir-load",
        help="[working copy] convert a WAV and load it into the active slot for preview (not saved)",
    )
    il.add_argument("wav", help="impulse response WAV (any rate, PCM 8/16/24/32-bit or float)")
    _add_convert_options(il)

    # flash
    sv = sub.add_parser(
        "save",
        help="[flash] save the active slot's working copy (header, IR, EQ) to flash",
    )
    sv.add_argument(
        "--name", dest="preset_name", type=name_arg, help=f"new preset name (printable ASCII, max {NAME_MAX_CHARS})"
    )
    _add_flash_options(sv)

    rn = sub.add_parser("rename", help="[flash] rename a slot (keeps its stored IR and EQ)")
    rn.add_argument("--slot", type=slot_arg, required=True, help=SLOT_HELP)
    rn.add_argument("new_name", metavar="NAME", type=name_arg, help=f"printable ASCII, max {NAME_MAX_CHARS}")
    _add_flash_options(rn)

    up = sub.add_parser(
        "ir-upload",
        help="[flash] convert a WAV, load it into slot N, turn the cab on and save",
        description="Select slot N (loaded from flash), load the converted IR, turn the cab on, "
        "save to flash and verify by reloading. Slot N stays active.",
    )
    up.add_argument("wav", help="impulse response WAV (any rate, PCM 8/16/24/32-bit or float)")
    up.add_argument("--slot", type=slot_arg, required=True, help=SLOT_HELP)
    up.add_argument(
        "--name", dest="preset_name", type=name_arg, help=f"new preset name (printable ASCII, max {NAME_MAX_CHARS})"
    )
    _add_convert_options(up)
    _add_flash_options(up)

    rs = sub.add_parser(
        "restore",
        help="[flash] write slots from a backup directory back to flash",
        description="Restore slots from a backup made by 'backup' (or an auto-backup). "
        "Without --slot every slot present in the backup is restored.",
    )
    rs.add_argument("dir", help="backup directory")
    rs.add_argument("--slot", type=slot_arg, nargs="+", action="extend", help=SLOT_HELP + "; repeatable")
    _add_flash_options(rs)
    return p


# -- device access ---------------------------------------------------------------


@contextmanager
def open_device(args: argparse.Namespace, trace: str | None = None) -> Iterator[IRBox]:
    """Open the USB-MIDI transport and yield an :class:`IRBox`."""
    if args.transport != "midi":
        raise UsageError(f"'{args.command}' is only supported with --transport midi")
    with midi.IRBoxMIDI(trace if trace is not None else args.trace) as m:
        m.open(args.midi_in, args.midi_out)
        yield IRBox(m, read_chunk=args.chunk or DEFAULT_READ_CHUNK, progress=print)


@dataclass(frozen=True)
class Snapshot:
    slot: int  # 0-based
    names: list[str | None]
    block: bytes  # working copy, 8192 bytes


async def _ble_snapshot(args: argparse.Namespace, trace: str | None) -> Snapshot:
    chunk = args.chunk or DEFAULT_READ_CHUNK
    async with ble.IRBoxBLE(trace) as dev:
        await dev.connect(args.address, args.ble_name)
        slot = (await dev.read(TYPE_DEV, ADDR_CURRENT_SLOT, 1, 1))[0]
        names = await dev.read(TYPE_USR, ADDR_NAME_TABLE, NAME_TABLE_LEN, NAME_TABLE_LEN)
        block = await dev.read(TYPE_USR, 0, PRESET_LEN, chunk)
    if slot >= NUM_SLOTS:
        raise RuntimeError(f"device reported implausible active slot index {slot}")
    return Snapshot(slot, parse_name_table(names), block)


def _snapshot(args: argparse.Namespace, trace: str | None = None) -> Snapshot:
    if args.transport == "ble":
        return asyncio.run(_ble_snapshot(args, trace if trace is not None else args.trace))
    with open_device(args, trace) as box:
        return Snapshot(box.active_slot(), box.names(), box.read_preset())


# -- output helpers --------------------------------------------------------------


def _on_off(value: int) -> str:
    return "on" if value else "off"


def _fmt_band(s: EqBandState) -> str:
    parts = [f"{s.band:<9}", "enabled " if s.enabled else "disabled", f"{s.freq_hz:>5} Hz"]
    if s.gain_db is not None:
        parts.append(f"{s.gain_db:+5.1f} dB")
    if s.q is not None:
        parts.append(f"Q {s.q:.1f}")
    return "  ".join(parts)


def _print_names(names: list[str | None], active: int) -> None:
    print("stored slot names (* = active):")
    for i, n in enumerate(names):
        mark = "*" if i == active else " "
        print(f"  {mark}{display_slot(i):2d}: {n if n is not None else '-'}")


def _print_preset(block: bytes) -> None:
    p = parse_preset(block)
    print(f"  name: {p.name!r}")
    print(f"  cab: {_on_off(p.cab_on)}   eq: {_on_off(p.eq_on)}   volume: {p.level}")
    if not p.magic_ok:
        print("  warning: preset magic bytes are not as expected")
    eq = block[EQ_OFFSET:EQ_END]
    print("  EQ bands (active only when eq is on and the band is enabled):")
    for band in EQ_BANDS:
        print(f"    {_fmt_band(describe_eq_band(eq, band))}")


def _print_conversion(path: str, r: ConvertResult, fade: int) -> None:
    print(f"converted {path}:")
    print(
        f"  source: {r.source_rate} Hz, {r.source_channels} channel(s), {r.source_frames} frames "
        f"({r.source_duration_ms:.1f} ms), channel used: {r.channel}"
    )
    if r.trimmed_frames:
        print(f"  trimmed {r.trimmed_frames} leading silent frames")
    print(f"  output: {IR_SAMPLES} samples @ {IR_RATE} Hz = {IR_DURATION_MS:.1f} ms, signed 24-bit")
    if r.truncated:
        fade_text = f"fade-out over the last {fade} samples" if fade else "no fade"
        print(f"  truncated: yes ({fade_text})")
    else:
        print("  truncated: no (shorter inputs are zero-padded)")
    peak = "-inf" if math.isinf(r.peak_dbfs) else f"{r.peak_dbfs:.2f}"
    print(f"  peak: {peak} dBFS")
    for w in r.warnings:
        print(f"  warning: {w}")


def _convert(args: argparse.Namespace) -> ConvertResult:
    result = convert(
        args.wav,
        channel=args.channel,
        normalize=args.normalize,
        target_peak_db=args.peak_db,
        gain_db=args.gain_db,
        fade_samples=args.fade,
        trim_leading_silence=args.trim_silence,
    )
    _print_conversion(args.wav, result, args.fade)
    return result


def _confirm(args: argparse.Namespace, steps: list[str]) -> bool:
    """Print the plan; return True only if --yes was given."""
    plan = list(steps)
    if not args.no_backup:
        plan.insert(0, f"back up all {NUM_SLOTS} slots to {BACKUP_ROOT}/<timestamp>/ (skip with --no-backup)")
    print("plan:")
    for i, step in enumerate(plan, 1):
        print(f"  {i}. {step}")
    if not args.yes:
        print("this writes the pedal's flash memory; re-run with --yes to proceed", file=sys.stderr)
        return False
    return True


def _new_backup_dir() -> Path:
    base = BACKUP_ROOT / time.strftime("%Y%m%d-%H%M%S")
    path = base
    n = 2
    while path.exists():
        path = base.with_name(f"{base.name}-{n}")
        n += 1
    return path


def _auto_backup(args: argparse.Namespace, box: IRBox) -> None:
    if args.no_backup:
        print("automatic backup skipped (--no-backup)")
        return
    out = _new_backup_dir()
    print(f"backing up all {NUM_SLOTS} slots to {out} ...")
    box.backup(out)
    print(f"backup complete: {out}")


# -- read-only commands ----------------------------------------------------------


async def cmd_scan(args: argparse.Namespace) -> int:
    results = await ble.scan(args.timeout, args.all)
    if not results:
        print("no devices found")
        return EXIT_ERROR
    for r in results:
        print(f"{r.name or '<no name>'}\t{r.address}\trssi={r.rssi}\t{','.join(r.service_uuids)}")
    return EXIT_OK


def cmd_midi_ports(args: argparse.Namespace) -> int:
    ins, outs = midi.list_ports()
    print("MIDI inputs:")
    for i, n in enumerate(ins):
        print(f"  {i}: {n}")
    print("MIDI outputs:")
    for i, n in enumerate(outs):
        print(f"  {i}: {n}")
    return EXIT_OK if ins or outs else EXIT_ERROR


def cmd_query(args: argparse.Namespace) -> int:
    if args.transport != "midi":
        raise UsageError("query is only implemented for --transport midi")
    with midi.IRBoxMIDI(args.trace) as dev:
        dev.open(args.midi_in, args.midi_out)
        reply = dev.query(args.timeout)
    print(f"name: {reply.name!r}")
    print(f"rest ({len(reply.rest)} bytes): {reply.rest.hex(' ')}")
    print(f"checksum: {'ok' if reply.checksum_ok else 'MISMATCH'}")
    return EXIT_OK


def cmd_info(args: argparse.Namespace) -> int:
    snap = _snapshot(args)
    print(f"active slot: {display_slot(snap.slot)}")
    print("working copy of the active slot (includes unsaved changes):")
    _print_preset(snap.block)
    _print_names(snap.names, snap.slot)
    return EXIT_OK


def cmd_dump(args: argparse.Namespace) -> int:
    out = Path(args.out) if args.out else Path("dumps") / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    trace = args.trace or str(out / "trace.jsonl")
    snap = _snapshot(args, trace)
    print(f"active slot: {display_slot(snap.slot)}")
    _print_names(snap.names, snap.slot)
    preset = parse_preset(snap.block)
    (out / "preset_raw.bin").write_bytes(snap.block)
    meta = {"slot": display_slot(snap.slot), "slot_index": snap.slot, **preset_to_dict(preset)}
    (out / "preset.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    (out / "names.json").write_text(json.dumps(snap.names, indent=2), encoding="utf-8")
    export_wav(preset.ir_samples, out / "ir.wav")
    print(f"saved dump to {out}")
    return EXIT_OK


def cmd_backup(args: argparse.Namespace) -> int:
    with open_device(args) as box:
        result = box.backup(Path(args.dir))
    print(f"backup of {NUM_SLOTS} slots written to {result.path}; active slot {display_slot(result.active_slot)} restored")
    return EXIT_OK


def cmd_ir_export(args: argparse.Namespace) -> int:
    with open_device(args) as box:
        block = box.read_slot(args.slot)
    samples = samples_from_preset(block)
    export_wav(samples, args.out)
    name = name_from_field(block[:NAME_STRIDE])
    print(f"exported IR of slot {display_slot(args.slot)} ({name or '-'}) to {args.out} "
          f"({IR_SAMPLES} samples, {IR_RATE} Hz, 24-bit mono)")
    return EXIT_OK


# -- working-copy commands -------------------------------------------------------

_NOT_SAVED = "(working copy only; run 'save --yes' to keep it)"


def cmd_select(args: argparse.Namespace) -> int:
    with open_device(args) as box:
        box.select(args.slot)
        name = box.names()[args.slot]
    print(f"active slot: {display_slot(args.slot)} ({name or '-'})")
    return EXIT_OK


def cmd_volume(args: argparse.Namespace) -> int:
    with open_device(args) as box:
        box.set_volume(args.value)
    print(f"volume set to {args.value} {_NOT_SAVED}")
    return EXIT_OK


def cmd_cab(args: argparse.Namespace) -> int:
    with open_device(args) as box:
        box.set_cab(args.state == "on")
    print(f"cab {args.state} {_NOT_SAVED}")
    return EXIT_OK


def cmd_eq(args: argparse.Namespace) -> int:
    with open_device(args) as box:
        box.set_eq(args.state == "on")
    print(f"eq {args.state} {_NOT_SAVED}")
    return EXIT_OK


def cmd_eq_band(args: argparse.Namespace) -> int:
    change = any(x is not None for x in (args.freq, args.gain, args.q, args.enable))
    spec = EQ_BANDS[args.band]
    if args.gain is not None and spec.gain is None:
        raise UsageError(f"band {args.band} has no gain parameter")
    if args.q is not None and spec.q is None:
        raise UsageError(f"band {args.band} has no Q parameter (only peak1..peak5 do)")
    with open_device(args) as box:
        if change:
            eq = box.set_eq_band(args.band, freq=args.freq, gain_db=args.gain, q=args.q, enable=args.enable)
        else:
            eq = box.read_eq()
        eq_on = box.read_header()[EQ_ON_OFFSET]
    desc = describe_eq_band(eq, args.band)
    print(_fmt_band(desc) + (f" {_NOT_SAVED}" if change else ""))
    if not eq_on:
        print("note: the EQ is off; run 'eq on' to hear the bands")
    elif not desc.enabled:
        print(f"note: band {args.band} is disabled; add --enable to hear it")
    return EXIT_OK


def cmd_ir_load(args: argparse.Namespace) -> int:
    result = _convert(args)
    with open_device(args) as box:
        slot = box.active_slot()
        box.load_ir(result.samples)
        cab_on = box.read_header()[CAB_ON_OFFSET]
    print(f"IR loaded into the working copy of slot {display_slot(slot)} {_NOT_SAVED}")
    if not cab_on:
        print("note: the cab is off; run 'cab on' to hear the IR")
    return EXIT_OK


# -- flash commands --------------------------------------------------------------

_VERIFY = "verify: name table, then reload the slot from flash and compare header, IR and EQ"


def cmd_save(args: argparse.Namespace) -> int:
    name = f" with name {args.preset_name!r}" if args.preset_name else ""
    steps = [
        f"write the header{name} to the working copy of the active slot",
        "save the active slot's working copy (header, IR, EQ) to flash",
        _VERIFY,
    ]
    if not _confirm(args, steps):
        return EXIT_REFUSED
    with open_device(args) as box:
        _auto_backup(args, box)
        slot = box.active_slot()
        box.save(slot, args.preset_name, verify=True)
        stored = box.names()[slot]
    print(f"saved slot {display_slot(slot)} ({stored or '-'})")
    return EXIT_OK


def cmd_rename(args: argparse.Namespace) -> int:
    n = display_slot(args.slot)
    steps = [
        f"reload slot {n} from flash (if it is the active slot, its unsaved changes are discarded)",
        f"write name {args.new_name!r} to its header and save slot {n} to flash",
        _VERIFY,
        "re-select the previously active slot if it was another one (its unsaved changes are kept)",
    ]
    if not _confirm(args, steps):
        return EXIT_REFUSED
    with open_device(args) as box:
        _auto_backup(args, box)
        box.rename(args.slot, args.new_name)
    print(f"renamed slot {n} to {args.new_name!r}")
    return EXIT_OK


def cmd_ir_upload(args: argparse.Namespace) -> int:
    result = _convert(args)
    n = display_slot(args.slot)
    name = f" with name {args.preset_name!r}" if args.preset_name else ""
    steps = [
        f"select slot {n} (loaded from flash; unsaved changes of the active slot are discarded)",
        "load the converted IR into the working copy, turn the cab on",
        f"save slot {n}{name} to flash",
        _VERIFY,
        f"slot {n} stays active",
    ]
    if not _confirm(args, steps):
        return EXIT_REFUSED
    with open_device(args) as box:
        _auto_backup(args, box)
        box.upload_ir(args.slot, result.samples, args.preset_name)
        stored = box.names()[args.slot]
    print(f"uploaded IR to slot {n} ({stored or '-'}) and verified it")
    return EXIT_OK


def cmd_restore(args: argparse.Namespace) -> int:
    blocks = load_backup(args.dir)
    targets = sorted(set(args.slot)) if args.slot else sorted(blocks)
    missing = [display_slot(s) for s in targets if s not in blocks]
    if missing:
        raise UsageError(f"backup {args.dir} has no data for slot(s) {missing}")
    steps = [
        f"slot {display_slot(s)}: select, write header/IR/EQ from {args.dir}, apply, save to flash, verify "
        f"({name_from_field(blocks[s][:NAME_STRIDE]) or '-'})"
        for s in targets
    ]
    steps.append("re-select the originally active slot")
    if not _confirm(args, steps):
        return EXIT_REFUSED
    with open_device(args) as box:
        _auto_backup(args, box)
        done = box.restore(args.dir, targets)
    print(f"restored slot(s) {[display_slot(s) for s in done]} from {args.dir}")
    return EXIT_OK


HANDLERS: dict[str, Callable[[argparse.Namespace], int]] = {
    "midi-ports": cmd_midi_ports,
    "query": cmd_query,
    "info": cmd_info,
    "dump": cmd_dump,
    "backup": cmd_backup,
    "ir-export": cmd_ir_export,
    "select": cmd_select,
    "volume": cmd_volume,
    "cab": cmd_cab,
    "eq": cmd_eq,
    "eq-band": cmd_eq_band,
    "ir-load": cmd_ir_load,
    "save": cmd_save,
    "rename": cmd_rename,
    "ir-upload": cmd_ir_upload,
    "restore": cmd_restore,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    try:
        if args.command == "scan":
            return asyncio.run(cmd_scan(args))
        return HANDLERS[args.command](args)
    except UsageError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_REFUSED
    except (OSError, RuntimeError, ValueError) as e:
        log.debug("command failed", exc_info=True)
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
