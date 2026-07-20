#!/usr/bin/env python3
"""hfchan -- one-way HF channel filter with a codec2 `ch`-COMPATIBLE CLI, built on
skywave's channel physics.

WHY THIS EXISTS. skywave's
channel physics (`watterson.WattersonChannel`, `rig_effects.*`) is the most complete
open HF station-chain model surveyed, but it
was only reachable through the two-station half-duplex simulator harness. codec2's `ch`
is the community-standard *portable* interface: a raw-int16 stdin->stdout pipe stage
any modem, in any language, can drop into a BER pipeline. This tool gives skywave's
physics that same portable interface -- and adds the four station-chain impairments
NO other simulator ships (Rapp soft-PA, receiver AGC, P.372 impulsive noise, CW QRM)
as opt-in flags on top of a drop-in `ch` command line.

DROP-IN COMPATIBILITY with codec2 `ch` (verified against the ch binary):
  * I/O contract:  hfchan IN OUT [opts]   ('-' = stdin/stdout; default both '-')
                   raw int16 mono, `--Fs` default 8000.
  * flags:         --No --Fs --freq --gain --clip --mpg --mpp --mpd
                   --multipath_delay --ssbfilt      (same names + semantics)
  * `--No` bridge (EXACT, cross-checked vs ch at No in {-40,-30,-20,-10}):
                   No = 10^(NodB/10)*1e6 ;  real-domain noise variance = Fs*No/2 ;
                   SNR3k = 10*log10(2*Psig/(No*3000)).
  * stderr stats:  SNR3k / C/No / peak / RMS / CPAPR / clipped% / OutClipped% and the
                   ">0.1%%  WARNING output clipping" line, in ch's format, so scripts
                   that scrape ch's stderr keep working.

SKYWAVE EXTRAS (opt-in; off by default so the baseline is a plain AWGN/fade pipe):
  --pa-rapp P[,VSAT]     Rapp soft-PA (AM/AM), same formula as channel_sim tx_shape
  --agc[=data|voice]     receiver AGC burst-head gain error (rig_effects.RxAgc)
  --impulsive-vd DB[,K]  P.372 Vd-calibrated non-Gaussian noise (rig_effects.ImpulsiveNoise)
  --qrm-occ OCC[,INR]    in-channel CW-Morse QRM (rig_effects.QrmGenerator)
  --noise-env ENV        P.372 man-made noise env (quiet/rural/residential/city) scaling
  --fade NAME            any named preset from watterson.PRESETS (poor, nvis, flutter, ...)
  --doppler HZ --delay MS  explicit two-tap fade geometry
  --seed N               deterministic realization (ch is unseeded; determinism is ours)

The fade is watterson.WattersonChannel -- the SAME engine skywave uses, externally
cross-calibrated to ch within 0.11 dB at the canonical `poor` cell -- so `--mpp` here
and `ch --mpp` are the same channel to within that gate. SNR3k is reported PRE-fade
and PRE-SSB (ch's convention), so the number is defined by --No and the input level
alone. The SSB stage is a Butterworth bandpass (matches channel_sim's RigBPF), not
ch's exact FIR; use --ssbfilt 0 for a filter-free A/B against ch.

  echo 'quick pipe:'   modem_tx | hfchan --No -20 --mpp | modem_rx
"""
import argparse
import sys

import numpy as np

import watterson
from rig_effects import ImpulsiveNoise, QrmGenerator, RxAgc, _hilbert_fir

try:
    from scipy.signal import butter, sosfilt
except ImportError:                                    # pragma: no cover
    butter = sosfilt = None

# codec2 ch preset -> (delay_ms, doppler_hz), from ch.c MPG/MPP/MPD_DELAY_MS + help text.
CH_PRESETS = {"mpg": (0.5, 0.1), "mpp": (2.0, 1.0), "mpd": (4.0, 2.0)}

# ITU-R P.372 Part-6 median man-made noise: Fam(dB) = c - d*log10(f_MHz). Same table
# and quiet-rural@7MHz anchor as channel_sim (--No sets the quiet anchor; a busier
# environment scales the floor UP from there, so the reported SNR3k drops accordingly).
# Categories are relative guides (underlying dataset is dated); default off = untouched.
P372_ENV = {"city": (76.8, 27.7), "residential": (72.5, 27.7),
            "rural": (67.2, 27.7), "quiet": (53.6, 28.6)}


