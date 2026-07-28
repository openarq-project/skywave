#!/usr/bin/env python3
"""Bench-qualification pass for the fading instrument: every named Watterson
preset, run at FULL ITU-R F.1487 Annex 3 s6 dwell (>= 3000 independent fade
states, i.e. max(3000/doppler, 600) seconds of channel time), measured at the
real audio rate through the fade applicator.

This is the run-once-per-RIG_GEN test (archive the output next to the RIG_GEN
note), not a campaign cell: channel time here costs only DSP time, so the
8.3-hour statistical dwell the standard demands for the 0.1 Hz preset takes
minutes of wall clock. Campaign cells stay short and carry their coverage in
the corpus `fade_units` column instead.

Per preset it reports, against theory for a 2-path Gaussian-scatter channel:
  spread   realized 2-sigma Doppler spread (complex-envelope autocorrelation
           fit) vs nominal — gate |err| <= 5%
  med/mean Rayleigh envelope median/mean vs 0.9394 — gate +-0.02
  P(-10dB) deep-fade probability P(env < 0.316 rms) vs 0.0952 — gate +-0.015
  power    mean output power vs input power (hf_gain doctrine) — gate +-0.2 dB

Usage: python -m skywave.qualify_fade [--filter milstd|codec2]
                                      [--presets poor,good,...] [--fs 48000]
Exit 0 = all gates green; 1 = any gate failed.
"""
import argparse
import sys

import numpy as np

from skywave import watterson

TONE_HZ = 1500.0
BLOCK = 1024


def qualify_preset(name, delay_ms, dop, fs, filter_mode):
    dwell_s = max(3000.0 / dop, 600.0)
    ch = watterson.WattersonChannel(float(fs), delay_ms, dop,
                                    dur_s=dwell_s + 30.0, seed=1487,
                                    filter_mode=filter_mode)
    n = np.arange(BLOCK)
    blk = 8000.0 * np.sin(2 * np.pi * TONE_HZ * n / fs)
    nblocks = int(dwell_s * fs / BLOCK)
    skip = int(1.0 * fs / BLOCK)
    # Complex envelope of the faded tone, decimated for the autocorr fit.
    # The demod leaves an image at -2*TONE_HZ; the boxcar decimator must NULL
    # it exactly (length = multiple of fs/(2*TONE_HZ)), else the aliased image
    # inflates the tiny small-lag decorrelation at high spreads (measured:
    # flutter read 13.6 Hz for a true 10.0 before this pin).
    img_null = int(round(fs / (2.0 * TONE_HZ)))          # 16 samples at 48k
    dec = max(1, int(round(fs / (64.0 * max(dop, 0.5)) / img_null))) * img_null
    env_chunks = []
    sq = ns = 0.0
    t0 = 0
    dem = np.exp(-2j * np.pi * TONE_HZ * np.arange(BLOCK) / fs)
    carry = np.empty(0, dtype=complex)
    for k in range(nblocks):
        y = ch.process(blk)
        if k < skip:
            t0 += BLOCK
            continue
        sq += float(np.sum(y * y))
        ns += BLOCK
        z = (y * (dem * np.exp(-2j * np.pi * TONE_HZ * t0 / fs)))
        carry = np.concatenate((carry, z))
        m = len(carry) // dec * dec
        if m:
            env_chunks.append(carry[:m].reshape(-1, dec).mean(axis=1))
            carry = carry[m:]
        t0 += BLOCK
    z = np.concatenate(env_chunks)
    low_fs = fs / dec
    lags = np.arange(1, 6)
    tau = lags / low_fs
    R = np.array([np.abs(np.vdot(z[:-k], z[k:]) / np.vdot(z, z)) for k in lags])
    v = -np.log(np.clip(R, 1e-12, 1.0)) / (2 * np.pi ** 2 * tau ** 2)
    spread = 2.0 * np.sqrt(np.maximum(v, 0.0)).mean()
    env = np.abs(z)
    rms = np.sqrt(np.mean(env ** 2))
    med_mean = float(np.median(env) / np.mean(env))
    p10 = float(np.mean(env < 0.316 * rms))
    pwr_db = 10.0 * np.log10((sq / ns) / (8000.0 ** 2 / 2.0))
    gates = {
        "spread": abs(spread / dop - 1.0) <= 0.05,
        "med/mean": abs(med_mean - 0.9394) <= 0.02,
        "P(-10dB)": abs(p10 - 0.0952) <= 0.015,
        "power": abs(pwr_db) <= 0.2,
    }
    return dwell_s, spread, med_mean, p10, pwr_db, gates


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--filter", default="milstd",
                    choices=list(watterson.FILTER_MODES))
    ap.add_argument("--presets", default="")
    ap.add_argument("--fs", type=float, default=48000.0)
    args = ap.parse_args(argv)
    names = ([p.strip() for p in args.presets.split(",") if p.strip()]
             or [k for k, v in watterson.PRESETS.items() if v is not None])
    print(f"qualify_fade: filter={args.filter} fs={args.fs:g} "
          f"(F.1487 A3 s6 dwell = max(3000/D, 600) s per preset)", flush=True)
    print(f"{'preset':18s} {'delay':>6s} {'dop':>6s} {'dwell':>8s} "
          f"{'spread':>7s} {'med/mn':>7s} {'P-10dB':>7s} {'power':>7s}  gates")
    ok_all = True
    for name in names:
        delay_ms, dop = watterson.PRESETS[name]
        dwell, spread, mm, p10, pwr, gates = qualify_preset(
            name, delay_ms, dop, args.fs, args.filter)
        ok = all(gates.values())
        ok_all &= ok
        bad = ",".join(k for k, v in gates.items() if not v)
        print(f"{name:18s} {delay_ms:6.1f} {dop:6.1f} {dwell:7.0f}s "
              f"{spread:7.3f} {mm:7.4f} {p10:7.4f} {pwr:+6.2f}dB  "
              f"{'PASS' if ok else 'FAIL:' + bad}", flush=True)
    print("qualify_fade:", "ALL GATES PASS" if ok_all else "GATE FAILURE(S)",
          flush=True)
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
