#!/usr/bin/env python3
"""Streaming Watterson HF channel applicator for channel_sim (ITU-R F.1487).

The Doppler *generation* is the proven `gen_fading.py` recipe (a numpy/scipy port of
codec2's doppler_spread.m / ch_fading.m, David Rowe): two INDEPENDENT complex-Gaussian
paths with a Gaussian-shaped Doppler spectrum of spread `doppler_hz` (the 2-sigma width,
NO frequency shift). This module adds the *application* half so a real passband audio
block stream can be faded on the fly, block by block, with state carried across blocks:

    faded(t) = hf_gain * Re{ p1(t)*z(t) + p2(t)*z(t - tau) }

where z(t) is the analytic signal of the real input (FIR Hilbert), `tau` is the
differential multipath delay (F.1487's second tap), and `hf_gain` normalizes the average
power to unity (1/sqrt(var p1 + var p2)) so the AWGN SNR calibration is preserved. The
two equal-power paths interfering across the `tau` delay give the frequency-SELECTIVE
fading that actually challenges a modem's pilots/equalizer — flat amplitude fading would
miss it.

Named channels (delay_ms, doppler_hz):
    CCIR-Good 0.5/0.1   CCIR-Moderate 1.0/0.5   CCIR-Poor 2.0/1.0  (canonical)
    low-lat-moderate 2.0/1.5 (the original "poor" preset)    flutter 0.5/10 (CCIR 520-2)
    nvis 3.0/1.0        nvis-max 4.0/1.0        (REALISTIC NVIS: ~0.3ms typ / ~3ms max
                                                 measured, low Doppler; see research doc)
    disturbed 6.0/10    nvis-disturbed 7.0/1.0   high-lat 7.0/30  (extremes / F.1487 tail;
                                                 nvis-disturbed = F.1487 Annex 3 §3.4)

Efficiency: the gain process is generated and stored at a LOW rate (>> 2*doppler) and
linearly interpolated up to the audio rate per block (the gain is slowly varying — at
1 Hz Doppler it barely moves across a 21 ms block), so the hot path is a handful of
vectorized numpy ops, not a 48 kHz complex FIR.
"""
import numpy as np
from scipy.signal import firwin2, lfilter

# F.1487 Annex 3 named channels: (differential delay ms, Doppler 2-sigma spread Hz).
PRESETS = {
    "off": None,
    "good": (0.5, 0.1),
    "moderate": (1.0, 0.5),
    # "poor" = canonical CCIR 520-2 / MIL-STD-188-110C Poor (2 ms / 1.0 Hz),
    # matching codec2 `ch --mpp`, PathSim, and DRM Channel 4. Before a later
    # correction this preset was 1.5 Hz = F.1487 LOW-LAT MODERATE (the hotter
    # cell), so the original "poor" numbers are NOT comparable to the
    # corrected "poor"; the old cell now lives under its correct name below
    #.
    "poor": (2.0, 1.0),
    "low-lat-moderate": (2.0, 1.5),
    # CCIR 520-2 "flutter" (0.5 ms / 10 Hz) — the one common standard cell
    # earlier presets lacked by name.
    "flutter": (0.5, 10.0),
    # NVIS (near-vertical incidence) is LOW-Doppler (~1 Hz). Measured mid-lat delay
    # spread is ~0.3 ms typical / ~3 ms observed-max. "nvis" = realistic;
    # "nvis-max" = observed-max stress, just under
    # just under a representative ~5 ms cyclic-prefix cliff; "nvis-disturbed"
    # (7 ms) = F.1487 Annex 3 §3.4
    # WORST-CASE tail (was the plain "nvis" preset before 2026-07-08 — relabeled so the
    # default "nvis" is realistic, not the disturbed extreme).
    "nvis": (3.0, 1.0),
    "nvis-max": (4.0, 1.0),
    "disturbed": (6.0, 10.0),
    "nvis-disturbed": (7.0, 1.0),
    "high-lat": (7.0, 30.0),
    # DAMSON-measured auroral 5%-exceedance worst case (Doppler 2-55 Hz, delay
    # 1-11 ms). The one
    # regime measurably outside the mid-lat presets; use only for explicit
    # high-latitude/auroral stress cells, well beyond a typical CP cliff.
    "auroral-max": (11.0, 55.0),
}


