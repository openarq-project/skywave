#!/usr/bin/env python3
"""Tier-A FM flat-fade physics for skywave's FM port (the FM port design
"Tier A — linear audio fade (IONOS-equivalent)").

The Tier-A channel is LINEAR and audio-domain: the received audio is the
transmitted (port-shaped) audio times a real, unit-mean-power ENVELOPE
process, plus noise added downstream by the harness. This is exactly the
IONOS-SIM architecture (audio-domain flat fade, no discriminator), so Tier-A
results are directly comparable to the published VARA-FM/IONOS numbers.
Threshold/click/capture physics deliberately does NOT exist here — that is
Tier B (`SIM_FM_PORT` docs).

Fade kinds (SIM_FM_FADE):

  ionos:<depth_db>:<rate_hz>[:sin|sq]
      The IONOS-SIM deterministic periodic flat fade (manual/deck: depth
      0-40 dB x log-stepped rate; the published VARA-FM fade cells are
      30 dB at 0.1/1/3 Hz). The instrument's exact fade waveform is an
      accepted residual unknown, so the SHAPE is a parameter:
      `sin` (default) = sinusoidal-in-dB swinging 0..-depth; `sq` =
      switched square-in-dB (half period at 0 dB, half at -depth, 5 ms
      raised-cosine edges so the gain step doesn't splatter) — the harsher
      reading, kept as a stress shape only: measured 2026-07-19, `sq`
      kills a cell the published curves show healthy, and `sin` under
      report-class dwell on a quiet host matched the published slow-fade
      collapse ratio (0.32 vs 0.29), so `sin` is the confirmed reading.
      The instrument source (github.com/ARSFI/HFSimulator, Fade()) later
      confirmed it: raised-cosine in dB, starting unfaded. Deterministic:
      the seed does not perturb it; phase starts at the envelope maximum
      (sq: starts in the unfaded half).
  ionosnc:<depth_db>:<rate_hz>:<sn0_db>
      The same periodic sinusoidal-in-dB S/N trajectory, realized the way
      the IONOS instrument actually does it (HFSim_BFD_2_03.ino Fade() ->
      AdjustS_N()): complementary signal/noise mixer gains
      sig = R/(1+R), noise = 1-sig, with R the target voltage S/N ratio
      10^((sn0-d(t))/20) floored at the instrument's -40 dB S/N stop.
      In a deep trough the NOISE rises to near full scale while the
      absolute signal drops only a few dB — total audio level stays
      roughly constant and the receiver hears loud hiss, not the near-
      silence of the plain `ionos` envelope fade. MEASURED 2026-07-27
      (VARA FM 4.x wide/data9600, depth {10,30} x rate {0.1,1} Hz vs the
      published grid): the two realizations are statistically equivalent
      on a squelchless data port — both match the published deep-fade
      cells (0.04 vs 0.044 at 30 dB/0.1 Hz) and NEITHER reproduces the
      published depth-10@0.1 Hz collapse to 0.26, which contradicts the
      report's own steady-state curve (0.70 at the cell's 20 dB trough
      S/N) and is judged a VARA-3.x-era slow-fade adaptation artifact.
      Keep `ionos` for published-comparable cells; use `ionosnc` where
      absolute trough level plausibly matters (micspk gated-squelch or
      VOX cells — untested, the level difference gates the RX path
      there). sn0_db is the cell's
      nominal unfaded S/N (the published fade cells ran 30 dB). Both
      gain tracks are normalized to 1 at the envelope maximum, so the
      unfaded calibration (TXGAIN/SIGMA) is untouched. The signal track
      rides the fade slot; the noise track is applied by the harness to
      its own noise fill (Link reads `.noise_gain` each block).
      Deterministic and seed-free like `ionos`.
  rayleigh:<fD_hz>
      Mobile/obstructed NLOS regime: |g(t)| of a complex Gaussian process
      with the classical Jakes/CLASS Doppler spectrum
      S(f) ~ 1/sqrt(1-(f/fD)^2), |f| < fD (ETSI TETRA "CLASS", GSM 05.05).
      Generation: filtered complex Gaussian (Jeruchim/Balaban/Shanmugan 2ed
      Sec. 9.1.3.5.3), frequency-sampled FIR at a low rate >= 32x fD
      (MIL-STD-188-110C App. E oversampling rule, as in watterson.py),
      linearly interpolated to the audio rate.
  rice:<fD_hz>[:<K_dB>]
      Fixed/pedestrian LOS regime per the ETSI TETRA RICE profile
      (EN 300 392-2 cl. 6.8): 0.5*CLASS(fD) + 0.5*static line at +0.7*fD,
      i.e. K = 0 dB with the discrete LOS component offset at 0.7*fD.
      K configurable; the 0.7*fD line offset and K=0 dB default are
      triple-sourced (TETRA, GSM 05.05, RapidM RS8 implementation).
  static
      No fast fade (envelope 1) — the `fixed` point-to-point regime;
      composes with shadowing for slow-outage-only cells.

Regime presets (SIM_FM_FADE=<name>, Doppler chosen by SIM_FM_BAND per the
preset table below — clause-literal where the standard pins our band,
v*f/c-derived otherwise):

              @2m (146 MHz)      @70cm (435 MHz)     model
  fixed       static             static              -
  pedestrian  0.7 Hz (derived)   2.0 Hz (derived)    RICE K=0 dB
  mobile-urban   6.4 Hz (TR 102 300-2 @138)  20.0 Hz (clause @430)  Rayleigh
  mobile-highway 27 Hz (derived) 80.6 Hz (clause @430)              Rayleigh

(severe-nlos Nakagami m=0.62 from NTIA TM-11-477 is a known gap,
not yet implemented — sub-Rayleigh generation needs a rank-matching stage;
cells wanting it stay non-campaign until it lands. Aircraft-scatter is a
future event preset.)

Log-normal (Suzuki) shadowing (SIM_FM_SHADOW=<sigma_db>:<tau_s>): a slow
Gaussian process IN dB, sigma per ITU-R P.1546-6 Annex 5 Sec. 12
(8/10/12 dB urban/suburban/rural classes; P.1406-2 Sec. 3.2.1 is the second,
lower-sigma source — quote both, pick per cell), first-order Gauss-Markov
(exponential autocorrelation) with e-folding time tau_s — the time-domain
analog of Gudmundson's exponential spatial correlation, with tau = d_corr/v
left to the cell author. Median 0 dB (multiplier 1): shadowing is NOT
power-normalized — a shadow fade-down is a real SNR loss, that IS the axis.
The fast fade IS normalized (realization E[env^2] = 1) so the AWGN SNR
calibration is preserved on average, same doctrine as watterson.hf_gain.

Module discipline: pure DSP, fs is a constructor parameter, NO env reads
(channel_sim resolves SIM_FM_* and passes numbers), stateful across blocks,
`FmFade.process(block, out=None)` matches WattersonChannel so it drops into
the Link fade slot unchanged. Seeded: same-seed constructions are
byte-identical (V1 gate).
"""
import numpy as np