def p372_fam_delta(env, band_mhz):
    """Man-made-noise delta (dB) of `env` at `band_mhz` relative to quiet-rural @ 7 MHz."""
    if env not in P372_ENV:
        sys.exit(f"hfchan: unknown --noise-env '{env}' (choices: {', '.join(P372_ENV)})")
    c, d = P372_ENV[env]
    cq, dq = P372_ENV["quiet"]
    return (c - d * np.log10(band_mhz)) - (cq - dq * np.log10(7.0))


class AnalyticClip:
    """Streaming analytic-magnitude clipper (codec2 `ch` --clip): form the analytic
    signal (FIR Hilbert, state across blocks), clip |z| to `clip`, return the real
    part. Off (clip >= 32767) leaves the real path untouched -- the baseline stays a
    plain pipe. Counts samples whose magnitude exceeded the ceiling."""

    def __init__(self, fs, clip, hilbert_taps=255):
        self.clip = float(clip)
        self.h, self.gdelay = _hilbert_fir(hilbert_taps)
        self.hist_len = len(self.h)
        self.hist = np.zeros(self.hist_len)
        self.nclipped = 0

    def process(self, block):
        n = len(block)
        buf = np.concatenate((self.hist, block))
        imag_full = np.convolve(buf, self.h, mode="valid")
        base = self.hist_len - self.gdelay
        re = buf[base:base + n]
        im = imag_full[base - self.gdelay: base - self.gdelay + n]
        mag = np.hypot(re, im)
        over = mag > self.clip
        self.nclipped += int(np.count_nonzero(over))
        scale = np.where(over, self.clip / np.maximum(mag, 1e-9), 1.0)
        out = re * scale
        self.hist[:] = buf[-self.hist_len:]
        return out


class FreqShift:
    """Constant carrier offset (codec2 `ch` --freq): y = Re{z * e^{j2*pi*f*t}}, analytic
    z via streaming FIR Hilbert. Same construction as rig_effects.FreqShift (imported
    separately there for the two-station differential; here it is one-way)."""

    def __init__(self, fs, foff_hz, hilbert_taps=255):
        self.fs = fs
        self.foff = float(foff_hz)
        self.h, self.gdelay = _hilbert_fir(hilbert_taps)
        self.hist_len = len(self.h)
        self.hist = np.zeros(self.hist_len)
        self.t = 0

    def process(self, block):
        n = len(block)
        buf = np.concatenate((self.hist, block))
        imag_full = np.convolve(buf, self.h, mode="valid")
        base = self.hist_len - self.gdelay
        z = buf[base:base + n] + 1j * imag_full[base - self.gdelay: base - self.gdelay + n]
        ph = (2.0 * np.pi * self.foff / self.fs) * (self.t + np.arange(n))
        out = np.real(z * np.exp(1j * ph))
        self.hist[:] = buf[-self.hist_len:]
        self.t += n
        return out


class SsbFilter:
    """Butterworth SSB audio bandpass, second-order sections, state carried across
    blocks -- the same design as channel_sim.RigBPF (order 6, 300-2700 Hz default).
    NOTE: comparable to, not bit-identical with, ch's FIR ssbfilt_coeff; SNR3k is
    reported pre-filter so this choice does not move the reported number."""

    def __init__(self, fs, lo=300.0, hi=2700.0, order=6):
        if butter is None:
            raise RuntimeError("hfchan --ssbfilt needs scipy (pip install scipy)")
        self.sos = butter(order, [lo, hi], btype="band", fs=fs, output="sos")
        self.zi = np.zeros((self.sos.shape[0], 2))

    def process(self, mono):
        y, self.zi = sosfilt(self.sos, mono, zi=self.zi)
        return y


def _pair(s, cast=float):
    """Parse 'A' or 'A,B' -> (A, B|None)."""
    parts = str(s).split(",")
    a = cast(parts[0])
    b = cast(parts[1]) if len(parts) > 1 and parts[1] != "" else None
    return a, b


