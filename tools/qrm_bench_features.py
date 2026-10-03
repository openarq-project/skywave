#!/usr/bin/env python3
"""qrm_bench_features.py — per-(capture, 3 kHz slice) features of the QRM IQ corpus, for selecting replay-bench strata.

    qrm_bench_features.py DIR [DIR ...] --out features.csv [--workers 4]

Each DIR holds KiwiSDR IQ captures (`*_iq*.wav` + `.json` sidecar; a wav without a sidecar is not a capture).
Slices follow skywave's QrmReplay convention: the USB band [dial, dial + 3000] Hz of the capture's baseband,
|dial| + bw <= 5000. Gateway centres get ONE dial covering the Winlink cluster; parking centres get three
near-disjoint dials. The floor is QrmReplay._floor_gain's estimator verbatim (Hann 1 s frames, per-bin p10 over
time / -ln 0.9, p25 across the slice's bins), so a slice classed here is scaled identically in a cell.

Columns (INR = dB over that floor):
  busy10 / detect3       fraction of 1 s frames whose slice-mean INR >= 10 / >= 3 dB (the pilot scorer's statistic)
  inr_p50/p90/max        per-frame slice-mean INR percentiles
  first10s_inr           max per-frame INR over the first 10 s (cold-start risk)
  carrier_frac           max over 25 Hz groups of the fraction of frames that group is >= +10 dB
  carrier_narrow         that group's +-100..200 Hz neighbours are elevated < 30 % as often (a tone, not a wide burst)
  bw_carrier/narrow/wide/broad  among busy frames, class of the widest +10 dB segment (<=100, 100-600, 600-2900, >2900 Hz)
  wide_frac              fraction of frames with > 50 % of the slice's 25 Hz groups >= +6 dB
  ac15 / ac_peak_lag/ac_peak   autocorrelation of per-frame power at lag 15 s; strongest lag in 2..20 s
  peak_sigma             envelope peak of the slice in units of the cell's sigma after QrmReplay scaling (fs 48 kHz),
                         x1.5 crossfade margin NOT applied — multiply by 1.5 for QrmReplay's rail bound
  floor_dbm_hz           sidecar S-meter anchor (rssi_min over USB 0-3 kHz at the centre) in dBm/Hz, carried to this
                         slice by the ratio of the two slices' floors (approximate: a busy centre lifts rssi_min)
"""
import argparse, glob, json, math, os, sys, wave
import datetime as dt
import numpy as np

EXP_P10 = -math.log(0.9)
FS_SIM = 48000
BW = 3000.0
GROUP_HZ = 25
# centre kHz -> list of (dial Hz, role)
DIALS = {
    7101.9: [(-1800, "gw")],                    # 7100.1-7103.1: the 7100.1-7102.6 cluster
    10146.4: [(-1600, "gw")],                   # 10144.8-10147.8: the 10144.8-10146.7 cluster
    14097.0: [(-2100, "gw")],                   # 14094.9-14097.9: the 14094.9-14098.3 cluster
    7107.0: [(-5000, "park"), (-1500, "park"), (2000, "park")],
    10133.0: [(-5000, "park"), (-1500, "park"), (2000, "park")],   # +2000 slice = 10135-10138: FT8 at 10136
    14108.0: [(-5000, "park"), (-1500, "park"), (2000, "park")],
}


def read_iq(path):
    with wave.open(path) as w:
        fs, n, ch = w.getframerate(), w.getnframes(), w.getnchannels()
        if ch != 2:
            raise ValueError("not 2-channel")
        x = np.frombuffer(w.readframes(n), dtype="<i2").reshape(-1, 2).astype(np.float64)
    return fs, x[:, 0] + 1j * x[:, 1]


def segments(mask):
    """widest run of True in a 1-D bool array, one-False gaps merged; returns length in elements."""
    m = mask.copy()
    gap = (~m[1:-1]) & m[:-2] & m[2:]
    m[1:-1] |= gap
    if not m.any():
        return 0
    d = np.diff(np.concatenate([[0], m.astype(np.int8), [0]]))
    return int((np.flatnonzero(d == -1) - np.flatnonzero(d == 1)).max())


