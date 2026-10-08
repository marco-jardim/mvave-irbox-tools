# APK and app analysis

How the frame protocol was recovered before any hardware was available, and
which app findings were later checked on the device. Markers as in
[protocol.md](protocol.md).

## Obtaining the app

The official apps were downloaded from M-VAVE:

* CubeSuite.apk: <https://resource.m-vave.com/software/app/CubeSuite.apk>
* Windows CubeSuite.zip: <https://yms-file-store.oss-cn-hongkong.aliyuncs.com/software/pc/CubeSuite.zip>
* MidiSuite_new.zip: <https://yms-file-store.oss-cn-hongkong.aliyuncs.com/software/pc/MidiSuite_new.zip>
* Download page: <https://www.m-vave.com/download>

Binaries are not stored in this repository (`*.apk` and `decompiled/` are
git-ignored).

## Decompilation

The APK was decompiled with jadx 1.5.6. Only static analysis was done; the app
was never run against the device. Decompiled sources are not copied here, and
everything below is a description in our own words. Places are cited by class
and method name: the IR box control screen (`IRBoxControlFragment`, e.g.
`loadWaveData`), the EQ editor view (`EqView`), and the WAV conversion helper
(`WaveUtils`).

## Tech survey

| App | Technology | Notes |
|---|---|---|
| CubeSuite (Android) | Java/Kotlin | BLE only, no USB code |
| CubeSuite (Windows) V2.8.10 | Qt5, C++, RtMidi on WinMM | Contains an `IRBox` class and the strings `IR-BOX` and `IMPULSE-R` (string and symbol inspection only) |
| MidiSuite (Windows) | Flutter / Dart (AOT compiled) | not analyzed |

The Windows CubeSuite findings pointed to USB-MIDI as the PC transport. The
device later confirmed it ([usb.md](usb.md)).

## What the app contributed, and what the device confirmed

| App finding | Device result |
|---|---|
| Frame `00 59 cmd len body cs`, checksum `~sum(body)` | **verified on device** |
| READ `0x23`, WRITE `0x22`, region types 4 (DEV) and 5 (USR) | **verified on device**. Other types reply with an error ACK. |
| ERASE `0x21` | not tested, and never will be (forbidden) |
| Address map (active slot, select, name table, preset block, refresh, save) | **verified on device** |
| Preset layout (header, cab header, IR at 44, EQ at 7168) | **verified on device**, byte for byte. Unused areas are `0xff`. |
| Refresh modes: 1 after IR / IR-on / EQ-on, 2 after EQ edit, 3 after volume | **verified on device** by audio measurement |
| Write chunks of 173 B; read chunks of 1000 B | **verified on device** over USB-MIDI |
| Save = header write at `0x0`, then `[0]` to `0xF0000000 + i` | **verified on device**, including persistence across power cycles |

## Query command names (from app code)

The app defines these empty-body commands but never sends them. Names are
paraphrased from the app's constants. The device behavior is from
[probing-log.md](probing-log.md).

| Cmd | App meaning | Device reply |
|---|---|---|
| `0x11` | name and version | `IR-BOX_010` (16-byte field) + 11 zero bytes |
| `0x12` | other info / short message | 6 zero bytes |
| `0x13` | firmware version | none |
| `0x17` | device type | none |
| `0x1B` | sound data address | none |
| `0x20` | codec value | none |
| `0x15` | MIDI write | not sent |

## EQ editor (`EqView`)

The EQ editor addresses its nine bands through the `enable[9]` array at preset
offset 7168 (write address `0x1C00`) with this index map (**from app code**):

| Index | Band |
|---|---|
| 0 | HPF |
| 1 | low shelf |
| 2–6 | peak 1–5 |
| 7 | high shelf |
| 8 | LPF |

Indices 0 (HPF), 2 (peak 1) and 8 (LPF) were **verified on device** by
measurement. The app's value ranges are peak Q 1–160, gains ±120 and
frequencies 20–20000 Hz. The device confirmed the gains are 0.1 dB steps
(peaks). Field offsets are in [protocol.md](protocol.md) section 12.

## Bugs in the app's IR converter (`WaveUtils`)

Observed from reading the code:

1. The resample ratio uses integer division, so 48 kHz input is not resampled,
   and 22.05 kHz input causes an infinite loop.
2. Only the first 6144 source frames are read.
3. 8-bit, 32-bit and float WAVs are silently replaced with zeros.
4. No normalization: 16-bit samples are written unscaled into a 24-bit slot.
5. Stereo files: only the left channel is used.
6. Output is truncated or zero-padded to 2048 samples.

These are real consequences on the device:

* The device expects full-scale signed 24-bit little-endian samples
  (**verified on device**). Factory IRs peak at about 1.0 FS
  ([hardware.md](hardware.md)). An unscaled 16-bit sample in a 24-bit slot is
  2^8 times too small, about 48 dB too quiet (bug 4). The preset volume is
  linear and factory volumes are around 50–70 of 127, so the volume byte can
  recover only about 6–8 dB of that (**inferred**).
* The IR rate is 44.1 kHz (**inferred**, see [protocol.md](protocol.md)
  section 13). A 48 kHz file passed through unresampled (bug 1) plays 8.8 %
  slow: every cab resonance is shifted down by about 1.5 semitones. The 2048
  samples then cover only 42.7 ms of the original.

This project does its own conversion instead (see the README): any PCM or float
WAV at any rate, Kaiser windowed-sinc resampling to 44.1 kHz, mono mix or
channel pick, first 2048 samples with a raised-cosine fade when truncating,
peak normalization to full scale by default (as in the factory IRs), and 24-bit
LE output.