def build_parser():
    p = argparse.ArgumentParser(
        prog="hfchan", add_help=True,
        description="One-way HF channel filter (codec2 `ch`-compatible CLI over "
                    "skywave channel physics).")
    p.add_argument("infile", nargs="?", default="-", help="raw int16 in ('-'=stdin)")
    p.add_argument("outfile", nargs="?", default="-", help="raw int16 out ('-'=stdout)")
    # --- codec2 ch-compatible ---
    p.add_argument("--No", type=float, default=-100.0, help="AWGN density dB/Hz (ch default -100)")
    p.add_argument("--Fs", type=int, default=8000, help="sample rate Hz (default 8000)")
    p.add_argument("--freq", "--foff", type=float, default=0.0, dest="freq",
                   help="carrier frequency offset Hz")
    p.add_argument("--gain", type=float, default=1.0, help="linear input gain")
    p.add_argument("--clip", type=float, default=32767.0, help="analytic-magnitude clip")
    p.add_argument("--mpg", action="store_const", const="mpg", dest="ch_preset")
    p.add_argument("--mpp", action="store_const", const="mpp", dest="ch_preset")
    p.add_argument("--mpd", action="store_const", const="mpd", dest="ch_preset")
    p.add_argument("--multipath_delay", type=float, default=None, help="override fade delay ms")
    p.add_argument("--ssbfilt", type=int, default=1, help="SSB bandpass 0|1 (default 1)")
    # --- skywave extras ---
    p.add_argument("--fade", default=None, help="named preset from watterson.PRESETS")
    p.add_argument("--doppler", type=float, default=None, help="explicit fade Doppler Hz")
    p.add_argument("--delay", type=float, default=None, help="explicit fade delay ms")
    p.add_argument("--seed", type=int, default=1234, help="RNG seed (deterministic)")
    p.add_argument("--dur", type=float, default=1200.0, help="fade pre-gen seconds (wraps after)")
    p.add_argument("--pa-rapp", default=None, metavar="P[,VSAT]", help="Rapp soft-PA")
    p.add_argument("--agc", nargs="?", const="data", default=None, metavar="data|voice",
                   help="receiver AGC (burst-head gain error)")
    p.add_argument("--impulsive-vd", default=None, metavar="DB[,K]",
                   help="P.372 impulsive noise (replaces Gaussian)")
    p.add_argument("--qrm-occ", default=None, metavar="OCC[,INR]", help="CW-Morse QRM")
    p.add_argument("--noise-env", default=None, metavar="quiet|rural|residential|city",
                   help="P.372 man-made noise environment (scales the floor from the "
                        "quiet-rural anchor --No sets)")
    p.add_argument("--band-mhz", type=float, default=7.0, help="band MHz for --noise-env")
    p.add_argument("--block", type=int, default=1024, help="processing block samples")
    p.add_argument("--quiet", action="store_true", help="suppress stderr stats")
    # Declarative channel profile (shared with the simulator). CLI flags override it.
    p.add_argument("--profile", default=None, metavar="FILE.toml",
                   help="load a channel_profile (TOML/JSON); flags override it")
    p.add_argument("--sigma", type=float, default=None, metavar="STD",
                   help="per-sample int16 noise std (profile-native; overrides --No)")
    return p


def resolve_fade(a):
    """Return (delay_ms, doppler_hz) or None. Priority: explicit --doppler/--delay >
    --fade NAME > ch --mpg/--mpp/--mpd. --multipath_delay overrides the delay (ch)."""
    fade = None
    if a.doppler is not None or a.delay is not None:
        fade = (a.delay if a.delay is not None else 1.0,
                a.doppler if a.doppler is not None else 1.0)
    elif a.fade:
        preset = watterson.PRESETS.get(a.fade)
        if preset is None:
            sys.exit(f"hfchan: unknown --fade '{a.fade}' "
                     f"(choices: {', '.join(k for k in watterson.PRESETS if k != 'off')})")
        fade = preset
    elif a.ch_preset:
        fade = CH_PRESETS[a.ch_preset]
    if fade is None:
        return None
    delay_ms, doppler_hz = fade
    if a.multipath_delay is not None:
        delay_ms = a.multipath_delay
    return (delay_ms, doppler_hz)


