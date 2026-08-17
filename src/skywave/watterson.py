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
    # Completing the Otnes/ITU draft latitude table (PathSim technical guide
    # s4.1.11; the other six conditions were already covered above): low-lat
    # quiet 0.5 ms/0.5 Hz, high-lat quiet 1 ms/0.5 Hz, high-lat moderate
    # 3 ms/10 Hz ("high-lat" above is that table's high-lat DISTURBED).
    "low-lat-quiet": (0.5, 0.5),
    "high-lat-quiet": (1.0, 0.5),
    "high-lat-moderate": (3.0, 10.0),
    # CCIR 520-1 FLAT fading: single-effective-path (zero differential
    # delay — the two equal-power taps sum to one Rayleigh gain), no
    # frequency selectivity; "flat" 0.2 Hz and the 1 Hz extreme. Distinct
    # from the FM Tier-A flat fades (those are envelope-only, no Hilbert).
    "flat": (0.0, 0.2),
    "flat-extreme": (0.0, 1.0),
    # DAMSON-measured auroral 5%-exceedance worst case (Doppler 2-55 Hz, delay
    # 1-11 ms). The one
    # regime measurably outside the mid-lat presets; use only for explicit
    # high-latitude/auroral stress cells, well beyond a typical CP cliff.
    "auroral-max": (11.0, 55.0),
}


FILTER_MODES = ("milstd", "codec2-2016")
# Legacy alias: pre-gen-8 corpora and scripts said "codec2"; the explicit
# -2016 name marks it as the frozen, documented-defective realization.
FILTER_ALIASES = {"codec2": "codec2-2016"}


def _milstd_fir(doppler_hz, low_fs):
    """MIL-STD-188-110C Appendix E fading-filter taps, verbatim construction:
    f_j(t) = k*sqrt(2)*exp(-pi^2*t^2*d^2) for -tau < t < tau, tau chosen so the
    tap magnitude at +-tau is <= 1% of the peak. A Gaussian impulse response
    analytically gives |H(f)|^2 = exp(-2*f^2/d^2) — the App E target tap-gain
    POWER spectrum with 2-sigma spread exactly `d` (sigma = d/2). Unit-energy
    normalized so white noise in -> unit-variance gain process out (the
    realization-level hf_gain normalization still applies downstream)."""
    d = float(doppler_hz)
    tau = np.sqrt(np.log(100.0)) / (np.pi * d)          # 1%-of-peak truncation
    m = int(np.ceil(tau * low_fs))
    t = np.arange(-m, m + 1) / low_fs
    b = np.sqrt(2.0) * np.exp(-(np.pi ** 2) * (t ** 2) * (d ** 2))
    return b / np.sqrt(np.sum(b ** 2))


