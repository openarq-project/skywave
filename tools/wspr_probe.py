#!/usr/bin/env python3
"""wspr_probe.py — WSPR transmissions inside the QRM 20 m IQ captures as constant-power channel probes.

    wspr_probe.py DIR [DIR ...] --out wspr.csv [--series-dir S] [--workers 9]     (resumable)
    wspr_probe.py --selftest                                                     (method floor vs SNR)

POWER ONLY: no demodulation, no decoding, no station identification (QRM pre-reg §5 / §9e hold).

Why WSPR: one transmitter per slot, constant power and constant envelope (continuous-phase 4-FSK, tone
spacing 1.4648 Hz, ~6 Hz occupied) for 110.6 s, starting 1 s after an even UTC minute. Total power in a
window that holds all four tones is therefore the PATH, sampled continuously at sub-second resolution.
Only the 14097.0 kHz captures hold a WSPR sub-band (20 m: 14097.0-14097.2 kHz = +0..+200 Hz in baseband).

Rules (fixed 2026-10-02 before any corpus read):
 SLOTS     slot k starts at even UTC minute + 1 s, ends +110.6 s; a slot is used when >= 40 s of it lies
           (amended 10-02 from 60 s on the smoke, before any corpus read: capture phase is uniform over the
           2-min cycle, so 60 s dropped ~15 % of captures; short slots simply contribute no long lags)
           inside the capture (capture clock = sidecar start_utc; 2.0 s is trimmed from each slot end
           for clock slop; the observed switch time vs the expected edge is reported as edge_err_s).
 DETECT    in each used slot, the slot-interval spectrum (1/T Hz bins) integrated over a sliding 7 Hz
           window across -10..+210 Hz; noise per bin = the 25th percentile of the PSD over -50..+250 Hz,
           converted to the exponential mean (signals sit high, so the median over-reads); a candidate is a
           local max >= +6 dB over 7 Hz of noise, >= 6 Hz from a stronger one; at most 15 per slot.
 MEASURE   +-12.5 Hz complex baseband (25 Hz) of the whole capture around the candidate; drift tracked
           as the power centroid within +-5 Hz per 10 s of the slot; level = power within +-3.5 Hz of the
           tracked centre, 0.2 s grid. Noise in the same 7 Hz from the per-sample noise variance (not from the
           neighbouring baseband, which holds other WSPR signals).
 ACCEPT    (all must hold) (a) median in-slot SNR over 7 Hz >= 10 dB; (b) >= 70 % of the +-6 Hz power
           inside +-3.5 Hz (width of a WSPR signal, not a wider one); (c) SLOT-GATED: at least one slot
           edge lies inside the capture with >= 3 s of outside-slot data in [edge-7, edge-2] / [edge+2, edge+7],
           and the mean power over those outside-slot seconds is >= 6 dB below the in-slot median (a carrier or
           any non-slotted signal fails this; a WSPR station transmitting in the adjacent slot on the
           same frequency also fails it — a conservative loss, not a contamination).
 STATS     as carrier_probe (levels re in-slot median, censored = within 3 dB of noise), on the in-slot
           interval only: |delta| p50/p90 at 0.2..90 s lags; slow envelope (10 s mean of power) |delta|
           at 10..90 s; fade p10/p1; per 12.6 s window p10 re window mean (FT8 comparison); downward
           crossings/min and mean fade duration at -6/-10 dB; Rician K (moments); envelope decorrelation
           time (power autocovariance falls to 1/e of its 0.4 s value; noise only loads lags < ~0.3 s).
 SELFTEST  a synthetic constant-envelope 4-FSK at known SNR in complex white noise through the SAME
           measure + stats: the method floor that real fading must clear.
"""
import argparse, glob, json, math, os, sys, wave
import datetime as dt
import numpy as np

