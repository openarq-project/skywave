#!/usr/bin/env python3
"""Channel stage for VectorAdapter campaigns: Watterson fade + AWGN over a
sample vector, using its sidecar to locate frames.

Sample rate comes from the SIDECAR, never a constant -- armstrong's PHY is 8 kHz
and modem73's is 48 kHz, and both must run through this unchanged.

SNR CONVENTION (must match across every adapter or nothing is comparable):

    SNR_B = S / (N0 * B),  N0 = 2*sigma^2/fs  =>  sigma^2 = S*fs / (2*B*10^(SNR/10))

S is the mean square over the sidecar's frame regions of the CLEAN vector,
measured BEFORE fading. That ordering is load-bearing: measuring S post-fade
makes the noise level a function of the particular fade draw, so the same nominal
SNR would mean a different thing in every cell. Gaps and lead-in silence are
excluded from S, or the figure would depend on how much silence the encoder
happened to insert.

Sanity check on the algebra: at SNR_2500 = 0 dB with fs = 48000,
sigma^2 = 9.6*S, i.e. noise power inside 2500 Hz equals signal power
(24000/2500 = 9.6).

INDEPENDENT BLOCK FADING, AND WHY IT IS DONE THE HARD WAY
---------------------------------------------------------
Each frame must see an independent fade draw, or N frames do not give N
independent trials and a binomial interval on FER is a lie. At CCIR-good
(0.1 Hz) the coherence time is ~10 s while frames are 1-27 s, so one continuous
realization leaves adjacent frames strongly correlated.

The obvious fix -- a fresh WattersonChannel per frame -- is WRONG. `hf_gain` is
1/sqrt(var(p1)+var(p2)) computed from the realization's own samples, so a short
low-Doppler realization normalizes that draw back to unit average power. A frame
that landed in a deep fade gets scaled back up, deleting the very event the sweep
exists to measure.

So: ONE long realization (hf_gain estimated over many fade cycles, as intended),
sliced at strides of at least 3/doppler seconds. For a Gaussian Doppler spectrum
the envelope autocorrelation is exp(-pi^2 d^2 tau^2 / 2), so a 3/d stride leaves
correlation ~e^-44. Frame i is faded at virtual fade-time i*stride with the
Hilbert history zeroed -- exact, because the signal preceding every frame really
is silence.

Group delay: the applicator's output is the input delayed by the Hilbert group
delay (127 samples at 255 taps), uniformly for every frame and cell. Identical
across all cells, so it biases nothing.
"""
import argparse
import json
import sys

import numpy as np

from skywave.vector_adapter import load_sidecar, read_vector, write_vector
from skywave.watterson import PRESETS, WattersonChannel

HILBERT_TAPS = 255
# Fade-time separation between consecutive frames, in units of 1/doppler.
STRIDE_COHERENCE_UNITS = 3.0
# Fraction of a frame carried past its end so the multipath tail and Hilbert
# group delay are not truncated. Scales with sample rate, unlike a fixed count.
TAIL_FRACTION = 0.05
TAIL_MIN = 512


def frame_tail(fs, longest):
    return max(TAIL_MIN, int(TAIL_FRACTION * max(longest, 1)), int(0.01 * fs))


def _mean_square(vec, offsets, lengths):
    acc, n = 0.0, 0
    for off, ln in zip(offsets, lengths):
        seg = np.asarray(vec[off:off + ln], dtype=np.float64)
        acc += float(np.dot(seg, seg))
        n += seg.size
    return (acc / n) if n else None


def clean_signal_power(vec, side):
    """Mean square of the clean signal, on the pre-fade vector.

    Option-A convention (owner-ratified 2026-07-31, boundary = the E1
    pathfinder campaign): when the sidecar marks ACTIVE PAYLOAD REGIONS
    (`payload_offsets`/`payload_lengths` — codec2 preamble+frame+postamble,
    excluding tx-internal silences and the PLH head), S is computed over
    those. Whole-frame S let the PLH head's energy inflate S (dS up to
    ~0.8 dB on PLH modes) and the silence fraction dilute it, so labeled
    SNRs were off by a mode-shape constant. Sidecars that predate the
    convention fall back to frame regions.
    """
    if side.get("payload_offsets") and side.get("payload_lengths"):
        s = _mean_square(vec, side["payload_offsets"], side["payload_lengths"])
        if s is not None:
            return s
    s = _mean_square(vec, side["frame_offsets"], side["frame_lengths"])
    if s is None:
        raise SystemExit("vector_channel: sidecar named no frame samples")
    return s


def legacy_signal_power(vec, side):
    """Pre-Option-A S (whole frame regions) — kept so the sweep can record the
    per-mode old-S/new-S conversion offset during the convention transition."""
    s = _mean_square(vec, side["frame_offsets"], side["frame_lengths"])
    if s is None:
        raise SystemExit("vector_channel: sidecar named no frame samples")
    return s


#: i16 headroom guard (Option-A bundle): peak target after noise addition, in
#: f32 full-scale units — the c2floor PEAK_TARGET=3000 pattern. Native TX
#: levels peak near -6 dBFS, so at deep SNR the added noise CLIPPED at the
#: driver's f32->i16 conversion, measuring ~0.3-0.5 dB PESSIMISTIC below
#: ~-8 dB SNR3000.
HEADROOM_PEAK = 3000.0 / 32768.0


