#!/usr/bin/env python3
"""ft8_chanest.py — decode-and-remodulate FT8 channel estimator (prototype).

Every FT8 signal that decodes is a KNOWN 12.64 s, 79-symbol waveform.  Re-synthesise it
from the decoded message, correlate symbol by symbol against the capture, and each signal
yields a complex channel gain every 160 ms plus a noise reading in the 6.25 Hz bins the
signal is not occupying.  Dozens of paths per 15 s slot, from any receiver, for free.

Per decoded signal:
  * fine sync            dt to 2.5 ms, frequency to 0.5 Hz (then a phase-ramp fit gives it to ~0.02 Hz)
  * g[k], k = 0..78      complex gain per symbol (160 ms)
  * N_bin[k]             noise+interference power per 6.25 Hz bin around the signal, per symbol
  * SNR2500              from |g|² / N_bin (compare with the decoder's own reading)
  * f_resid              residual frequency = decoder error + Doppler SHIFT (not separable here)
  * doppler_2sigma_hz    2σ width of the gain process spectrum after the noise floor is removed
                         (MIL-STD-188-110C / F.1487 convention; 12.64 s record ⇒ 0.079 Hz resolution)
  * fade p10/p50/p90/min dB relative to the RMS gain; amplitude ACF → coherence time

Pipeline tools (built from kgoba/ft8_lib, MIT): `decode_ft8` for the decode, `ft8_tones` (a
20-line helper in this repo's notes) to re-encode a message text into its 79 tones.

    ft8_chanest.py synth  --out slot.wav --preset poor --snr -10 [--seed 1]   # truth test input
    ft8_chanest.py run    slot.wav [more.wav …] --out est.jsonl [--truth truth.json] [--plot DIR]

TRUTH TEST (pre-registered bars, checked by `synth` + `run --truth`):
  T1 sync:    |dt error| < 2 ms + differential delay and |f error| < 0.1 Hz + 0.25·spread for every decoded signal
              (f is judged against f0 + the true series' own phase ramp; a spread realization's
              centroid is only defined to ~σ/√(d·T) over one 12.6 s record)
  T2 gain:    corr(|g_est|, |g_true|) ≥ 0.9 at SNR ≥ −10 dB
  T3 spread:  doppler_2sigma within ±30 % per signal of the SAME statistic on the true gain series,
              and the cell's mean est/true within ±15 % (the campaign averages over signals; one
              12.6 s record scatters ±30 % about the nominal preset). Truth = the estimator run on
              the noiseless channel output, so definitional gaps are excluded by construction.
  T4 SNR:     |SNR_est(mean power) − SNR_set| ≤ 2 dB
  Status 2026-09-07: 14 cells pass (off/good/moderate/poor/low-lat-moderate/nvis/nvis-max, 0 to
  −18 dB, on- and off-grid frequencies). Known limit: the moment spread reads +19 % at 14 dB per
  bin (moderate, −12 dB) — that cell is flagged unreliable rather than fixed.

REAL-CAPTURE FINDINGS (Coventry OH KiwiSDR, 20 m, 5 slots, 172 decodes, 2026-09-07 17:02 UTC):
  * spectral SNR after successive cancellation agrees with jt9 to −0.4 ± 4.7 dB; cancellation
    lowered the band floor 2.5 dB (that much of a jt9-style median floor is decoded signals).
  * the LOCAL empty-bin reading is an SINR, not an SNR: on a 24-decode slot a neighbour usually
    overlaps one side of a signal's bins (−3.8 dB vs spectral on average, −19 dB worst); it is
    kept as `snr2500_mean_db` (what a modem would see) next to `snr2500_spectral_db` (noise).
  * 42 signals with reliable spread: 2σ 0.15/0.22/0.65 Hz (p25/50/75), t_coh 0.5/0.6/1.7 s,
    fade p10 −12/−7/−4 dB, fade minimum median −20 dB within 12.6 s.
"""
from __future__ import annotations

import argparse
import json
import re
import math
import os
import subprocess
import sys
import wave

import numpy as np
from scipy.signal import hilbert

FS = 12000
SYM_T = 0.16
NSPS = int(FS * SYM_T)          # 1920
NSYM = 79
TONE_HZ = 6.25
BT = 2.0
GFSK_K = math.pi * math.sqrt(2.0 / math.log(2.0))
SLOT_S = 15.0
SIG_START_S = 0.5               # WSJT-X convention: symbol 0 begins 0.5 s into the slot
BW_REF_HZ = 2500.0
# Timing must be accurate to well under a millisecond: an offset Δ turns into a TONE-DEPENDENT
# phase 2π·6.25·m_k·Δ on the gain sequence (0.55 rad across the 8 tones at 2 ms), which reads as
# wideband phase noise and inflated the Doppler spread by 25 % at 2 ms. (A symbol-edge guard was
# tried and made it worse: it flattened the timing metric without removing the mechanism.)
_WIN = np.ones(NSPS)
HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.environ.get("FT8_LIB_DIR", os.path.join(HERE, "..", ".ft8_lib"))   # → ~/tools/ft8_lib (built
# from kgoba/ft8_lib + tools/ft8_lib-chanest.patch: decode_ft8 prints TONES lines; tools/ft8_tones.c)


