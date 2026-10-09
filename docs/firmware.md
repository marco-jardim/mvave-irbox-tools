# Firmware

Status as of 2026-10-08: **no IR Box firmware image is publicly available**, so
the firmware could not be decompiled. This page records what was searched, what
the M-VAVE update tooling looks like, and what would be needed to go further.

Legend: **verified** (checked by us), **from app code**, **community** (third-party
reverse engineering of sibling products), **inferred**.

## Installed version

| Item | Value | Source |
|---|---|---|
| Query 0x11 name | `IR-BOX_010` | verified |
| Likely firmware version | 010 (M-VAVE names packages `<MODEL>_<NNN>.fwsc`, e.g. `FM-1_011.fwsc`) | inferred |
| USB identity | VID 0x4353, PID 0x4B4D, product "SINCO" | verified |

## Where M-VAVE firmware lives

| Source | Finding |
|---|---|
| https://www.m-vave.com/download | Lists `.fw` / `.fwsc` packages for ~30 products (BlackBox, FM-1, TANK-G/B, SMC-PAD, SMK series, ...) on `yms-file-store.oss-cn-hongkong.aliyuncs.com/software/firmware/`. IR Box and IMPULSE-R appear only in the "supported devices" lists of the updaters (M-UPGRADE, SincoOTA), with **no package**. (verified) |
| OSS bucket listing | `?prefix=software/firmware/` returns 403; listing is disabled. (verified) |
| Name guessing | 252 HEAD requests (`IRBOX`, `IR-BOX`, `IRBox`, `IMPULSE-R`, `IMPULSE`, `CubeBaby`, ... with and without `_008`..`_020`, flat and in a same-named folder): no hits. Control `SMC-PAD/SMC-PAD_005.fwsc` returned 200 (517,940 B). (verified) |
| CubeSuite app (Android) | Firmware URL base `http://47.107.244.214:7080/resources/APP/Firmware/`; host unreachable (connection timeout). (from app code; verified unreachable) |
| SincoOTA (Android) | No URLs; the user picks a local `.fwsc`. `IR-BOX` is in its device filter. Bundles the JieLi `jl_bt_ota` SDK. BLE OTA service `ae00` / write `ae01` / notify `ae02`. (from app code) |
| M-UPGRADE (Windows, Qt6) | User picks a local `.fwsc`; no embedded IR Box image (no `JLUFW`/`@JMUA` magic in the binary). Its shipped developer log shows only FM-1, MK300, URM-1000 and SC791FEMXGQ updates. (verified / from logs) |
| CubeSuite (Windows), MidiSuite (Windows) | Contain IR Box UI strings but no firmware image. (verified) |
| Product page https://www.m-vave.com/product?id=ir-box | Download list is built from `downloads-data.js`. For `ir-box` it offers CubeSuite (PC + mobile) and SincoOTA only. There is no firmware entry and no `pc-firmware` item that supports `ir-box`. Manual: `manualf.oss-cn-hongkong.aliyuncs.com/manual/IRBOX/IRBOX_ZE.pdf`. (verified) |
| Internet Archive: download page | Snapshots 2026-05-02 and 2026-08-30 (`downloads-data.js`) list IRBOX/IMPULSE-R only as devices supported by CubeSuite/SincoOTA. Firmware lists contain no IR Box. (verified) |
| Internet Archive: OSS bucket | 48 archived URLs under `software/firmware/` (2024-07 → 2026-09), including older versions such as `FootCtrlPlus_009..013`, `SMK-37 Pro_012..015` and release-note `.txt` files. **None for IR Box, IMPULSE-R or Cube Baby.** (verified) |
| Internet Archive: legacy app server `47.107.244.214:7080` | Archived directory listings (2024-03): `/resources/APP/Firmware/` (19 `.ufw`: CubeTurner, FootCtrl, LooperDrum*, LooperAutoX, LooperMini, LooperPro*, MidiPortA/B, RJMICRO25, SMC-Mixer, SMK25*, TurnerPro, minikey25), `/FirmwareChaos/` (MidiPortB_035) and `/TestFirmware/` (FootCtrl_032, LooperDrum_001/002/030, MidiPortA_035, MidiPortB_035, SMK25_055/056). **No IR Box.** (verified) |
| community.m-vave.com | Vue SPA; downloads go through `/bbs/api/software?file=`. `IRBOX.zip`, `IR-BOX.zip`, `IMPULSE-R.zip` and `IRBOX.fwsc` return 404. Archived files are PC editors and manuals for other products only. No IR Box firmware thread was found. (verified) |

