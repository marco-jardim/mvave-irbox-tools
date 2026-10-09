# Flash dump runbook (not performed)

Status: **not attempted.** The project decided not to pursue a firmware dump for
now, because a suitable adapter is hard to obtain. This page records the
procedure so the work can be picked up later. Nothing here has been tested on
an IR Box.

Legend: **community** (third-party documentation), **inferred** (our reasoning).

## Why it would work

- The IR Box SoC is a JieLi part (`JL` / `BP1Y35B-65C4`, FCC photos). It is
  probably an AC695N/AC696N (BR23/BR25), but that is **inferred**. See
  [hardware.md](hardware.md#inside-the-unit-fcc-internal-photos).
- Every JieLi chip has a mask-ROM USB download mode ("UBOOT1.00"). At boot, the
  ROM watches the USB data lines for a 16-bit key (`0x16EF`, D- = clock,
  D+ = data). If it sees the key, it acknowledges by pulling both lines low for
  1–2 ms. It then calibrates its oscillator from USB SOF packets and enumerates
  as `UBOOT1.00` (VID 0x4C4A). Because this mode lives in ROM, it cannot be
  overwritten by firmware. (community:
  [kagaimiq/jl-uboot-tool docs/how-to-enter-uboot.md](https://github.com/kagaimiq/jl-uboot-tool/blob/main/docs/how-to-enter-uboot.md))
- [kagaimiq/jl-uboot-tool](https://github.com/kagaimiq/jl-uboot-tool) can read
  flash in that mode. Its README lists BR23 (AC695N/AC635N) and BR25
  (AC696N/AC636N/AC608N) as **Working**, and WL82 (AC791N) as unknown.
  (community)
- The README says the last number of a genuine JieLi marking is the flash size
  in megabits. `-65C4` would mean **4 Mbit (512 KiB)**. Confirm it with the
  flash ID: its last byte is log2 of the size. (community + inferred)
- The IR Box's own USB-C port probably connects straight to the SoC, because the
  SoC enumerates as the USB audio/MIDI device. If so, no case opening is needed.
  (inferred)

## What you need

| Item | Notes |
|---|---|
| JieLi "USB Updater" / forced-download dongle (杰理强制下载工具 / USB升级工具) | Sends the USB_KEY and switches power and USB. V2/V3 dongles use an AC6925B; V4 uses an AC5213B with a dedicated USB switch. **Prefer listings that name AC695x/AC696x/AC697x/AC79 or V3/V4.** Listings that only name AC460/AC690x–AC692x may not support newer chips, which also accept an ISP_KEY entry. |
| USB 2.0 hub (high-speed) | Isolates the full-speed bus during SOF calibration, as recommended by the doc. Do not share the bus with other FS/LS devices. |
| USB-A to USB-C data cable | Dongle to IR Box. |
| `jl-uboot-tool` (Python) | Use only `read` and `dump`. |

## Procedure (read-only)

1. Take a toolkit backup first: `uv run python scripts/irbox_tool.py backup backups/pre-dump --raw`.
2. Close CubeSuite, MidiSuite and any other MIDI app.
3. **Unplug the 9 V DC supply.** The pedal must be powered only through the
   dongle's USB, so the dongle can control power-up.
4. Wire it up: PC → USB 2.0 hub → dongle → USB-A–C cable → IR Box USB-C.
5. Switch the dongle's power off and then on (or plug in the IR Box last). The
   dongle's LED indicates when the key is accepted.
6. Check enumeration: `python jldevfind.py` should list a `UBOOT…` device. The
   pedal will **not** show as "SINCO". If nothing appears, stop. The pedal will
   boot normally once the dongle is removed.
7. In `jluboottool.py`:
   - read the flash ID and compute the size (expect 512 KiB = `0x80000`);
   - `read 0x0 0x80000 irbox_flash_a.bin`
   - `read 0x0 0x80000 irbox_flash_b.bin`
   - `exit`
8. Compare the two dumps (`Get-FileHash`). They must be identical; if they are
   not, the read is unreliable.
9. Unplug, reconnect normally, and confirm the pedal works
   (`irbox_tool.py query` → `IR-BOX_010`).

**Never run `write`, `erase` or `erasechip`.** Those are the only commands that
can brick the pedal. If this is ever done, a wrapper that refuses every command
except `read`, `dump` and `exit` is recommended.

## Failure modes

| Symptom | Likely cause | Damage? |
|---|---|---|
| Pedal boots normally, no UBOOT device | Dongle key not accepted (unsupported dongle or chip), USB-C not wired to the SoC, or the DC supply is still connected | None |
| UBOOT device appears, then drops out | SOF calibration disturbed (no hub / shared bus) | None |
| Reads fail or two dumps differ | Protocol quirk on this chip family | None |

## After a dump

1. Unpack: JieLi flash layout tools in
   [kagaimiq/jl-misctools](https://github.com/kagaimiq/jl-misctools) and the
   format notes at https://kagaimiq.github.io/jielie/.
2. Disassemble `app.bin` (pi32 / pi32v2 core) in Ghidra. AL-255/FM-1-RE has
   loader notes for JieLi images.
3. Look for the preset/IR handling, the protocol command table (0x11/0x12/0x21/
   0x22/0x23), the 2048-tap IR length, and the refresh/save handlers.
4. Do not redistribute the dump; it is M-VAVE's firmware.