try:
    from scipy.signal import firwin2 as _firwin2, lfilter as _lfilter
except ImportError:                     # scipy needed only when a fade is on
    _firwin2 = _lfilter = None

# Preset table (see the regime presets above): name -> (model, fD@2m, fD@70cm)
PRESETS = {
    "fixed":          ("static",   0.0,  0.0),
    "pedestrian":     ("rice",     0.7,  2.0),
    "mobile-urban":   ("rayleigh", 6.4, 20.0),
    "mobile-highway": ("rayleigh", 27.0, 80.6),
}
BANDS = ("2m", "70cm")
RICE_LINE_FD_FRAC = 0.7          # TETRA/GSM 05.05/RS8: LOS line at 0.7*fD
_SHADOW_FS = 20.0                # shadow process generation rate (Hz)


def _need_scipy():
    if _firwin2 is None:
        raise RuntimeError("SIM_FM_FADE needs scipy (pip install scipy)")


def _jakes_gain_lowrate(fd_hz, low_fs, n_low, rng, ntaps=257):
    """One complex-Gaussian path at `low_fs` with the Jakes/CLASS Doppler
    spectrum band-limited at fd_hz. Frequency-sampling FIR of sqrt(S(f)); the
    f=fd singularity is capped at the 0.995*fd value (finite grid), giving
    the characteristic band-edge peaking without an unbounded tap. The V1
    Doppler-band gate measures the realized containment."""
    x = np.arange(0.0, low_fs / 2.0 + 1e-9, low_fs / 512.0)
    ratio = np.clip(x / fd_hz, 0.0, None)
    s = np.zeros_like(x)
    inband = ratio < 0.995
    s[inband] = 1.0 / np.sqrt(1.0 - ratio[inband] ** 2)
    cap = 1.0 / np.sqrt(1.0 - 0.995 ** 2)
    s[(ratio >= 0.995) & (ratio < 1.0)] = cap
    y = np.sqrt(s)                       # filter |H| = sqrt(PSD)
    f = x / (low_fs / 2.0)
    f[0], f[-1] = 0.0, 1.0
    y[-1] = 0.0
    b = _firwin2(ntaps, f, y)
    w = rng.standard_normal(n_low + ntaps) + 1j * rng.standard_normal(n_low + ntaps)
    g = _lfilter(b, [1.0], w)[ntaps:]
    return g.astype(np.complex128)


