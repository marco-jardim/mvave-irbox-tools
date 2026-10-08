# M-VAVE IR Box protocol

Canonical protocol reference for the M-VAVE IR Box, firmware `IR-BOX_010`,
probed over USB-MIDI from Windows on 2026-10-08
([probing-log.md](probing-log.md)).

Each fact carries one of three source markers:

| Marker | Meaning |
|---|---|
| **verified on device** | Observed on the real unit over USB-MIDI (trace or measurement). |
| **from app code** | Read from the official Android app by static analysis ([apk-analysis.md](apk-analysis.md)); not contradicted on the device but not independently tested. |
| **inferred** | Deduced from the above; treat as a hypothesis. |

All hex is lowercase. Multi-byte integers are little-endian unless stated.
"Slot index" is 0–31 on the wire. The display and the CLI show index + 1.

Related pages: [usb.md](usb.md) (SysEx transport and 8→7 packing),
[protocol-ble.md](protocol-ble.md) (BLE GATT transport),
[hardware.md](hardware.md) (controls, audio path, factory content),
[measurement.md](measurement.md) (how the audio effects were measured).

## 1. Transports

| Transport | Carrier | Status |
|---|---|---|
| USB-MIDI | Raw frames (section 3) packed 8→7 and wrapped in SysEx `f0 … f7` | **verified on device**: every result in this document was obtained this way. |
| BLE GATT | The same raw frames, unwrapped, on service `ae40` (write `ae41`, notify `ae42`) | **from app code**. The unit was never seen advertising during a Windows BLE scan, so BLE is untested. See [protocol-ble.md](protocol-ble.md). |

The protocol is strictly request/response. The device sends nothing
unsolicited: no SysEx, notes, CCs or other MIDI messages while knobs are turned,
buttons are pressed or bypass is toggled (**verified on device**). Program Change
on all 16 channels is ignored (**verified on device**).

## 2. SysEx wrapping (USB-MIDI)

A raw frame is sent as `f0 <encode7(frame)> f7`, with no manufacturer ID
(**verified on device**). `encode7` treats the frame as an LSB-first bit stream
and emits 7 bits per output byte. The device always emits the final flush byte,
so `n` raw bytes become `n + n // 7 + 1` packed bytes (**verified on device**).
Because `00 59` packs to `00 32`, every message starts `f0 00 32`. Algorithm,
reference vectors and decoding pitfalls are in [usb.md](usb.md).

## 3. Frame format

```
offset  0   1   2    3..5          6 .. 6+len-1   6+len
        00  59  cmd  len (u24 LE)  body           cs
```

| Field | Size | Meaning | Source |
|---|---|---|---|
| sync | 2 | always `00 59` | **verified on device** |
| cmd | 1 | command (section 5) | **verified on device** |
| len | 3 | body length, u24 LE. Total frame = `7 + len` | **verified on device** |
| body | len | command-specific | **verified on device** |
| cs | 1 | checksum (section 4) | **verified on device** |

## 4. Checksum

`cs = (~sum(body)) & 0xff`. The six header bytes are not included. An empty body
gives `cs = ff`. For reads and writes the body starts with the region type byte,
so the sum covers type, address, length and data. **Verified on device** for
every frame type in section 17. The device also sends a valid checksum on junk
replies (section 16, quirk 1), so a valid checksum does not prove the reply is
correct.

Example: the `0x11` reply body is `"IR-BOX_010"` followed by 17 zero bytes.
The sum is `0x2a1`, the low byte is `a1`, and `~a1 = 5e`.

## 5. Commands

| Cmd | Name (app) | Direction | Behavior on this unit | Source |
|---|---|---|---|---|
| `00` | ACK / status | device → host | Reply to every write, and to reads of unsupported region types. Body = 1 status byte (section 6). | **verified on device** |
| `11` | name + version query | host → device | Reply `cmd 11`, 27-byte body: 16-byte NUL-padded name `IR-BOX_010`, then 11 zero bytes. | **verified on device** |
| `12` | "other info" query | host → device | Reply `cmd 12`, body = 6 zero bytes. Meaning unknown. | **verified on device** |
| `13` | firmware version query | host → device | No reply (timeout). | **verified on device** |
| `15` | MIDI write | – | Defined in the app, never used by it. Not sent by this toolkit. | **from app code** |
| `17` | device type query | host → device | No reply. | **verified on device** |
| `1b` | sound data address query | host → device | No reply. | **verified on device** |
| `20` | codec value query | host → device | No reply. | **verified on device** |
| `21` | ERASE | – | **FORBIDDEN.** Exists in the app. The toolkit refuses to send it on any transport. Never test it. | **from app code** |
| `22` | WRITE | host → device | Section 8. | **verified on device** |
| `23` | READ | host → device / reply | Section 7. | **verified on device** |

