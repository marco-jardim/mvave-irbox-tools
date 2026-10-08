# USB side: identity, MIDI ports, SysEx transport

Markers as in [protocol.md](protocol.md): **verified on device**, **from app
code**, **inferred**. The frame protocol carried inside the SysEx is in
[protocol.md](protocol.md). This page covers the USB device, the MIDI ports,
and the SysEx wrapping and 8→7 packing.

## 1. Device identity (verified on device)

| Item | Value |
|---|---|
| Vendor ID | `0x4353` |
| Product ID | `0x4B4D` |
| Product string | `SINCO` |
| Interfaces | USB Audio (44.1 kHz stereo capture and playback, see [hardware.md](hardware.md)) + USB MIDI |
| Firmware (query `0x11`) | `IR-BOX_010` |
| rtmidi port names (Windows / WinMM) | input `USB-Midi 0`, output `USB-Midi 1` |

The trailing number is added by rtmidi on WinMM. It is the port's position in
rtmidi's input or output list, not a property of the device (**inferred**). On
another PC the numbers can differ. The toolkit matches the substring `USB-Midi`
(case-insensitive) and takes the first match. `--midi-in` / `--midi-out`
override it with an index or a name substring. `midi-ports` lists what rtmidi
sees.

### WinMM ports are exclusive

On Windows a WinMM MIDI port can be open in only one process at a time
(**verified on device**). A stuck probe process blocked later runs with:

```
MidiInWinMM::openPort: error creating Windows MM MIDI input port
```

Close CubeSuite, DAWs, MIDI monitors and any leftover Python processes before
running the tools. The A/B PowerShell scripts open and close the port once per
step, so they must never overlap.

## 2. Why USB-MIDI SysEx

Before probing, the conclusion was **inferred** from these points:

* The Android CubeSuite app has no USB code.
* The Windows CubeSuite V2.8.10 (Qt5) links RtMidi on WinMM and contains an
  `IRBox` class and the strings `IR-BOX` and `IMPULSE-R`. This comes from string
  and symbol inspection only; no code was copied.
* Other M-VAVE / Cuvave devices (Cube Baby, Blackbox, Chocolate) carry the same
  `00 59` framing in SysEx (see Sources).

It is now **verified on device**: the IR Box answers the BLE frame protocol,
wrapped in SysEx, on its USB-MIDI ports.

## 3. SysEx wrapping (verified on device)

A raw frame `00 59 <cmd> <len u24> <body> <cs>` is sent as:

```
f0 <encode7(frame)> f7
```

There is no manufacturer ID. Because `00 59` packs to `00 32`, every message
starts `f0 00 32`.

### 3.1 Packing: `encode7`

The frame is treated as one continuous LSB-first bit stream: stream bit `j` is
bit `j % 8` of byte `j // 8`. Output byte `k` carries stream bits
`7k .. 7k+6`, so it is always below `0x80`.

```
accum = 0; nbits = 0
for each byte b:
    accum |= b << nbits; nbits += 8
    while nbits >= 7:
        emit(accum & 0x7f); accum >>= 7; nbits -= 7
emit(accum & 0x7f)          # final flush byte, emitted even when nbits == 0
```

`n` raw bytes become `n + n // 7 + 1` packed bytes. When `n` is a multiple of 7,
the flush byte is a pure `00` pad.

* The IR Box always sends the flush byte (**verified on device**: every
  captured reply has this length).
* The toolkit also sends it, and the device accepts it (**verified on device**).
* Community captures of other devices also show a short form without the pad,
  `ceil(8n / 7)` bytes. Whether the IR Box accepts the short form is untested.
  `irbox.protocol.encode7(raw, flush_zero=False)` produces it.

### 3.2 Unpacking: `decode7`

```
n_raw = (7 * len(packed)) // 8
output byte k = stream bits 8k .. 8k+7     (k < n_raw; leftover bits dropped)
```

If the result starts with `00 59`, trim it to the header length `6 + len + 1`.
Drop extra trailing bytes only if they are all zero. Fewer bytes than announced
is an error.

Pitfall: never strip trailing `00` bytes from the packed payload to "remove the
pad". A frame can end in a zero checksum, and then its last packed bytes are
zero too. Decoding by bit count and trimming by the header length handles the
padded and unpadded forms without losing data.

### 3.3 Reference vectors

From the unit tests of pferreir/cuvave-midi (Cube Baby), reproduced in
`tests/test_sysex.py`:

