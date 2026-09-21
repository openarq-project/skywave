#!/usr/bin/env python3
"""qrm_d3_waveform_check.py — the D3 cross-check the pilot handback asked for (HANDBACK.md §2, 2026-09-21).

    qrm_d3_waveform_check.py [--seconds 3000] [--occ 0.01,0.02,...] [--seed 1] [--out D3-WAVEFORM-CHECK.md]

qrm_pilot_rulings.py's generator side (D3) is a Monte-Carlo on a 1 s FRAME model: each QrmGenerator interferer is scored
at its duty-averaged passband INR, 10log10(1 + INR_lin * 22/50), for its whole duration, so a 10 s interferer at +10 dB
reads as one 10-frame busy run. That is not how the pilot instrument sees it. A KiwiSDR waterfall frame is ONE
1024-point FFT of a 1/rbw = 35 ms slice once per second (sites A and B: median - p25 = 4.0 dB, time-sd 5.8-6.0 dB =
single exponential bins), so a PARIS-keyed CW interferer (dot 20-120 ms) is caught keyed-ON in only ~44 % of its
frames, and the frame model may OVERSTATE the generator's busy-run length.

This script renders the real QrmGenerator WAVEFORM (Poisson onsets, Exp(10 s) durations, PARIS keying, N(10, 6) dB
level capped at 16 dB over the 2.4 kHz channel noise) into white noise at the waterfall's sample rate, takes the
instrument's snapshot (Hann-windowed 1024-point FFT of the first 35 ms of every second), and pushes the resulting
"waterfall" through qrm_occupancy.py's OWN functions unchanged (file_floor, channel_levels, runs_of; per-file = 300 s
dwell exactly like the pilot, busy >= 10 dB, detect >= 3 dB), on a nominal-occupancy grid. The frame model
(qrm_pilot_rulings.gen_sim) runs on the same grid alongside.

DECISION RULE (written before the run). Pilot measured busy-run p50 = 1 s (109 gateway channel-rows, both widths; the
1 s frame is the floor). Pre-reg §0 D3: median ratio > 3x either way => REPLACE. The frame model gave generator p50 =
8-9 s (ratio 0.12). If, at the nominal occupancy where the WAVEFORM path READS the pilot's median busy fraction
(0.019; also checked at 0.05 and 0.15), the generator's busy-run p50 is:
  * > 3 s  -> ratio < 1/3, D3 REPLACE stands on the busy-run statistic;
  * <= 3 s -> the busy-run statistic is WITHIN 3x: the pilot instrument cannot distinguish a keyed 10 s CW interferer
              from 1 s bursts by run length, so D3 must be ruled on a statistic that survives the 35 ms snapshot
              (bandwidth class of the busy events, idle-run structure) rather than on the run-length ratio.
The idle-run ratio (measured p50 2-200 s vs frame-model 20-1560 s) is reported on the same footing.
"""
import argparse, os, sys
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
import qrm_occupancy as occ                      # the pilot scorer, unchanged
import qrm_pilot_rulings as rul                  # the frame-model Monte-Carlo, unchanged
from skywave.rig_effects import QrmGenerator     # the generator under test, unchanged

SPAN_HZ = 29296.875          # zoom-10 KiwiSDR span: 1024 bins of 28.61 Hz
NFFT = 1024
RBW = SPAN_HZ / NFFT
DWELL_S = 300                # the pilot's 5-minute waterfall dwell (runs censored there, as in the pilot)
BAND = (300.0, 2700.0)       # the generator's channel: a 2400 Hz passband, tone uniform inside
CHAN_KHZ = 1.5               # the 2400 Hz channel window centred on the generator's band