All queries have an empty body: `00 59 <cmd> 00 00 00 ff`. Command names come
from the app (**from app code**); the app itself never sends the query commands.

## 6. ACK / status frame

```
00 59 00 01 00 00 <status> <cs>
```

| Status | Meaning | Example | Source |
|---|---|---|---|
| `00` | accepted | `00 59 00 01 00 00 00 ff` | **verified on device** (every accepted write, select, refresh, save) |
| `01` | error | `00 59 00 01 00 00 01 fe` | **verified on device** (reads of region types 0, 1, 2, 3, 6, 7) |

Other status values were never observed. The empty-body frame
`00 59 00 00 00 00 ff` appears in community reference vectors for other M-VAVE
devices. The IR Box never sent it, and reads and writes work without it
(**verified on device**).

## 7. Read (`23`)

Request, always 15 bytes, with header `len = 8`:

```
00 59 23 08 00 00 | tt | aa aa aa aa | ll ll ll | cs
                    type  addr u32 LE  len u24 LE
```

Reply: `00 59 23 <N+8 u24> | tt | addr | N u24 | data[N] | cs`, which is
`N + 15` bytes in total. The device echoes type, address and length
(**verified on device**). Reads of region types the device does not serve get
an error ACK instead of a `23` reply (**verified on device**).

| Constraint | Value | Source |
|---|---|---|
| Max length per request | 1000 B tested and working; larger untested | **verified on device** |
| Name table | must be read as one 544-byte request (quirk 2) | **verified on device** |
| Preset block | a read must not cross offset 8192 (quirk 3) | **verified on device** |
| Active slot | type 4 `0x20000000` returns valid data only for length 1 | **verified on device** |
| Latency | replies arrive about 2 ms after the request in traces | **verified on device** |
| App pacing | 1000-byte chunks, 3 s timeout, 200 ms between chained reads | **from app code** |

## 8. Write (`22`)

```
00 59 22 <N+8 u24> | tt | aa aa aa aa | ll ll ll | data[N] | cs
```

The device answers every write with an ACK (section 6). **Verified on device**
for types 4 and 5 at the addresses in section 10.

| Constraint | Value | Source |
|---|---|---|
| Chunk size | 173 B per frame at `base + k*173`. Wait for each ACK before sending the next frame. | **from app code**, **verified on device** (36 chunks for the 6164-byte IR block, about 70–80 ms total) |
| Larger chunks | untested | – |
| Save latency | the ACK for a save (`0xF0000000+i`) arrives after about 77 ms (flash write) | **verified on device** |

## 9. Region types

The type byte selects a memory region. Names are from the app.

| Type | App name | Read result on this unit | Used |
|---|---|---|---|
| 0 | NOR | error ACK, status 1 | no |
| 1 | NAND | error ACK, status 1 | no |
| 2 | SD | error ACK, status 1 | no |
| 3 | EFF | error ACK, status 1 | no |
| 4 | DEV | replies; device control (active slot, select) | yes |
| 5 | USR | replies; working copy, name table, refresh, save | yes |
| 6 | RSV1 | error ACK, status 1 | no |
| 7 | RSV2 | error ACK, status 1 | no |

Types 0–3 and 6–7 were read at 11 addresses each: `0`, `0x100`, `0x1000`,
`0x10000`, and `0x20000000`, `0x40000000`, `0x80000000`, `0xA0000000`,
`0xC0000000`, `0xE0000000`, `0xF0000000`. Every read returned status 1
(**verified on device**). Nothing was ever written to those types.

## 10. Address map