class _InterpTrack:
    """Low-rate real track linearly interpolated at the audio rate with
    modulo wrap (watterson._gain_block pattern). For the deterministic
    periodic tracks (ionos/ionosnc) the wrap IS the waveform; for RANDOM
    tracks (rayleigh/rice envelope, shadowing) a wrap replays the same
    realization and stops accumulating independent fade states, so those
    construct with warn_wrap=True and announce the first cycle."""

    def __init__(self, track, low_fs, fs, warn_wrap=False, label=""):
        self.track = np.ascontiguousarray(track, dtype=np.float64)
        self.low_fs = low_fs
        self.fs = fs
        self.n = len(track)
        self.warn_wrap = warn_wrap
        self.label = label
        self._warned = False

    def block(self, t0, n):
        idx = (t0 + np.arange(n)) * (self.low_fs / self.fs)
        if self.warn_wrap and not self._warned and \
                (t0 + n) * (self.low_fs / self.fs) >= self.n - 1:
            self._warned = True
            import sys as _sys
            print(f"fm_channel: {self.label or 'fade'} realization wrapped at "
                  f"t={t0 / self.fs:.0f}s — the run now replays the same "
                  f"{(self.n - 1) / self.low_fs:.0f}s trace (raise the fade "
                  "duration to cover the run)", file=_sys.stderr, flush=True)
        idx = np.mod(idx, self.n - 1)
        i0 = idx.astype(np.int64)
        frac = idx - i0
        g0 = self.track[i0]
        return g0 + (self.track[i0 + 1] - g0) * frac


