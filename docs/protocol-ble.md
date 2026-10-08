# BLE transport

The frame protocol (commands, address map, preset layout, refresh, select and
save) is the same on every transport and is documented in
[protocol.md](protocol.md). This page covers only what is specific to BLE.

Everything here is **from app code** (Android CubeSuite, see
[apk-analysis.md](apk-analysis.md)). BLE is **untested on hardware**: during a
Windows BLE scan the unit was never seen advertising
([probing-log.md](probing-log.md)). For that reason USB-MIDI is the toolkit's
default transport.

## GATT layout (from app code)

| Item | UUID |
|---|---|
| Service | `0000ae40-0000-1000-8000-00805f9b34fb` |
| Write characteristic | `0000ae41-0000-1000-8000-00805f9b34fb` |
| Notify characteristic | `0000ae42-0000-1000-8000-00805f9b34fb` |
| OTA (firmware update) | `0000ae00`, `0000ae01`, `0000ae02` (same base). **Never touch.** |

The app recognizes the unit by the advertised name `IR-BOX` (**from app
code**). The IMPULSE-R reportedly shares the same UI. The
toolkit's `scan` command lists matching advertisers; `--all` lists every
advertiser.

## Session (from app code)

1. Connect. There is no pairing or handshake.
2. Enable notifications on `ae42`.
3. Wait about 3000 ms.
4. Send raw frames (no SysEx wrapping, no 8→7 packing) to `ae41`.
   Replies arrive as notifications on `ae42` and may span several
   notifications. Reassemble by the header length (`7 + len`).

| Item | Value | Source |
|---|---|---|
| Read chunks | 1000 B, 3 s timeout, 200 ms between chained reads | **from app code** |
| Write chunks | 173 B, wait for each ACK | **from app code** (the same size works over USB-MIDI, **verified on device**) |
| Write type | with or without response: unknown. The toolkit picks one at connect time and logs it. | – |
| MTU | unknown. The 173-byte chunk suggests a conservative limit (**inferred**). | – |

## Hazards

* The OTA characteristics could brick the unit. `irbox.ble` refuses UUIDs
  with those prefixes.
* Command `0x21` (erase) is refused on BLE as on every transport.