| Type | Address | Length | Access | Meaning | Source |
|---|---|---|---|---|---|
| 4 | `0x20000000` | 1 (only) | R | Active slot index 0–31. Display shows index + 1. | **verified on device** |
| 4 | `0xE0000000 + i` | 1 | W `[i]` | Select slot `i` (section 14) | **verified on device** |
| 5 | `0x0000`–`0x1FFF` | ≤1000 per read | R/W | Working copy of the active preset (section 11) | **verified on device** |
| 5 | `0x0000` | 24 | W | Header: name, cab_on, eq_on, `"patch"` | **verified on device** |
| 5 | `0x0011` | 1 | W | cab_on (IR on/off) | **verified on device** |
| 5 | `0x0012` | 1 | W | eq_on | **verified on device** |
| 5 | `0x0018` | 6164 | W (173-B chunks) | Cab header + IR samples (offsets 24–6187). **Includes the volume byte at 43.** | **verified on device** |
| 5 | `0x002B` | 1 | W | Volume 0–127 | **verified on device** |
| 5 | `0x1C00` | 40 | W | EQ block (section 12). Single fields can be written on their own. | **verified on device** |
| 5 | `0x80000000` | 544, one request | R | Name table (section 13) | **verified on device** |
| 5 | `0xA0000000` | 1 | W `[mode]` | Refresh / apply (section 14) | **verified on device** |
| 5 | `0xF0000000 + i` | 1 | W `[0]` | Save the working copy to slot `i` (section 14) | **verified on device** |

Type-5 write addresses below `0x2000` are plain byte offsets into the working
copy. This was **verified on device** for every address listed above (header,
cab_on, eq_on, IR block, volume, and individual EQ fields `0x1C00`, `0x1C02`,
`0x1C08`, `0x1C0E`, `0x1C24`, `0x1C26`). It is **inferred** for other offsets,
which were never written.

Stored slots are not directly addressable. The only way to read slot `i` is to
select it, which replaces the working copy (**inferred** from the region sweep:
no other readable region holds preset data).

### Unmapped addresses

On types 4 and 5, reads outside the map do not fail. They return a stale buffer
(quirk 1). **Verified on device** for type 4 at every sweep address, including
`0x20000000` read with length 16. Also verified for type 5 at `0x10000`, `0x20000000`,
`0x40000000`, `0xA0000000`, `0xC0000000`, `0xE0000000` and `0xF0000000`. Clients
must validate addresses themselves.

## 11. Preset block layout (8192 bytes, type 5 `0x0`)

Layout **from app code**. It matches the device byte for byte (**verified on
device**). Every region the app does not define reads as `0xff`, i.e. erased
flash, in all 32 slots.

| Offset | Hex | Size | Field | Encoding / notes | Source |
|---|---|---|---|---|---|
| 0 | `0x0000` | 17 | name | ASCII, NUL-terminated, max 16 chars. Bytes after the NUL can hold leftovers. | **verified on device** |
| 17 | `0x0011` | 1 | cab_on | 0/1. `0xff` observed on display slots 7, 25 and 30 (meaning unknown). | **verified on device** |
| 18 | `0x0012` | 1 | eq_on | 0/1 | **verified on device** |
| 19 | `0x0013` | 5 | magic | `"patch"` | **verified on device** |
| 24 | `0x0018` | 4 | cab magic | `"CAB\0"` | **verified on device** |
| 28 | `0x001C` | 14 | cab name | ASCII. Empty in observed factory slots. | **verified on device** |
| 42 | `0x002A` | 1 | cab type | `0x02` | **verified on device** (the app writes `0x02` on upload) |
| 43 | `0x002B` | 1 | volume | 0–127, linear amplitude (section 15) | **verified on device** |
| 44 | `0x002C` | 6144 | IR | 2048 × s24 LE (section 13) | **verified on device** |
| 6188 | `0x182C` | 980 | unused | all `0xff` | **verified on device** |
| 7168 | `0x1C00` | 40 | EQ | section 12 | **verified on device** |
| 7208 | `0x1C28` | 984 | unused | all `0xff` | **verified on device** |

The 24-byte header write (`0x00`) covers offsets 0–23. The IR upload (`0x18`,
6164 B) covers offsets 24–6187: a 20-byte cab header followed by 6144 sample
bytes. The cab header includes the volume byte, so an IR upload overwrites the
volume. Re-send the current volume in byte 19 of the cab header. The offsets
are **verified on device**; the app also puts the level in that byte (**from
app code**).

