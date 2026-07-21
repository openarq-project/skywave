#!/usr/bin/env python3
"""Realism effects for channel_sim.

Every class here is stateful per channel direction, block-stream driven, and OFF
by default in channel_sim (the knob's env var unset/0 keeps the baseline bit-exact
— the classes are not even constructed). 
  FreqShift      SIM_FOFF_HZ           two-station LO mismatch (+iono Doppler)
  ClockSkew      SIM_CLOCK_PPM         soundcard sample-clock drift (per-burst)
  AlcOvershoot   SIM_ALC_OVERSHOOT_DB  TX burst-onset ALC overshoot
  RxAgc          SIM_RX_AGC            receiver AGC (burst-head gain error)
  ImpulsiveNoise SIM_NOISE_VD          P.372 Vd-calibrated non-Gaussian noise
  QrmGenerator   SIM_QRM_OCC           in-channel CW-Morse QRM + swept carrier
"""
import math

import numpy as np

from skywave.watterson import _hilbert_fir


class FreqShift:
    """Constant carrier-frequency offset on a real passband stream.

    y(t) = Re{ z(t) * e^(j*2*pi*f*t) } with z the analytic signal (FIR Hilbert,
    state carried across blocks; output delayed by the Hilbert group delay, same
    as the Watterson applicator). SIM_FOFF_HZ is the two-station DIFFERENTIAL:
    channel_sim applies +f on A->B and -f on B->A (opposing LO errors).
    Realistic values: typ +-7..15 Hz (TCXO pair), worst ~+-100 Hz (research doc
    Section 2 axis 10)."""

    def __init__(self, fs, foff_hz, hilbert_taps=255, ramp_to_hz=None, ramp_s=600.0):
        self.fs = fs
        self.foff = float(foff_hz)
        # Optional slow carrier-drift ramp. The
        # instantaneous offset ramps linearly from foff_hz to ramp_to_hz over
        # ramp_s seconds, then holds — the measured ~1-2 Hz greyline drift
        # (magnitude literature-cited; the ramp schedule is ours). The phase is
        # the INTEGRAL of the instantaneous frequency, so a changing offset
        # stays phase-continuous (a per-block constant-freq would step-jump).
        self.ramp_to = None if ramp_to_hz is None else float(ramp_to_hz)
        self.ramp_samp = max(1.0, float(ramp_s) * fs)
        self.h, self.gdelay = _hilbert_fir(hilbert_taps)
        self.hist_len = len(self.h)
        self.hist = np.zeros(self.hist_len, dtype=np.float64)
        self.t = 0
        self.phase = 0.0        # accumulated carrier phase (rad), for ramp continuity

    def _foff_at(self, t_samp):
        """Instantaneous offset (Hz) at absolute sample indices `t_samp`."""
        if self.ramp_to is None:
            return np.full_like(t_samp, self.foff, dtype=np.float64)
        frac = np.clip(t_samp / self.ramp_samp, 0.0, 1.0)
        return self.foff + (self.ramp_to - self.foff) * frac

    def process(self, block):
        n = len(block)
        buf = np.concatenate((self.hist, block))
        imag_full = np.convolve(buf, self.h, mode="valid")
        base = self.hist_len - self.gdelay
        z = buf[base:base + n] + 1j * imag_full[base - self.gdelay:
                                                base - self.gdelay + n]
        if self.ramp_to is None:
            # Static path: preserve the original formula EXACTLY (bit-exact
            # baseline — ph[k] = 2*pi*foff*(t+k)/fs).
            ph = (2.0 * np.pi * self.foff / self.fs) * (self.t + np.arange(n))
        else:
            # Ramp path: phase = cumulative integral of the (time-varying)
            # instantaneous frequency, so a changing offset stays
            # phase-continuous. Sampled at t+k+1 so the running accumulator
            # matches a constant-freq integral when ramp_to == foff.
            f = self._foff_at(self.t + np.arange(n) + 1)
            ph = self.phase + np.cumsum(2.0 * np.pi * f / self.fs)
            self.phase = float(ph[-1])
        out = np.real(z * np.exp(1j * ph))
        self.hist[:] = buf[-self.hist_len:]
        self.t += n
        return out


