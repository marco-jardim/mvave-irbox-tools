# mvave-irbox-tools

Unofficial, open-source editor and toolkit for the **M-VAVE IR Box** cab-sim
pedal over USB-MIDI. It reads and edits presets, uploads impulse responses,
adjusts EQ and volume, and backs up and restores all 32 slots from a PC,
without the vendor app.

Not affiliated with or endorsed by M-VAVE / Cuvave. Use at your own risk.

## Status

The protocol is fully mapped and **verified on the real device** (firmware
`IR-BOX_010`, Windows, USB-MIDI). That covers framing, read/write/ACK, the
address map, the preset and EQ layout, refresh modes, select/save and
persistence. Audio effects (IR byte order, volume law, EQ units) were confirmed
by recording the device's USB audio output. See [docs/protocol.md](docs/protocol.md)
and [docs/probing-log.md](docs/probing-log.md).

BLE is implemented from the Android app's code but untested: the unit was never
seen advertising on Windows.

## Features

* Device query, active slot, name table, full preset dump (JSON, raw binary,
  IR as WAV).
* Backup of all 32 slots and restore (whole backup or selected slots).
* Live edits to the working copy: select slot, volume, cab on/off, EQ on/off,
  per-band EQ (HPF, low shelf, 5 peaks, high shelf, LPF), and loading an IR,
  all audible immediately.
* Persistent operations: save, rename, upload an IR to a slot. Each requires
  `--yes` and makes an automatic backup first.
* IR converter: any PCM 16/24/32-bit or float WAV at any sample rate →
  44.1 kHz, 2048 samples, 24-bit LE, normalized to full scale like the factory
  IRs.
* Low-level probing tool with a write allowlist, plus audio measurement and A/B
  scripts.
* Protocol logic is pure Python and unit-tested (no device needed for
  `pytest`).

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). Tested on Windows.

```
uv sync                      # core: python-rtmidi, bleak
uv sync --extra measure      # optional: numpy + sounddevice for scripts/measure.py
uv run pytest
```

Before running anything, **close other MIDI applications** (CubeSuite, DAWs,
MIDI monitors). On Windows a WinMM MIDI port can be open in only one process at
a time. A leftover process causes
`MidiInWinMM::openPort: error creating Windows MM MIDI input port`.

## Usage

```
uv run python scripts/irbox_tool.py [global options] <command> [args]
```

Global options: `--transport midi|ble` (default `midi`), `--midi-in`,
`--midi-out` (port index or name substring; default is the first port whose
name contains `USB-Midi`), `--trace FILE` (JSONL log of every frame).

**Slot numbers are 1–32, as on the pedal's display.** On the wire they are
0–31.

### Read-only

```
uv run python scripts/irbox_tool.py midi-ports            # list MIDI ports
uv run python scripts/irbox_tool.py scan                  # BLE scan
uv run python scripts/irbox_tool.py query                 # device name/firmware (IR-BOX_010)
uv run python scripts/irbox_tool.py info                  # active slot + all slot names
uv run python scripts/irbox_tool.py dump --out dumps/now  # working copy: preset.json, preset_raw.bin, ir.wav, names.json
uv run python scripts/irbox_tool.py backup backups/manual # every slot + name table
uv run python scripts/irbox_tool.py ir-export --slot 24 matchless.wav
```

`backup` and `ir-export` of a non-active slot must select that slot to read it.
The tool captures the active slot's working copy first and puts it back
afterwards (without saving), so unsaved edits survive a backup.

Backup files use 0-based protocol indexes (`slot_00.bin` = display slot 1);
`manifest.json` records both numberings and a checksum per slot, which
`restore` verifies. In `dump`, `preset.json` has `slot` (display, 1–32) and
`slot_index` (protocol, 0–31). `--chunk` sets the read size (1–1000, default 1000).

### Working copy (not saved)

These commands change the RAM working copy of the active preset and send the
matching refresh, so the change is audible at once. Nothing is written to flash.
Selecting another slot reverts them, and they are not expected to survive a
power cycle.

```
uv run python scripts/irbox_tool.py select 24
uv run python scripts/irbox_tool.py volume 60                 # 0-127, linear amplitude
uv run python scripts/irbox_tool.py cab off
uv run python scripts/irbox_tool.py eq on
uv run python scripts/irbox_tool.py eq-band hpf --freq 80 --enable
uv run python scripts/irbox_tool.py eq-band peak2 --freq 400 --gain -3.5 --q 1.4 --enable
uv run python scripts/irbox_tool.py eq-band highshelf --freq 6000 --gain -2 --enable
uv run python scripts/irbox_tool.py eq-band lpf --disable
uv run python scripts/irbox_tool.py ir-load my_cab.wav --channel left --trim-silence
```

`eq-band BAND` accepts `hpf`, `lowshelf`, `peak1`…`peak5`, `highshelf` and
`lpf`, with `--freq HZ`, `--gain DB` (±12 dB in 0.1 dB steps), `--q Q`
(0.1–16), and `--enable` / `--disable`. Bands only take effect while `eq on`.
`ir-load` does not turn the cab on; it warns if the cab is off.

### Persistent (writes flash)

Each of these requires `--yes`. Each first writes an automatic backup to
`backups/<timestamp>/`, unless `--no-backup` is given.

```
uv run python scripts/irbox_tool.py save --name "My Cab" --yes        # save working copy to the active slot
uv run python scripts/irbox_tool.py rename --slot 32 "Bass DI" --yes
uv run python scripts/irbox_tool.py ir-upload my_cab.wav --slot 32 --name "My Cab" --yes
uv run python scripts/irbox_tool.py restore backups/20261008-191500 --yes            # all slots
uv run python scripts/irbox_tool.py restore backups/20261008-191500 --slot 32 --yes  # one slot
```