class FmFade:
    """Stateful per-direction Tier-A envelope applicator: fast flat fade
    (ionos | rayleigh | rice | static) x optional log-normal shadowing.
    One instance per channel direction; process(block, out=None) like
    WattersonChannel."""

    def __init__(self, fs, kind, dur_s, seed, fd_hz=0.0, k_db=0.0,
                 ionos_depth_db=0.0, ionos_rate_hz=0.0,
                 shadow_sigma_db=0.0, shadow_tau_s=0.0, ionos_shape="sin",
                 ionos_sn0_db=0.0):
        self.fs = fs
        self.kind = kind
        self.fd_hz = fd_hz
        rng = np.random.default_rng(seed)
        self.env = None                  # fast-fade track (None => unity)
        self.noise_env = None            # ionosnc noise track (None => unity)
        self.noise_gain = None           # per-block noise gain the harness reads
        self.g_low = None                # complex low-rate gain (V1 gates)
        self.low_fs = None
        if kind == "ionosnc":
            if ionos_rate_hz <= 0.0 or ionos_depth_db < 0.0:
                raise ValueError("ionosnc fade needs depth_db>=0 and rate_hz>0")
            # One-period tracks like `ionos`, but split into the instrument's
            # complementary signal/noise gains (see the module docstring).
            low_fs = max(50.0, 1024.0 * ionos_rate_hz)
            n = int(round(low_fs / ionos_rate_hz)) + 1
            ph = ionos_rate_hz * (np.arange(n) / low_fs)   # 0..1 over the period
            d_db = ionos_depth_db * 0.5 * (1.0 - np.cos(2 * np.pi * ph))
            sn_db = np.maximum(ionos_sn0_db - d_db, -40.0)  # instrument floor
            r0 = 10.0 ** (ionos_sn0_db / 20.0)
            r = 10.0 ** (sn_db / 20.0)
            a = (r * (1.0 + r0)) / (r0 * (1.0 + r))         # signal, a(0)=1
            b = (1.0 + r0) / (1.0 + r)                      # noise,  b(0)=1
            self.env_db_low = 20.0 * np.log10(a)            # V1 gate inspects
            self.env = _InterpTrack(a, low_fs, fs)
            self.noise_env = _InterpTrack(b, low_fs, fs)
            self.low_fs = low_fs
        elif kind == "ionos":
            if ionos_rate_hz <= 0.0 or ionos_depth_db < 0.0:
                raise ValueError("ionos fade needs depth_db>=0 and rate_hz>0")
            # Deterministic periodic fade in dB, 0..-depth, starting unfaded.
            # Track one full period at >=1024 points; interp wraps exactly.
            low_fs = max(50.0, 1024.0 * ionos_rate_hz)
            n = int(round(low_fs / ionos_rate_hz)) + 1
            t = np.arange(n) / low_fs
            ph = ionos_rate_hz * t                     # 0..1 over the period
            if ionos_shape == "sin":
                env_db = -ionos_depth_db * 0.5 * (1.0 - np.cos(2 * np.pi * ph))
            elif ionos_shape == "sq":
                # half period up, half at -depth; raised-cosine edges over
                # EDGE_S so the gain step doesn't splatter wideband energy
                EDGE_S = 0.005
                e = min(EDGE_S * ionos_rate_hz, 0.05)  # edge width in phase
                frac = np.mod(ph, 1.0)
                env_db = np.full(n, 0.0)
                down = (frac >= 0.5) & (frac < 1.0)
                env_db[down] = -ionos_depth_db
                for edge, sgn in ((0.5, -1.0), (1.0, +1.0)):
                    m = (frac >= edge - e) & (frac < edge)
                    x = (frac[m] - (edge - e)) / e     # 0..1 across the edge
                    lvl = 0.5 * (1.0 - np.cos(np.pi * x))
                    env_db[m] = -ionos_depth_db * (lvl if sgn < 0 else 1.0 - lvl)
                self.env_db_low = env_db               # V1 gate inspects
            else:
                raise ValueError(f"unknown ionos fade shape '{ionos_shape}'"
                                 " (use sin|sq)")
            self.env = _InterpTrack(10.0 ** (env_db / 20.0), low_fs, fs)
            self.low_fs = low_fs
        elif kind in ("rayleigh", "rice"):
            _need_scipy()
            if fd_hz <= 0.0:
                raise ValueError(f"{kind} fade needs fD > 0")
            low_fs = max(50.0, np.ceil(32.0 * fd_hz))
            n_low = int(np.ceil(dur_s * low_fs)) + 4
            g = _jakes_gain_lowrate(fd_hz, low_fs, n_low, rng)
            g /= np.sqrt(np.mean(np.abs(g) ** 2))       # diffuse power -> 1
            if kind == "rice":
                k_lin = 10.0 ** (k_db / 10.0)
                t = np.arange(n_low) / low_fs
                phi0 = rng.uniform(0.0, 2 * np.pi)
                los = np.exp(1j * (2 * np.pi * RICE_LINE_FD_FRAC * fd_hz * t + phi0))
                g = (np.sqrt(k_lin / (k_lin + 1.0)) * los
                     + np.sqrt(1.0 / (k_lin + 1.0)) * g)
            env = np.abs(g)
            env /= np.sqrt(np.mean(env ** 2))           # realized E[env^2]=1
            self.env = _InterpTrack(env, low_fs, fs, warn_wrap=True,
                                    label=f"{kind} fast-fade")
            self.g_low = g
            self.low_fs = low_fs
        elif kind != "static":
            raise ValueError(f"unknown FM fade kind '{kind}'")
        self.shadow = None
        self.shadow_sigma_db = shadow_sigma_db
        self.shadow_tau_s = shadow_tau_s
        if shadow_sigma_db > 0.0:
            if shadow_tau_s <= 0.0:
                raise ValueError("shadowing needs tau_s > 0")
            n_sh = int(np.ceil(dur_s * _SHADOW_FS)) + 4
            srng = np.random.default_rng(seed + 500)    # dedicated stream
            a = float(np.exp(-1.0 / (_SHADOW_FS * shadow_tau_s)))
            innov = srng.standard_normal(n_sh) * shadow_sigma_db * np.sqrt(1.0 - a * a)
            db = np.empty(n_sh)
            db[0] = srng.standard_normal() * shadow_sigma_db
            for i in range(1, n_sh):                    # AR(1), one-time init
                db[i] = a * db[i - 1] + innov[i]
            self.shadow_db_low = db                     # V1 gate inspects
            self.shadow = _InterpTrack(10.0 ** (db / 20.0), _SHADOW_FS, fs,
                                       warn_wrap=True, label="shadowing")
        self.t = 0                                      # absolute sample index

    def process(self, block, out=None):
        n = len(block)
        if out is None:
            out = np.empty(n, dtype=np.float64)
        np.copyto(out, block)
        if self.noise_env is not None:
            # Same time window as the signal track below; the harness applies
            # this to the noise it adds for this block (Link._fill_noise).
            self.noise_gain = self.noise_env.block(self.t, n)
        if self.env is not None:
            out *= self.env.block(self.t, n)
        if self.shadow is not None:
            out *= self.shadow.block(self.t, n)
        self.t += n
        return out