SRATE = 25.0
STEP = 0.2
WIN = 3.5
SLOT_LEN = 110.6
LAGS = [0.2, 0.5, 1, 2, 5, 10, 20, 30, 60, 90]
SLOW_LAGS = [10, 20, 30, 60, 90]
STAT_KEYS = (["snr7_db", "censored", "fade_p10", "fade_p1"] + [f"d{l:g}_{q}" for l in LAGS for q in ("p50", "p90")]
             + [f"slow{l}_{q}" for l in SLOW_LAGS for q in ("p50", "p90")] + ["slow_range_db", "win12p6_p10_med",
             "lcr6_per_min", "fade6_mean_s", "lcr10_per_min", "fade10_mean_s", "K", "tenv_s"])


def read_iq(path):
    with wave.open(path) as w:
        fs, n = w.getframerate(), w.getnframes()
        x = np.frombuffer(w.readframes(n), dtype="<i2").reshape(-1, 2).astype(np.float64)
    return fs, x[:, 0] + 1j * x[:, 1]


def baseband(Z, fs, n, fc):
    T = n / fs
    K = int(round(SRATE * T))
    k0 = int(round(fc * T)) - K // 2
    idx = (k0 + np.arange(K)) % n
    return np.fft.ifft(np.fft.ifftshift(Z[idx])) * K / n