def features(path):
    side = json.load(open(path[:-4] + ".json"))
    centre = round(float(side["centre_khz"]), 1)
    if centre not in DIALS:
        return []
    fs, z = read_iq(path)
    seg = fs
    nfr = len(z) // seg
    if nfr < 20:
        return []
    win = np.hanning(seg)
    fr = np.fft.fftfreq(seg, 1.0 / fs)
    F = np.empty((nfr, seg))
    for i in range(nfr):
        F[i] = np.abs(np.fft.fft(z[i * seg:(i + 1) * seg] * win)) ** 2
    F /= (np.sum(win ** 2) * fs)
    Zfull = np.fft.fft(z)
    ffull = np.fft.fftfreq(len(z), 1.0 / fs)
    start = dt.datetime.fromisoformat(side["start_utc"])
    loc = start - dt.timedelta(hours=4)
    rec = side.get("receiver", {})
    base = dict(file=os.path.basename(path)[:-4], dir=os.path.dirname(path), station=side.get("station", ""),
                band=side.get("band", ""), centre_khz=centre, start_utc=start.strftime("%Y-%m-%dT%H:%M:%S"),
                local_hour=loc.hour, weekend=int(loc.weekday() >= 5),
                contest=int(start.date() in (dt.date(2026, 9, 26), dt.date(2026, 9, 27))),
                seconds=round(len(z) / fs, 1), adc_ov=rec.get("adc_ov", ""))
    # absolute anchor: the sidecar S-meter reads the USB [0, 3000] Hz passband at the centre; carry it to every slice
    # through the ratio of the two slices' floors (same estimator)
    c_sel = (fr >= 0) & (fr < BW)
    n0_centre = float(np.percentile(np.percentile(F[:, c_sel], 10, axis=0) / EXP_P10, 25))
    anchor = side.get("rssi_min_dbm")
    out = []
    for dial, role in DIALS[centre]:
        sel = (fr >= dial) & (fr < dial + BW)
        idx = np.flatnonzero(sel)
        idx = idx[np.argsort(fr[idx])]
        P = F[:, idx]                                            # frames x 1 Hz bins, ascending frequency
        n0 = float(np.percentile(np.percentile(P, 10, axis=0) / EXP_P10, 25))
        inr = 10 * np.log10(P.mean(axis=1) / n0)
        ng = P.shape[1] // GROUP_HZ
        G = P[:, :ng * GROUP_HZ].reshape(nfr, ng, GROUP_HZ).mean(axis=2) / n0
        e10 = G >= 10.0
        e6 = G >= 10 ** 0.6
        gfrac = e10.mean(axis=0)
        gi = int(np.argmax(gfrac))
        nb = [j for j in list(range(gi - 8, gi - 3)) + list(range(gi + 4, gi + 9)) if 0 <= j < ng]
        carrier_narrow = int(bool(nb) and gfrac[nb].max() < 0.3 * max(gfrac[gi], 1e-9))
        busy = inr >= 10.0
        widths = np.array([segments(e10[i]) * GROUP_HZ for i in np.flatnonzero(busy)])
        nbsy = max(len(widths), 1)
        pw = P.mean(axis=1); pw = pw - pw.mean()
        den = float(np.dot(pw, pw)) or 1e-30
        ac = {L: float(np.dot(pw[:-L], pw[L:]) / den) for L in range(2, 21)}
        Lp = max(ac, key=ac.get)
        keep = (ffull >= dial) & (ffull < dial + BW)
        zs = np.fft.ifft(np.where(keep, Zfull, 0))
        peak_sigma = math.sqrt(2.0) * float(np.abs(zs).max()) / math.sqrt(n0 * FS_SIM / 2.0)
        floor = (anchor - 10 * math.log10(3000.0) + 10 * math.log10(n0 / n0_centre) if anchor is not None else None)
        out.append(dict(base, dial_hz=dial, role=role,
                        slice_khz=f"{centre + dial / 1000:.1f}-{centre + (dial + BW) / 1000:.1f}",
                        busy10=round(float(busy.mean()), 4), detect3=round(float((inr >= 3).mean()), 4),
                        inr_p50=round(float(np.percentile(inr, 50)), 2), inr_p90=round(float(np.percentile(inr, 90)), 2),
                        inr_max=round(float(inr.max()), 2), first10s_inr=round(float(inr[:10].max()), 2),
                        carrier_frac=round(float(gfrac[gi]), 3), carrier_khz=round(centre + (dial + gi * GROUP_HZ + GROUP_HZ / 2) / 1000, 4),
                        carrier_narrow=carrier_narrow,
                        bw_carrier=round(float((widths <= 100).sum() / nbsy), 3) if len(widths) else 0,
                        bw_narrow=round(float(((widths > 100) & (widths <= 600)).sum() / nbsy), 3) if len(widths) else 0,
                        bw_wide=round(float(((widths > 600) & (widths <= 2900)).sum() / nbsy), 3) if len(widths) else 0,
                        bw_broad=round(float((widths > 2900).sum() / nbsy), 3) if len(widths) else 0,
                        wide_frac=round(float((e6.mean(axis=1) > 0.5).mean()), 4),
                        ac15=round(ac[15], 3), ac_peak_lag=Lp, ac_peak=round(ac[Lp], 3),
                        peak_sigma=round(peak_sigma, 2),
                        floor_dbm_hz=round(floor, 1) if floor is not None else ""))
    return out


def safe(path):
    try:
        return features(path)
    except Exception as e:
        print(f"SKIP {path}: {e}", file=sys.stderr, flush=True)
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="+"); ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    files = sorted(p for d in a.dirs for p in glob.glob(os.path.join(d, "*_iq*.wav")) if os.path.exists(p[:-4] + ".json"))
    import csv, multiprocessing as mp
    w = None
    done = set()
    if os.path.exists(a.out) and os.path.getsize(a.out) > 0:          # resume: skip captures already written
        with open(a.out) as fh:
            rd = csv.DictReader(fh)
            done = {os.path.join(r["dir"], r["file"] + ".wav") for r in rd}
            fields = rd.fieldnames
    files = [f for f in files if f not in done]
    print(f"{len(files)} captures with sidecars to do ({len(done)} already done)", flush=True)
    n = 0
    with open(a.out, "a" if done else "w", newline="") as fh, mp.Pool(a.workers) as pool:
        if done:
            w = csv.DictWriter(fh, fieldnames=fields)
        for rows in pool.imap_unordered(safe, files, chunksize=4):
            for r in rows:
                if w is None:
                    w = csv.DictWriter(fh, fieldnames=list(r.keys())); w.writeheader()
                w.writerow(r)
            n += 1
            if n % 200 == 0:
                print(f"{n}/{len(files)}", flush=True); fh.flush()
    print("done", flush=True)


if __name__ == "__main__":
    main()