class ClockSkew:
    """Sample-clock mismatch (SIM_CLOCK_PPM): stateful fractional resampler.

    Models the RECEIVER's ADC clock running (1 + ppm*1e-6) relative to the
    sender's DAC — accumulating symbol-timing skew proportional to burst length,
    a distinct mechanism from FreqShift's static tone offset. PER-BURST: the
    resampler clock resets at each burst onset (real modems re-acquire timing on
    every preamble, so inter-burst drift is invisible to them; what stresses the
    tracking loops is INTRA-burst drift = ppm x burst duration). The small slack
    prefill (default 20 ms << the 80 ms HD hangtime, so the HD deliver gate
    stays aligned) absorbs the read-ahead of a fast receiver clock; it bounds
    the max burst length at slack/(ppm*1e-6) (~133 s at 150 ppm) — beyond that
    the read head freezes on the last sample (glitch) rather than crashing.
    Realistic values: typ 10-50 ppm differential, worst ~150-200 (research doc
    axis 11)."""

    def __init__(self, fs, ppm, slack_ms=20.0, act_thresh=200.0, rearm_blocks=4):
        self.fs = fs
        self.rate = 1.0 + float(ppm) * 1e-6
        self.slack = max(2, int(round(slack_ms * 1e-3 * fs)))
        self.thresh = act_thresh
        self.rearm = rearm_blocks
        self.idle = rearm_blocks     # start re-armed
        self._reset()

    def _reset(self):
        self.buf = np.zeros(self.slack, dtype=np.float64)
        self.pos = 0.0

    def process(self, block):
        n = len(block)
        rms = math.sqrt(float(np.dot(block, block)) / n) if n else 0.0
        if rms > self.thresh:
            if self.idle >= self.rearm:
                self._reset()        # new burst: timing re-acquired, drift restarts
            self.idle = 0
        else:
            self.idle += 1
        self.buf = np.concatenate((self.buf, block))
        idx = self.pos + np.arange(n) * self.rate
        i0 = np.minimum(idx.astype(np.int64), len(self.buf) - 2)
        frac = np.minimum(idx - i0, 1.0)
        out = self.buf[i0] * (1.0 - frac) + self.buf[i0 + 1] * frac
        consumed = int(i0[-1]) if n else 0
        self.buf = self.buf[consumed:]
        self.pos = (idx[-1] - consumed) + self.rate if n else self.pos
        return out


class AlcOvershoot:
    """TX burst-onset ALC overshoot: gain(t) = 1 + (10^(dB/20)-1)*exp(-t/tau).

    Applied to the post-gain TX signal BEFORE the PA/clip stage, so the
    overshoot drives the existing nonlinearity harder on every burst head —
    exactly the measured behavior (IC-706MKII 130-145 W first-dit regardless of
    power setting; modern DSP rigs ~0.2-1 dB; research doc axis 12). Onset =
    block RMS rising past act_thresh after >= rearm_blocks of idle, so
    intra-burst gaps shorter than the re-arm window do NOT retrigger.
    SIM_ALC_OVERSHOOT_DB: +0.5 modern / +6 legacy. SIM_ALC_SETTLE_MS is the
    exponential time constant tau (settled by ~3*tau; measured range 2-30 ms)."""

    def __init__(self, fs, overshoot_db, settle_ms=10.0, nch=1,
                 act_thresh=200.0, rearm_blocks=4, rearm_s=None):
        self.fs = fs
        self.amp = 10.0 ** (float(overshoot_db) / 20.0) - 1.0
        self.tau = max(1.0, float(settle_ms) * 1e-3 * fs)   # samples
        self.nch = nch
        self.thresh = act_thresh
        # The `legacy` preset's overshoot re-arms only after a LONG
        # silence (~5 s, IC-706MKII-class QEX measurement) — that cadence
        # matches HD ARQ turnaround, so every fresh burst after a turnaround
        # takes the full spike. rearm_s (samples, block-size-independent)
        # overrides the legacy block-count re-arm when set.
        self.rearm = rearm_blocks
        self.rearm_samp = float(rearm_s) * fs if rearm_s else 0.0  # 0 => block path
        self.idle = rearm_blocks                    # start re-armed (block path)
        self.idle_samp = self.rearm_samp            # start re-armed (sample path)
        self.burst_t = 0

    def process(self, w):
        """In-place on the interleaved float block; returns w."""
        frames = len(w) // self.nch
        rms = math.sqrt(float(np.dot(w, w)) / len(w)) if len(w) else 0.0
        if rms > self.thresh:
            rearmed = (self.idle_samp >= self.rearm_samp if self.rearm_samp > 0
                       else self.idle >= self.rearm)
            if rearmed:
                self.burst_t = 0                 # fresh burst: overshoot fires
            self.idle = 0
            self.idle_samp = 0
            if self.burst_t < 8.0 * self.tau and self.amp != 0.0:
                t = self.burst_t + np.arange(frames, dtype=np.float64)
                g = 1.0 + self.amp * np.exp(-t / self.tau)
                w *= np.repeat(g, self.nch)
            self.burst_t += frames
        else:
            self.idle += 1
            self.idle_samp += frames
        return w