| Raw frame | Packed (between `f0` and `f7`) |
|---|---|
| `00 59 22 09 00 00 05 09 00 00 80 01 00 00 01 6f` (Cube Baby write) | `00 32 09 49 00 00 40 02 09 00 00 00 18 00 00 00 01 5e 01` |
| `00 59 11 00 00 00 ff` (query) | `00 32 45 00 00 00 40 7f 00` |
| `00 59 00 00 00 00 ff` (empty ACK / init) | `00 32 01 00 00 00 40 7f 00` |

Captured on the IR Box (**verified on device**):

| Frame | Raw | Wire |
|---|---|---|
| Query `0x11` | `00 59 11 00 00 00 ff` | `f0 00 32 45 00 00 00 40 7f 00 f7` |
| Write ACK ok | `00 59 00 01 00 00 00 ff` | `f0 00 32 01 08 00 00 00 00 7f 01 f7` |
| Error ACK | `00 59 00 01 00 00 01 fe` | `f0 00 32 01 08 00 00 40 00 7e 01 f7` |
| Read type 4 addr 0 len 16 | `00 59 23 08 00 00 04 00 00 00 00 10 00 00 eb` | `f0 00 32 0d 41 00 00 00 02 00 00 00 00 00 02 00 00 6b 01 f7` |
| Write volume 32 | `00 59 22 09 00 00 05 2b 00 00 00 01 00 00 20 ae` | `f0 00 32 09 49 00 00 40 02 2b 00 00 00 10 00 00 00 20 5c 02 f7` |

More examples are in [protocol.md](protocol.md) section 17.

A community capture, `f0 00 32 09 49 00 00 40 02 1b 00 00 00 18 00 00 00 01 3a 01 f7`,
is a Cube Baby write to type 5 `0x8000001B`. On the IR Box that address is in
the name-table region. Do not replay it.

## 4. Sizes and timing over USB-MIDI (verified on device)

| Item | Value |
|---|---|
| Read chunk | up to 1000 B per request works. A 1000-byte read is a 1015-byte frame, about 1163 bytes of SysEx, and arrives intact through python-rtmidi / WinMM. Larger reads untested. |
| Name table | must be read in **one** 544-byte request. The device ignores the offset ([protocol.md](protocol.md) quirk 2). Chunked reads garble the names. |
| Preset block | 8192 B in 1000-byte chunks. Never let a request cross offset 8192. |
| Write chunk | 173 B, as in the app. The 6164-byte IR block takes 36 chunks, about 70–80 ms in total including ACKs. |
| Reply latency | about 2 ms per request/reply pair |
| Save | the ACK arrives about 77 ms after the save frame |
| Unanswered queries | `0x13`, `0x17`, `0x1b`, `0x20`: no reply; the caller times out |

## 5. Toolkit implementation

* `irbox.protocol` (pure, no I/O): `encode7`, `decode7`, `wrap_sysex`,
  `unwrap_sysex`, frame builders, `SysExAssembler` (reassembles `f0..f7` from
  fragments), reply parsing (query reply, read response, ACK).
* `irbox.midi`: python-rtmidi transport. Opens the input with
  `ignore_types(sysex=False)`, collects complete SysEx on the rtmidi callback
  thread, and decodes it in the caller's thread. `transact(frame, expect_cmd,
  timeout)` is the generic request/reply primitive.
* Safety: command `0x21` is refused before anything is sent, on every
  transport.
* Trace (`--trace FILE`): JSONL records with `t`, `dir` (`TX` / `RX` / `EVT`),
  `sysex` (wire hex), `decoded` (raw frame hex), and `midi` for unrelated MIDI
  messages. `EVT` records note port opens, e.g.
  `opened in='USB-Midi 0' out='USB-Midi 1'`.

## 6. Memory map caveat

The Cube Baby map documented by the community differs from the IR Box map. On
the Cube Baby, type 4 is the IR region and `0x80000000+` holds parameters. The
IR Box map in [protocol.md](protocol.md) section 10 came from the Android app
and is now **verified on device**. Do not assume anything from other M-VAVE
maps.

## 7. Open questions

* Whether the device accepts the short (unpadded) SysEx form.
* Largest SysEx the device accepts (reads > 1000 B, writes > 173 B).
* What the second MIDI port, if any, carries. Only the `USB-Midi 0` in /
  `USB-Midi 1` out pair was used.

## Sources

* github.com/pferreir/cuvave-midi: Rust crate for the Cube Baby over USB-MIDI.
  Source of the packing description and reference vectors.
* github.com/jvsobrinho/mvave-blackbox-ble: see `docs/usb_sysex_protocol.md`
  (M-VAVE Blackbox USB SysEx notes).
* gist.github.com/michaelforney/d3a2790bb5f8cbcb1c931eabb50b5f20: community
  notes on M-VAVE SysEx.
* github.com/cbix/mvave-chocolate-sysex: M-VAVE Chocolate SysEx.