# ------------------------------------------------------------ reference waveform --
def gfsk_pulse(nsps=NSPS, bt=BT):
    from scipy.special import erf
    i = np.arange(3 * nsps)
    t = i / nsps - 1.5
    return (erf(GFSK_K * bt * (t + 0.5)) - erf(GFSK_K * bt * (t - 0.5))) / 2


def gfsk_phase(tones, f0, fs=FS):
    """Port of ft8_lib synth_gfsk: returns the instantaneous phase phi[n] of the FT8 waveform
    (real signal = sin(phi)). Length NSYM*NSPS, symbol 0 starts at n = 0."""
    nsps = int(round(fs * SYM_T))
    n_wave = len(tones) * nsps
    dphi_peak = 2 * math.pi / nsps
    dphi = np.full(n_wave + 2 * nsps, 2 * math.pi * f0 / fs)
    pulse = gfsk_pulse(nsps)
    for i, s in enumerate(tones):
        dphi[i * nsps:i * nsps + 3 * nsps] += dphi_peak * s * pulse
    dphi[:2 * nsps] += dphi_peak * pulse[nsps:] * tones[0]
    dphi[n_wave:n_wave + 2 * nsps] += dphi_peak * pulse[:2 * nsps] * tones[-1]
    phi = np.cumsum(dphi[nsps:nsps + n_wave]) - dphi[nsps]      # phi[0] = 0 like the C loop
    return phi


def ft8_reference(tones, f0, fs=FS):
    """Complex analytic reference exp(j*phi) with the C code's edge ramps on the first/last symbol."""
    phi = gfsk_phase(tones, f0, fs)
    ref = np.exp(1j * phi)
    nsps = int(round(fs * SYM_T))
    n_ramp = nsps // 8
    ramp = (1 - np.cos(2 * math.pi * np.arange(n_ramp) / (2 * n_ramp))) / 2
    ref[:n_ramp] *= ramp
    ref[-n_ramp:] *= ramp[::-1]
    return ref


# ------------------------------------------------------------------ ft8_lib glue --
def read_wav(path):
    with wave.open(path, "rb") as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2, "need mono 16-bit"
        fs = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float64) / 32768.0
    return fs, x


def write_wav(path, x, fs=FS):
    x = np.clip(x, -1, 1)
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(fs)
        w.writeframes((x * 32767).astype(np.int16).tobytes())


DECODER = os.environ.get("FT8_DECODER", "jt9")     # jt9 (WSJT-X, ~3 dB more sensitive) or ft8lib


def decode_ft8(path):
    """Lines: HHMMSS snr dt freq ~ message. Returns [(snr, dt, freq, msg, dt_is_wsjtx)].
    jt9's dt is WSJT-X's (relative to 0.5 s into the slot); decode_ft8's is from the file start."""
    if DECODER == "jt9":
        import tempfile
        with tempfile.TemporaryDirectory() as td:          # jt9 litters its cwd with side files
            out = subprocess.run(["jt9", "-8", "-d", "3", os.path.abspath(path)], capture_output=True,
                                 text=True, timeout=120, cwd=td)
    else:
        out = subprocess.run([os.path.join(TOOLS, "decode_ft8"), path], capture_output=True, text=True)
    res = []
    for ln in out.stdout.splitlines():
        parts = ln.split(None, 5)
        if len(parts) == 6 and parts[4] == "~":
            msg = re.split(r"\s{2,}", parts[5].strip())[0]      # jt9 appends AP flags after a gap
            res.append((float(parts[1]), float(parts[2]), float(parts[3]), msg, DECODER == "jt9"))
    return res


def decoder_tones(path):
    """ft8_lib decoder patched to print `TONES freq dt <79 digits> message` per decode — the
    codeword's own tones, so hashed-call messages need no re-encoding. Keyed by frequency."""
    out = subprocess.run([os.path.join(TOOLS, "decode_ft8"), path], capture_output=True, text=True).stdout
    res = []
    for ln in out.splitlines():
        if ln.startswith("TONES "):
            _, fr, dt, t, msg = ln.split(None, 4)
            res.append((float(fr), float(dt), np.array([int(c) for c in t], dtype=np.int64), msg.strip()))
    return res


def tones_for(messages):
    out = subprocess.run([os.path.join(TOOLS, "ft8_tones")], input="\n".join(messages) + "\n",
                         capture_output=True, text=True).stdout
    tones = {}
    for ln in out.splitlines():
        if ln.startswith("OK "):
            _, t, msg = ln.split(" ", 2)
            tones[msg] = np.array([int(c) for c in t], dtype=np.int64)
    return tones