def resolve_fade_spec(fade_str, band):
    """Parse SIM_FM_FADE into (kind, fd_hz, k_db, depth_db, rate_hz, shape,
    sn0_db, desc). Raises ValueError with a usable message on bad input
    (channel_sim turns it into the provenance-doctrine config error)."""
    s = (fade_str or "off").strip().lower()
    if s == "off":
        return None
    if band not in BANDS:
        raise ValueError(f"unknown SIM_FM_BAND '{band}' (use {'|'.join(BANDS)})")
    if s in PRESETS:
        model, fd2m, fd70 = PRESETS[s]
        fd = fd2m if band == "2m" else fd70
        if model == "static":
            return ("static", 0.0, 0.0, 0.0, 0.0, "sin", 0.0,
                    f"{s}@{band}(static)")
        k = 0.0                                          # TETRA RICE K=0 dB
        desc = f"{s}@{band}({model},fD={fd:g}Hz" + (",K=0dB)" if model == "rice" else ")")
        return (model, fd, k, 0.0, 0.0, "sin", 0.0, desc)
    parts = s.split(":")
    if parts[0] == "ionos" and len(parts) in (3, 4):
        depth, rate = float(parts[1]), float(parts[2])
        shape = parts[3] if len(parts) == 4 else "sin"
        if shape not in ("sin", "sq"):
            raise ValueError(f"unknown ionos fade shape '{shape}' (use sin|sq)")
        return ("ionos", 0.0, 0.0, depth, rate, shape, 0.0,
                f"ionos({depth:g}dB@{rate:g}Hz,{shape})")
    if parts[0] == "ionosnc" and len(parts) == 4:
        depth, rate, sn0 = float(parts[1]), float(parts[2]), float(parts[3])
        return ("ionosnc", 0.0, 0.0, depth, rate, "sin", sn0,
                f"ionosnc({depth:g}dB@{rate:g}Hz,SN0={sn0:g}dB)")
    if parts[0] == "rayleigh" and len(parts) == 2:
        fd = float(parts[1])
        return ("rayleigh", fd, 0.0, 0.0, 0.0, "sin", 0.0,
                f"rayleigh(fD={fd:g}Hz)")
    if parts[0] == "rice" and len(parts) in (2, 3):
        fd = float(parts[1])
        k = float(parts[2]) if len(parts) == 3 else 0.0
        return ("rice", fd, k, 0.0, 0.0, "sin", 0.0,
                f"rice(fD={fd:g}Hz,K={k:g}dB)")
    if parts[0] == "static":
        return ("static", 0.0, 0.0, 0.0, 0.0, "sin", 0.0, "static")
    raise ValueError(
        f"unknown SIM_FM_FADE '{fade_str}' (use off | {'|'.join(PRESETS)} | "
        "ionos:<depth_db>:<rate_hz>[:sin|sq] | "
        "ionosnc:<depth_db>:<rate_hz>:<sn0_db> | rayleigh:<fD> | "
        "rice:<fD>[:<K_dB>] | static)")