def apply_headroom(noisy):
    """Scale the signal+noise composite DOWN (never up) so its peak fits the
    i16 conversion with the c2floor-style margin. A joint scale is
    SNR-invariant, so the labeled SNR is untouched."""
    peak = float(np.max(np.abs(noisy))) if len(noisy) else 0.0
    if peak > HEADROOM_PEAK:
        return (noisy * (HEADROOM_PEAK / peak)).astype(noisy.dtype, copy=False)
    return noisy


def sigma_for(S, fs, bw_hz, snr_db):
    return float(np.sqrt(S * fs / (2.0 * bw_hz * (10.0 ** (snr_db / 10.0)))))


def apply_fade(vec, side, preset, seed, filter_mode="milstd"):
    """Fade each frame with an independent draw from one long realization.
    -> (faded float64 array, per-frame realized gain in dB)."""
    if preset not in PRESETS:
        raise SystemExit(f"vector_channel: unknown preset '{preset}' "
                         f"(have: {', '.join(k for k in PRESETS if k)})")
    spec = PRESETS[preset]
    out = np.asarray(vec, dtype=np.float64).copy()
    if spec is None:                       # "off" -- AWGN only
        return out, []

    fs = int(side["sample_rate"])
    delay_ms, doppler_hz = spec
    offsets = list(side["frame_offsets"])
    lengths = list(side["frame_lengths"])
    n = min(len(offsets), len(lengths))
    tail = frame_tail(fs, max(lengths) if lengths else 0)

    stride_s = max((max(lengths) + tail) / fs,
                   STRIDE_COHERENCE_UNITS / max(doppler_hz, 1e-3))
    # +2 strides of headroom so the interpolation grid never wraps; a wrap would
    # replay the realization and silently re-correlate the draws.
    dur_s = stride_s * (n + 2)

    ch = WattersonChannel(fs, delay_ms, doppler_hz, dur_s, seed,
                          hilbert_taps=HILBERT_TAPS, filter_mode=filter_mode)
    stride_samples = int(round(stride_s * fs))

    gains_db = []
    for i in range(n):
        a = offsets[i]
        b = min(a + lengths[i] + tail, out.size)
        block = np.asarray(vec[a:b], dtype=np.float64)
        ch.t = i * stride_samples
        ch.hist[:] = 0.0
        faded = ch.process(block).copy()
        out[a:b] = faded
        clean = np.asarray(vec[a:a + lengths[i]], dtype=np.float64)
        cp = float(np.dot(clean, clean))
        fseg = faded[:lengths[i]]
        fp = float(np.dot(fseg, fseg))
        gains_db.append(10.0 * np.log10(fp / cp) if cp > 0 and fp > 0 else -99.0)
    return out, gains_db


def add_awgn(vec, sigma, seed):
    rng = np.random.default_rng(seed)
    return vec + rng.standard_normal(vec.size) * sigma


def apply(vector_path, sidecar_path, out_path, preset="off", snr_db=0.0,
          bw_hz=2500.0, seed=1, filter_mode="milstd", report_path=None):
    """Full stage: measure clean S, fade, then add AWGN. -> info dict."""
    side = load_sidecar(sidecar_path) if isinstance(sidecar_path, str) \
        else sidecar_path
    vec = read_vector(vector_path)
    if vec.size == 0:
        raise SystemExit(f"vector_channel: {vector_path} is empty")

    fs = int(side["sample_rate"])
    S = clean_signal_power(vec, side)
    sigma = sigma_for(S, fs, bw_hz, snr_db)
    faded, gains_db = apply_fade(vec, side, preset, seed, filter_mode)
    write_vector(out_path, add_awgn(faded, sigma, seed + 1))

    spec = PRESETS.get(preset)
    info = {
        "preset": preset,
        "delay_ms": spec[0] if spec else None,
        "doppler_hz": spec[1] if spec else None,
        "filter_mode": filter_mode if spec else None,
        "snr_db": snr_db, "bw_hz": bw_hz, "sample_rate": fs,
        "signal_power": S, "sigma": sigma,
        "fade_seed": seed, "noise_seed": seed + 1,
        "stride_coherence_units": STRIDE_COHERENCE_UNITS,
        "frame_gain_db": gains_db,
    }
    if report_path:
        with open(report_path, "w") as f:
            json.dump(info, f, indent=2)
    return info


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--sidecar", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--preset", default="off")
    ap.add_argument("--snr", type=float, required=True)
    ap.add_argument("--bw", type=float, default=2500.0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--filter", default="milstd")
    ap.add_argument("--report", default="")
    a = ap.parse_args()

    info = apply(a.inp, a.sidecar, a.out, a.preset, a.snr, a.bw, a.seed,
                 a.filter, a.report or None)
    g = info["frame_gain_db"]
    msg = (f"vector_channel: {a.preset}"
           + (f" ({info['delay_ms']} ms / {info['doppler_hz']} Hz)"
              if info["delay_ms"] is not None else " (AWGN only)")
           + f" snr={a.snr:.2f} dB / {a.bw:.0f} Hz @ {info['sample_rate']} Hz"
             f"  S={info['signal_power']:.3e} sigma={info['sigma']:.3e}")
    if g:
        arr = np.array(g)
        msg += (f"  draws: {arr.size}, median {np.median(arr):+.1f} dB, "
                f"min {arr.min():+.1f}, deep(<-10dB) {int((arr < -10).sum())}")
    print(msg + f" -> {a.out}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