# ---------------------------------------------------------------------- estimator --
def spread_2sigma(g, floor=0.0, nfft=1024):
    """2σ width (F.1487 / MIL-STD convention) of the gain process spectrum from one 79-sample
    record. Hann-windowed periodogram (a rectangular window's 1/f² sidelobes inflate the second
    moment), noise floor subtracted, centroid removed. Returns (centroid_hz, spread_hz).
    One 12.64 s record holds only ~d·T independent fade samples, so a single reading scatters
    ±30 %: average the PSDs over many signals on a path before quoting a spread."""
    w = np.hanning(len(g))
    P = np.abs(np.fft.fft(g * w, nfft)) ** 2 / np.sum(w ** 2)
    fr = np.fft.fftfreq(nfft, SYM_T)
    Pc = P - floor
    # Moments over the SIGNAL region only: clipping negative residuals to zero over the many
    # pure-noise bins leaves a positive-biased tail that inflates the second moment (measured
    # +35 % on a 1 Hz cell at −10 dB). Keep bins within 25 dB of the peak and above the floor.
    thr = max(floor, 10 ** (-2.5) * float(np.max(Pc)))
    Pc = np.where(Pc > thr, Pc, 0.0)
    if Pc.sum() <= 0:
        return float("nan"), float("nan"), float("nan")
    fc = float(np.sum(fr * Pc) / Pc.sum())
    moment = float(2 * math.sqrt(np.sum(Pc * (fr - fc) ** 2) / Pc.sum()))
    # Gaussian fit (F.1487 defines the spread for a Gaussian PSD): weighted LS of ln P on (f−fc)²
    # over the same region. Less sensitive than the moment to the threshold-edge bias that reads
    # +19 % at 14 dB per-bin SNR; report both — the moment is the general statistic.
    m = Pc > 0
    x = (fr[m] - fc) ** 2
    y = np.log(Pc[m])
    w = Pc[m]
    A = np.vstack([np.ones(m.sum()), -x]).T * w[:, None]
    try:
        _, inv2s2 = np.linalg.lstsq(A, y * w, rcond=None)[0]
        gauss = float(2 * math.sqrt(0.5 / inv2s2)) if inv2s2 > 0 else float("nan")
    except np.linalg.LinAlgError:
        gauss = float("nan")
    return fc, moment, gauss


def _sym_dots(ya_seg, ref):
    """Per-symbol correlation of a capture segment (analytic) against the reference, over the
    guarded window (unit-magnitude reference ⇒ normalise by the window length)."""
    return ((ya_seg.reshape(NSYM, NSPS) * np.conj(ref.reshape(NSYM, NSPS))) * _WIN).sum(axis=1) / _WIN.sum()


def fine_sync(ya, tones, f_dec, dt_dec, fs=FS, dt_span=0.8, dt_step=0.02, df_span=3.5, df_step=0.5, wsjtx_dt=False):
    """Noncoherent (per-symbol energy) grid search around the decoder's (freq, dt)."""
    n = len(ya)
    best = (-1.0, None, None)
    t_ax = np.arange(NSYM * NSPS) / fs
    # decode_ft8 reports dt from the FILE start (measured: ~0.15–0.2 s late vs the true symbol-0
    # instant on synthetic slots), not WSJT-X's 0.5 s-relative dt — search a wide window around it.
    nom = int(round(((SIG_START_S if wsjtx_dt else 0.0) + dt_dec) * fs))
    if wsjtx_dt:            # jt9's dt/freq are good to ~20 ms / ~1 Hz: an 8× smaller grid suffices
        dt_span, df_span = 0.2, 1.5
    dts = np.arange(-dt_span, dt_span + 1e-9, dt_step)
    dfs = np.arange(-df_span, df_span + 1e-9, df_step)
    for df in dfs:
        ref = ft8_reference(tones, f_dec + df, fs)
        for d in dts:
            s0 = nom + int(round(d * fs))
            if s0 < 0 or s0 + NSYM * NSPS > n:
                continue
            e = float(np.sum(np.abs(_sym_dots(ya[s0:s0 + NSYM * NSPS], ref)) ** 2))
            if e > best[0]:
                best = (e, s0, f_dec + df)
    if best[1] is None:                      # message does not fit inside the file
        return None, None
    # refine dt to 2.5 ms around the coarse optimum at the chosen frequency
    _, s0c, f = best
    ref = ft8_reference(tones, f, fs)
    fine = int(0.0025 * fs)
    for s0 in range(s0c - int(dt_step * fs), s0c + int(dt_step * fs) + 1, fine):
        if s0 < 0 or s0 + NSYM * NSPS > n:
            continue
        e = float(np.sum(np.abs(_sym_dots(ya[s0:s0 + NSYM * NSPS], ref)) ** 2))
        if e > best[0]:
            best = (e, s0, f)
    return best[1], best[2]


def phase_ramp_hz(g):
    """|g|-weighted linear fit of the unwrapped phase → frequency in Hz (per-symbol samples)."""
    t = (np.arange(len(g)) + 0.5) * SYM_T
    ph = np.unwrap(np.angle(g))
    w = np.abs(g)
    A = np.vstack([t, np.ones(len(g))]).T * w[:, None]
    slope = np.linalg.lstsq(A, ph * w, rcond=None)[0][0]
    return slope / (2 * math.pi)