class RxAgc:
    """Receiver AGC: asymmetric envelope follower + bounded make-up gain.

    Applied at the very end of the receive chain (after noise and the RX BPF) —
    what it models is the burst-HEAD gain error: after a quiet gap the gain sits
    at max (pinned by the noise floor), so the first symbols of a burst arrive
    over-amplified (and clip at the int16 rail) until the attack settles — the
    real cost a fixed-gain sim never charges (research doc axis 13). Envelope
    tracks sub-block peaks (sub=64 samples = 1.3 ms resolution, well under the
    2 ms attack), attack when the peak exceeds the envelope, release otherwise.
    Presets: attack 2 ms; release 100 ms ~= a rig's FAST AGC (IC-7300 0.1 s)."""

    def __init__(self, fs, attack_ms=2.0, release_ms=100.0, target=8000.0,
                 max_gain_db=30.0, sub=64):
        self.sub = sub
        self.ca = math.exp(-sub / (fs * attack_ms * 1e-3))
        self.cr = math.exp(-sub / (fs * release_ms * 1e-3))
        self.target = float(target)
        self.gmax = 10.0 ** (max_gain_db / 20.0)
        self.env = self.target                     # unity gain at start

    def process(self, mono):
        n = len(mono)
        nsub = n // self.sub
        m = mono[: nsub * self.sub].reshape(nsub, self.sub)
        pk = np.abs(m).max(axis=1)
        gains = np.empty(nsub)
        env = self.env
        for i in range(nsub):                      # 16 iterations/block: cheap
            # gain applies from the PREVIOUS loop state — the real AGC reacts
            # AFTER the audio passes, which is exactly the burst-head gain
            # error this models (fresh burst after a quiet gap hits max gain
            # for the first sub-blocks until the attack catches up)
            gains[i] = min(self.gmax, self.target / max(env, 1e-6))
            c = self.ca if pk[i] > env else self.cr
            env = c * env + (1.0 - c) * pk[i]
        self.env = env
        out = (m * gains[:, None]).reshape(-1)
        if nsub * self.sub < n:                    # ragged tail (non-multiple block)
            out = np.concatenate([out, mono[nsub * self.sub:] * gains[-1]])
        return out


class ImpulsiveNoise:
    """P.372 Vd-calibrated non-Gaussian noise (research doc axis 6/7).

    Gaussian core + Bernoulli-gated Gaussian impulses (a two-component
    Middleton-Class-A-like mixture), with the per-sample impulse probability p
    solved AT INIT (fixed calibration RNG, deterministic) so the ENVELOPE
    voltage deviation Vd = 20*log10(env_rms/env_mean) hits the requested target
    (P.372's own impulsiveness metric: Gaussian = 1.05 dB; temperate-latitude HF
    typically 2-8 dB). Total power is exactly sigma^2, so the SNR axis is
    unchanged — only the amplitude STATISTICS move. k_db sets the impulse-to-
    core power ratio (26 dB default; bounds the max reachable Vd)."""

    def __init__(self, sigma, vd_db, k_db=26.0):
        from scipy.signal import hilbert
        self.sigma = float(sigma)
        k2 = 10.0 ** (float(k_db) / 10.0)
        cal = np.random.default_rng(202607)        # fixed: calibration is deterministic
        ncal = 100000
        g = cal.standard_normal(ncal)
        imp = cal.standard_normal(ncal) * math.sqrt(k2)
        u = cal.random(ncal)

        def vd_of(p):
            x = g + (u < p) * imp
            env = np.abs(hilbert(x / x.std()))
            return 20.0 * math.log10(
                math.sqrt(float((env ** 2).mean())) / float(env.mean()))

        grid = np.logspace(-5, math.log10(0.3), 40)
        vds = np.array([vd_of(p) for p in grid])
        target = float(vd_db)
        if target > vds.max() + 0.5:
            raise ValueError(f"SIM_NOISE_VD={target} dB unreachable with "
                             f"k_db={k_db} (max ~{vds.max():.1f} dB) — raise "
                             f"SIM_NOISE_VD_K_DB")
        self.p = float(grid[int(np.argmin(np.abs(vds - target)))])
        self.vd_realized = float(vds[int(np.argmin(np.abs(vds - target)))])
        # power split: sigma^2 = sb^2 * (1 + p*k2)
        self.sb = self.sigma / math.sqrt(1.0 + self.p * k2)
        self.si = self.sb * math.sqrt(k2)

    def fill(self, rng, out):
        """Fill `out` (preallocated float array) with one block of noise, using
        the LINK's rng (seed-deterministic like the Gaussian path)."""
        n = len(out)
        rng.standard_normal(n, out=out)
        out *= self.sb
        mask = rng.random(n) < self.p
        k = int(mask.sum())
        if k:
            out[mask] += rng.standard_normal(k) * self.si