## 12. EQ block (40 bytes at `0x1C00`)

| Offset | Write addr | Size | Field | Encoding | Range | Source |
|---|---|---|---|---|---|---|
| 7168 | `0x1C00` | 9 | enable[9] | u8 0/1 per band, index map below | 0–1 | layout **from app code**; indices 0, 2, 8 **verified on device** |
| 7177 | `0x1C09` | 5 | peak Q[5] | u8, Q × 10 | 1–160 (Q 0.1–16) | **from app code**; scale **inferred** |
| 7182 | `0x1C0E` | 5 | peak gain[5] | s8, 0.1 dB | −120…+120 (±12 dB) | **verified on device** |
| 7187 | `0x1C13` | 1 | low-shelf gain | s8, 0.1 dB | ±120 | **from app code**; unit **inferred** (same as peaks) |
| 7188 | `0x1C14` | 1 | high-shelf gain | s8, 0.1 dB | ±120 | **from app code**; unit **inferred** |
| 7189 | `0x1C15` | 1 | reserved | `0xff` observed | – | **verified on device** |
| 7190 | `0x1C16` | 10 | peak freq[5] | u16 LE, Hz | 20–20000 | **from app code** |
| 7200 | `0x1C20` | 2 | low-shelf freq | u16 LE, Hz | 20–20000 | **from app code** |
| 7202 | `0x1C22` | 2 | high-shelf freq | u16 LE, Hz | 20–20000 | **from app code** |
| 7204 | `0x1C24` | 2 | LPF freq | u16 LE, Hz | 20–20000 | **verified on device** (1000 Hz) |
| 7206 | `0x1C26` | 2 | HPF freq | u16 LE, Hz | 20–20000 | **verified on device** (800 Hz) |

Enable index map, **from app code** (EqView):

| Index | Addr | Band | Device check |
|---|---|---|---|
| 0 | `0x1C00` | HPF | **verified on device** |
| 1 | `0x1C01` | low shelf | – |
| 2 | `0x1C02` | peak 1 | **verified on device** |
| 3 | `0x1C03` | peak 2 | – |
| 4 | `0x1C04` | peak 3 | – |
| 5 | `0x1C05` | peak 4 | – |
| 6 | `0x1C06` | peak 5 | – |
| 7 | `0x1C07` | high shelf | – |
| 8 | `0x1C08` | LPF | **verified on device** |

Defaults observed in factory slots (**verified on device**): all bands disabled;
peak Q 5 (Q 0.5); peak gains 0; peak frequencies 100, 400, 1000, 2000 and
4000 Hz; low shelf 80 Hz; high shelf 12000 Hz; LPF 18000 Hz; HPF 40 Hz.

The bands take effect only if eq_on is set and applied with `refresh(1)`.
Band parameters and enable bits are then applied with `refresh(2)`
(**verified on device**, section 14).

## 13. Name table and IR format

### Name table (type 5 `0x80000000`, 544 bytes)

| Item | Value | Source |
|---|---|---|
| Layout | 32 entries × 17 bytes, entry `i` at `i*17` | **verified on device** |
| Entry | ASCII, NUL-terminated (max 16 chars). Bytes after the NUL can be non-zero leftovers. | **verified on device** |
| Charset | printable ASCII 32–126; empty name = unused slot | **from app code** |
| Read | in a single 544-byte request; the device ignores the offset (quirk 2) | **verified on device** |
| Update | written by save from the working-copy header. The entry changes immediately after the save ACK. | **verified on device** |

### IR format

| Item | Value | Source |
|---|---|---|
| Location | preset offsets 44–6187 (upload via the `0x18` block) | **verified on device** |
| Samples | 2048, mono | **verified on device** (layout) / **from app code** (mono) |
| Encoding | signed 24-bit two's complement, **little-endian** | **verified on device** (scale-LE vs scale-BE A/B, [probing-log.md](probing-log.md)) |
| Scale | full scale = ±2^23. Factory IRs peak at about 1.0 FS. | **verified on device** |
| Sample rate | 44.1 kHz (2048 samples = 46.4 ms) | **inferred**: the app resamples to 44.1 kHz and the device's audio clock is 44.1 kHz; no direct rate test |
| Applied | only after `refresh(1)` | **verified on device** |