def _doppler_gain_lowrate(doppler_hz, low_fs, n_low, rng):
    """One complex-Gaussian path at `low_fs`, Gaussian Doppler spectrum of 2-sigma width
    `doppler_hz` (gen_fading.doppler_spread's filter, generated at the low rate and kept
    there for interpolation rather than resampled up)."""
    sigma = doppler_hz / 2.0
    ntaps = 100
    # Gaussian frequency response 0..low_fs/2, FIR via frequency sampling (octave fir2).
    x = np.arange(0.0, low_fs / 2.0 + 1e-9, low_fs / 100.0)
    y = (1.0 / (sigma * np.sqrt(2 * np.pi))) * np.exp(-(x ** 2) / (2 * sigma * sigma))
    f = x / (low_fs / 2.0)
    f[0], f[-1] = 0.0, 1.0
    y[-1] = 0.0  # zero gain at Nyquist so the even-tap firwin2 is valid
    b = firwin2(ntaps, f, y)
    w = rng.standard_normal(n_low + ntaps) + 1j * rng.standard_normal(n_low + ntaps)
    g = lfilter(b, [1.0], w)[ntaps:]  # drop the filter transient
    return g.astype(np.complex128)


def _hilbert_fir(ntaps):
    """Odd-length type-III FIR Hilbert transformer (Hamming-windowed). Group delay =
    (ntaps-1)/2 samples; pairs with a matched delay of the real part to form the analytic
    signal. Usable band ~ (fs/ntaps) .. (fs/2 - fs/ntaps), which covers the 0.5-2.7 kHz
    modem band comfortably at 48 kHz with ntaps=255."""
    if ntaps % 2 == 0:
        ntaps += 1
    m = (ntaps - 1) // 2
    n = np.arange(-m, m + 1)
    h = np.zeros(ntaps)
    odd = n % 2 != 0
    h[odd] = 2.0 / (np.pi * n[odd])
    h *= np.hamming(ntaps)
    return h, m


class WattersonChannel:
    """Stateful per-direction Watterson applicator. One instance per channel direction
    (independent fading), fed fixed-size real float blocks via `process(block, out)`."""

    def __init__(self, fs, delay_ms, doppler_hz, dur_s, seed, hilbert_taps=255):
        self.fs = fs
        self.doppler_hz = doppler_hz
        self.delay = int(round(delay_ms * 1e-3 * fs))      # differential delay in samples
        # Low-rate gain process: oversample the Doppler at >= 32x the 2-sigma
        # spread (MIL-STD-188-110C Appendix E's implementation rule; this
        # was 20x, which the self-verification harness already passed but sat
        # below the written guideline), floor 50 Hz so very slow fades still
        # get a smooth interpolation grid.
        self.low_fs = max(50.0, np.ceil(32.0 * max(doppler_hz, 0.05)))
        n_low = int(np.ceil(dur_s * self.low_fs)) + 4
        rng = np.random.default_rng(seed)
        self.p1 = _doppler_gain_lowrate(doppler_hz, self.low_fs, n_low, rng)
        self.p2 = _doppler_gain_lowrate(doppler_hz, self.low_fs, n_low, rng)
        # F.1487: equal mean power, normalized so the average faded power == input power.
        self.hf_gain = 1.0 / np.sqrt(np.var(self.p1) + np.var(self.p2))
        self.n_low = len(self.p1)
        # Hilbert FIR + history so the analytic signal is continuous across blocks.
        self.h, self.gdelay = _hilbert_fir(hilbert_taps)
        # History must cover the Hilbert reach AND the path delay: enough past real samples
        # to form z[n] and z[n - delay] for the whole block.
        self.hist_len = len(self.h) + self.delay
        self.hist = np.zeros(self.hist_len, dtype=np.float64)
        self.t = 0          # absolute audio-sample index (for Doppler interpolation)

    def _gain_block(self, gain_low, n):
        """Linear-interpolate `n` complex gain samples at the current absolute time onto
        the low-rate grid; wrap modulo the generated length so a session longer than
        `dur_s` keeps fading (a small seam once per cycle is harmless for goodput stats)."""
        idx = (self.t + np.arange(n)) * (self.low_fs / self.fs)
        idx = np.mod(idx, self.n_low - 1)
        i0 = idx.astype(np.int64)
        frac = idx - i0
        g0 = gain_low[i0]
        g1 = gain_low[i0 + 1]
        return g0 + (g1 - g0) * frac

    def process(self, block, out=None):
        """Fade one real float block (length N). Returns the faded real float block.
        State (Hilbert/delay history, Doppler phase) carries to the next call."""
        n = len(block)
        if out is None:
            out = np.empty(n, dtype=np.float64)
        # Concatenate history + this block, compute the analytic signal over the block.
        buf = np.concatenate((self.hist, block))
        # Imag part: full-band FIR Hilbert (valid region aligned so z[k] matches buf sample
        # at offset hist_len - gdelay + k... we index explicitly below).
        imag_full = np.convolve(buf, self.h, mode="valid")  # length = len(buf)-len(h)+1
        # buf index of the analytic sample aligned to output sample k (k in 0..n-1):
        #   real = buf[hist_len + k - gdelay]      (real part, delayed by the Hilbert group)
        #   imag = imag_full[hist_len + k - gdelay - (len(h)-1)... ] -> align via base
        base = self.hist_len - self.gdelay          # buf index of output sample 0's real part
        real0 = buf[base:base + n]
        # imag_full[j] corresponds to buf center index j + gdelay; we want center = base + k.
        imv0 = imag_full[base - self.gdelay: base - self.gdelay + n]
        z0 = real0 + 1j * imv0
        # Delayed path: same analytic signal `delay` samples earlier.
        real1 = buf[base - self.delay: base - self.delay + n]
        imv1 = imag_full[base - self.gdelay - self.delay: base - self.gdelay - self.delay + n]
        z1 = real1 + 1j * imv1
        g1 = self._gain_block(self.p1, n)
        g2 = self._gain_block(self.p2, n)
        faded = self.hf_gain * np.real(g1 * z0 + g2 * z1)
        out[:] = faded
        # Roll history: keep the last hist_len samples of buf for the next block.
        self.hist[:] = buf[-self.hist_len:]
        self.t += n
        return out