## Update tooling and container (sibling products)

Community work on the FM-1 and SMK-37 Pro documents the same tooling, and very
likely applies to the IR Box.

- **Chip:** JieLi AC791N (pi32v2 core, XIP at 0x02000000) in FM-1 and SMK-37 Pro.
  Sources: [AL-255/FM-1-RE](https://github.com/AL-255/FM-1-RE),
  [probonopd SMK-37 Pro gist](https://gist.github.com/probonopd/18b3ed65a69d0229eb630c47d7e316dc).
  (community) The IR Box's BLE UUIDs (`ae00` OTA, `ae40` data) and framing match
  JieLi-based M-VAVE products, so a JieLi part is likely. Its USB VID differs
  (0x4353 vs JieLi's 0x4C4A), so the vendor ID is customized. (inferred)
- **`.fwsc` container:**
  - The first 20 × 48 bytes interleave 47 data bytes with 1 marker byte. The markers
    encode the package name and version (SincoOTA `Tools.unpackFWSCFile`).
  - The logical content is a JieLi `JLUFW` / `@JMUA` image: `flash.bin`,
    `isd_config.ini`, `ota.bin`, `script.ver`, ... It can be unpacked with
    [kagaimiq/jl-misctools](https://github.com/kagaimiq) (`fwunpack_newfw.py`).
  - There is only integrity checking (CRC). There is no asymmetric signature.

  (from app code + community)
- **USB-MIDI OTA (FM-1):**
  1. Handshake query 0x11.
  2. The upgrade-mode SysEx `F0 22 24 35 7F F7` reboots the device into an OTA
     personality (`ota-<model>`, different VID/PID).
  3. Data is sent as `00 59 30 ...` frames with the same checksum and 8→7 packing as
     this toolkit.
  4. Terminal writes to 0xE0000000 (verify) and 0xF0000000 (finish).

  (community: AL-255/FM-1-RE `docs/io/11-ota-protocol.md`)
- **Recovery:** FM-1 work shows no software recovery path. Mask-ROM (`UBOOT1.00`)
  access needs a hardware USB-key dongle, e.g. RP2040-based ones (see
  `ip2k/mvave-fm1-open-firmware`, `kurogedelic/FM-1-transporter`). (community)

## Reading the firmware from the device

- Protocol region types 0–3 (NOR, NAND, SD, EFF) and 6–7 answer every read with
  error status 1, so flash cannot be dumped through the normal protocol. (verified)
- Unexplored, and deliberately **not attempted**:
  - Sending the upgrade-mode command. It reboots into OTA mode with no known
    way back other than completing an update.
  - JieLi mask-ROM access via a USB-key dongle. This is hardware work and needs
    the case open. It is read-capable in principle (kagaimiq's `jl-uboot-tool`).

## Fix and improvement opportunities

Without an image, firmware-level changes are not possible yet. The issues we can
see from the outside are:

| Issue | Where it can be fixed |
|---|---|
| Official app converter: 16-bit IRs written about 48 dB too quiet, 48 kHz not resampled, 22.05 kHz hang, float/32-bit silently zeroed | Host side. **Fixed in this toolkit** (`scripts/irbox/ir_convert.py`). |
| IR length capped at 2048 samples (46.4 ms) | Firmware (buffer size and convolution budget). Probably a CPU limit; not changeable from the host. |
| Knob and bypass state not readable, no MIDI out on changes | Firmware |
| Program Change ignored over USB-MIDI | Firmware. The host can select slots with the 0xE0000000 write (`irbox_tool.py select`). |
| Unsupported query commands 0x13/0x17/0x1B/0x20 give no reply (no NAK) | Firmware |
| Out-of-map reads return stale buffers instead of an error | Firmware. The toolkit validates addresses instead. |
| BLE not seen advertising on Windows | Unknown. Possibly needs pairing mode, or is disabled while USB is connected. |

## Factory reset (located in CubeSuite for Windows)

The manual says factory settings can be restored "in the computer software".
Static analysis of CubeSuite V2.8.10 for Windows (32-bit Qt5, `CubeSuite.exe`)
shows that **the factory data is on the host, not in the pedal**. Function
addresses below are for that build. (from app code; the result is verified on
the device.)

| Step | Code | What it does |
|---|---|---|
| Button handler | `0x4D3F90` | Confirmation dialog: "Are you sure you want to Restore all factory IR?". It allocates a 0x40000-byte buffer. |
| Loader | `0x4D3320` | Loads `bin/%1.bin`, a file next to the executable, into the buffer. It reports "Recovery failure! Configuration file loss!" if the file is missing. |
| Action (lambda) | `0x4D1AD0` | Loops `esi = 0x70000000 … 0x7003F000` step `0x1000`, calling `0x5063A0(type=5, addr)`. Then it calls `0x506590(type=5, addr=0x70000000, buf, len=0x40000)`. |
| `0x5063A0` → `0x507770` | Frame builder | Writes `00 59 21 05 00 00 <type> <addr4> <~sum>`. This is **CMD 0x21 ERASE** of one 4 KiB sector. |

So "Restore all factory IR" works in two steps:
1. Erase 64 sectors (256 KiB) of the preset flash window at type 5 / 0x70000000.
2. Write the 256 KiB file `bin/BOR.bin` (262,144 B = 32 × 8192, dated
   2024-03-07) back into that window.

Findings:

- **`bin/BOR.bin` is the factory image.** Its 32 blocks use the normal preset
  layout ('patch', 'CAB\0', 2048-sample IR, EQ). The names match the manual's
  preset list (1 TweedDeluxe … 32 Work 4050 2 BASS). (verified)
- **Type 5 / 0x70000000–0x7003FFFF is the raw preset flash**, slot *i* at
  `0x70000000 + i·8192`. It is readable with normal 0x23 reads. A full 256 KiB
  read matched a select-based backup byte for byte. (verified)
- On this unit, slots 15–32 were byte-identical to `BOR.bin` and slots 1–14
  differed (custom bass cabs). (verified)
- **Restored 2026-10-08.** Slots 1–14 were restored from `BOR.bin` with the
  toolkit's normal path: select, write working copy, refresh, save, then
  reselect and verify. **No erase and no raw-flash write** were used. A
  subsequent raw read of all 256 KiB was identical to `BOR.bin`. The previous
  content of slots 1–14 is in the automatic backup `backups/20261008-205252/`
  (and `dumps/backup_full/`). (verified)

The toolkit never sends 0x21 and never writes the 0x7xxxxxxx window. Restoring
slot by slot through the save path gives the same result without the risk of a
power loss between erase and write. `BOR.bin` is M-VAVE's file and is not
redistributed here. Get it from the official CubeSuite download (`bin/BOR.bin`).

## Next steps if firmware work is wanted

1. Ask M-VAVE support for `IR-BOX` firmware (`.fwsc`) or an update changelog.
   Packages for other products are public, so it may simply be unpublished.
2. If an image becomes available, unpack it with `jl-misctools` and load
   `app.bin` in Ghidra. AL-255/FM-1-RE has pi32v2 loader scripts and notes.
3. Mask-ROM dumping with a USB-key dongle is the only path that needs no file
   from M-VAVE. It requires opening the unit; do it read-only first.