`save` always targets the active slot. `rename` and `ir-upload` start from the
target slot's stored flash contents (unsaved edits on that slot are lost) and
leave the target slot active. `ir-upload` also turns the cab on.

### IR conversion options (`ir-load`, `ir-upload`)

| Option | Meaning |
|---|---|
| `--channel mix\|left\|right` | how to reduce multichannel files to mono |
| `--normalize peak\|none` | peak-normalize to full scale (default `peak`, like the factory IRs) or keep the level |
| `--peak-db DB` | target peak for `--normalize peak` |
| `--gain-db DB` | extra gain |
| `--fade N` | length in samples of the raised-cosine fade-out applied when the IR is truncated |
| `--trim-silence` | drop leading silence before taking the first 2048 samples |

Pipeline: read any PCM int 16/24/32 or float WAV at any rate → Kaiser windowed-sinc
resample to 44.1 kHz → keep the first 2048 samples (46.4 ms), with a
raised-cosine fade if truncated → normalize → signed 24-bit LE. The official
app's converter has known bugs: 16-bit IRs end up about 48 dB too quiet, and
48 kHz files are not resampled. See [docs/apk-analysis.md](docs/apk-analysis.md).

## Safety

* **Working copy vs flash.** Every edit goes to a RAM working copy and is lost
  on slot change, until it is saved. Only `save`, `rename`, `ir-upload` and
  `restore` write flash.
* **Automatic backup.** Persistent commands back up every slot to
  `backups/<timestamp>/` first. A backup/restore round trip was verified
  byte-identical on the device.
* **Explicit confirmation.** Persistent commands do nothing without `--yes`.
* **Never erase, never OTA.** Command `0x21` (erase) is refused on every
  transport. The BLE OTA characteristics (`ae00`/`ae01`/`ae02`) are never
  touched. There is no firmware-update feature.
* **Keep `backups/`.** It holds your only copy of the content that shipped on
  your unit. Keep it outside version control and do not publish it, since the
  factory IRs are M-VAVE's content.
* The device does not reject reads or writes to unmapped addresses (see
  [docs/protocol.md](docs/protocol.md) quirks). Use the CLI rather than
  hand-made frames.

## Scripts

| Script | Purpose |
|---|---|
| `scripts/irbox_tool.py` | The CLI above. |
| `scripts/irbox/` | Library: `protocol` (frames, SysEx 8→7 packing, parsers, denylist), `midi` (python-rtmidi transport), `ble` (bleak transport), `model` (preset and name-table decoding), `wav` (IR conversion and WAV I/O). |
| `scripts/probe.py` | Low-level probing: `read TYPE ADDR LEN`, `regions`, `monitor SECONDS`, `poll SECONDS [--full]`, `query [CODE]`, `irtest same\|dirac\|dirac-be\|dirac-full\|scale-le\|scale-be [--refresh]`, `backup DIR`, `save SLOT_INDEX --yes-flash`, `write TYPE ADDR HEX --yes`. `write` is limited to an allowlist: type 5 `0x11`, `0x12`, `0x2B`, `0xA0000000`, the EQ range `0x1C00`–`0x1C27`, and slot select (type 4 `0xE0000000+i`). probe.py uses slot indices 0–31. |
| `scripts/measure.py` | USB audio measurement: sweep loopback and record-only band report. See [docs/measurement.md](docs/measurement.md). |
| `scripts/sequence_volume_eq.ps1`, `sequence_eq_bands.ps1`, `sequence_eq_units.ps1` | Scripted A/B recordings of volume and EQ changes on the working copy (never save). |

## Docs

| Document | Content |
|---|---|
| [docs/protocol.md](docs/protocol.md) | Canonical protocol: framing, commands, address map, preset and EQ layout, refresh, select/save, quirks, worked hex examples |
| [docs/usb.md](docs/usb.md) | USB identity, MIDI ports, SysEx wrapping and 8→7 packing, sizes and timing |
| [docs/protocol-ble.md](docs/protocol-ble.md) | BLE GATT transport (from app code, untested) |
| [docs/hardware.md](docs/hardware.md) | Controls, display, USB audio path, headphone notes, content found on the unit |
| [docs/measurement.md](docs/measurement.md) | How the audio measurements and A/B tests work, and their limits |
| [docs/probing-log.md](docs/probing-log.md) | Dated log of the device probing sessions |
| [docs/apk-analysis.md](docs/apk-analysis.md) | How the protocol was recovered from the official app, and the app's converter bugs |

## License

MIT, see [LICENSE](LICENSE).

## Credits

The SysEx packing and the idea that M-VAVE devices carry the `00 59` frames in
SysEx come from community work on related devices:

* [pferreir/cuvave-midi](https://github.com/pferreir/cuvave-midi): Cube Baby
  over USB-MIDI. Packing algorithm and reference vectors.
* [jvsobrinho/mvave-blackbox-ble](https://github.com/jvsobrinho/mvave-blackbox-ble):
  Blackbox BLE and USB SysEx notes.
* [michaelforney's gist](https://gist.github.com/michaelforney/d3a2790bb5f8cbcb1c931eabb50b5f20):
  M-VAVE SysEx notes.
* [cbix/mvave-chocolate-sysex](https://github.com/cbix/mvave-chocolate-sysex):
  M-VAVE Chocolate SysEx.