class ScheduledFade:
    """A time sequence of Watterson (or pass-through 'off') segments within one
    session — the instrument for testing ADAPTIVE rate control (static presets
    never exercise mode switching).

    Segments are `(preset_name, duration_s)`; the final duration may be 0 =
    "rest of the run". Transitions crossfade linearly over `xfade_s` (a real
    channel varies continuously — a hard preset swap would inject an artificial
    gain/phase step). All segments are constructed up front (each independently
    seeded from `seed` so paired-seed A/Bs see identical realizations) and each
    plays from its own t=0 when it becomes active. `on_transition(t_s, frm,
    to)` is called once per boundary with the elapsed audio seconds — the
    ground truth for scoring mode-switch latency.

    `.process(block)` matches WattersonChannel's interface. An 'off' segment
    passes the block through unfaded (gain 1)."""

    def __init__(self, fs, segments, dur_s, seed, xfade_s=1.0,
                 on_transition=None, hilbert_taps=255):
        self.fs = fs
        self.xfade = max(1, int(round(xfade_s * fs)))
        self.on_transition = on_transition
        self.segs = []          # (name, WattersonChannel|None, start_sample, end_sample)
        t = 0
        for i, (name, secs) in enumerate(segments):
            length = None if secs == 0 else int(round(secs * fs))
            preset = PRESETS.get(name)
            ch = None
            if preset is not None:
                delay_ms, dop = preset
                ch = WattersonChannel(fs, delay_ms, dop, dur_s, seed + 100 * i,
                                      hilbert_taps)
            end = None if length is None else t + length
            self.segs.append([name, ch, t, end])
            if length is None:
                break
            t += length
        self.t = 0              # absolute audio-sample index
        self.cur = 0            # index of the currently-primary segment
        self._announced = 0     # highest transition index already logged

    def _seg_out(self, seg, block):
        ch = seg[1]
        return block if ch is None else ch.process(block)

    def process(self, block):
        n = len(block)
        seg = self.segs[self.cur]
        # Advance to the segment whose span contains t (segments are contiguous;
        # a block spanning a boundary is handled by the crossfade below, which
        # runs both neighbours — exact sub-block alignment is unnecessary at the
        # 21 ms block grain vs multi-second segments).
        while seg[3] is not None and self.t >= seg[3] and self.cur + 1 < len(self.segs):
            self.cur += 1
            seg = self.segs[self.cur]
            if self.cur > self._announced:
                self._announced = self.cur
                if self.on_transition is not None:
                    self.on_transition(self.t / self.fs, self.segs[self.cur - 1][0],
                                       seg[0])
        out = self._seg_out(seg, block)
        # Crossfade the trailing edge of the PREVIOUS segment into this one for
        # `xfade` samples after each boundary (both segments must be advanced so
        # their Doppler clocks stay real-time; the previous one is run for its
        # blend contribution only).
        if self.cur > 0:
            since = self.t - seg[2]
            if since < self.xfade:
                prev = self.segs[self.cur - 1]
                prev_out = self._seg_out(prev, block)
                a = np.clip((since + np.arange(n)) / self.xfade, 0.0, 1.0)
                out = a * out + (1.0 - a) * prev_out
            elif self.segs[self.cur - 1][1] is not None:
                # keep the just-passed segment's clock warm one block past the
                # blend so a subsequent re-entry (not used today) stays smooth;
                # cheap and keeps state consistent.
                pass
        self.t += n
        return out