## 14. Control addresses: refresh, select, save

### Refresh / apply (type 5 `0xA0000000`, data `[mode]`)

Writes to the working copy are ACKed and read back immediately, but they change
the sound only after the matching refresh.

| Mode | Applies | Send after writing | Source |
|---|---|---|---|
| 1 | IR samples, cab_on, eq_on | `0x18` IR block, `0x11`, `0x12` | **verified on device** (measured) |
| 2 | EQ band parameters and enable bits | anything in `0x1C00`–`0x1C27` | **verified on device** (measured) |
| 3 | volume | `0x2B` | **verified on device** (measured) |

Measured evidence:

* Volume 20 without refresh: no change. With `refresh(3)`: −11 dB.
* A new IR changed the sound only after `refresh(1)`.
* eq_on followed by `refresh(2)` had no effect. eq_on followed by `refresh(1)`
  enabled the EQ.

Other mode values were never sent and are not supported by this toolkit.

Recommended EQ order (**verified on device**): write eq_on, send `refresh(1)`,
write band fields, send `refresh(2)`.

### Select (type 4 `0xE0000000 + i`, data `[i]`)

* Loads slot `i` from flash into the working copy, makes it active, and applies
  it in full without a refresh. Unsaved edits to the previous working copy are
  discarded. (**verified on device**)
* The display changes to `i + 1` (**verified on device**).
* Selecting a neighbor slot and coming back restores the stored version. That
  is how the A/B scripts revert their edits (**verified on device**).
  Re-selecting the already active slot was not tested.

### Save (type 5 `0xF0000000 + i`, data `[0]`)

* Sequence (**from app code**, **verified on device**): write the 24-byte header
  at `0x00` (name, cab_on, eq_on, `"patch"`), then write `[0]` to
  `0xF0000000 + i`. The ACK arrives after about 77 ms.
* Writes the entire working copy to flash slot `i` (IR, EQ, volume and header)
  and updates name-table entry `i` (**verified on device**).
* Only tested with `i` equal to the active slot. Saving to a non-active slot is
  untested. The toolkit selects the target slot first.
* Whether the header write is required before a save was not tested. The
  toolkit always sends it, as the app does.

## 15. Persistence model and parameter semantics

| State | Lives in | Changed or lost by | Survives power cycle | Source |
|---|---|---|---|---|
| Working copy (8192 B at type 5 `0x0`) | RAM | select (any slot change), power cycle | no | **verified on device** (select); power cycle **inferred** |
| Stored slots 0–31 | flash | save over it | yes | **verified on device** (renamed slot and copied IR persisted) |
| Name table | flash, rewritten by save | save | yes | **verified on device** |
| Active slot index | persistent | select, `−`/`+` buttons | yes | **verified on device** |
| Knobs (low cut, volume, high cut) | analog controls, not in readable memory | – | – | **verified on device** (see [hardware.md](hardware.md)) |
| Bypass | not visible in any readable memory | – | not tested | **verified on device** |

Parameter semantics:

| Parameter | Semantics | Source |
|---|---|---|
| volume (`0x2B`) | Linear amplitude. 70 → 20 measured −11.0 dB against a predicted 20·log10(20/70) = −10.9 dB. Whether 127 is unity gain is unknown. | **verified on device** |
| peak gain | 0.1 dB steps: +120 → +14 dB, +60 → +6.5 dB, −120 → −5.3 dB in the 60–250 Hz band at 100 Hz | **verified on device**. That the cut reads smaller is **inferred** to be a band-averaging effect: a narrow cut removes less of the band's energy than a boost adds. |
| HPF / LPF freq | Hz. HPF 800: 60–250 Hz −24 dB, 250–1k −15 dB, 1–2.5k unchanged. LPF 1000: 2.5–5k −8 dB. Slopes not characterized. | **verified on device** |
| cab_on | 0 bypasses the IR, 1 enables it; applied by `refresh(1)` | **verified on device** |

## 16. Quirks

