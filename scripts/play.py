"""Play test signals through the IR Box USB playback device.

  uv run python scripts/play.py tone --freq 1000 --level -30 --seconds 2
  uv run python scripts/play.py noise --level -30 --seconds 4      # pink noise
  uv run python scripts/play.py file song.wav --level -12
"""

from __future__ import annotations

import argparse
import sys
import wave

import numpy as np
import sounddevice as sd

RATE = 44100


def find_output(name: str, hostapi: str) -> int:
    for i, d in enumerate(sd.query_devices()):
        api = sd.query_hostapis(d["hostapi"])["name"]
        if name.lower() in d["name"].lower() and hostapi.lower() in api.lower() and d["max_output_channels"] > 0:
            return i
    raise SystemExit(f"no output device matching {name!r} on {hostapi}")


def fade(x: np.ndarray, ms: float = 20) -> np.ndarray:
    n = int(RATE * ms / 1000)
    x[:n] *= np.linspace(0, 1, n)
    x[-n:] *= np.linspace(1, 0, n)
    return x


def pink(n: int) -> np.ndarray:
    spec = np.fft.rfft(np.random.default_rng(1).standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / RATE)
    spec[1:] /= np.sqrt(f[1:])
    spec[0] = 0
    y = np.fft.irfft(spec, n)
    return y / np.abs(y).max()


def read_wav(path: str) -> np.ndarray:
    with wave.open(path, "rb") as w:
        if w.getframerate() != RATE:
            raise SystemExit(f"{path}: need {RATE} Hz, got {w.getframerate()}")
        width, ch = w.getsampwidth(), w.getnchannels()
        raw = w.readframes(w.getnframes())
    if width == 2:
        y = np.frombuffer(raw, "<i2").astype(np.float64) / 32768
    elif width == 3:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3)
        v = b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) | (b[:, 2].astype(np.int32) << 16)
        y = np.where(v >= 1 << 23, v - (1 << 24), v).astype(np.float64) / (1 << 23)
    else:
        raise SystemExit(f"{path}: unsupported sample width {width}")
    return y.reshape(-1, ch).mean(axis=1)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("kind", choices=["tone", "noise", "file"])
    p.add_argument("path", nargs="?")
    p.add_argument("--freq", type=float, default=1000.0)
    p.add_argument("--seconds", type=float, default=2.0)
    p.add_argument("--level", type=float, default=-30.0, help="peak level in dBFS")
    p.add_argument("--device", default="USB-Audio")
    p.add_argument("--hostapi", default="WASAPI")
    a = p.parse_args()
    if a.level > -6:
        raise SystemExit("refusing levels above -6 dBFS")
    n = int(a.seconds * RATE)
    if a.kind == "tone":
        x = np.sin(2 * np.pi * a.freq * np.arange(n) / RATE)
    elif a.kind == "noise":
        x = pink(n)
    else:
        if not a.path:
            raise SystemExit("file needs a path")
        x = read_wav(a.path)
        x = x / max(np.abs(x).max(), 1e-9)
    x = fade(x.astype(np.float64)) * 10 ** (a.level / 20)
    dev = find_output(a.device, a.hostapi)
    print(f"playing {a.kind} on {sd.query_devices(dev)['name']} at {a.level} dBFS peak, {len(x)/RATE:.1f} s", flush=True)
    sd.play(np.stack([x, x], 1).astype(np.float32), RATE, device=dev)
    sd.wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
