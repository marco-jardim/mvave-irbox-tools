"""Generate "voice effect" IRs (telephone, AM radio, megaphone, walkie-talkie,
gramophone) as 2048-sample, 44.1 kHz, 24-bit mono WAVs ready for ir-upload.

Each IR is designed in the frequency domain from simple filter shapes, made
minimum-phase (no added latency) via the real cepstrum, optionally given a few
early reflections, truncated to 2048 samples with a short fade, and scaled so
its peak gain is +6 dB (in line with the factory IRs).

  uv run python scripts/make_voice_irs.py --out examples/irs
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from irbox.ir_convert import export_wav  # noqa: E402

RATE = 44100
TAPS = 2048
NFFT = 1 << 15


def hp(f, fc, n):
    return 1.0 / np.sqrt(1.0 + (fc / np.maximum(f, 1e-3)) ** (2 * n))


def lp(f, fc, n):
    return 1.0 / np.sqrt(1.0 + (f / fc) ** (2 * n))


def peak(f, fc, gain_db, width_oct):
    x = np.log2(np.maximum(f, 1e-3) / fc) / (width_oct / 2.355)
    return 10 ** (gain_db * np.exp(-0.5 * x * x) / 20)


VOICES = {
    "telephone": dict(
        name="Telephone",
        mag=lambda f: hp(f, 300, 4) * lp(f, 3400, 8) * peak(f, 1800, 3, 1.0),
        taps=[],
    ),
    "am_radio": dict(
        name="AM Radio",
        mag=lambda f: hp(f, 180, 2) * lp(f, 4500, 6) * peak(f, 1100, 4, 1.0) * peak(f, 300, -3, 0.8),
        taps=[],
    ),
    "megaphone": dict(
        name="Megaphone",
        mag=lambda f: hp(f, 600, 4) * lp(f, 3200, 4) * peak(f, 1400, 8, 0.5),
        taps=[(0.0009, 0.40), (0.0021, 0.25)],
    ),
    "walkie_talkie": dict(
        name="Walkie Talkie",
        mag=lambda f: hp(f, 450, 6) * lp(f, 2600, 8) * peak(f, 1800, 5, 0.4),
        taps=[],
    ),
    "gramophone": dict(
        name="Gramophone",
        mag=lambda f: hp(f, 250, 3) * lp(f, 5000, 4) * peak(f, 700, 6, 0.3) * peak(f, 2500, 4, 0.3),
        taps=[(0.0006, 0.30)],
    ),
}


def min_phase(mag: np.ndarray) -> np.ndarray:
    """Minimum-phase impulse response from a one-sided magnitude (len NFFT//2+1)."""
    full = np.concatenate([mag, mag[-2:0:-1]])
    cep = np.fft.ifft(np.log(np.maximum(full, 1e-8))).real
    fold = np.zeros_like(cep)
    fold[0] = cep[0]
    fold[1 : NFFT // 2] = 2 * cep[1 : NFFT // 2]
    fold[NFFT // 2] = cep[NFFT // 2]
    return np.fft.ifft(np.exp(np.fft.fft(fold))).real


def design(spec: dict) -> np.ndarray:
    f = np.fft.rfftfreq(NFFT, 1 / RATE)
    h = min_phase(spec["mag"](f))[:TAPS].copy()
    for delay_s, gain in spec["taps"]:
        d = int(round(delay_s * RATE))
        h[d:] += gain * h[: TAPS - d]
    fade = 256
    h[-fade:] *= 0.5 * (1 + np.cos(np.linspace(0, np.pi, fade)))
    peak_gain = np.abs(np.fft.rfft(h, NFFT)).max()
    h *= 2.0 / peak_gain  # +6 dB peak gain
    if np.abs(h).max() >= 1.0:
        h *= 0.99 / np.abs(h).max()
    return h


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="examples/irs")
    a = p.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    f = np.fft.rfftfreq(8192, 1 / RATE)
    for key, spec in VOICES.items():
        h = design(spec)
        samples = [int(round(v * 8388607)) for v in h]
        path = out / f"voice_{key}.wav"
        export_wav(samples, path)
        H = 20 * np.log10(np.abs(np.fft.rfft(h, 8192)) + 1e-12)
        pts = {hz: round(float(H[int(np.argmin(np.abs(f - hz)))]), 1) for hz in (100, 300, 1000, 2000, 4000, 8000)}
        print(f"{path.name:26s} '{spec['name']}'  peak sample {np.abs(h).max():.2f}  dB@Hz {pts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
