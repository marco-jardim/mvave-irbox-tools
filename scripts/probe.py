"""Low-level probing helpers for the IR Box USB-MIDI protocol.

Read-only by default. The only write path is ``write``, which is limited to
an allowlist of working-area addresses and requires ``--yes``. Slot save
(0xF0000000+i), erase (0x21) and OTA are never sent.

Examples:
  uv run python scripts/probe.py read 5 0x0 64
  uv run python scripts/probe.py regions
  uv run python scripts/probe.py monitor 30
  uv run python scripts/probe.py poll 60
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from irbox.midi import IRBoxMIDI  # noqa: E402
from irbox.protocol import (  # noqa: E402
    CMD_WRITE,
    ReadResponse,
    WriteAck,
    build_write,
)

# (type, addr) pairs the probe may write. Everything here targets the
# working copy of the active preset or slot selection; nothing persists
# until a slot save, which is deliberately absent.
WRITE_ALLOW = {
    (5, 0x11): "IR on/off",
    (5, 0x12): "EQ on/off",
    (5, 0x2B): "volume",
    (5, 0xA0000000): "refresh/apply",
}
SELECT_BASE = 0xE0000000  # type 4, + slot index


def hexdump(data: bytes, base: int = 0) -> str:
    lines = []
    for off in range(0, len(data), 16):
        row = data[off : off + 16]
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        lines.append(f"{base + off:08X}  {row.hex(' '):<47}  {text}")
    return "\n".join(lines)


def cmd_read(dev: IRBoxMIDI, a: argparse.Namespace) -> int:
    data = dev.read(a.type, a.addr, a.length, chunk=a.chunk)
    print(hexdump(data, a.addr))
    return 0


def cmd_regions(dev: IRBoxMIDI, a: argparse.Namespace) -> int:
    addrs = [0x0, 0x100, 0x1000, 0x10000, 0x20000000, 0x40000000,
             0x80000000, 0xA0000000, 0xC0000000, 0xE0000000, 0xF0000000]
    for type_ in range(8):
        for addr in addrs:
            try:
                data = dev.read(type_, addr, 16, timeout=a.timeout)
                print(f"type {type_} addr 0x{addr:08X}: {data.hex(' ')}")
            except (TimeoutError, RuntimeError) as e:
                print(f"type {type_} addr 0x{addr:08X}: {type(e).__name__}: {e}")
            time.sleep(0.03)
    return 0


def cmd_monitor(dev: IRBoxMIDI, a: argparse.Namespace) -> int:
    print(f"listening {a.seconds} s; turn knobs / press buttons now")
    end = time.monotonic() + a.seconds
    while time.monotonic() < end:
        time.sleep(0.2)
    print("done; see the trace file for non-SysEx messages")
    return 0


def _snapshot(dev: IRBoxMIDI, full: bool = False) -> dict[str, bytes]:
    snap = {
        "slot": dev.read(4, 0x20000000, 1),
        "head": dev.read(5, 0x0, 44),
        "eq": dev.read(5, 0x1C00, 40),
        "query": dev.query().raw,
    }
    if full:
        snap["block"] = dev.read(5, 0x0, 8192, chunk=1000)
        snap["names"] = dev.read(5, 0x80000000, 544, chunk=1000)
    return snap


def cmd_poll(dev: IRBoxMIDI, a: argparse.Namespace) -> int:
    print(f"polling {a.seconds} s every {a.interval} s; turn ONE knob at a time")
    prev = _snapshot(dev, a.full)
    print(f"{time.strftime('%H:%M:%S')} initial slot={prev['slot'][0]} head={prev['head'].hex(' ')}", flush=True)
    end = time.monotonic() + a.seconds
    n = 0
    while time.monotonic() < end:
        time.sleep(a.interval)
        cur = _snapshot(dev, a.full)
        n += 1
        stamp = time.strftime("%H:%M:%S")
        if n % 20 == 0:
            print(f"{stamp} alive ({n} snapshots)", flush=True)
        keys = [("slot", 0x20000000), ("head", 0x0), ("eq", 0x1C00), ("query", 0)]
        if a.full:
            keys += [("block", 0x0), ("names", 0x80000000)]
        for key, base in keys:
            old, new = prev[key], cur[key]
            for i, (x, y) in enumerate(zip(old, new)):
                if x != y:
                    print(f"{stamp} {key}+{i} (addr 0x{base + i:X}): {x} -> {y}", flush=True)
        prev = cur
    return 0


# Empty-body query commands named in the official app (all read-only by name).
QUERY_CMDS = {
    0x11: "name and version",
    0x12: "other info / short message",
    0x13: "firmware version",
    0x17: "device type",
    0x1B: "sound data address",
    0x20: "codec value",
}


def cmd_query(dev: IRBoxMIDI, a: argparse.Namespace) -> int:
    codes = list(QUERY_CMDS) if a.code is None else [a.code]
    for code in codes:
        if code not in QUERY_CMDS:
            print(f"refused: 0x{code:02X} is not a known query command")
            return 2
        raw = bytes([0x00, 0x59, code, 0, 0, 0, 0xFF])
        try:
            frame = dev.transact(raw, None, a.timeout)
        except TimeoutError as e:
            print(f"0x{code:02X} {QUERY_CMDS[code]}: timeout ({e})")
            continue
        body = frame.raw[6:-1]
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in body)
        print(f"0x{code:02X} {QUERY_CMDS[code]}: reply cmd=0x{frame.raw[2]:02X} "
              f"len={len(body)} body={body.hex(' ')} |{text}|")
        time.sleep(0.05)
    return 0


def cmd_write(dev: IRBoxMIDI, a: argparse.Namespace) -> int:
    key = (a.type, a.addr)
    is_select = a.type == 4 and SELECT_BASE <= a.addr < SELECT_BASE + 32
    is_eq = a.type == 5 and 0x1C00 <= a.addr and a.addr + len(bytes.fromhex(a.data)) <= 0x1C28
    if key not in WRITE_ALLOW and not is_select and not is_eq:
        print(f"refused: type {a.type} addr 0x{a.addr:X} is not on the allowlist")
        return 2
    data = bytes.fromhex(a.data)
    if not a.yes:
        print("dry run (add --yes to send):", build_write(a.type, a.addr, data).hex(" "))
        return 0
    frame = dev.transact(build_write(a.type, a.addr, data), None, a.timeout)
    kind = type(frame).__name__
    print(f"reply {kind}: {frame.raw.hex(' ')}")
    if isinstance(frame, WriteAck):
        print(f"status={frame.status} ok={frame.ok}")
    elif isinstance(frame, ReadResponse) or frame.raw[2] != CMD_WRITE:
        print("note: reply is not a plain ack")
    return 0


IR_BASE = 24      # "CAB\0" magic; app uploads 6164 B from here
IR_BLOCK = 6164   # 20 B cab header + 2048 x 24-bit samples


def _write_chunked(dev: IRBoxMIDI, type_: int, addr: int, data: bytes, timeout: float) -> int:
    n = 0
    for off in range(0, len(data), 173):
        part = data[off : off + 173]
        frame = dev.transact(build_write(type_, addr + off, part), None, timeout)
        if not isinstance(frame, WriteAck) or not frame.ok:
            raise RuntimeError(f"write at 0x{addr + off:X} failed: {frame.raw.hex(' ')}")
        n += 1
    return n


def cmd_irtest(dev: IRBoxMIDI, a: argparse.Namespace) -> int:
    """Upload an IR to the working area only (never saved) and read it back."""
    current = dev.read(5, IR_BASE, IR_BLOCK, chunk=1000)
    if a.mode == "same":
        payload = current
    elif a.mode in ("scale-le", "scale-be"):
        order = "little" if a.mode == "scale-le" else "big"
        body = bytearray()
        for i in range(20, IR_BLOCK, 3):
            v = int.from_bytes(current[i : i + 3], order, signed=True)
            body += (v // 2).to_bytes(3, order, signed=True)
        payload = current[:20] + bytes(body)
    else:
        samples = bytearray(2048 * 3)
        peak = int(0x7FFFFF * 0.5)
        if a.mode == "dirac-full":
            samples[0:3] = bytes([0x7F, 0xFF, 0x7F])  # ~+1.0 FS in either byte order
        else:
            samples[0:3] = peak.to_bytes(3, "big" if a.mode == "dirac-be" else "little")
        payload = current[:20] + bytes(samples)
    t0 = time.monotonic()
    chunks = _write_chunked(dev, 5, IR_BASE, payload, a.timeout)
    t_write = time.monotonic() - t0
    if a.refresh:
        frame = dev.transact(build_write(5, 0xA0000000, bytes([1])), None, a.timeout)
        print(f"refresh(1): {frame.raw.hex(' ')}")
    back = dev.read(5, IR_BASE, IR_BLOCK, chunk=1000)
    diff = [i for i, (x, y) in enumerate(zip(payload, back)) if x != y]
    print(f"mode={a.mode} chunks={chunks} write_time={t_write:.2f}s "
          f"readback_equal={not diff} diffs={len(diff)} first={diff[:10]}")
    return 0 if not diff else 1


def _select(dev: IRBoxMIDI, slot: int, timeout: float) -> None:
    frame = dev.transact(build_write(4, SELECT_BASE + slot, bytes([slot])), None, timeout)
    if not isinstance(frame, WriteAck) or not frame.ok:
        raise RuntimeError(f"select {slot} failed: {frame.raw.hex(' ')}")


def cmd_backup(dev: IRBoxMIDI, a: argparse.Namespace) -> int:
    """Read all 32 slots from flash (via select) into OUTDIR; restores the active slot."""
    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    original = dev.read(4, 0x20000000, 1)[0]
    (out / "names.bin").write_bytes(dev.read(5, 0x80000000, 544, chunk=1000))
    try:
        for slot in range(32):
            _select(dev, slot, a.timeout)
            time.sleep(0.2)
            if dev.read(4, 0x20000000, 1)[0] != slot:
                raise RuntimeError(f"slot {slot} did not become active")
            block = dev.read(5, 0x0, 8192, chunk=1000)
            (out / f"slot_{slot:02d}.bin").write_bytes(block)
            name = block[:17].split(b"\x00")[0].decode("ascii", "replace")
            print(f"slot {slot:2d}: {name}", flush=True)
    finally:
        _select(dev, original, a.timeout)
    print(f"restored active slot {original}; backup in {out}")
    return 0


def cmd_save(dev: IRBoxMIDI, a: argparse.Namespace) -> int:
    """Write a 24-byte header (name/cab_on/eq_on/'patch') to the working copy and SAVE to flash."""
    if not a.yes_flash:
        print("refused: saving writes flash; pass --yes-flash")
        return 2
    active = dev.read(4, 0x20000000, 1)[0]
    if active != a.slot:
        print(f"refused: active slot is {active}, not {a.slot}; select it first")
        return 2
    head = dev.read(5, 0x0, 24)
    if a.name is not None:
        raw = a.name.encode("ascii")
        if len(raw) > 16:
            print("refused: name longer than 16 characters")
            return 2
        head = raw.ljust(17, b"\x00") + head[17:]
    if a.header_hex:
        head = bytes.fromhex(a.header_hex)
        if len(head) != 24 or head[19:24] != b"patch":
            print("refused: header must be 24 bytes ending in 'patch'")
            return 2
    for addr, data in ((0x0, head), (0xF0000000 + a.slot, b"\x00")):
        frame = dev.transact(build_write(5, addr, data), None, a.timeout)
        print(f"write 0x{addr:08X}: {frame.raw.hex(' ')}")
        if not isinstance(frame, WriteAck) or not frame.ok:
            raise RuntimeError("write failed")
    time.sleep(0.5)
    names = dev.read(5, 0x80000000, 544, chunk=1000)
    entry = names[a.slot * 17 : a.slot * 17 + 17]
    print(f"name table slot {a.slot}: {entry.split(b'\x00')[0].decode('ascii', 'replace')!r}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--trace", help="JSONL trace file")
    p.add_argument("--midi-in")
    p.add_argument("--midi-out")
    p.add_argument("--timeout", type=float, default=2.0)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("read")
    r.add_argument("type", type=int)
    r.add_argument("addr", type=lambda s: int(s, 0))
    r.add_argument("length", type=lambda s: int(s, 0))
    r.add_argument("--chunk", type=int, default=1000)

    sub.add_parser("regions")
    m = sub.add_parser("monitor")
    m.add_argument("seconds", type=float)
    po = sub.add_parser("poll")
    po.add_argument("seconds", type=float)
    po.add_argument("--interval", type=float, default=0.3)
    po.add_argument("--full", action="store_true", help="also diff the whole 8192 B block and name table")

    q = sub.add_parser("query", help="send the app's empty-body query commands")
    q.add_argument("code", nargs="?", type=lambda s: int(s, 0))

    it = sub.add_parser("irtest", help="working-area IR upload + readback (never saves)")
    it.add_argument("mode", choices=["same", "dirac", "dirac-be", "dirac-full", "scale-le", "scale-be"])
    it.add_argument("--refresh", action="store_true", help="send refresh(1) after upload")

    bk = sub.add_parser("backup", help="dump all 32 slots from flash")
    bk.add_argument("outdir")

    sv = sub.add_parser("save", help="write header and SAVE active slot to flash (persistent)")
    sv.add_argument("slot", type=int)
    sv.add_argument("--name")
    sv.add_argument("--header-hex", help="exact 24-byte header to restore")
    sv.add_argument("--yes-flash", action="store_true")

    w = sub.add_parser("write")
    w.add_argument("type", type=int)
    w.add_argument("addr", type=lambda s: int(s, 0))
    w.add_argument("data", help="hex bytes, e.g. 37")
    w.add_argument("--yes", action="store_true")

    a = p.parse_args(argv)
    handlers = {"read": cmd_read, "regions": cmd_regions, "monitor": cmd_monitor,
                "poll": cmd_poll, "query": cmd_query, "irtest": cmd_irtest, "backup": cmd_backup, "save": cmd_save,
                "write": cmd_write}
    with IRBoxMIDI(trace_path=a.trace, chunk_timeout=a.timeout) as dev:
        dev.open(a.midi_in, a.midi_out)
        return handlers[a.cmd](dev, a)


if __name__ == "__main__":
    raise SystemExit(main())
