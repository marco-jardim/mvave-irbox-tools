# Probing log

Chronological record of the work on the real device. Markers as in
[protocol.md](protocol.md). The trace files named here (`captures/`, `dumps/`)
are local and git-ignored.

Setup for all entries: Windows PC, IR Box on USB (firmware `IR-BOX_010`),
python-rtmidi on WinMM, ports `USB-Midi 0` (in) / `USB-Midi 1` (out). Audio was
taken from the IR Box's own USB audio device through WASAPI.

---

### 2026-10-08 #1: Identification over USB

* The PC enumerates the unit as VID `0x4353`, PID `0x4B4D`, product string
  `SINCO`, with a USB Audio interface and a USB MIDI interface.
* rtmidi lists the MIDI ports as `USB-Midi 0` (input) and `USB-Midi 1`
  (output). WASAPI lists a `USB-Audio` capture and playback device.
* Details: [usb.md](usb.md) section 1.

### 2026-10-08 #2: BLE scan finds nothing

* `irbox_tool.py scan --all` on Windows: no `IR-BOX` advertiser, and nothing
  attributable to the unit.
* Conclusion: BLE is unusable from this host, so USB-MIDI becomes the primary
  transport. BLE stays implemented but untested
  ([protocol-ble.md](protocol-ble.md)).

### 2026-10-08 #3: APK decompile gives the BLE protocol

* Android CubeSuite decompiled with jadx. Recovered: frame format, checksum,
  READ/WRITE/ERASE commands, region types, address map, preset and EQ layout,
  refresh modes, chunk sizes, GATT UUIDs.
* The Android app has no USB code. The Windows CubeSuite V2.8.10 (Qt5) links
  RtMidi/WinMM and has an `IRBox` class.
* Details: [apk-analysis.md](apk-analysis.md).

### 2026-10-08 #4: Community references show the same framing in SysEx

* pferreir/cuvave-midi, jvsobrinho/mvave-blackbox-ble, the michaelforney gist
  and cbix/mvave-chocolate-sysex describe other M-VAVE devices that carry the
  same `00 59` frames inside SysEx, packed 8→7 with no manufacturer ID.
* The reference vectors were turned into `tests/test_sysex.py`.
* Hypothesis: the IR Box speaks the BLE frame protocol in SysEx over USB.

### 2026-10-08 #5: First query reply

Trace: `captures/query1.jsonl`.

```
TX raw  00 59 11 00 00 00 ff            wire f0 00 32 45 00 00 00 40 7f 00 f7
RX raw  00 59 11 1b 00 00 49 52 2d 42 4f 58 5f 30 31 30 00 … 00 5e
```

* The body is 27 bytes: `IR-BOX_010` NUL-padded to 16, then 11 zero bytes.
  The checksum is valid.
* **Verified on device**: the transport hypothesis, the packing, the checksum
  and the flush byte (the device sends the long form).

### 2026-10-08 #6: First dump; garbled names show the name table ignores the offset

* `irbox_tool.py dump` with 128-byte read chunks. The preset block decoded
  cleanly: name, cab_on, eq_on, `"patch"`, `"CAB\0"`, IR samples, EQ values.
* Offsets 6188–7167 and 7208–8191 are all `0xff` (erased flash). The app's
  layout matches the device exactly.
* The name table came back wrong. Entry 8 read `"10"`, which is bytes 8.. of
  entry 0 (`"EBS PL 410"`). The second chunk, requested at offset 128, had
  started again at entry 0. Conclusion: the device ignores the offset into the
  name table. A single 544-byte read returns all 32 names correctly.
* 1000-byte read chunks also work over USB-MIDI.

### 2026-10-08 #7: Region sweep

Trace: `captures/regions.jsonl` (`probe.py regions`). The sweep read 16 bytes
for every type 0–7 at each address.

| Type | `0x0` | `0x100` | `0x1000` | `0x10000` | `0x20000000` | `0x40000000` | `0x80000000` | `0xA/C/E/F0000000` |
|---|---|---|---|---|---|---|---|---|
| 0, 1, 2, 3, 6, 7 | err | err | err | err | err | err | err | err |
| 4 | stale | stale | stale | stale | stale¹ | stale | stale | stale |
| 5 | preset | preset | preset | stale | stale | stale | names | stale |

