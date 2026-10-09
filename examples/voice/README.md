# Voice test sample

- `voice_sample_ptbr.wav`: about 12.7 s of synthetic pt-BR speech (Windows "Microsoft Maria Desktop" TTS), 44.1 kHz, 16-bit mono. Generated locally, no third-party recording.
- `preview_<name>.wav`: the sample convolved in software with `../irs/voice_<name>.wav`, RMS-matched to the original. This is what the pedal should sound like with that IR loaded.

To hear them through the IR Box USB output:

```
uv run python scripts/play.py file examples/voice/preview_telephone.wav --level -24
```

USB playback is **not** processed by the pedal's IR (see [docs/hardware.md](../../docs/hardware.md)). To test the real thing, feed the sample into the 1/4" input, for example from a PC/phone headphone out with a 1/8"→1/4" cable, and step through presets 01–05.