def residual_bins(seg, ref, g, tones, f, s0, fs=FS):
    """Noise+interference power per 6.25 Hz bin per symbol: project the RESIDUAL (capture minus
    the estimated signal g_k·ref) onto the 7 tone bins the symbol does not occupy. Projecting the
    raw capture instead is leakage-limited (GFSK sidelobes two bins out are ~25 dB down, equal
    to the per-bin noise at 0 dB SNR2500; measured −1…−2 dB SNR bias)."""
    segm = seg.reshape(NSYM, NSPS)
    refm = ref.reshape(NSYM, NSPS)
    resid = (segm - g[:, None] * refm) * _WIN
    n_ax = (np.arange(NSPS) + s0) / fs
    basis = np.exp(-2j * math.pi * np.outer(n_ax, f + np.arange(8) * TONE_HZ))     # 1920 × 8
    e = np.abs(resid @ basis / _WIN.sum()) ** 2                                       # 79 × 8
    e[np.arange(NSYM), tones] = np.nan
    # ROBUST: an uncancelled neighbour sits on ONE side of the signal and fills up to 7 of the
    # bins (measured: +4..+7 at −14 dB vs −27 dB far bins). Take the mean of each side's bins
    # and keep the lower side; ~−1 dB bias from the min of two noisy estimates, documented.
    # Only when the two sides disagree by > 6 dB (a neighbour; means of 3 exponential draws differ
    # by 2× a third of the time on their own); otherwise the plain 7-bin mean, which is unbiased
    # and what the low-SNR spread floor needs (the min alone cost +9 % there).
    out = np.empty(NSYM)
    for k in range(NSYM):
        lo, hi = e[k, :tones[k]], e[k, tones[k] + 1:]
        cands = [np.mean(v) for v in (lo, hi) if len(v) >= 2]
        out[k] = min(cands) if len(cands) == 2 and max(cands) > 4 * min(cands) else np.nanmean(e[k])
    return out


def fine_timing(ya, tones, s0, f, fs=FS, span=36, step=3):
    """Sharp timing refinement: the per-symbol energy metric is flat within ±10 ms (tone
    correlation vs offset is triangular over a symbol), but a misalignment leaks the NEIGHBOURING
    symbol's tone into the residual — and, worse, into the gain sequence as a wideband term that
    inflated the Doppler spread by 25 % at 2 ms (measured). Minimise the residual-bin energy."""
    best = (np.inf, s0)
    ref = ft8_reference(tones, f, fs)
    for ds in range(-span, span + 1, step):
        s = s0 + ds
        if s < 0 or s + NSYM * NSPS > len(ya):
            continue
        seg = ya[s:s + NSYM * NSPS]
        g = _sym_dots(seg, ref)
        m = float(np.mean(residual_bins(seg, ref, g, tones, f, s, fs)))
        if m < best[0]:
            best = (m, s)
    return best[1]


def timing_from_tone_phase(g, tones):
    """Closed-form timing residual from the tone-dependent phase: between adjacent symbols
    Δφ_k = θ'·T + 2π·6.25·(m_{k+1} − m_k)·Δ; the Doppler term is uncorrelated with the tone
    hops, so a weighted regression of Δφ on Δm gives Δ (seconds)."""
    dphi = np.angle(g[1:] * np.conj(g[:-1]))
    dm = (tones[1:] - tones[:-1]).astype(float)
    w = np.abs(g[1:] * g[:-1])
    A = np.vstack([dm, np.ones(len(dm))]).T * w[:, None]
    slope = np.linalg.lstsq(A, dphi * w, rcond=None)[0][0]
    # a signal arriving Δ LATER than the reference reads g_k ∝ exp(−j2π f_k Δ) ⇒ slope = −2π·6.25·Δ
    return -slope / (2 * math.pi * TONE_HZ)


def refine_timing(ya, tones, s0, f, fs=FS, iters=3):
    """Iterate integer-sample shifts from the tone-phase regression; return (s0, fractional Δ s)."""
    ref = ft8_reference(tones, f, fs)
    d = 0.0
    for _ in range(iters):
        g = _sym_dots(ya[s0:s0 + NSYM * NSPS], ref)
        d = timing_from_tone_phase(g, tones)
        step = int(round(d * fs))
        if step == 0:
            break
        if s0 + step < 0 or s0 + step + NSYM * NSPS > len(ya):
            break
        s0 += step
    return s0, d