`err` = ACK `00 59 00 01 00 00 01 fe` (status 1). `preset` / `names` = real
data. `stale` = junk buffer (entry #8).
¹ Type 4 `0x20000000` is valid only with length 1 (the active slot).

A read in the preset block that crosses offset 8192 also returns junk past
the end.

### 2026-10-08 #8: Junk-buffer quirk

* Every `stale` reply is a well-formed `23` frame that echoes type, address and
  length, and has a valid checksum.
* `data[0]` is the request's own checksum byte. For type 4 with length 16 that
  is `0xeb − sum(addr bytes)`, e.g. `eb` for address 0 and `6b` for
  `0x80000000`.
* The remaining bytes are `0xff` on type 4. On type 5 they are leftovers of the
  previous reply's data. For example, after the name-table read, the
  `0xA0000000` read returned `4a` followed by `42 53 20 50 4c 20 34 31 30 …`
  (`"BS PL 410"`).
* Example (type 4, addr 0, len 16):
  `00 59 23 18 00 00 04 00 00 00 00 10 00 00 eb ff…ff 0f`.
* Consequence: clients must validate addresses against the known map. Neither
  the checksum nor the echo detects a bad address.

### 2026-10-08 #9: Write ACK format

* The first write was a no-op: `probe.py write 5 0x2B 37 --yes` wrote the
  current volume 55 back to the active slot (index 21). Trace:
  `captures/write_noop.jsonl`.
* Reply `00 59 00 01 00 00 00 ff` (wire `f0 00 32 01 08 00 00 00 00 7f 01 f7`):
  command `00`, a 1-byte body with status 0.
* Together with the region sweep this gives status 0 = accepted and status 1 =
  error.

### 2026-10-08 #10: Knob and bypass polling

Traces: `captures/knobs*.jsonl`, `captures/knobs*_poll.txt` (`probe.py monitor`,
`probe.py poll --full --interval 0.2`).

* For 3+ minutes the tool polled every 0.2 s: the full 8192-byte working copy,
  the name table, the active slot and the `0x11` reply. MIDI input was logged
  throughout.
* Turning low cut, volume and high cut one at a time changed **nothing**.
  Toggling bypass (`−` and `+` together) changed **nothing**. No MIDI messages
  arrived.
* The `−` and `+` buttons change the active slot. The display shows slot index
  + 1.
* Conclusion: the knobs are live controls that are not stored in readable
  memory. Bypass is not visible. See [hardware.md](hardware.md).

### 2026-10-08 #11: No-op and reversible writes

* Policy: first write values that are already there (entry #9), then only
  changes that can be undone without flash access.
* `probe.py irtest same`: the IR block (6164 B at `0x18`) was rewritten with
  its own contents in 36 chunks of 173 B in about 70–80 ms. All chunks were
  ACKed and the readback was identical.
* Reversible volume change. Trace: `captures/write_test.jsonl`, active slot
  index 23, stored volume 70. Steps are in entry #12.

### 2026-10-08 #12: Working-copy semantics

Trace `captures/write_test.jsonl`:

| Step | Frame(s) | Readback of `0x2B` |
|---|---|---|
| write volume 32 | `… 05 2b 00 00 00 01 00 00 20 ae` | 32 |
| refresh(3) | `… 05 00 00 00 a0 01 00 00 03 56` | 32 |
| select index 22, then index 23 | type 4 `0xE0000016` `[22]`, `0xE0000017` `[23]` | **70** (stored value) |
| write 70, refresh(3) | | 70 |

* Type-5 writes land in a RAM working copy of the active preset. They read back
  immediately.
* Selecting another slot and coming back reloads the stored version from flash,
  which discards the edit. This became the standard way to undo, and it is used
  by all A/B scripts.
* Nothing reaches flash without an explicit save.

### 2026-10-08 #13: Unused query commands

Trace: `captures/queries.jsonl` (`probe.py query`).

| Cmd | App name | Reply |
|---|---|---|
| `11` | name + version | `IR-BOX_010` (16-byte field) + 11 zero bytes |
| `12` | other info | `00 59 12 06 00 00 00 00 00 00 00 00 ff` (6 zero bytes) |
| `13` | firmware version | none (timed out) |
| `17` | device type | none |
| `1b` | sound data address | none |
| `20` | codec value | none |

### 2026-10-08 #14: Refresh requirement

* An IR uploaded to the working copy with `probe.py irtest` without
  `--refresh` did not change the sound, although the readback matched.
* The same upload with `--refresh`, which sends `refresh(1)` afterwards, did
  change the sound.
* Conclusion: written data is applied only after a refresh. Mode 1 applies the
  IR. Modes 2 (EQ) and 3 (volume) were confirmed by measurement in entries
  #18–#20.

### 2026-10-08 #15: Headphone listening is unreliable

* Listening on the headphone output: overall level quiet. The broken
  big-endian IR (entry #17), which fills the USB capture with clipping
  broadband noise, did not stand out clearly on the headphones.
* Conclusion: headphones are not a valid monitor for these tests. Evaluation
  moved to USB capture recordings. Open question: the headphone feed may be
  pre-IR or mixed differently ([hardware.md](hardware.md)).

### 2026-10-08 #16: USB loopback measurement

`measure.py` sweep mode ([measurement.md](measurement.md)):

| Item | Value |
|---|---|
| Sweep | 20 Hz–20 kHz, −20 dBFS |
| Recorded peak | 0.0986 (≈ −20.1 dBFS, unity gain) |
| Response | −2.7 … −5.1 dB relative to the analysis reference |
| Flatness | within ±1.5 dB, 100 Hz–15 kHz |

Conclusion: USB playback is routed straight back to USB capture, not through
the IR. The IR Box cannot be measured by reamping over USB. All further
measurements record a live instrument with `measure.py --record-only`.

### 2026-10-08 #17: Guitar/bass A/B recordings, IR byte order

The bass was played continuously and the IR in the working copy was swapped
with `probe.py irtest … --refresh`.

| Take | IR in working copy | RMS dBFS | Spectrum |
|---|---|---|---|
| original | stored IR of the active slot | −22.1 | cab-shaped |
| `dirac` | dirac, 0.5 FS, LE | −37.5 | – |
| `scale-le` | stored IR read as LE, halved, written LE | −32.3 | **same shape** as original |
| `scale-be` | stored IR read as BE, halved, written BE | −5.1 (peak 0 dBFS) | **flat broadband noise** |

* If the device read samples big-endian, `scale-be` would sound like the
  original and `scale-le` would be noise. The opposite happened.
* **Verified on device**: IR samples are signed 24-bit **little-endian**.
* The level differences between takes are within live-playing variance plus
  the expected IR-energy differences. Only the spectral shape is used as
  evidence.

### 2026-10-08 #18: Volume is linear

`scripts/sequence_volume_eq.ps1`:

| Step | Writes | RMS dBFS |
|---|---|---|
| baseline (volume 70) | – | −28.2 |
| volume 20, no refresh | `0x2B` = 20 | −27.1 (no change) |
| + refresh(3) | `0xA0000000` = 3 | −39.2 |
| restored | `0x2B` = 70, refresh(3) | −29.0 |

* 70 → 20 measured −11.0 dB against a predicted 20·log10(20/70) = −10.9 dB.
  Volume is linear amplitude.
* Volume needs refresh mode 3.

### 2026-10-08 #19: EQ needs refresh(1) after eq_on, plus enable bits

* eq_on written, LPF 1 kHz written, `refresh(2)` only: **no effect**.
* eq_on followed by `refresh(1)` turns the EQ on. Band parameters then take
  effect with `refresh(2)`, but only for bands whose `enable[]` bit is set.
  Enable index map from the app: 0 = HPF, 1 = low shelf, 2–6 = peaks 1–5,
  7 = high shelf, 8 = LPF.
* Scripts: `scripts/sequence_eq_bands.ps1`, `scripts/sequence_eq_units.ps1`.

### 2026-10-08 #20: HPF, LPF and peak units

Band changes relative to the EQ-on flat take (`measure.py` band report):

| Test | Writes (after eq_on + refresh(1)) | 60–250 Hz | 250–1k | 1–2.5k | 2.5–5k |
|---|---|---|---|---|---|
| HPF 800 Hz | `enable[0]`=1, `hpHz`=800, refresh(2) | −24 dB | −15 dB | ≈ 0 | |
| LPF 1 kHz | `enable[8]`=1, `lpHz`=1000, refresh(2) | | | | −8 dB |
| peak 1, 100 Hz, +120 | `enable[2]`=1, `peakDb[0]`=+120, refresh(2) | +14 dB (RMS +13.4 dB) | | | |
| peak 1, 100 Hz, +60 | `peakDb[0]`=+60, refresh(2) | +6.5 dB | | | |
| peak 1, 100 Hz, −120 | `peakDb[0]`=−120, refresh(2) | −5.3 dB | | | |

* Filter frequencies are in Hz (u16 LE). Peak gain is in 0.1 dB steps
  (±120 = ±12 dB). The smaller cut reading is **inferred** to be band
  averaging.
* Live-playing variance is ±3–6 dB per band ([measurement.md](measurement.md)).
* Not measured: Q scale, shelf gains and frequencies, filter slopes.

### 2026-10-08 #21: Program Change ignored

* Program Change was sent on all 16 MIDI channels. The active slot and the
  display did not change.

### 2026-10-08 #22: Full backup

`probe.py backup dumps/backup_full` selects each slot in turn, reads 8192 B and
the name table, then reselects the original slot. Output: `names.bin` and
`slot_00.bin` … `slot_31.bin`.

Preset volume per slot (index 0–31; display = index + 1):

| Index | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Volume | 60 | 90 | 60 | 45 | 45 | 55 | 63 | 63 | 50 | 50 | 60 | 45 | 127 | 127 | 50 | 50 |

| Index | 16 | 17 | 18 | 19 | 20 | 21 | 22 | 23 | 24 | 25 | 26 | 27 | 28 | 29 | 30 | 31 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Volume | 65 | 50 | 55 | 25 | 60 | 55 | 45 | 70 | 50 | 50 | 25 | 40 | 37 | 20 | 40 | 20 |

Observations, with names in [hardware.md](hardware.md):

* The unused areas are `0xff` in all 32 slots.
* The stored IRs are 16-bit sources bit-replicated to 24 bits, with peaks
  around 1.0 FS.
* Index 23 (Matchless ES212): peak 1.0, DC gain +6.3 dB, energy +7 dB, last
  non-zero sample ≈ 1680.
* Indices 12 and 13 (Null Cab Mono / Stereo) hold band-limited impulses with
  peaks 0.30 and 0.43, at volume 127 with the EQ on.
* cab_on = `0xff` on display slots 7, 25 and 30.

### 2026-10-08 #23: Save and power-cycle tests

Traces: `captures/save_test.jsonl`, `captures/save_ir_test.jsonl`. Test slot:
index 31 (display 32, `Work 4050 2 BASS`). The full backup from #22 was taken
first.

1. **Rename.** Header write with name `TEST SAVE` (cab_on 1, eq_on 0,
   `"patch"`), then save `00 59 22 09 00 00 05 1f 00 00 f0 01 00 00 00 ea`. The
   ACK arrived after about 77 ms. Name-table entry 31 read `TEST SAVE`
   immediately. After a power cycle the name was still there, and slot 31 was
   still the active slot.
2. **Restore name.** Wrote the original 24-byte header back and saved.
3. **IR.** Copied an IR from another slot into the working copy, applied it
   with `refresh(1)`, and saved. After a power cycle the slot still read back
   the copied IR.
4. **Restore slot.** Wrote the original slot content back from the backup and
   saved. The slot read back **byte-identical** to `backup_full/slot_31.bin`.

Conclusions (**verified on device**):

* Save stores the whole working copy and updates the name table at once.
* The active slot persists across power cycles.
* A backup/restore round trip is lossless.

---

## Entry template

```
### YYYY-MM-DD #N: short title

- Host / adapter:
- Device firmware / display state:
- Command run:
- Trace file:
- Observations (verified on device / inferred):
- Deviations from docs/protocol.md:
- Follow-ups:
```

## 2026-10-08 — CLI end-to-end on the device

- Command run: `irbox_tool.py info`; `ir-export --slot 24`; `ir-upload test_ir_48k_16bit_stereo.wav --slot 32 --name "RT TEST"` (dry run, then `--yes`); `ir-export --slot 32`; `restore backups/20261008-201408 --slot 32 --yes`; `select 24`.
- Trace file: `captures/cli_upload.jsonl`
- Observations (verified on device):
  - Without `--yes` the upload printed the conversion summary and the plan and exited with status 2 without any I/O.
  - With `--yes`: auto-backup of all 32 slots, upload, save, reload-and-compare all passed.
  - The test IR (48 kHz, 16-bit stereo, 2.5 kHz decaying tone) came back from flash with its spectral peak at 2500.5 Hz, so the 48 kHz to 44.1 kHz resampling preserves pitch. (The official app would have shifted it by about 8.8 %.) A point-sample decay comparison was not meaningful, because single samples of an oscillating tone are not its envelope. An envelope-based decay check is still a follow-up.
  - `restore --slot 32` brought the slot back. A full 32-slot re-dump was then byte-identical to the original backup taken before any flash write.
- Deviations from docs/protocol.md: none.
- Follow-ups: BLE transport still untested on hardware (the unit does not advertise to Windows).

## 2026-10-08 — Factory image found and slots 1–14 restored

- Command run:
  - `factory-restore`-equivalent `restore dumps/factory_BOR --slot 1..14 --yes` (auto-backup `backups/20261008-205252`)
  - raw read of type 5 `0x70000000`…`0x7003FFFF`
  - `factory-list BOR.bin --compare`
  - `backup --raw`
- Trace file: `captures/factory_restore.jsonl`, `captures/raw_flash_read.jsonl`
- Observations (verified on device):
  - CubeSuite for Windows "Restore all factory IR" = erase 64 sectors at type 5 `0x70000000` and write `bin/BOR.bin` (256 KiB) there. Found by static analysis, never executed.
  - The window is readable. The full 256 KiB matched the select-based backup byte for byte.
  - BOR.bin slots 15–32 were already identical to the unit. Slots 1–14 differed (custom bass cabs) and were restored through the save path.
  - Afterwards, all 32 slots in raw flash are identical to BOR.bin.
  - `backup --raw` takes about 8 s, against about 60 s for the select-based backup.
- Deviations from docs/protocol.md: new region, documented in the addendum.
- Follow-ups: none.