def measure(x, a, b):
    """x: 25 Hz baseband (candidate near 0 Hz); [a, b) in-slot sample range. Returns the 0.2 s level
    series (whole capture), share of +-6 Hz power inside +-3.5 Hz, drift Hz/min."""
    m = int(10 * SRATE)
    seg = x[a:b]
    t = np.arange(len(x)) / SRATE
    fk, tk = [], []
    for i in range(len(seg) // m):
        c = seg[i * m:(i + 1) * m]
        F = np.abs(np.fft.fftshift(np.fft.fft(c * np.hanning(m), 4 * m))) ** 2
        fr = np.fft.fftshift(np.fft.fftfreq(4 * m, 1 / SRATE))
        sel = np.abs(fr) <= 5
        fk.append(float(np.sum(fr[sel] * F[sel]) / np.sum(F[sel]))); tk.append((a + (i + 0.5) * m) / SRATE)
    if len(fk) >= 2:
        ft = np.interp(t, tk, fk)
        drift = float(np.polyfit(tk, fk, 1)[0] * 60)
    else:
        ft = np.full(len(x), fk[0] if fk else 0.0); drift = 0.0
    xd = x * np.exp(-2j * np.pi * np.cumsum(ft) / SRATE)
    X = np.fft.fft(xd); f = np.fft.fftfreq(len(xd), 1 / SRATE)
    xn = np.fft.ifft(np.where(np.abs(f) <= WIN, X, 0))
    # in-slot share of +-6 Hz power inside +-3.5 Hz (noise-subtracted)
    S = np.abs(np.fft.fft(xd[a:b])) ** 2; fs_ = np.fft.fftfreq(b - a, 1 / SRATE)
    nper = np.median(S[(np.abs(fs_) > 8)]) if np.any(np.abs(fs_) > 8) else 0.0
    inw = np.sum(S[np.abs(fs_) <= WIN] - nper); w6 = np.sum(S[np.abs(fs_) <= 6] - nper)
    frac = float(inw / w6) if w6 > 0 else float("nan")
    k = int(round(STEP * SRATE))
    p = np.abs(xn) ** 2
    L = len(p) // k
    p02 = p[:L * k].reshape(L, k).mean(axis=1)
    return p02, frac, drift


def noise7(sigma2, fs):
    """noise power inside the +-3.5 Hz level window, given the capture's per-sample complex noise variance:
    the 25 Hz baseband carries sigma2 * SRATE / fs per sample, of which 7/25 passes the window."""
    return sigma2 * (2 * WIN) / fs


def stats(p, N):
    """p: 0.2 s power series (signal + noise) over the slot interval; N: noise power in the window."""
    s = np.clip(p - N, 1e-30, None)
    cens = p < 2 * N
    L = 10 * np.log10(s)
    unc = ~cens
    med = np.median(L[unc]) if unc.any() else np.nan
    Lr = L - med
    d = {k: np.nan for k in STAT_KEYS}          # every column always present (CSV header = first row's keys)
    d.update({"snr7_db": round(float(10 * np.log10(np.median(p) / N)), 1), "censored": round(float(cens.mean()), 4)})
    u = Lr[unc]
    d["fade_p10"] = round(float(np.percentile(u, 10)), 2) if len(u) else np.nan
    d["fade_p1"] = round(float(np.percentile(u, 1)), 2) if len(u) else np.nan
    for lag in LAGS:
        k = int(round(lag / STEP))
        if 0 < k < len(L):
            ok = unc[:-k] & unc[k:]
            dd = np.abs(L[k:] - L[:-k])[ok]
            d[f"d{lag:g}_p50"] = round(float(np.median(dd)), 2) if len(dd) else np.nan
            d[f"d{lag:g}_p90"] = round(float(np.percentile(dd, 90)), 2) if len(dd) else np.nan
    w = int(round(10 / STEP))
    if len(s) > w:
        sl = 10 * np.log10(np.convolve(s, np.ones(w) / w, mode="valid"))
        for lag in SLOW_LAGS:
            k = int(round(lag / STEP))
            if k < len(sl):
                dd = np.abs(sl[k:] - sl[:-k])
                d[f"slow{lag}_p50"] = round(float(np.median(dd)), 2)
                d[f"slow{lag}_p90"] = round(float(np.percentile(dd, 90)), 2)
        d["slow_range_db"] = round(float(sl.max() - sl.min()), 2)
    wn = int(round(12.6 / STEP)); vals = []
    for i in range(0, len(s) - wn + 1, wn):
        seg = s[i:i + wn]
        if cens[i:i + wn].mean() < 0.1:
            vals.append(np.percentile(10 * np.log10(seg / seg.mean()), 10))
    d["win12p6_p10_med"] = round(float(np.median(vals)), 2) if vals else np.nan
    mins = len(L) * STEP / 60
    for thr in (6, 10):
        below = (Lr < -thr) | cens
        d[f"lcr{thr}_per_min"] = round(int(np.sum(np.diff(below.astype(np.int8)) == 1)) / mins, 2)
        runs = np.diff(np.flatnonzero(np.diff(np.concatenate([[0], below.astype(np.int8), [0]]))))[::2]
        d[f"fade{thr}_mean_s"] = round(float(runs.mean() * STEP), 2) if len(runs) else 0.0
    g = s.var() / s.mean() ** 2
    d["K"] = round(float(math.sqrt(max(1 - g, 0)) / max(1 - math.sqrt(max(1 - g, 0)), 1e-6)), 2) if g < 1 else 0.0
    # envelope decorrelation: autocovariance of power, 1/e of its value at 0.4 s
    y = s - s.mean(); n = len(y)
    ac = np.fft.ifft(np.abs(np.fft.fft(y, 2 * n)) ** 2)[:n].real
    k0 = int(round(0.4 / STEP))
    if n > k0 + 2 and ac[k0] > 0:
        below = np.flatnonzero(ac[k0:] < ac[k0] / math.e)
        d["tenv_s"] = round(float((k0 + below[0]) * STEP), 2) if len(below) else round(float(n * STEP), 2)
    else:
        d["tenv_s"] = np.nan
    return d


def slots_in(start, secs):
    """[(slot_start_s_rel_capture, slot_end_s_rel, a, b)] with >= 40 s inside the capture."""
    t0 = start.replace(second=0, microsecond=0)
    if t0.minute % 2:
        t0 -= dt.timedelta(minutes=1)
    out = []
    for k in range(-1, 3):
        s0 = (t0 + dt.timedelta(minutes=2 * k, seconds=1) - start).total_seconds()
        s1 = s0 + SLOT_LEN
        a, b = max(s0 + 2.0, 0.0), min(s1 - 2.0, secs)
        if b - a >= 40:
            out.append((s0, s1, a, b))
    return out


def process(path, series_dir=None):
    side = json.load(open(path[:-4] + ".json"))
    if round(float(side["centre_khz"]), 1) != 14097.0:
        return []
    fs, z = read_iq(path)
    n = len(z); secs = n / fs
    start = dt.datetime.fromisoformat(side["start_utc"])
    if start.tzinfo is None:
        start = start.replace(tzinfo=dt.timezone.utc)
    Z = np.fft.fft(z)
    rows = []
    base = dict(file=os.path.basename(path)[:-4], station=side.get("station", ""), start_utc=side["start_utc"][:19],
                seconds=round(secs, 1), fs=fs, rssi_min_dbm=side.get("rssi_min_dbm"))
    for si, (s0, s1, a, b) in enumerate(slots_in(start, secs)):
        ia, ib = int(a * fs), int(b * fs)
        seg = z[ia:ib]; T = len(seg) / fs
        S = np.abs(np.fft.fft(seg * np.hanning(len(seg)))) ** 2
        f = np.fft.fftfreq(len(seg), 1 / fs)
        band = (f >= -50) & (f <= 250)
        n0 = float(np.percentile(S[band], 25)) / -math.log(0.75)        # exponential: p25 -> mean (signals sit high)
        sigma2 = n0 / np.sum(np.hanning(len(seg)) ** 2)                # per-sample complex noise variance
        N7 = noise7(sigma2, fs)
        o = np.argsort(f[band]); fb = f[band][o]; Sb = S[band][o]
        wbins = max(int(round(2 * WIN * T)), 1)
        integ = np.convolve(Sb, np.ones(wbins), mode="same")
        snr = integ / (n0 * wbins)
        ok = (fb >= -10) & (fb <= 210) & (snr >= 10 ** 0.6)
        cands = []
        for i in np.argsort(-snr):
            if not ok[i]:
                continue
            if any(abs(fb[i] - c) < 6 for c in cands):
                continue
            cands.append(float(fb[i]))
            if len(cands) >= 15:
                break
        for fc in cands:
            x = baseband(Z, fs, n, fc)
            A, B = int(a * SRATE), int(b * SRATE)
            p02, frac, drift = measure(x, A, B)
            ka, kb = int(round(a / STEP)), int(round(b / STEP))
            inslot = p02[ka:kb]
            if len(inslot) < 50:
                continue
            med_in = np.median(inslot)
            # slot-gated test: outside-slot seconds adjacent to each edge inside the capture
            gates, edge_err = [], []
            for edge, side_ in ((s0, -1), (s1, +1)):
                lo, hi = (edge - 7.0, edge - 2.0) if side_ < 0 else (edge + 2.0, edge + 7.0)
                lo, hi = max(lo, 0.0), min(hi, secs)
                if hi - lo >= 3.0:
                    out = p02[int(lo / STEP):int(hi / STEP)]
                    if len(out):
                        g = 10 * np.log10(max(np.mean(out), 1e-30) / med_in)
                        gates.append(g)
                        # observed switch time: 1 s-smoothed log power crosses the midpoint within +-5 s of the edge
                        if g <= -6:
                            sm = np.convolve(p02, np.ones(5) / 5, mode="same")
                            lp = 10 * np.log10(np.maximum(sm, 1e-30)); mid = 10 * np.log10(med_in) + g / 2
                            i0, i1 = max(int((edge - 5) / STEP), 0), min(int((edge + 5) / STEP), len(lp) - 1)
                            w_ = lp[i0:i1]
                            cr = np.flatnonzero((w_[:-1] < mid) & (w_[1:] >= mid)) if side_ < 0 else np.flatnonzero((w_[:-1] >= mid) & (w_[1:] < mid))
                            if len(cr):
                                edge_err.append(round((i0 + cr[0]) * STEP - edge, 2))
            gate_db = min(gates) if gates else np.nan
            st = stats(inslot, N7)
            acc = (st["snr7_db"] >= 10) and (frac >= 0.7 if not np.isnan(frac) else False) and (not np.isnan(gate_db) and gate_db <= -6)
            row = dict(base, slot=si, slot_start_rel_s=round(s0, 2), in_slot_s=round(b - a, 1), f_off_hz=round(fc, 2),
                       tone_khz=round(14097.0 + fc / 1000, 5), frac_in_win=round(frac, 3), gate_db=round(float(gate_db), 2) if gates else "",
                       edge_err_s=edge_err[0] if edge_err else "", drift_hz_per_min=round(drift, 3), accepted=int(acc), **st)
            rows.append(row)
            if series_dir and acc:
                os.makedirs(series_dir, exist_ok=True)
                np.savez_compressed(os.path.join(series_dir, f"{base['file']}_s{si}_{fc:+.1f}.npz"),
                                    p02=p02.astype(np.float32), N7=N7, ka=ka, kb=kb)
    return rows


def synth_fsk(fs, secs, snr7_db, rng):
    """constant-envelope continuous-phase 4-FSK (WSPR tone spacing/rate) in complex white noise; SNR over 7 Hz."""
    n = int(fs * secs); sym = 8192 / 12000.0
    t = np.arange(n) / fs
    tones = rng.integers(0, 4, int(secs / sym) + 2)
    fi = (tones[(t / sym).astype(int)] - 1.5) * 12000 / 8192 + 100.0
    sig = np.exp(2j * np.pi * np.cumsum(fi) / fs)
    noise_var = (1.0 / 10 ** (snr7_db / 10)) * fs / 7.0
    noise = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * math.sqrt(noise_var / 2)
    return sig + noise, noise_var


def selftest():
    rng = np.random.default_rng(7)
    fs = 12000; secs = 112.0
    print("method floor: constant-envelope 4-FSK in white noise, same measure + stats (5 reps each)")
    keys = ["snr7_db", "fade_p10", "win12p6_p10_med", "d1_p90", "d10_p90", "slow30_p50", "slow30_p90", "slow60_p90", "slow_range_db", "lcr10_per_min", "K", "tenv_s"]
    print("  target " + " ".join(f"{k:>14s}" for k in keys))
    for snr in (10, 13, 16, 20, 25, 30):
        acc = []
        for r in range(5):
            z, nv = synth_fsk(fs, secs, snr, rng)
            n = len(z); Z = np.fft.fft(z)
            x = baseband(Z, fs, n, 100.0)
            a, b = 1.0, secs - 1.0
            p02, frac, drift = measure(x, int(a * SRATE), int(b * SRATE))
            st = stats(p02[int(a / STEP):int(b / STEP)], noise7(nv, fs))
            acc.append([st.get(k, np.nan) for k in keys])
        m = np.nanmedian(np.array(acc, float), axis=0)
        print(f"  {snr:5d}  " + " ".join(f"{v:14.2f}" for v in m) + f"   frac {frac:.2f}")


def safe(args):
    path, sd = args
    try:
        return process(path, sd)
    except Exception as e:
        print(f"SKIP {path}: {e!r}", file=sys.stderr, flush=True)
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="*"); ap.add_argument("--out")
    ap.add_argument("--series-dir"); ap.add_argument("--workers", type=int, default=9)
    ap.add_argument("--files"); ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    import csv, multiprocessing as mp
    files = ([l.strip() for l in open(a.files) if l.strip()] if a.files else
             sorted(p for d in a.dirs for p in glob.glob(os.path.join(d, "*_iq140970_*.wav")) if os.path.exists(p[:-4] + ".json")))
    donelog = a.out + ".done"
    done = set(open(donelog).read().split()) if os.path.exists(donelog) else set()
    files = [f for f in files if f not in done]
    print(f"{len(files)} captures to do ({len(done)} done)", flush=True)
    fields = None
    if os.path.exists(a.out) and os.path.getsize(a.out) > 0:
        fields = csv.DictReader(open(a.out)).fieldnames
    w = None; n = 0
    with open(a.out, "a", newline="") as fh, open(donelog, "a") as dl, mp.Pool(a.workers) as pool:
        if fields:
            w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        for path, rows in zip(files, pool.imap(safe, [(f, a.series_dir) for f in files], chunksize=2)):
            for r in rows:
                if w is None:
                    w = csv.DictWriter(fh, fieldnames=list(r.keys()), extrasaction="ignore"); w.writeheader()
                w.writerow(r)
            dl.write(path + "\n"); n += 1
            if n % 100 == 0:
                fh.flush(); dl.flush(); print(f"{n}/{len(files)}", flush=True)
    print("done", flush=True)


if __name__ == "__main__":
    main()