def spectral_floor(ya, fs=FS):
    """Median power per 6.25 Hz bin over 300–2700 Hz of the whole file, computed as per-symbol
    (0.16 s) periodograms — the same units as nbin, so |g|²/floor is an independent SNR."""
    n = (len(ya) // NSPS) * NSPS
    blocks = ya[:n].reshape(-1, NSPS)
    P = np.abs(np.fft.fft(blocks, axis=1) / NSPS) ** 2
    fr = np.fft.fftfreq(NSPS, 1 / fs)
    m = (fr >= 300) & (fr <= 2700)
    return float(np.median(P[:, m]))


def estimate(ya, tones, s0, f, fs=FS, frac_delay_s=0.0):
    """Sync-refined per-symbol gains. Returns (f_final, g_raw, gd, nbin): g_raw is the raw
    correlation at the final reference (for reconstruction), gd the detrended gain process."""
    seg = ya[s0:s0 + NSYM * NSPS]
    ref = ft8_reference(tones, f, fs)
    g = _sym_dots(seg, ref)
    rot = np.exp(-2j * math.pi * TONE_HZ * tones * frac_delay_s)   # sub-sample timing residual
    t = (np.arange(NSYM) + 0.5) * SYM_T
    f_resid = phase_ramp_hz(g * rot)
    # Fine frequency: regenerate the reference at f + f_resid. Leaving a 0.25 Hz (half-grid)
    # error in the reference puts a 14° phase ramp INSIDE each symbol; the residual after the
    # constant-g fit then has −20 dB sidebands in the neighbouring bins, which read as noise.
    if abs(f_resid) > 0.02:
        f = f + f_resid
        ref = ft8_reference(tones, f, fs)
        g = _sym_dots(seg, ref)
        f_resid = phase_ramp_hz(g * rot)
    nbin = residual_bins(seg, ref, g, tones, f, s0, fs)
    gd = g * rot * np.exp(-2j * math.pi * f_resid * t)
    return f, f_resid, g, gd, nbin


def stats(gd, nbin, floor_spec=None):
    bwc = 10 * math.log10(BW_REF_HZ / TONE_HZ)
    fc, spread, spread_g = spread_2sigma(gd, floor=float(np.mean(nbin)))
    rms = math.sqrt(np.mean(np.abs(gd) ** 2))
    a_db = 20 * np.log10(np.maximum(np.abs(gd) / rms, 1e-6))
    a = np.abs(gd) - np.mean(np.abs(gd))
    acf = [1.0] + [float(np.sum(a[:-k] * a[k:]) / np.sum(a * a)) for k in range(1, 25)]
    tcoh = next((k * SYM_T for k, v in enumerate(acf) if v < 0.5), float("nan"))
    snr2500 = 10 * np.log10(np.abs(gd) ** 2 / nbin) - bwc
    snr_mean = 10 * math.log10(np.mean(np.abs(gd) ** 2) / np.mean(nbin)) - bwc
    snr_bin_db = snr_mean + bwc
    snr_spec = 10 * math.log10(np.mean(np.abs(gd) ** 2) / floor_spec) - bwc if floor_spec else float("nan")
    return {"snr2500_mean_db": snr_mean, "snr_bin_db": snr_bin_db, "snr2500_spectral_db": snr_spec,
            # measured on the truth cells: the moment reads +19 % at 14 dB per bin, within ±5 % at
            # ≥ 16 dB (SNR2500 ≥ −10 dB); below that quote the flag, not the number
            "spread_reliable": bool(snr_bin_db >= 16.0),
            "g_re": gd.real.round(6).tolist(), "g_im": gd.imag.round(6).tolist(),
            "nbin": nbin.round(9).tolist(), "spread_centroid_hz": float(fc),
            "doppler_2sigma_hz": float(spread), "doppler_2sigma_gauss_hz": float(spread_g),
            "snr2500_median_db": float(np.median(snr2500)), "snr2500_p10_db": float(np.percentile(snr2500, 10)),
            "fade_p10_db": float(np.percentile(a_db, 10)), "fade_p50_db": float(np.percentile(a_db, 50)),
            "fade_p90_db": float(np.percentile(a_db, 90)), "fade_min_db": float(a_db.min()),
            "amp_acf": [round(v, 4) for v in acf], "t_coh_s": tcoh}


def reconstruct(ya_len, sigs, fs=FS):
    """Sum of every decoded signal rebuilt from its per-symbol gains (g_raw·ref)."""
    yhat = np.zeros(ya_len, dtype=complex)
    for sg in sigs:
        ref = ft8_reference(sg["tones"], sg["f"], fs)
        yhat[sg["s0"]:sg["s0"] + NSYM * NSPS] += (np.repeat(sg["g_raw"], NSPS) * ref)
    return yhat


def run_file(path, truth=None, plot_dir=None):
    fs, x = read_wav(path)
    if fs != FS:
        # KiwiSDR streams come at 11999 or 20250 Hz depending on the unit; the decoders want 12000.
        # polyphase resample for a real ratio change, plain interp for the 8e-5 clock-rate case
        # (1 ms of drift over a message, 0.1 Hz of tone error — well inside the estimator's sync)
        if abs(fs - FS) > 10:
            from scipy.signal import resample_poly
            from math import gcd
            g = gcd(int(fs), FS)
            x = resample_poly(x, FS // g, int(fs) // g)
        else:
            x = np.interp(np.arange(0, len(x), fs / FS), np.arange(len(x)), x)
        fs = FS
        rs_dir = os.path.join(os.path.dirname(path), "_rs12k")
        os.makedirs(rs_dir, exist_ok=True)
        rs_path = os.path.join(rs_dir, os.path.basename(path))
        write_wav(rs_path, x, FS)
        path = rs_path
    ya = hilbert(x)
    floor = spectral_floor(ya)
    decs = decode_ft8(path)
    tones = tones_for([d[3] for d in decs])
    # ft8_lib's decoder supplies tones for messages jt9 decoded but cannot be re-encoded (hashed
    # calls), and any decodes jt9 missed
    extra = decoder_tones(path)
    for fr, dt, tn, msg in extra:
        if msg not in tones and "Error" not in msg:
            tones[msg] = tn
        if not any(abs(d[2] - fr) < 3 and d[3] == msg for d in decs):
            decs.append((float("nan"), dt, fr, msg, False))
    rows = []
    # pass 1: sync + gains for every decoded signal
    sigs = []
    for snr_dec, dt_dec, f_dec, msg, wsjtx_dt in decs:
        if msg not in tones:
            print(f"[skip] no re-encode for '{msg}' (hashed call or non-standard)", file=sys.stderr)
            continue
        s0, f = fine_sync(ya, tones[msg], f_dec, dt_dec, wsjtx_dt=wsjtx_dt)
        if s0 is None:
            continue
        s0 = fine_timing(ya, tones[msg], s0, f)
        s0, frac = refine_timing(ya, tones[msg], s0, f)
        f, f_resid, g_raw, gd, nbin = estimate(ya, tones[msg], s0, f, frac_delay_s=frac)
        sigs.append({"msg": msg, "snr_dec": snr_dec, "dt_dec": dt_dec, "f_dec": f_dec, "s0": s0, "frac": frac,
                     "f": f, "f_resid": f_resid, "g_raw": g_raw, "gd": gd, "nbin1": nbin, "tones": tones[msg]})
    # pass 2: successive cancellation — subtract EVERY decoded signal, then read noise+interference
    # in each signal's empty bins from the cleaned capture (a neighbour within ±50 Hz otherwise
    # sits in most of a signal's 7 bins on a 24-decode slot; the median cannot reject that)
    ya_clean = ya - reconstruct(len(ya), sigs)
    floor_clean = spectral_floor(ya_clean)
    for sg in sigs:
        seg = ya_clean[sg["s0"]:sg["s0"] + NSYM * NSPS]
        ref = ft8_reference(sg["tones"], sg["f"])
        sg["nbin"] = residual_bins(seg, ref, np.zeros(NSYM), sg["tones"], sg["f"], sg["s0"])
    for sg in sigs:
        est = stats(sg["gd"], sg["nbin"], floor_spec=floor_clean)
        est_pre = stats(sg["gd"], sg["nbin1"])
        row = {"file": os.path.basename(path), "msg": sg["msg"], "snr_dec_db": sg["snr_dec"], "dt_dec_s": sg["dt_dec"],
               "f_dec_hz": sg["f_dec"], "start_s": sg["s0"] / fs + sg["frac"] - SIG_START_S, "f_hz": sg["f"],
               "f_resid_hz": sg["f_resid"], "snr2500_pre_cancel_db": est_pre["snr2500_mean_db"],
               "floor_spectral_raw": floor, "floor_spectral_clean": floor_clean,
               "n_decoded_in_slot": len(sigs), **est}
        msg, f_total = sg["msg"], sg["f"]
        if truth is not None:
            tr = next((t for t in truth["signals"] if t["msg"] == msg), None)
            if tr:
                gtc = np.array(tr["g_true_re"]) + 1j * np.array(tr["g_true_im"])
                gt = np.abs(gtc)
                ge = np.abs(np.array(est["g_re"]) + 1j * np.array(est["g_im"]))
                # a spread channel's realization has its own phase ramp (Doppler centroid): the
                # estimator is judged against f0 + that ramp, not against f0 alone
                row["truth"] = {"dt_err_ms": 1000 * (row["start_s"] - tr["dt_s"]),
                                "f_err_hz": f_total - (tr["f0_hz"] + phase_ramp_hz(gtc)),
                                "gain_corr": float(np.corrcoef(gt, ge)[0, 1]) if np.std(gt) > 1e-9 else 1.0,
                                "snr_err_db": est["snr2500_mean_db"] - (truth["snr_db"] + 10 * math.log10(np.mean(gt ** 2))),
                                "spread_true_hz": truth["doppler_hz"],
                                "spread_true_from_series_hz": tr["spread_from_series_hz"]}
        rows.append(row)
        if plot_dir:
            plot_signal(row, os.path.join(plot_dir, f"{os.path.splitext(os.path.basename(path))[0]}_{int(f_total)}.png"),
                        truth_row=next((t for t in truth["signals"] if t["msg"] == msg), None) if truth else None)
    return rows


# --------------------------------------------------------------------- synthesis --
def synth(args):
    sys.path.insert(0, os.path.join(HERE, "..", "src"))
    from skywave import watterson
    rng = np.random.default_rng(args.seed)
    msgs = [m for m in args.messages.split(";")]
    f0s = [float(v) for v in args.freqs.split(",")]
    tones = tones_for(msgs)
    n = int(SLOT_S * FS)
    delay_ms, dop = watterson.PRESETS[args.preset] if args.preset != "off" else (0.0, 0.0)
    signals, truth = [], {"preset": args.preset, "delay_ms": delay_ms, "doppler_hz": dop,
                          "snr_db": args.snr, "seed": args.seed, "signals": []}
    for i, (msg, f0) in enumerate(zip(msgs, f0s)):
        dt = float(rng.uniform(-0.3, 0.3))
        clean = np.zeros(n)
        s0 = int(round((SIG_START_S + dt) * FS))
        ref = ft8_reference(tones[msg], f0)
        clean[s0:s0 + len(ref)] = np.imag(ref)                 # sin(phi), as the C synth
        if args.preset != "off":
            ch = watterson.WattersonChannel(FS, delay_ms, dop, SLOT_S + 1, args.seed + 7 * i)
            faded = ch.process(clean.copy())
            # TRUTH = what a perfect estimator returns on the NOISELESS channel output: the same
            # per-symbol correlation applied to the faded-but-clean signal. This isolates noise and
            # sync effects (what the test validates) from the definitional gap between a point-
            # sampled two-path gain and a symbol-averaged one, which is ±20 % near selective nulls
            # (measured: the 2 ms/1 Hz cell read 1.45 vs a point-sampled 1.18 with no noise effect).
            s0c = s0 + ch.gdelay                  # the applicator delays its output by the Hilbert group delay
            g_true = _sym_dots(hilbert(faded)[s0c:s0c + NSYM * NSPS], ref)
            # the analytic two-path gain at each symbol's tone, for information only
            fc = f0 + tones[msg] * TONE_HZ
            tc = (s0 + (np.arange(NSYM) + 0.5) * NSPS) / FS
            idx = tc * ch.low_fs
            i0 = idx.astype(int); fr = idx - i0
            p1 = ch.p1[i0] + (ch.p1[i0 + 1] - ch.p1[i0]) * fr
            p2 = ch.p2[i0] + (ch.p2[i0 + 1] - ch.p2[i0]) * fr
            g_point = (p1 + p2 * np.exp(-2j * math.pi * fc * ch.delay / FS)) * ch.hf_gain
            dt += ch.gdelay / FS
        else:
            faded = clean
            g_true = np.ones(NSYM, dtype=complex)
            g_point = g_true
        # spread of the truth series measured the same way the estimator does (finite-record limit)
        sp_series = spread_2sigma(g_true)[1]
        sp_series_g = spread_2sigma(g_true)[2]
        signals.append(faded)
        truth["signals"].append({"msg": msg, "f0_hz": f0, "dt_s": dt, "g_true_re": g_true.real.round(6).tolist(),
                                 "g_true_im": g_true.imag.round(6).tolist(), "spread_from_series_hz": sp_series, "spread_gauss_from_series_hz": sp_series_g,
                                 "spread_point_sampled_hz": spread_2sigma(g_point)[1]})
    # SNR in 2.5 kHz: signal power 1/2 (unit-amplitude tone); white noise σ² over FS/2 Hz
    snr_lin = 10 ** (args.snr / 10)
    sigma2 = 0.5 / snr_lin * (FS / 2) / BW_REF_HZ
    noise = rng.standard_normal(n) * math.sqrt(sigma2)
    x = sum(signals) + noise
    scale = 0.5 / np.max(np.abs(x))
    write_wav(args.out, x * scale)
    json.dump(truth, open(args.out + ".truth.json", "w"))
    print(f"[synth] {args.out}: {len(msgs)} signals, preset {args.preset} ({delay_ms} ms / {dop} Hz), SNR {args.snr} dB")


# -------------------------------------------------------------------------- plots --
SURFACE, INK, INK2, INK3, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#8a8985", "#e6e5e1"
S1, S2 = "#2a78d6", "#eb6834"


def _style(ax):
    ax.set_facecolor(SURFACE)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.grid(True, color=GRID, linewidth=0.8); ax.set_axisbelow(True)
    ax.tick_params(colors=INK2, labelsize=8); ax.title.set_color(INK)


def plot_signal(row, out_png, truth_row=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    os.makedirs(os.path.dirname(out_png), exist_ok=True)
    g = np.array(row["g_re"]) + 1j * np.array(row["g_im"])
    t = (np.arange(NSYM) + 0.5) * SYM_T
    fig, axes = plt.subplots(3, 1, figsize=(9, 8.5), facecolor=SURFACE)
    for ax in axes:
        _style(ax)
    rms = math.sqrt(np.mean(np.abs(g) ** 2))
    axes[0].plot(t, 20 * np.log10(np.abs(g) / rms), color=S1, linewidth=2, label="estimated |g|")
    if truth_row:
        gt = np.array(truth_row["g_true_re"]) + 1j * np.array(truth_row["g_true_im"])
        axes[0].plot(t, 20 * np.log10(np.abs(gt) / math.sqrt(np.mean(np.abs(gt) ** 2))), color=S2, linewidth=1.5, label="true |g|")
    axes[0].set_ylabel("gain, dB rel. RMS"); axes[0].set_xlabel("s")
    axes[0].set_title(f"{row['msg']}   f={row['f_hz']:.1f} Hz  SNR {row['snr2500_median_db']:.1f} dB  "
                      f"spread {row['doppler_2sigma_hz']:.2f} Hz  t_coh {row['t_coh_s']:.2f} s", fontsize=10, loc="left")
    axes[0].legend(fontsize=8, frameon=False, labelcolor=INK2)
    axes[1].plot(t, np.degrees(np.unwrap(np.angle(g))), color=S1, linewidth=2)
    axes[1].set_ylabel("phase, deg (ramp removed)"); axes[1].set_xlabel("s")
    axes[1].set_title(f"residual frequency {row['f_resid_hz']:+.3f} Hz", fontsize=10, loc="left")
    P = np.abs(np.fft.fft(g, 1024)) ** 2 / NSYM
    fr = np.fft.fftshift(np.fft.fftfreq(1024, SYM_T)); P = np.fft.fftshift(P)
    axes[2].plot(fr, 10 * np.log10(P / P.max() + 1e-9), color=S1, linewidth=2)
    axes[2].axhline(10 * np.log10(np.mean(row["nbin"]) / P.max() + 1e-9), color=INK3, linewidth=0.8)
    axes[2].set_xlim(-3, 3); axes[2].set_ylim(-40, 2)
    axes[2].set_xlabel("Hz"); axes[2].set_ylabel("gain-process PSD, dB")
    axes[2].set_title("Doppler spectrum (grey = noise floor from empty bins)", fontsize=10, loc="left")
    fig.tight_layout(); fig.savefig(out_png, dpi=110, facecolor=SURFACE); plt.close(fig)


# --------------------------------------------------------------------------- main --
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("synth")
    p.add_argument("--out", required=True)
    p.add_argument("--preset", default="poor", help="skywave watterson preset or 'off'")
    p.add_argument("--snr", type=float, default=-10.0, help="SNR in 2.5 kHz, dB (same for all signals)")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--messages", default="CQ N4FPV FM05;K1ABC W9XYZ -12;W1AW K9ZZZ RR73")
    p.add_argument("--freqs", default="800,1500,2200")
    p = sub.add_parser("run")
    p.add_argument("wavs", nargs="+")
    p.add_argument("--out", default="")
    p.add_argument("--truth", default="", help="truth json from synth (enables the T1–T4 checks)")
    p.add_argument("--plot", default="", help="directory for per-signal plots")
    args = ap.parse_args()
    if args.cmd == "synth":
        synth(args); return
    truth = json.load(open(args.truth)) if args.truth else None
    rows = []
    for w in args.wavs:
        rows += run_file(w, truth, args.plot or None)
    fh = open(args.out, "w") if args.out else None
    print(f"{'msg':<22} {'f Hz':>7} {'dt s':>6} {'SNRdec':>6} {'SNRest':>6} {'SNRspc':>6} {'fres':>6} {'spread':>6} {'tcoh':>5} {'p10':>5} {'min':>6}")
    for r in rows:
        print(f"{r['msg']:<22} {r['f_hz']:7.1f} {r['start_s']:6.2f} {r['snr_dec_db']:6.1f} {r['snr2500_mean_db']:6.1f} {r['snr2500_spectral_db']:6.1f} "
              f"{r['f_resid_hz']:+6.2f} {r['doppler_2sigma_hz']:6.2f} {r['t_coh_s']:5.2f} {r['fade_p10_db']:5.1f} {r['fade_min_db']:6.1f}"
              + (f"   truth: dt {r['truth']['dt_err_ms']:+.1f} ms  f {r['truth']['f_err_hz']:+.3f} Hz  "
                 f"corr {r['truth']['gain_corr']:.3f}  snr {r['truth']['snr_err_db']:+.1f} dB  "
                 f"spread est {r['doppler_2sigma_hz']:.2f} vs series {r['truth']['spread_true_from_series_hz']:.2f} (nominal {r['truth']['spread_true_hz']})" if "truth" in r else ""))
        if fh:
            fh.write(json.dumps(r) + "\n")
    if truth and not rows:
        print("[truth] NO DECODES — nothing to score")
    elif truth:
        ok = True
        for r in rows:
            t = r["truth"]
            # a Gaussian-spread realization's centroid is only defined to ~σ/√(d·T) over 12.6 s
            f_bar = 0.1 + 0.25 * truth["doppler_hz"]
            # a two-path channel has no single arrival: a one-tap fit lands anywhere between the paths
            dt_bar = 2.0 + truth["delay_ms"]
            bars = [abs(t["dt_err_ms"]) < dt_bar, abs(t["f_err_hz"]) < f_bar,
                    t["gain_corr"] >= 0.9 or truth["snr_db"] < -10, abs(t["snr_err_db"]) <= 2]
            if truth["doppler_hz"] >= 0.5:
                st = t["spread_true_from_series_hz"]
                bars.append(abs(r["doppler_2sigma_hz"] - st) <= 0.3 * st)   # per signal ±30 %
            ok &= all(bars)
            print(f"[truth] {r['msg']:<22} T1 dt {bars[0]} f {bars[1]}  T2 corr {bars[2]}  T4 snr {bars[3]}"
                  + (f"  T3 spread {bars[4]}" if len(bars) > 4 else "  T3 n/a (< 0.5 Hz)"))
        if truth["doppler_hz"] >= 0.5 and rows:
            ratios = [r["doppler_2sigma_hz"] / r["truth"]["spread_true_from_series_hz"] for r in rows]
            tg = [next(t for t in truth["signals"] if t["msg"] == r["msg"])["spread_gauss_from_series_hz"] for r in rows]
            ratios_g = [r["doppler_2sigma_gauss_hz"] / g for r, g in zip(rows, tg) if g == g and r["doppler_2sigma_gauss_hz"] == r["doppler_2sigma_gauss_hz"]]
            pop = abs(float(np.mean(ratios)) - 1) <= 0.15                   # cell mean within ±15 %
            reliable = all(r["spread_reliable"] for r in rows)
            print(f"[truth] T3 population: moment est/true {np.mean(ratios):.3f}, gaussian-fit est/true "
                  f"{np.mean(ratios_g) if ratios_g else float('nan'):.3f} over {len(ratios)} signals → {pop}"
                  + ("" if reliable else "  (cell below the 16 dB per-bin reliability floor: informational)"))
            ok &= pop or not reliable
        print("[truth] ALL PASS" if ok else "[truth] FAIL")


if __name__ == "__main__":
    main()