def main(argv=None):
    p = build_parser()
    # a --profile supplies argparse defaults; explicitly-passed flags still override
    # (set_defaults changes the default, a passed flag wins). Harness-only profile fields
    # (link/tr/alc/reverse) have no hfchan knob and are ignored.
    prelim, _ = p.parse_known_args(argv)
    if prelim.profile:
        import channel_profile
        p.set_defaults(**channel_profile.to_hfchan_defaults(
            channel_profile.load_profile(prelim.profile)))
    a = p.parse_args(argv)
    Fs = a.Fs
    rng = np.random.default_rng(a.seed)

    # Noise floor: --sigma (profile-native per-sample int16 std) wins; else the --No
    # bridge (see module docstring; verified vs the ch binary). SNR3k is reported from
    # measured power either way; under --sigma we back-fill a.No so the banner is honest.
    if a.sigma is not None:
        noise_std = float(a.sigma)
        a.No = (10.0 * np.log10(2.0 * noise_std * noise_std / (Fs * 1e6))
                if noise_std > 0 else -100.0)
    else:
        No = 10.0 ** (a.No / 10.0) * 1e6
        noise_std = np.sqrt(Fs * No / 2.0)
    # P.372 man-made noise: scale the floor before it feeds Gaussian/impulsive/QRM,
    # exactly as channel_sim scales SIGMA (so all noise sources track the environment).
    if a.noise_env is not None:
        noise_std *= 10.0 ** (p372_fam_delta(a.noise_env, a.band_mhz) / 20.0)

    # ---- build the stage chain (each stage: real block -> real block) ----
    clip_stage = AnalyticClip(Fs, a.clip) if a.clip < 32767.0 else None
    if a.pa_rapp is not None:
        pa_p, pa_vsat = _pair(a.pa_rapp)
        pa_vsat = pa_vsat if pa_vsat is not None else 32767.0
    else:
        pa_p = None
    foff_stage = FreqShift(Fs, a.freq) if a.freq else None
    fade = resolve_fade(a)
    fade_stage = (watterson.WattersonChannel(Fs, fade[0], fade[1], a.dur, a.seed)
                  if fade is not None else None)
    impulsive = None
    if a.impulsive_vd is not None:
        vd, kdb = _pair(a.impulsive_vd)
        impulsive = ImpulsiveNoise(noise_std, vd, kdb if kdb is not None else 26.0)
    qrm = None
    if a.qrm_occ is not None:
        occ, inr = _pair(a.qrm_occ)
        qrm = QrmGenerator(Fs, rng, noise_std, occupancy=occ,
                           inr_db=inr if inr is not None else 10.0)
    ssb = SsbFilter(Fs) if a.ssbfilt else None
    env_meter = _Envelope()
    agc = None
    if a.agc is not None:
        rel = 100.0 if a.agc == "data" else 300.0
        agc = RxAgc(Fs, attack_ms=2.0, release_ms=rel)

    fin = sys.stdin.buffer if a.infile == "-" else open(a.infile, "rb")
    fout = sys.stdout.buffer if a.outfile == "-" else open(a.outfile, "wb")

    # ---- ch-compatible stats accumulators ----
    nsamples = 0
    tx_pwr = 0.0            # analytic power of the shaped TX (ch convention: 2x real)
    noise_pwr = 0.0         # complex-equivalent injected-noise power (2x real)
    peak = 0.0
    nclipped = 0
    noutclipped = 0

    bytes_per = 2
    while True:
        raw = fin.read(a.block * bytes_per)
        if not raw:
            break
        block = np.frombuffer(raw, dtype="<i2").astype(np.float64)
        n = len(block)
        if n == 0:
            break

        # --- TX shaping: gain -> (Rapp PA | analytic clip) ---
        w = block * a.gain
        if pa_p is not None:                           # Rapp soft-PA (channel_sim formula)
            ax = np.abs(w) / pa_vsat
            nclipped += int(np.count_nonzero(ax > 1.0))
            w = w / np.power(1.0 + np.power(ax, 2.0 * pa_p), 1.0 / (2.0 * pa_p))
        elif clip_stage is not None:
            w = clip_stage.process(w)

        # --- TX-power / peak stats (analytic magnitude, post-shaping, pre-fade) ---
        za = env_meter.mag(w)
        tx_pwr += float(np.dot(za, za))
        if nsamples > 255:                             # skip HT priming, like ch
            peak = max(peak, float(za.max()))

        # --- channel: freq shift -> fade ---
        if foff_stage is not None:
            w = foff_stage.process(w)
        if fade_stage is not None:
            w = fade_stage.process(w)

        # --- additive noise (Gaussian or P.372 impulsive) + optional QRM ---
        noise = np.empty(n)
        if impulsive is not None:
            impulsive.fill(rng, noise)
        else:
            rng.standard_normal(n, out=noise)
            noise *= noise_std
        w = w + noise
        noise_pwr += 2.0 * float(np.dot(noise, noise))
        if qrm is not None:
            qrm.fill(w)                                # in-place add of interference

        # --- RX chain: SSB filter -> AGC ---
        if ssb is not None:
            w = ssb.process(w)
        if agc is not None:
            w = agc.process(w)

        # --- output int16 with rail clipping (ch counts OutClipped) ---
        noutclipped += int(np.count_nonzero((w > 32767.0) | (w < -32767.0)))
        np.clip(w, -32767.0, 32767.0, out=w)
        fout.write(w.astype("<i2").tobytes())
        if fout is sys.stdout.buffer:
            fout.flush()
        nsamples += n

    if fin is not sys.stdin.buffer:
        fin.close()
    if fout is not sys.stdout.buffer:
        fout.close()

    if not a.quiet and nsamples:
        _print_stats(sys.stderr, a, Fs, nsamples, tx_pwr, noise_pwr, peak,
                     nclipped, noutclipped)
    return 0