1. **Stale reply buffer.** On types 4 and 5, a read outside the map returns a
   well-formed `23` reply with the request echoed and a valid checksum. `data[0]`
   equals the request's own checksum byte. The rest is `0xff`, or the data bytes
   of the previous reply. For type 4 with length 16, `data[0] = 0xeb − sum(addr
   bytes)`. **Verified on device.** Mechanism (**inferred**): the reply is built
   in the receive buffer, and frame offset 14 still holds the request checksum.
2. **Name table ignores the offset.** Every read at `0x80000000 + k` returns
   the table from entry 0. With 128-byte chunks, the chunk at offset 128 started
   again at entry 0, so entry 8 (offset 136) decoded as bytes 8.. of entry 0
   (`"10"` from `"EBS PL 410"`). Read all 544 bytes in one request.
   **Verified on device.**
3. **Reads crossing 8192** in the preset block return junk past the end.
   **Verified on device.**
4. **Active slot is length 1 only.** A 16-byte read at type 4 `0x20000000`
   returns the stale buffer. **Verified on device.**
5. **Unsupported types reply with ACK status 1**, not with a `23` frame. Parsers
   must accept a `00` frame as the reply to a read. **Verified on device.**
6. **Silent queries.** `13`, `17`, `1b` and `20` never reply. Use short timeouts.
   **Verified on device.**
7. **Writes are inaudible until refreshed**, and eq_on needs mode 1, not mode 2
   (section 14). **Verified on device.**
8. **IR upload overwrites volume** (section 11). The 6164-byte block covers
   offset 43. The offsets are **verified on device**; that the firmware does
   not special-case the byte is **inferred**.
9. **Name fields contain leftovers after the NUL.** Compare names only up to the
   NUL, and back up and restore whole 17-byte fields. **Verified on device.**
10. **cab_on = `0xff`** on three factory slots instead of 0/1. Its meaning is
    unknown. **Verified on device.**
11. **No MIDI output** for front-panel actions, and **Program Change is ignored**.
    **Verified on device.**

## 17. Worked examples

Captured on the device unless marked computed. Wire = SysEx bytes on the USB
cable.

### 17.1 Query `0x11`

```
TX raw   00 59 11 00 00 00 ff
TX wire  f0 00 32 45 00 00 00 40 7f 00 f7
RX raw   00 59 11 1b 00 00
         49 52 2d 42 4f 58 5f 30 31 30 00 00 00 00 00 00   "IR-BOX_010" (16-byte field)
         00 00 00 00 00 00 00 00 00 00 00                  11 zero bytes
         5e
RX wire  f0 00 32 45 58 01 00 40 24 52 5a 08 7a 04 6b 17 18 31 60
         (19 × 00) 40 17 f7
```

### 17.2 Query `0x12`

```
TX raw   00 59 12 00 00 00 ff           wire f0 00 32 49 00 00 00 40 7f 00 f7
RX raw   00 59 12 06 00 00 00 00 00 00 00 00 ff
RX wire  f0 00 32 49 30 00 00 00 00 00 00 00 00 00 60 3f f7
```

### 17.3 Read the active slot

```
TX raw   00 59 23 08 00 00 04 00 00 00 20 01 00 00 da
TX wire  f0 00 32 0d 41 00 00 00 02 00 00 00 00 12 00 00 00 5a 01 f7
RX raw   00 59 23 09 00 00 04 00 00 00 20 01 00 00 17 c3     slot index 0x17 = 23 (display 24)
RX wire  f0 00 32 0d 49 00 00 00 02 00 00 00 00 12 00 00 00 17 06 03 f7
```

### 17.4 Read outside the map (stale buffer, quirk 1)

Type 4, address 0, length 16:

```
TX raw   00 59 23 08 00 00 04 00 00 00 00 10 00 00 eb
TX wire  f0 00 32 0d 41 00 00 00 02 00 00 00 00 00 02 00 00 6b 01 f7
RX raw   00 59 23 18 00 00 04 00 00 00 00 10 00 00
         eb ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff   data[0] = request checksum
         0f                                               checksum is valid
```

### 17.5 Read on an unsupported type (error ACK)

Type 0, address 0, length 16:

```
TX raw   00 59 23 08 00 00 00 00 00 00 00 10 00 00 ef
RX raw   00 59 00 01 00 00 01 fe
RX wire  f0 00 32 01 08 00 00 40 00 7e 01 f7
```