def resolve_shadow_spec(shadow_str):
    """Parse SIM_FM_SHADOW '<sigma_db>:<tau_s>' -> (sigma_db, tau_s) or None."""
    s = (shadow_str or "off").strip().lower()
    if s == "off":
        return None
    parts = s.split(":")
    if len(parts) != 2:
        raise ValueError("SIM_FM_SHADOW wants '<sigma_db>:<tau_s>' "
                         "(e.g. 8:10 = P.1546 urban sigma, 10 s e-folding)")
    sigma, tau = float(parts[0]), float(parts[1])
    if sigma <= 0.0 or tau <= 0.0:
        raise ValueError("SIM_FM_SHADOW sigma_db and tau_s must be > 0")
    return (sigma, tau)


# IONOS noise-shaping FIR spec (manual Rev-1.8; taps re-derived at OUR fs —
# the instrument's 120-tap count is tied to its own sample rate; the SPEC is
# the passband edge + stopband edge/depth, which the V1 gate verifies):
#   3 kHz: flat 0-3300 Hz, >= 66.8 dB down above 4500 Hz
#   6 kHz: flat 0-6300 Hz, >= 59.9 dB down above 7300 Hz
NOISE_BW_SPECS = {
    3000.0: (3300.0, 4500.0, 66.8),
    6000.0: (6300.0, 7300.0, 59.9),
}


class NoiseShaper:
    """Stateful per-channel FIR that band-limits the harness noise stream to
    an IONOS-equivalent bandwidth. Gain-normalized to unity in the passband
    (SIGMA stays the in-band per-sample sigma)."""

    def __init__(self, fs, bw_hz, nch=1):
        _need_scipy()
        spec = NOISE_BW_SPECS.get(float(bw_hz))
        if spec is None:
            raise ValueError(f"SIM_FM_NOISE_BW={bw_hz:g} not an IONOS bandwidth "
                             f"(use {'|'.join(str(int(k)) for k in NOISE_BW_SPECS)})")
        pass_hz, stop_hz, atten_db = spec
        # Kaiser-windowed FIR sized for the spec'd stopband depth + margin.
        from scipy.signal import kaiserord, firwin
        numtaps, beta = kaiserord(atten_db + 6.0, (stop_hz - pass_hz) / (fs / 2.0))
        numtaps |= 1
        self.b = firwin(numtaps, (pass_hz + stop_hz) / 2.0, window=("kaiser", beta), fs=fs)
        self.zi = [np.zeros(numtaps - 1) for _ in range(nch)]
        self.nch = nch

    def process(self, interleaved):
        for c in range(self.nch):
            y, self.zi[c] = _lfilter(self.b, [1.0], interleaved[c::self.nch],
                                     zi=self.zi[c])
            interleaved[c::self.nch] = y
        return interleaved