def _doppler_gain_lowrate(doppler_hz, low_fs, n_low, rng, filter_mode="milstd"):
    """One complex-Gaussian path at `low_fs` with Gaussian Doppler spectrum of
    nominal 2-sigma width `doppler_hz`, shaped per `filter_mode`:

    milstd (default since RIG_GEN 8): MIL-STD-188-110C App E time-domain
      Gaussian taps (_milstd_fir), which realize the standard's tap-gain
      POWER spectrum exactly (|H|^2 Gaussian, spread = nominal within 0.5%
      at 0.1-30 Hz; the F.1487 Eq. 2 convention).
    codec2-2016 (frozen legacy; alias "codec2"): gen_fading.doppler_spread's
      filter — the Gaussian PSD is handed to the frequency-sampling design
      as the AMPLITUDE target (no sqrt), so the realized power spectrum is
      Gaussian^2. Measured realized/nominal spread is SPREAD-DEPENDENT
      (2026-07-28, this exact code): 6.03x at 0.1 Hz, 1.33x at 0.5 Hz,
      0.91x at 1 Hz, 0.79x at >=2 Hz — the low-spread blowup is this
      port's 50 Hz low_fs floor colliding with the fixed 100-point design
      grid (upstream codec2 is a uniform ~0.71x). Kept byte-frozen ONLY to
      reproduce pre-gen-8 corpora; never use for new cells."""
    filter_mode = FILTER_ALIASES.get(filter_mode, filter_mode)
    if filter_mode == "milstd":
        b = _milstd_fir(doppler_hz, low_fs)
        ntaps = len(b)
        w = rng.standard_normal(n_low + ntaps) + 1j * rng.standard_normal(n_low + ntaps)
        return lfilter(b, [1.0], w)[ntaps:].astype(np.complex128)
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

    def __init__(self, fs, delay_ms, doppler_hz, dur_s, seed, hilbert_taps=255,
                 filter_mode="milstd"):
        self.fs = fs
        self.doppler_hz = doppler_hz
        filter_mode = FILTER_ALIASES.get(filter_mode, filter_mode)
        self.filter_mode = filter_mode                     # canonical name
        if filter_mode not in FILTER_MODES:
            raise ValueError(f"unknown fade filter mode '{filter_mode}' "
                             f"(use {'|'.join(FILTER_MODES)})")
        self.delay = int(round(delay_ms * 1e-3 * fs))      # differential delay in samples
        # Low-rate gain process: oversample the Doppler at >= 32x the 2-sigma
        # spread (MIL-STD-188-110C Appendix E's implementation rule; this
        # was 20x, which the self-verification harness already passed but sat
        # below the written guideline), floor 50 Hz so very slow fades still
        # get a smooth interpolation grid.
        self.low_fs = max(50.0, np.ceil(32.0 * max(doppler_hz, 0.05)))
        n_low = int(np.ceil(dur_s * self.low_fs)) + 4
        rng = np.random.default_rng(seed)
        self.p1 = _doppler_gain_lowrate(doppler_hz, self.low_fs, n_low, rng,
                                        filter_mode)
        self.p2 = _doppler_gain_lowrate(doppler_hz, self.low_fs, n_low, rng,
                                        filter_mode)
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
        self._buf = None    # persistent [hist | block] scratch (lazy: block size unknown)
        self._ar = None     # cached float64 arange for the interpolation grid
        self._wrap_warned = False   # one loud warning when the realization cycles

    def _interp_grid(self, n):
        """(i0, frac) low-rate interpolation coordinates for the next `n` audio samples,
        shared by both paths (identical grid — computing it twice was pure waste); wrap
        modulo the generated length so a session longer than `dur_s` keeps fading (a
        small seam once per cycle is harmless for goodput stats). float64 arange is
        bit-identical to the previous int arange (sample indices are exact in a double).

        A wrap REPLAYS the same realization: past `dur_s` the run stops
        accumulating independent fade states (F.1487 Annex 3 §6 statistics),
        so the first wrap warns loudly rather than passing silently."""
        if self._ar is None or self._ar.shape[0] < n:
            self._ar = np.arange(n, dtype=np.float64)
        idx = (self.t + self._ar[:n]) * (self.low_fs / self.fs)
        if not self._wrap_warned and \
                (self.t + n) * (self.low_fs / self.fs) >= self.n_low - 1:
            self._wrap_warned = True
            import sys as _sys
            print(f"watterson: fade realization wrapped at t="
                  f"{self.t / self.fs:.0f}s — the run now REPLAYS the same "
                  f"{(self.n_low - 1) / self.low_fs:.0f}s fading trace and stops "
                  "accumulating independent fade states (raise SIM_FADE_DUR_S "
                  "to cover the run, or count fade_units per F.1487 A3 s6)",
                  file=_sys.stderr, flush=True)
        idx = np.mod(idx, self.n_low - 1)
        i0 = idx.astype(np.int64)
        frac = idx - i0
        return i0, frac

    def _gain_block(self, gain_low, n):
        """Linear-interpolate `n` complex gain samples at the current absolute time
        (kept for API compatibility; process() uses the shared-grid path)."""
        i0, frac = self._interp_grid(n)
        g0 = gain_low[i0]
        g1 = gain_low[i0 + 1]
        return g0 + (g1 - g0) * frac

    def process(self, block, out=None):
        """Fade one real float block (length N). Returns the faded real float block.
        State (Hilbert/delay history, Doppler phase) carries to the next call."""
        n = len(block)
        if out is None:
            out = np.empty(n, dtype=np.float64)
        # [history | block] in a persistent scratch (was a per-block np.concatenate);
        # the analytic signal is computed over the whole span.
        if self._buf is None or self._buf.shape[0] != self.hist_len + n:
            self._buf = np.empty(self.hist_len + n, dtype=np.float64)
        buf = self._buf
        buf[:self.hist_len] = self.hist
        buf[self.hist_len:] = block
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
        # Delayed path: same analytic signal `delay` samples earlier.
        real1 = buf[base - self.delay: base - self.delay + n]
        imv1 = imag_full[base - self.gdelay - self.delay: base - self.gdelay - self.delay + n]
        i0, frac = self._interp_grid(n)             # one grid, both paths
        g1 = self.p1[i0] + (self.p1[i0 + 1] - self.p1[i0]) * frac
        g2 = self.p2[i0] + (self.p2[i0 + 1] - self.p2[i0]) * frac
        # Re{g1*z0 + g2*z1} expanded on the real/imag parts directly — identical
        # elementwise arithmetic (a complex multiply's real part IS gr*zr - gi*zi),
        # without materializing z0/z1 or the complex products' imaginary halves.
        out[:] = (g1.real * real0 - g1.imag * imv0) + (g2.real * real1 - g2.imag * imv1)
        out *= self.hf_gain
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
                 on_transition=None, hilbert_taps=255, filter_mode="milstd"):
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
                                      hilbert_taps, filter_mode=filter_mode)
            end = None if length is None else t + length
            self.segs.append([name, ch, t, end])
            if length is None:
                break
            t += length
        self.t = 0              # absolute audio-sample index
        self.cur = 0            # index of the currently-primary segment
        self._announced = 0     # highest transition index already logged

    def _seg_out(self, seg, block):
        # "blackout": total signal loss for the segment's duration (the GEN2
        # stall-wrongness cell -- a modem silent through no fault of its own).
        # Entered/left through the standard crossfade below, like any segment;
        # a run that never names it is byte-identical to before.
        if seg[0] == "blackout":
            return np.zeros_like(block)
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