class _Envelope:
    """Streaming analytic-magnitude meter for stats only (does not touch the signal
    path). Carries FIR-Hilbert history across blocks -- same construction as the
    signal stages -- so there is no per-block transient and `peak`/CPAPR match ch's
    own HT-ripple envelope rather than a windowing artifact."""

    def __init__(self, hilbert_taps=255):
        self.h, self.gdelay = _hilbert_fir(hilbert_taps)
        self.hist_len = len(self.h)
        self.hist = np.zeros(self.hist_len)

    def mag(self, block):
        n = len(block)
        buf = np.concatenate((self.hist, block))
        imag_full = np.convolve(buf, self.h, mode="valid")
        base = self.hist_len - self.gdelay
        re = buf[base:base + n]
        im = imag_full[base - self.gdelay: base - self.gdelay + n]
        self.hist[:] = buf[-self.hist_len:]
        return np.hypot(re, im)


def _print_stats(f, a, Fs, nsamples, tx_pwr, noise_pwr, peak, nclipped, noutclipped):
    rms = np.sqrt(tx_pwr / nsamples)
    papr = 10.0 * np.log10(peak * peak / (tx_pwr / nsamples)) if peak > 0 else 0.0
    cno = 10.0 * np.log10(tx_pwr / (noise_pwr / Fs)) if noise_pwr > 0 else float("inf")
    snr3k = cno - 10.0 * np.log10(3000.0)
    outclip_pct = noutclipped * 100.0 / nsamples
    print(f"hfchan: Fs: {Fs} NodB: {a.No:.2f} foff: {a.freq:.2f} Hz "
          f"clip: {a.clip:.2f} ssbfilt: {a.ssbfilt}", file=f)
    print(f"hfchan: SNR3k(dB): {snr3k:8.2f}  C/No....: {cno:8.2f}", file=f)
    print(f"hfchan: peak.....: {peak:8.2f}  RMS.....: {rms:8.2f}   CPAPR.....: {papr:5.2f}",
          file=f)
    print(f"hfchan: Nsamples.: {nsamples:8d}  clipped.: {nclipped * 100.0 / nsamples:8.2f}%  "
          f"OutClipped: {outclip_pct:5.2f}%", file=f)
    if outclip_pct > 0.1:
        print("hfchan: WARNING output clipping", file=f)


if __name__ == "__main__":
    sys.exit(main())