# "PARIS" — the standard 50-unit WPM reference word — as (keyed, units) runs:
# dit=1 on, dah=3 on, intra-char gap 1, inter-char gap 3, inter-word gap 7.
# 22 of 50 units keyed => 44% duty.
_PARIS = ((1, 1), (0, 1), (1, 3), (0, 1), (1, 3), (0, 1), (1, 1), (0, 3),  # P
          (1, 1), (0, 1), (1, 3), (0, 3),                                  # A
          (1, 1), (0, 1), (1, 3), (0, 1), (1, 1), (0, 3),                  # R
          (1, 1), (0, 1), (1, 1), (0, 3),                                  # I
          (1, 1), (0, 1), (1, 1), (0, 1), (1, 1), (0, 7))                  # S


def _paris_word_env(dot):
    """Keying envelope of ONE "PARIS" word (50*dot samples) with raised-cosine
    rise/fall of dot/10 on every keyed segment (Mendieta-Otero eqs. 18-19).
    The interferer tiles it by modular indexing — the paper's model is PARIS
    repeated for the whole QSO, and one word keeps the spawn cost O(50*dot)
    (real-time block-loop headroom; see test_perf_headroom)."""
    edge = max(1, dot // 10)
    ramp = 0.5 - 0.5 * np.cos(np.pi * (np.arange(edge) + 0.5) / edge)   # 0 -> 1
    env = np.zeros(50 * dot)
    pos = 0
    for on, units in _PARIS:
        seg = units * dot
        if on:
            e = np.ones(seg)
            r = min(edge, seg // 2)
            if r:
                e[:r] = ramp[:r]
                e[seg - r:] = ramp[:r][::-1]
            env[pos:pos + seg] = e
        pos += seg
    return env


class QrmGenerator:
    """In-channel amateur-band QRM: occupancy-driven CW-Morse interferer +
    optional swept carrier.

    Timing and keying follow Mendieta-Otero et al. (IEEE Trans. EMC, DOI
    10.1109/TEMC.2014.2313064; arXiv:2402.04742): Poisson onsets,
    exponential durations (mean 10 s), the "PARIS" CW envelope with
    dot = duration/331 (clamped 10-60 WPM) and raised-cosine edges of
    dot/10. Density and level are re-keyed to skywave's axes:

    - `occupancy` is the IN-CHANNEL busy fraction. The paper's whole-band
      contest-peak lambda (6.68/s over ~3.8 MHz of amateur allocations)
      scales to ~0.04 occupancy in one 2.4 kHz passband. At most ONE
      interferer is active (Erlang-loss M/G/1/1; P(>=2 in-channel) is
      negligible at any realistic occupancy), so the spawn rate is
      lambda = occ / (mean_dur * (1 - occ)).
    - each interferer draws its level ONCE from Normal(inr_db,
      inr_spread_db) dB over the channel noise POWER sigma^2 (INR),
      truncated at inr_max_db — the caller's rail-headroom budget
      (channel_sim computes it from the RX pad).

    The sweeper is an OTHR-ish sawtooth chirp at sweep_rate sweeps/s
    (IARUMS: 10/s; not from the paper) over a VIRTUAL span of
    sweep_band_hz — real swept-carrier systems chirp tens of kHz to MHz,
    so a 2.4 kHz passband sees only the crossing: a short chirp burst
    sweep_rate times per second, in-channel duty = passband/sweep_band_hz
    (10% at the 24 kHz default). Only the in-passband crossing is
    rendered. sweep_inr_db is the PEAK (while-crossing) level; the
    average in-channel INR is sweep_inr_db + 10*log10(duty). The
    pre-redesign model confined the whole sweep to the passband (100%
    duty = a continuous +INR broadband jammer, the same
    whole-band-phenomenon-in-passband mis-scaling as the retired lambda
    model — v4 smoke: -5 dB effective SINR, deterministic transfer
    loss). Deterministic per Link rng."""

    def __init__(self, fs, rng, sigma, occupancy=0.0, inr_db=10.0,
                 inr_spread_db=6.0, inr_max_db=16.0, band=(300.0, 2700.0),
                 mean_dur_s=10.0, sweep=False, sweep_inr_db=10.0,
                 sweep_rate=10.0, sweep_band_hz=24000.0):
        if not 0.0 <= float(occupancy) < 1.0:
            raise ValueError(f"QRM occupancy {occupancy} outside [0, 1)")
        if sweep and float(sweep_band_hz) < band[1] - band[0]:
            raise ValueError(
                f"QRM sweep_band_hz {sweep_band_hz:g} narrower than the "
                f"passband ({band[1] - band[0]:g} Hz) — the virtual sweep "
                "span must cover the channel")
        self.fs = fs
        self.rng = rng
        self.sigma = float(sigma)
        self.occ = float(occupancy)
        self.mean_dur = float(mean_dur_s)
        self.lam_spawn = (self.occ / (self.mean_dur * (1.0 - self.occ))
                          if self.occ > 0.0 else 0.0)
        self.inr_db = float(inr_db)
        self.inr_spread = float(inr_spread_db)
        self.inr_cap = float(inr_max_db)
        self.band = band
        self.active = None                            # at most one (M/G/1/1)
        self.sweep = bool(sweep)
        self.sweep_amp = self.sigma * (10.0 ** (float(sweep_inr_db) / 20.0)) * math.sqrt(2.0)
        self.sweep_rate = float(sweep_rate)
        self.sweep_band = float(sweep_band_hz)
        self.sweep_phase = 0.0
        self.t = 0

    def _spawn(self):
        """One interferer: frequency/phase/duration/level draws + one PARIS
        word envelope (tiled in fill(); spawn stays O(50*dot) for the
        real-time block loop)."""
        f = float(self.rng.uniform(*self.band))
        ph0 = float(self.rng.uniform(0, 2 * np.pi))
        dur_s = min(max(float(self.rng.exponential(self.mean_dur)), 0.1), 120.0)
        inr = min(float(self.rng.normal(self.inr_db, self.inr_spread)),
                  self.inr_cap)
        dot_s = min(max(dur_s / 331.0, 0.02), 0.12)   # eq. 18, 10-60 WPM clamp
        wenv = _paris_word_env(max(1, int(round(dot_s * self.fs))))
        return {"f": f, "ph0": ph0, "inr_db": inr,
                "amp": self.sigma * (10.0 ** (inr / 20.0)) * math.sqrt(2.0),
                "wenv": wenv, "wlen": len(wenv),
                "pos": 0, "n": int(round(dur_s * self.fs))}

    def fill(self, out):
        """ADD interference into `out` (mono float block)."""
        n = len(out)
        if self.active is None and self.lam_spawn > 0.0:
            if self.rng.random() < self.lam_spawn * n / self.fs:
                self.active = self._spawn()
        it = self.active
        if it is not None:
            m = min(n, it["n"] - it["pos"])
            t = it["pos"] + np.arange(m)
            out[:m] += (it["amp"] * it["wenv"][t % it["wlen"]]
                        * np.sin(it["ph0"] + (2 * np.pi * it["f"] / self.fs) * t))
            it["pos"] += m
            if it["pos"] >= it["n"]:
                self.active = None
        if self.sweep:
            # Virtual sweep lo -> lo+sweep_band; render only the passband
            # crossing (frac < duty). Out-of-band fvec exceeds Nyquist but is
            # masked to zero; phase continuity across bursts is arbitrary,
            # as for a real chirp re-entering the passband.
            lo, hi = self.band
            frac = ((self.t + np.arange(n)) * self.sweep_rate / self.fs) % 1.0
            fvec = lo + self.sweep_band * frac
            ph = self.sweep_phase + np.cumsum(2 * np.pi * fvec / self.fs)
            self.sweep_phase = float(ph[-1] % (2 * np.pi))
            out += self.sweep_amp * np.sin(ph) * (frac < (hi - lo) / self.sweep_band)
        self.t += n
