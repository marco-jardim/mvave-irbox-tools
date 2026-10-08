"""Measure the IR Box's live audio path over its own USB audio interface.

Plays an exponential sine sweep on the IR Box USB playback device while
recording its USB capture device, then deconvolves (Farina inverse filter)
to estimate the impulse response of whatever path connects them.

Requires the optional ``measure`` extra:  uv sync --extra measure

Examples:
  uv run python scripts/measure.py --out captures/measure_orig
  uv run python scripts/measure.py --level -20 --seconds 4 --out captures/m1
"""

from __future__ import annotations

import argparse
import json
import sys
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd

RATE = 44100


def find_device(name: str, kind: str, hostapi: str) -> int:
    for i, d in enumerate(sd.query_devices()):
        api = sd.query_hostapis(d["hostapi"])["name"]
        chans = d["max_input_channels"] if kind == "input" else d["max_output_channels"]
        if name.lower() in d["name"].lower() and hostapi.lower() in api.lower() and chans > 0:
            return i
    raise SystemExit(f"no {kind} device matching {name!r} on {hostapi}")


def sweep(seconds: float, f1: float, f2: float, level_db: float) -> tuple[np.ndarray, np.ndarray]:
    t = np.arange(int(seconds * RATE)) / RATE
    k = np.log(f2 / f1)
    x = np.sin(2 * np.pi * f1 * seconds / k * (np.exp(t * k / seconds) - 1))
    fade = int(0.01 * RATE)
    x[:fade] *= np.linspace(0, 1, fade)
    x[-fade:] *= np.linspace(1, 0, fade)
    x *= 10 ** (level_db / 20)
    inv = x[::-1] * np.exp(-t * k / seconds)  # amplitude-compensated time reverse
    inv /= np.abs(np.fft.rfft(np.convolve(x, inv)[: len(x) * 2])).max()
    return x.astype(np.float32), inv


def write_wav(path: Path, data: np.ndarray) -> None:
    pcm = np.clip(data, -1, 1)
    pcm = (pcm * 0x7FFFFF).astype("<i4")
    raw = b"".join(int(v).to_bytes(3, "little", signed=True) for v in pcm)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(3)
        w.setframerate(RATE)
        w.writeframes(raw)


def band_report(y: np.ndarray) -> dict[str, float]:
    """Average power spectrum (Hann, 4096) summarized in octave-ish bands, dB re full scale."""
    n = 4096
    win = np.hanning(n)
    frames = [y[i : i + n] * win for i in range(0, len(y) - n, n // 2)]
    if not frames:
        return {}
    psd = np.mean([np.abs(np.fft.rfft(f)) ** 2 for f in frames], axis=0)
    freqs = np.fft.rfftfreq(n, 1 / RATE)
    edges = [60, 250, 1000, 2500, 5000, 8000, 12000, 20000]
    out = {}
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (freqs >= lo) & (freqs < hi)
        out[f"{lo}-{hi}"] = round(float(10 * np.log10(psd[m].sum() + 1e-20)), 1)
    return out


def record_only(dev_in: int, seconds: float, out_dir: Path) -> int:
    print(f"recording {seconds} s from {sd.query_devices(dev_in)['name']} - play now", flush=True)
    rec = sd.rec(int(seconds * RATE), samplerate=RATE, channels=2, device=dev_in, dtype="float32")
    sd.wait()
    report: dict[str, object] = {"device": sd.query_devices(dev_in)["name"], "seconds": seconds}
    for ch in range(2):
        y = rec[:, ch].astype(np.float64)
        peak = float(np.abs(y).max())
        write_wav(out_dir / f"rec_ch{ch}.wav", y)
        report[f"ch{ch}"] = {
            "peak_dbfs": round(float(20 * np.log10(peak + 1e-12)), 1),
            "rms_dbfs": round(float(20 * np.log10(np.sqrt(np.mean(y**2)) + 1e-12)), 1),
            "band_db": band_report(y),
        }
    (out_dir / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--device", default="USB-Audio")
    p.add_argument("--hostapi", default="WASAPI")
    p.add_argument("--seconds", type=float, default=3.0)
    p.add_argument("--level", type=float, default=-20.0, help="sweep level, dBFS")
    p.add_argument("--ir-len", type=int, default=4096)
    p.add_argument("--channel", choices=["left", "right", "both"], default="both",
                   help="which USB playback channel carries the sweep")
    p.add_argument("--out", required=True)
    p.add_argument("--record-only", type=float, metavar="SECONDS",
                   help="just record the USB capture device (e.g. while playing guitar) and report spectrum")
    a = p.parse_args(argv)

    out_dir = Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    dev_in = find_device(a.device, "input", a.hostapi)
    if a.record_only:
        return record_only(dev_in, a.record_only, out_dir)
    dev_out = find_device(a.device, "output", a.hostapi)
    x, inv = sweep(a.seconds, 20.0, 20000.0, a.level)
    pad = np.zeros(int(0.5 * RATE), dtype=np.float32)
    mono = np.concatenate([pad, x, pad, pad])
    play = np.zeros((len(mono), 2), dtype=np.float32)
    if a.channel in ("left", "both"):
        play[:, 0] = mono
    if a.channel in ("right", "both"):
        play[:, 1] = mono

    rec = sd.playrec(play, samplerate=RATE, channels=2, device=(dev_in, dev_out), dtype="float32")
    sd.wait()

    report: dict[str, object] = {"devices": [sd.query_devices(dev_in)["name"], sd.query_devices(dev_out)["name"]],
                                 "level_dbfs": a.level, "channel": a.channel}
    for ch in range(2):
        y = rec[:, ch].astype(np.float64)
        peak = float(np.abs(y).max())
        rms_db = float(20 * np.log10(np.sqrt(np.mean(y**2)) + 1e-12))
        n = len(y) + len(inv) - 1
        nfft = 1 << (n - 1).bit_length()
        h = np.fft.irfft(np.fft.rfft(y, nfft) * np.fft.rfft(inv, nfft), nfft)
        lag = int(np.argmax(np.abs(h)))
        start = max(0, lag - 64)
        ir = h[start : start + a.ir_len]
        write_wav(out_dir / f"rec_ch{ch}.wav", (y / max(peak, 1e-9)) * 0.9)
        write_wav(out_dir / f"ir_ch{ch}.wav", ir / max(np.abs(ir).max(), 1e-12) * 0.9)
        np.save(out_dir / f"ir_ch{ch}.npy", ir)
        spec = np.abs(np.fft.rfft(ir, 8192))
        freqs = np.fft.rfftfreq(8192, 1 / RATE)
        bands = {}
        for f in (100, 300, 1000, 3000, 6000, 10000, 15000):
            idx = int(np.argmin(np.abs(freqs - f)))
            bands[str(f)] = round(float(20 * np.log10(spec[idx] + 1e-12)), 1)
        report[f"ch{ch}"] = {"peak": round(peak, 4), "rms_dbfs": round(rms_db, 1),
                             "ir_peak_index_in_recording": lag, "ir_peak": float(np.abs(h).max()),
                             "response_db_at_hz": bands}
    (out_dir / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