### 17.6 Write volume, apply, ACK

```
TX raw   00 59 22 09 00 00 05 2b 00 00 00 01 00 00 20 ae     volume = 0x20 (32)
TX wire  f0 00 32 09 49 00 00 40 02 2b 00 00 00 10 00 00 00 20 5c 02 f7
RX raw   00 59 00 01 00 00 00 ff                             ACK ok
RX wire  f0 00 32 01 08 00 00 00 00 7f 01 f7
TX raw   00 59 22 09 00 00 05 00 00 00 a0 01 00 00 03 56     refresh(3)
TX wire  f0 00 32 09 49 00 00 40 02 00 00 00 00 1a 00 00 00 03 2c 01 f7
```

`refresh(1)` ends `… 01 58` and `refresh(2)` ends `… 02 57` (computed).

### 17.7 EQ: enable the LPF at 1 kHz

```
00 59 22 09 00 00 05 08 1c 00 00 01 00 00 01 d4           enable[8] = 1 (LPF)
00 59 22 0a 00 00 05 24 1c 00 00 02 00 00 e8 03 cd        lpHz = 1000
00 59 22 09 00 00 05 00 00 00 a0 01 00 00 02 57           refresh(2) (computed)
```

This assumes eq_on was already applied:
`00 59 22 09 00 00 05 12 00 00 00 01 00 00 01 e6` followed by `refresh(1)`
(computed).

### 17.8 Select slot index 22 (display 23)

```
TX raw   00 59 22 09 00 00 04 16 00 00 e0 01 00 00 16 ee
TX wire  f0 00 32 09 49 00 00 00 02 16 00 00 00 1e 00 00 00 16 5c 03 f7
```

### 17.9 Rename and save slot index 31 (display 32)

```
header   00 59 22 20 00 00 05 00 00 00 00 18 00 00
         54 45 53 54 20 53 41 56 45 00 00 00 00 00 00 00 00    "TEST SAVE" (17)
         01 00 70 61 74 63 68                                  cab_on=1 eq_on=0 "patch"
         42
save     00 59 22 09 00 00 05 1f 00 00 f0 01 00 00 00 ea
wire     f0 00 32 09 49 00 00 40 02 1f 00 00 00 1f 00 00 00 00 54 03 f7
ACK      00 59 00 01 00 00 00 ff   (about 77 ms after the save frame)
```

### 17.10 IR upload framing

Computed. The 6164-byte block at `0x18` is sent as 35 chunks of 173 bytes plus
one chunk of 109 bytes. The first chunk header is
`00 59 22 b5 00 00 05 18 00 00 00 ad 00 00 …` (header len 181 = `0xb5`,
data len 173 = `0xad`). The last chunk starts at `0x17BF`. Follow with
`refresh(1)`.

### 17.11 Name table read

```
TX raw   00 59 23 08 00 00 05 00 00 00 80 20 02 00 58     544 bytes
RX       00 59 23 28 02 00 05 00 00 00 80 20 02 00 <544 B> cs
```

## 18. Hazards

* **Never send `0x21` (erase).** The toolkit has a command denylist on every
  transport.
* **Never touch the BLE OTA characteristics** (`ae00`, `ae01`, `ae02`). See
  [protocol-ble.md](protocol-ble.md).
* **Save overwrites a flash slot.** Back up first. The CLI makes an automatic
  backup and requires `--yes`.
* **Do not write outside the map.** The device does not reject unmapped
  addresses on types 4 and 5. Their effect on writes is unknown.
* **Do not send undocumented refresh modes** or writes to type 4 other than
  select.

## 19. Open questions

* Meaning of the `0x12` reply (6 zero bytes), and why `13`, `17`, `1b` and `20`
  are silent.
* Whether save works for a non-active slot, and whether the header write is
  required.
* Q scale and shelf-gain units (not measured). Filter slopes and peak bandwidth.
* Meaning of cab_on = `0xff`.
* Whether volume 127 equals unity gain.
* Where the bypass state and knob positions live. Neither is readable.
* Whether re-selecting the active slot reloads it from flash.
* Whether unsaved edits survive a power cycle (expected not).
* BLE transport on real hardware.
