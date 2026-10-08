# Hardware notes

Observations on one unit (firmware `IR-BOX_010`) connected to a Windows PC over
USB. Markers as in [protocol.md](protocol.md). Session details are in
[probing-log.md](probing-log.md). The measurement method is in
[measurement.md](measurement.md).

## 1. Controls and display

| Control | Behavior | Visible over MIDI? | Source |
|---|---|---|---|
| `−` button | previous preset | yes: the active slot (type 4 `0x20000000`) changes | **verified on device** |
| `+` button | next preset | yes: same | **verified on device** |
| `−` and `+` together | bypass on/off | **no**: no byte changes in any readable memory | **verified on device** |
| 2-digit display | shows slot index + 1 (1–32) | – | **verified on device** |
| Low cut knob | live analog-style control | **no**: not stored in readable memory, no MIDI sent | **verified on device** |
| Volume knob | live control, independent of the preset volume byte | **no** | **verified on device** |
| High cut knob | live control | **no** | **verified on device** |

How this was checked: for 3+ minutes the tool polled every 0.2 s. Each poll read
the full 8192-byte working copy, the name table, the active slot and the `0x11`
query reply. MIDI input was logged at the same time. Knobs were turned one at a
time and bypass was toggled. Only the `−`/`+` buttons produced any change, in
the active slot. The device sent no MIDI at all.

Consequences:

* Knob positions affect every audio measurement but cannot be read. Keep them
  fixed during an A/B session and note their positions.
* Bypass cannot be detected or controlled over MIDI. Program Change is ignored
  (**verified on device**).
* The preset volume byte (`0x2B`) and the volume knob act as separate gain
  stages (**inferred**: the knob does not change the byte, and the byte changes
  the level).

## 2. USB audio interface

| Item | Value | Source |
|---|---|---|
| Class | USB Audio (enumerates as `USB-Audio` under WASAPI) | **verified on device** |
| Format | 44.1 kHz, stereo capture and stereo playback | **verified on device** |
| Capture content | the processed signal: instrument input → IR / EQ / volume → capture | **verified on device** (IR, EQ and volume edits are audible in the capture) |
| Playback → capture | looped straight back to capture, **not** through the IR. Flat within ±1.5 dB from 100 Hz to 15 kHz, at unity gain. | **verified on device** |

Loopback measurement: a −20 dBFS exponential sweep was played on USB playback
and recorded on USB capture. Recorded peak 0.0986 (−20.1 dBFS, i.e. unity). The
deconvolved response stayed between −2.7 and −5.1 dB relative to the analysis
reference, flat within ±1.5 dB over 100 Hz–15 kHz. A cab IR would show a strong
high-frequency roll-off, so this path does no cab filtering.

Consequence (**inferred**): the IR cannot be measured by reamping through USB.
To measure the processed path, feed the instrument input from an external
interface, or record a live instrument and compare takes
([measurement.md](measurement.md)).

## 3. Headphone output

| Observation | Source |
|---|---|
| Sounded quiet in normal use. | **verified on device** (listening) |
| Did not reveal an obviously broken IR. The big-endian garbage IR filled the USB capture with broadband noise and clipped it at 0 dBFS, but the headphones did not show this clearly. | **verified on device** (listening) |

Hypothesis (**inferred**, open question): the headphone feed is a pre-IR or
differently mixed monitor signal, not the USB capture signal. Until that is
resolved, judge results on the USB capture, not on headphones.

## 4. Content found on this unit

These 32 slots were read from this unit. They are probably factory presets, but
some could have been modified by a user. Volume is the preset byte at `0x2B`.

| Display | Index | Name | Volume | Notes |
|---|---|---|---|---|
| 1 | 0 | EBS PL 410 | 60 | |
| 2 | 1 | Hartke XL410 | 90 | |
| 3 | 2 | TCElectronicBC41 | 60 | |
| 4 | 3 | AmpegHeritageB15 | 45 | |
| 5 | 4 | Sunn 200s | 45 | |
| 6 | 5 | Mesa RR215 | 55 | |
| 7 | 6 | Ampeg SVT810E | 63 | cab_on = `0xff` |
| 8 | 7 | ElectroVoiceM12L | 63 | |
| 9 | 8 | Orange PPC212 | 50 | |
| 10 | 9 | Celestion V30 | 50 | |
| 11 | 10 | Sunn 200s MK3 | 60 | |
| 12 | 11 | Ampeg SVT810E MK | 45 | |
| 13 | 12 | Null Cab Mono | 127 | band-limited impulse, peak 0.30, EQ on |
| 14 | 13 | Null Cab Stereo | 127 | band-limited impulse, peak 0.43, EQ on |
| 15 | 14 | VoxAC30 2 | 50 | |
| 16 | 15 | TwinD120s 2 | 50 | |
| 17 | 16 | ShowmanD130s 2 | 65 | |
| 18 | 17 | Orange4X12 | 50 | |
| 19 | 18 | Diezel V30 | 55 | |
| 20 | 19 | Mesa Boogie | 25 | |
| 21 | 20 | Electrovoice | 60 | |
| 22 | 21 | Rimental | 55 | |
| 23 | 22 | Supro | 45 | |
| 24 | 23 | Matchless ES212 | 70 | peak 1.0, DC gain +6.3 dB, energy +7 dB, last non-zero sample ≈ 1680 |
| 25 | 24 | MesaRectifierV30 | 50 | cab_on = `0xff` |
| 26 | 25 | Jensen 4050 BASS | 50 | |
| 27 | 26 | Studio 441 BASS | 25 | |
| 28 | 27 | AMPEG DIS BASS | 40 | |
| 29 | 28 | Aguilar  BASS | 37 | name contains two spaces |
| 30 | 29 | TP5 421 BASS | 20 | cab_on = `0xff` |
| 31 | 30 | EDEN DIS BASS | 40 | |
| 32 | 31 | Work 4050 2 BASS | 20 | used for the save / power-cycle tests, then restored byte-identical |

Properties of the stored IRs (**verified on device**, from the full backup):

* They are 16-bit sources bit-replicated to 24 bits: `s24 = (s16 << 8) | (s16
  >> 8 & 0xff)`, which maps 16-bit full scale to 24-bit full scale. Peaks are
  about 1.0 FS, so the stored IRs are normalized to full scale.
* "Null Cab" slots 13 and 14 hold band-limited impulses, not a pure dirac.
  Their peaks are 0.30 and 0.43, the preset volume is 127, and the EQ is on.
* The unused preset areas (offsets 6188–7167 and 7208–8191) are `0xff`
  (erased flash) in every slot.
* cab_on is `0xff` instead of 0/1 on display slots 7, 25 and 30. Meaning
  unknown.

## 5. Persistence

| Item | Survives power cycle | Source |
|---|---|---|
| Active slot | yes | **verified on device** |
| Saved slot content (name, IR, EQ, volume) | yes | **verified on device** |
| Unsaved working-copy edits | expected no | **inferred** |
