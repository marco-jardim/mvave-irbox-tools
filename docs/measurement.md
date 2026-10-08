# Audio measurement

How the audio-side claims in [protocol.md](protocol.md) were measured: refresh
modes, little-endian IR samples, volume linearity, and EQ units. The tools are
`scripts/measure.py` and the `scripts/sequence_*.ps1` A/B scripts. Results are
in [probing-log.md](probing-log.md).

Setup:

```
uv sync --extra measure        # numpy + sounddevice
```

The IR Box's own USB audio device is used for both playback and capture
(default match `--device USB-Audio`, `--hostapi WASAPI`) at 44.1 kHz.

## 1. `measure.py`, sweep mode (loopback)

```
uv run python scripts/measure.py --out captures/m1 [--level -20] [--seconds 3]
                                 [--channel left|right|both] [--ir-len 4096]
```

1. Generates an exponential sine sweep from 20 Hz to 20 kHz: `--seconds` long,
   `--level` dBFS peak, 10 ms fade in and out.
2. Pads it with 0.5 s of silence before and 1.0 s after, and plays it on the
   chosen USB playback channel(s). Both capture channels are recorded at the
   same time (`sounddevice.playrec`).
3. Deconvolves each capture channel with a Farina inverse filter (the
   time-reversed sweep with exponential amplitude compensation, normalized so
   that the sweep convolved with it has a spectral peak of 1). The convolution
   is done by FFT.
4. Takes a window of `--ir-len` samples, starting 64 samples before the
   largest peak.

Outputs per channel:

| File | Content |
|---|---|
| `rec_chN.wav` | raw capture, normalized to 0.9 peak (24-bit) |
| `ir_chN.wav` | deconvolved IR, normalized to 0.9 peak |
| `ir_chN.npy` | deconvolved IR, unnormalized |
| `report.json` | capture peak and RMS, IR peak position, and response in dB at 100, 300, 1k, 3k, 6k, 10k and 15 kHz |

The response values are relative to the analysis reference, not absolute
gain. For absolute gain, compare the capture peak with the sweep level: on the
loopback a −20 dBFS sweep recorded a peak of 0.0986, i.e. unity.

What it can measure on the IR Box: only the USB playback → USB capture path.
That path loops straight back without passing through the IR (flat within
±1.5 dB from 100 Hz to 15 kHz, see [hardware.md](hardware.md)). Sweep mode
therefore cannot characterize the cab IR or the EQ. It is still useful to check
the audio interface and the levels.

## 2. `measure.py`, record-only mode (band report)

```
uv run python scripts/measure.py --record-only 6 --out captures/take1
```

Records `SECONDS` of both capture channels while someone plays the instrument
into the IR Box. Outputs:

| File | Content |
|---|---|
| `rec_chN.wav` | capture at its real level (24-bit, clipped to ±1, not normalized) |
| `report.json` | `peak_dbfs`, `rms_dbfs`, and `band_db` per channel |

`band_db` is computed as follows. Average a Hann-windowed 4096-point power
spectrum over frames with 50 % overlap. Sum the power into seven bands:
60–250, 250–1k, 1k–2.5k, 2.5k–5k, 5k–8k, 8k–12k and 12k–20k Hz. Express each sum
in dB. The values only mean something as differences between takes made with
the same setup.

## 3. A/B method

Each A/B script records a baseline take, applies working-copy writes with
`probe.py write … --yes` (allowlisted addresses only), records another take,
and so on. At the end it restores the stored preset by selecting a neighbor
slot and coming back. None of the scripts saves.

| Script | Steps |
|---|---|
| `sequence_volume_eq.ps1` | baseline; volume 20 without refresh; `refresh(3)`; volume 70 restored; eq_on + LPF 1 kHz without refresh; `refresh(2)`; `refresh(1)`; restore by reselect |
| `sequence_eq_bands.ps1` | baseline; LPF 1 kHz; HPF 800 Hz; peak 1 at 100 Hz +120; restore |
| `sequence_eq_units.ps1` | eq_on + `refresh(1)`; LPF 1 kHz; peak 1 at 100 Hz +120, −120, +60; restore |

All three take `-Seconds` (default 6) and `-Out DIR`. They write one
`report.json` per step plus a shared `trace.jsonl`. Run them with the player
playing continuously, for example a repeated riff or a held chord, for the
whole script (about 45–60 s).

How to read the results: compare `rms_dbfs` and `band_db` of each step against
the baseline and against the restored take. The restored take checks drift:
if it does not land close to the baseline, the playing changed.

`probe.py irtest MODE --refresh` is used the same way for IR experiments
(`same`, `dirac`, `dirac-be`, `dirac-full`, `scale-le`, `scale-be`). It uploads a
modified IR to the working copy only, sends `refresh(1)`, and reads the IR back.
Record a take after each upload.

## 4. Limitations

* **Live-playing variance is ±3–6 dB per band** between takes of "the same"
  playing. Only effects well above that are conclusive. This holds for the
  findings that matter: −11 dB volume, −24 dB HPF, +14 dB peak, and the
  broadband garbage of the BE test. Smaller numbers, such as the exact −5.3 dB
  cut, are indicative only.
* Band sums average over wide bands. A narrow cut reads smaller than an equal
  boost (**inferred**).
* The physical knobs (low cut, volume, high cut) act on the signal but cannot
  be read ([hardware.md](hardware.md)). Do not touch them during a sequence.
* Absolute levels are not comparable across sessions (input gain, knobs,
  instrument).
* The headphone output is not a reliable monitor of the processed signal
  ([hardware.md](hardware.md)). Judge results on the USB capture.
* Each step starts a new `uv run` process and opens the WinMM ports again,
  which takes about 0.3 s per write. Nothing else may hold the MIDI ports
  ([usb.md](usb.md)).
* A repeatable reamp needs an external interface feeding a fixed DI file into
  the instrument input, because USB playback bypasses the IR (**inferred**).
  This was not done.