def render(occ_nom, seconds, seed, inr=10.0, spread=6.0, cap=16.0, mean_dur=10.0):
    """Waveform -> instrument snapshots. Returns (dbm[frames, 513], f_khz, truth_on[frames]) where truth_on marks
    frames whose 35 ms slice contained an active interferer (keyed on or off)."""
    rng = np.random.default_rng(seed)
    fs = SPAN_HZ
    sigma = 1.0                                              # channel (2400 Hz) noise RMS
    v = sigma ** 2 * fs / (2.0 * (BAND[1] - BAND[0]))        # white-noise variance giving sigma^2 in the passband
    g = QrmGenerator(fs, rng, sigma, occupancy=occ_nom, inr_db=inr, inr_spread_db=spread, inr_max_db=cap,
                     band=BAND, mean_dur_s=mean_dur)
    win = np.hanning(NFFT)
    per_s = int(round(fs))                                   # samples per second (29297)
    blk = 1024                                               # generator block (spawn Bernoulli per block, lam*blk/fs << 1)
    n_frames = int(seconds)
    dbm = np.empty((n_frames, NFFT // 2 + 1), np.float32)
    truth = np.zeros(n_frames, bool)
    buf = np.zeros(per_s)
    for i in range(n_frames):
        buf[:] = 0.0
        for s in range(0, per_s, blk):
            e = min(s + blk, per_s)
            blkview = buf[s:e]
            was = g.active is not None
            g.fill(blkview)
            if s == 0:
                truth[i] = was or g.active is not None
        x = buf[:NFFT] + rng.normal(0.0, np.sqrt(v), NFFT)   # the 35 ms snapshot, noise added at the instrument
        X = np.fft.rfft(x * win)
        p = (np.abs(X) ** 2) / np.sum(win ** 2)              # per-bin power, window-energy normalised
        dbm[i] = 10 * np.log10(np.maximum(p, 1e-30))
    f_khz = (np.arange(NFFT // 2 + 1) + 0.5) * RBW / 1000.0
    return dbm, f_khz, truth


def score_frames(dbm, f_khz):
    """The pilot scorer on 300 s files: busy/detect per frame for the 2400 Hz window at CHAN_KHZ and every 500 Hz window
    inside the band; runs censored per file (as the pilot's events were)."""
    out = {}
    for width in (2400, 500):
        busy_all, det_all, bl, il = [], [], [], []
        for f0 in range(0, dbm.shape[0], DWELL_S):
            d = dbm[f0:f0 + DWELL_S]
            if len(d) < 60:
                continue
            ffloor = occ.file_floor(d)
            centres, lev, _ = occ.channel_levels(d, f_khz, RBW, width, floor_db=ffloor)
            if width == 2400:
                cols = [int(np.argmin(np.abs(centres - CHAN_KHZ)))]
            else:
                cols = [j for j, c in enumerate(centres) if BAND[0] / 1000 + 0.25 <= c <= BAND[1] / 1000 - 0.25]
            for j in cols:
                b = lev[:, j] >= 10.0; dt = lev[:, j] >= 3.0
                busy_all.append(b); det_all.append(dt)
                for s, n, val, cl, cr in occ.runs_of(b):
                    (bl if val else il).append(n)
        b = np.concatenate(busy_all); dt = np.concatenate(det_all)
        out[width] = dict(busy=float(b.mean()), det=float(dt.mean()),
                          tail=float(((dt & ~b).sum() / dt.sum()) if dt.sum() else np.nan),
                          bp50=float(np.median(bl)) if bl else np.nan, bp90=float(np.percentile(bl, 90)) if bl else np.nan,
                          ip50=float(np.median(il)) if il else np.nan, nruns=len(bl))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=int, default=3000)
    ap.add_argument("--occ", default="0.01,0.02,0.05,0.1,0.2,0.3,0.5")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    grid = [float(x) for x in a.occ.split(",")]
    L = ["# D3 cross-check — QrmGenerator WAVEFORM through the pilot instrument vs the 1 s frame model\n",
         f"seconds per cell {a.seconds}, seed {a.seed}, snapshot = Hann 1024-pt FFT of the first {1000/RBW:.0f} ms of each second, "
         f"rbw {RBW:.2f} Hz, files of {DWELL_S} s, busy >= 10 dB / detect >= 3 dB passband INR over qrm_occupancy.file_floor.\n",
         "| nominal occ | truth frac (interferer present in the slice) | frame-model read busy | frame-model busy p50 s | frame-model idle p50 s | "
         "WAVEFORM 2400 Hz read busy | busy p50 / p90 s | idle p50 s | D5 tail | WAVEFORM 500 Hz read busy | busy p50 s | idle p50 s | D5 tail |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    rows = []
    for o in grid:
        fm = rul.gen_sim(o, n_s=max(a.seconds * 20, 200_000), seed=a.seed)
        fb, fi = rul.runs(fm)
        dbm, f_khz, truth = render(o, a.seconds, a.seed)
        r = score_frames(dbm, f_khz)
        w, n = r[2400], r[500]
        rows.append((o, truth.mean(), fm.mean(), np.median(fb) if len(fb) else np.nan, np.median(fi) if len(fi) else np.nan, w, n))
        L.append(f"| {o:.2f} | {truth.mean():.3f} | {fm.mean():.3f} | {rows[-1][3]:.0f} | {rows[-1][4]:.0f} | "
                 f"{w['busy']:.3f} | {w['bp50']:.0f} / {w['bp90']:.0f} | {w['ip50']:.0f} | {w['tail']:.2f} | "
                 f"{n['busy']:.3f} | {n['bp50']:.0f} | {n['ip50']:.0f} | {n['tail']:.2f} |")
        print(L[-1], flush=True)
    # matched-busy readout by log interpolation on the waveform path
    L.append("\n## Matched to the pilot's busy fractions (2400 Hz, log-interpolated on the grid)\n")
    L.append("| pilot busy frac | nominal occ needed (waveform) | nominal occ needed (frame model) | waveform busy p50 s | frame-model busy p50 s | measured p50 s | ratio measured/waveform | ratio measured/frame |")
    L.append("|---|---|---|---|---|---|---|---|")
    xs = np.array([r[0] for r in rows]); wb = np.array([r[5]['busy'] for r in rows]); fb_ = np.array([r[2] for r in rows])
    wp = np.array([r[5]['bp50'] for r in rows]); fp = np.array([r[3] for r in rows])
    for target in (0.019, 0.05, 0.15):
        def solve(y):
            ok = np.isfinite(y) & (y > 0)
            return float(np.exp(np.interp(np.log(target), np.log(y[ok]), np.log(xs[ok])))) if ok.sum() >= 2 else np.nan
        nw, nf = solve(wb), solve(fb_)
        pw = float(np.interp(np.log(nw), np.log(xs), wp)) if np.isfinite(nw) else np.nan
        pf = float(np.interp(np.log(nf), np.log(xs), fp)) if np.isfinite(nf) else np.nan
        L.append(f"| {target:.3f} | {nw:.3f} | {nf:.3f} | {pw:.1f} | {pf:.1f} | 1.0 | {1/pw if pw else np.nan:.2f} | {1/pf if pf else np.nan:.2f} |")
    txt = "\n".join(L) + "\n"
    if a.out:
        open(a.out, "w").write(txt)
    print("\n".join(L[-5:]))


if __name__ == "__main__":
    main()
