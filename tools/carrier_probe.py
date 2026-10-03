#!/usr/bin/env python3
"""carrier_probe.py — steady carriers in the QRM IQ corpus as channel probes (level + Doppler, 0.04 s – 2 min).

    carrier_probe.py DIR [DIR ...] --out tones.csv [--series-dir S] [--workers 8]   (resumable)

Level and spectrum measurements only: no demodulation, no decoding, no station identification
(QRM pre-reg §5 / §9e hold).

Per capture (KiwiSDR IQ wav + .json sidecar; fixed gain, AGC off):
 1. DETECT persistent lines over the capture's flat band (50 Hz <= |f| <= 4950 Hz; DC +-50 Hz masked):
    long-average spectrum (4 s Hann segments, 0.25 Hz bins), local floor = median over +-25 Hz;
    a candidate peaks >= +15 dB over its local floor (amended 10-02 before the corpus run: the stratum's tones
    read +19..+25 dB, so +20 missed real ones); peaks within 3 Hz merge. Presence: in 1 s
    frames, power within +-2 Hz >= 10x (+10 dB) the frame's local floor (5 bins) in >= 90 % of frames.
    At most 12 lines per capture (strongest first).
 2. EXTRACT each line: the f0 +-12.5 Hz block of the whole-capture FFT inverse-transformed to a 25 Hz
    complex baseband (0.04 s samples). Noise: the same 25 Hz width at the quietest offset among
    +-40, +-60, +-80 Hz. Drift: f0 re-estimated per 10 s chunk (peak of the chunk's baseband
    spectrum); the baseband is de-rotated by the piecewise-linear drift track before Doppler work.
 3. CLASSIFY (thresholds fixed here before any corpus read, 2026-10-02):
    - weak:   median tone SNR in the +-1.5 Hz narrowband < 10 dB -> no statistics (amended 10-02 before
              the corpus run, from 25 Hz / 15 dB: the corpus' tones are ~0 dB in 25 Hz)
    - fsk:    two lines 60-260 Hz apart, both present, 0.2 s power correlation < -0.3 -> one probe,
              powers summed (constant-envelope FSK idle; each line alone is keyed)
    - keyed:  >= 5 % of 0.2 s samples within 3 dB of noise AND >= 3 abrupt drops (>= 15 dB fall
              within 0.2 s) -> excluded (CW beacon / on-off keying, not the path)
    - local:  Rician K >= 20 AND noise-corrected coherence time >= 10 s (amended 10-02 on the 9-capture smoke,
              before the corpus run: the Doppler second moment is contaminated by neighbouring lines within
              +-5 Hz, so it is reported, not used to classify; skywave fading at >= 0.1 Hz spread gives
              coherence times of a few seconds at most) ->
              receiver-local spur or ground wave: NO ionospheric fading. Kept as the instrument control.
    - sky:    everything else -> the probe.
 4. STATISTICS (sky and local alike; censored = 0.2 s sample within 3 dB of the noise):
    LEVEL is measured in +-1.5 Hz around the drift-tracked tone (frac_in_nb = share of the +-5 Hz
    noise-subtracted tone power inside it; low => Doppler spread wider than the band), on a 0.5 s grid;
    level dB re capture median; |delta| p50/p90 at lags 0.2..90 s; the SLOW envelope
    (10 s running mean of power) |delta| at 10..90 s; fade depth p10/p1 re median; per 12.6 s window
    p10 re window mean (the FT8 campaign's statistic, for validation); downward crossings per min and
    mean fade duration at -6 / -10 dB re median; K factor (moments); Doppler 2sigma after drift removal
    (20 s Welch, bins > 3x the per-bin noise only, noise-subtracted, within +-5 Hz — SECONDARY: any other line
    within +-5 Hz inflates it; coherence time is the primary fading-rate statistic); coherence time (|ACF| of the de-rotated baseband
    falls to 0.5); drift Hz/min (TX oscillator + Kiwi clock + ionosphere: NOT separable, no GPS-lock field).
"""
import argparse, glob, json, math, os, sys, wave
import numpy as np

SRATE = 25.0           # baseband rate Hz (block width)
LAGS = [0.5, 1, 2, 5, 10, 20, 30, 60, 90]
SLOW_LAGS = [10, 20, 30, 60, 90]


def read_iq(path):
    with wave.open(path) as w:
        fs, n = w.getframerate(), w.getnframes()
        x = np.frombuffer(w.readframes(n), dtype="<i2").reshape(-1, 2).astype(np.float64)
    return fs, x[:, 0] + 1j * x[:, 1]


def detect(z, fs):
    seg = int(4 * fs)
    nseg = len(z) // seg
    win = np.hanning(seg)
    P = np.zeros(seg)
    for i in range(nseg):
        P += np.abs(np.fft.fft(z[i * seg:(i + 1) * seg] * win)) ** 2
    f = np.fft.fftfreq(seg, 1 / fs)
    o = np.argsort(f); f, P = f[o], P[o]
    df = f[1] - f[0]
    k = int(round(25 / df))
    from numpy.lib.stride_tricks import sliding_window_view
    pad = np.pad(P, k, mode="edge")
    floor = np.median(sliding_window_view(pad, 2 * k + 1), axis=1)
    ratio = P / floor
    ok = (np.abs(f) >= 50) & (np.abs(f) <= 4950) & (ratio >= 10 ** 1.5)
    cand = []
    for i in np.flatnonzero(ok):
        if ratio[i] == ratio[max(0, i - 4):i + 5].max():
            if not cand or f[i] - cand[-1][0] > 3:
                cand.append((float(f[i]), float(ratio[i])))
            elif ratio[i] > cand[-1][1]:
                cand[-1] = (float(f[i]), float(ratio[i]))
    if not cand:
        return []
    # presence in 1 s frames
    s1 = int(fs); n1 = len(z) // s1; w1 = np.hanning(s1)
    f1 = np.fft.fftfreq(s1, 1 / fs)
    F = np.empty((n1, s1))
    for i in range(n1):
        F[i] = np.abs(np.fft.fft(z[i * s1:(i + 1) * s1] * w1)) ** 2
    out = []
    for f0, r in sorted(cand, key=lambda c: -c[1]):
        on = np.abs(f1 - f0) <= 2.0
        ref = (np.abs(f1 - f0) >= 5) & (np.abs(f1 - f0) <= 25)
        sig = F[:, on].sum(axis=1); flo = np.median(F[:, ref], axis=1) * on.sum()
        pres = float(np.mean(sig >= 10 * flo))
        if pres >= 0.9:
            out.append((f0, 10 * math.log10(r), pres))
        if len(out) >= 12:
            break
    return out


def baseband(Z, fs, n, fc):
    """complex baseband of [fc - 12.5, fc + 12.5) Hz at 25 Hz, fc at 0 Hz, from the whole-capture FFT Z."""
    T = n / fs
    K = int(round(SRATE * T))
    k0 = int(round(fc * T)) - K // 2
    idx = (k0 + np.arange(K)) % n
    return np.fft.ifft(np.fft.ifftshift(Z[idx])) * K / n


def series(Z, fs, n, f0):
    x = baseband(Z, fs, n, f0)                       # tone at 0 Hz (+- its drift)
    noises = []
    for off in (40, -40, 60, -60, 80, -80):
        y = baseband(Z, fs, n, f0 + off)
        noises.append(np.mean(np.abs(y) ** 2))
    N = float(min(noises))
    return x, N


NB_HZ = 1.5            # narrowband half-width around the drift-tracked tone
STEP = 0.5             # level grid s


def narrowband(xd):
    """+-NB_HZ low-pass of the de-rotated baseband (tone at 0 Hz); returns the filtered series."""
    X = np.fft.fft(xd)
    f = np.fft.fftfreq(len(xd), 1 / SRATE)
    return np.fft.ifft(np.where(np.abs(f) <= NB_HZ, X, 0))


def level02(p, step=None):
    m = int(round((step or 0.2) * SRATE))
    L = len(p) // m
    return p[:L * m].reshape(L, m).mean(axis=1)


def drift_track(x):
    """per-10 s peak frequency of the baseband (Hz re block start); returns de-rotated baseband + track."""
    m = int(10 * SRATE); nch = len(x) // m
    t = np.arange(len(x)) / SRATE
    fk, tk = [], []
    for i in range(nch):
        c = x[i * m:(i + 1) * m]
        F = np.abs(np.fft.fft(c * np.hanning(m), 8 * m)) ** 2
        fr = np.fft.fftfreq(8 * m, 1 / SRATE)
        fk.append(fr[np.argmax(F)]); tk.append((i + 0.5) * 10)
    fk = np.array(fk); tk = np.array(tk)
    if len(fk) < 2:
        return x, 0.0, fk
    ft = np.interp(t, tk, fk)
    ph = 2 * np.pi * np.cumsum(ft) / SRATE
    slope = np.polyfit(tk, fk, 1)[0] * 60.0
    return x * np.exp(-1j * ph), float(slope), fk


def doppler_2sigma(xd, N):
    m = int(20 * SRATE)
    nseg = len(xd) // m
    if nseg < 2:
        return float("nan")
    S = np.zeros(m)
    w = np.hanning(m)
    for i in range(nseg):
        S += np.abs(np.fft.fftshift(np.fft.fft(xd[i * m:(i + 1) * m] * w))) ** 2
    S /= nseg
    f = np.fft.fftshift(np.fft.fftfreq(m, 1 / SRATE))
    nfl = N * np.sum(w ** 2)                         # noise per bin
    sig = S > 3 * nfl                                # only bins clearly above the noise (+4.8 dB)
    S = np.where(sig, S - nfl, 0.0)
    sel = np.abs(f) <= 5
    S, f = S[sel], f[sel]
    if S.sum() <= 0:
        return float("nan")
    mu = np.sum(f * S) / S.sum()
    return float(2 * math.sqrt(np.sum((f - mu) ** 2 * S) / S.sum()))


def coherence_time(xd, Nw=0.0):
    """lag where |ACF| of the de-rotated 25 Hz baseband falls to 0.5; the white noise (power Nw per
    sample) only loads lag 0, so lags > 0 are normalised by the SIGNAL power R(0) - Nw * n."""
    y = xd
    n = len(y)
    F = np.fft.fft(y, 2 * n)
    ac = np.fft.ifft(np.abs(F) ** 2)[:n]
    r0 = max(np.abs(ac[0]) - Nw * n, 1e-30)
    ac = np.abs(ac) / r0
    ac[0] = 1.0
    below = np.flatnonzero(ac < 0.5)
    return float(below[0] / SRATE) if len(below) else float(n / SRATE)


def stats(p02, N, xd, frac_nb=float("nan"), Nw=0.0):
    """p02: STEP-s narrowband power series (tone + noise), N its noise power. Returns a dict of statistics."""
    s = np.clip(p02 - N, 1e-30, None)
    cens = p02 < 2 * N
    L = 10 * np.log10(s)
    med = np.median(L[~cens]) if (~cens).any() else np.nan
    Lr = L - med
    d = {}
    d["snr_nb_db"] = round(float(10 * np.log10(np.median(p02) / N)), 1)
    d["frac_in_nb"] = round(float(frac_nb), 3)
    d["censored"] = round(float(cens.mean()), 4)
    unc = Lr[~cens]
    d["fade_p10"] = round(float(np.percentile(unc, 10)), 2) if len(unc) else np.nan
    d["fade_p1"] = round(float(np.percentile(unc, 1)), 2) if len(unc) else np.nan
    for lag in LAGS:
        k = int(round(lag / STEP))
        if 0 < k < len(L):
            ok = ~cens[:-k] & ~cens[k:]
            dd = np.abs(L[k:] - L[:-k])[ok]
            d[f"d{lag:g}_p50"] = round(float(np.median(dd)), 2) if len(dd) else np.nan
            d[f"d{lag:g}_p90"] = round(float(np.percentile(dd, 90)), 2) if len(dd) else np.nan
    # slow envelope: 10 s running mean of power
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
    # FT8-comparable: per 12.6 s window, p10 re window mean power
    wn = int(round(12.6 / STEP)); vals = []
    for i in range(0, len(s) - wn + 1, wn):
        seg = s[i:i + wn]; c = cens[i:i + wn]
        if c.mean() < 0.1:
            vals.append(np.percentile(10 * np.log10(seg / seg.mean()), 10))
    d["win12p6_p10_med"] = round(float(np.median(vals)), 2) if vals else np.nan
    for thr in (6, 10):
        below = (Lr < -thr) | cens
        tr = np.flatnonzero(np.diff(below.astype(np.int8)) == 1)
        mins = len(L) * STEP / 60
        d[f"lcr{thr}_per_min"] = round(len(tr) / mins, 2)
        runs = np.diff(np.flatnonzero(np.diff(np.concatenate([[0], below.astype(np.int8), [0]]))))[::2]
        d[f"fade{thr}_mean_s"] = round(float(runs.mean() * STEP), 2) if len(runs) else 0.0
    m1 = s.mean(); v = s.var(); g = v / m1 ** 2
    d["cv2"] = round(float(g), 4)
    d["K"] = round(float(math.sqrt(max(1 - g, 0)) / max(1 - math.sqrt(max(1 - g, 0)), 1e-6)), 2) if g < 1 else 0.0
    d["doppler_2sigma_hz"] = round(doppler_2sigma(xd, Nw), 3)     # Nw: the 25 Hz baseband's noise power
    d["tcoh_s"] = round(coherence_time(xd, Nw), 2)
    # abrupt drops: >= 15 dB fall within one STEP
    d["abrupt_drops"] = int(np.sum((L[1:] - L[:-1]) <= -15))
    return d, L, cens


def process(path, series_dir=None):
    side = json.load(open(path[:-4] + ".json"))
    fs, z = read_iq(path)
    n = len(z)
    if n / fs < 60:
        return []
    lines = detect(z, fs)
    if not lines:
        return []
    Z = np.fft.fft(z)
    base = dict(file=os.path.basename(path)[:-4], dir=os.path.dirname(path), station=side.get("station", ""),
                band=side.get("band", ""), centre_khz=round(float(side["centre_khz"]), 1),
                start_utc=side["start_utc"][:19], fs=fs, seconds=round(n / fs, 1),
                adc_ov=side.get("receiver", {}).get("adc_ov", ""))
    probes = []
    for f0, prom, pres in lines:
        x, N = series(Z, fs, n, f0)
        xd, drift, fk = drift_track(x)
        xn = narrowband(xd)
        Nn = N * (2 * NB_HZ / SRATE)
        tot5 = None
        X = np.abs(np.fft.fft(xd)) ** 2; fr = np.fft.fftfreq(len(xd), 1 / SRATE)
        nb = np.sum(X[np.abs(fr) <= NB_HZ]) - N * len(xd) * np.mean(np.abs(fr) <= NB_HZ)
        w5 = np.sum(X[np.abs(fr) <= 5]) - N * len(xd) * np.mean(np.abs(fr) <= 5)
        probes.append(dict(f0=f0, prom=prom, pres=pres, x=x, xd=xd, drift=drift, fk=fk, N=Nn, Nw=N,
                           frac_nb=nb / w5 if w5 > 0 else float("nan"),
                           p02=level02(np.abs(xn) ** 2, STEP), p02f=level02(np.abs(x) ** 2, 0.2)))
    # FSK pairs
    used = set(); groups = []
    for i, a in enumerate(probes):
        if i in used:
            continue
        mate = None
        for j, b in enumerate(probes):
            if j <= i or j in used:
                continue
            if 60 <= abs(a["f0"] - b["f0"]) <= 260:
                L = min(len(a["p02f"]), len(b["p02f"]))
                r = np.corrcoef(a["p02f"][:L], b["p02f"][:L])[0, 1]
                if r < -0.3:
                    mate = (j, r); break
        if mate:
            used |= {i, mate[0]}; groups.append(("fsk", [i, mate[0]], mate[1]))
        else:
            used.add(i); groups.append(("single", [i], np.nan))
    rows = []
    for kind, idx, r in groups:
        P = [probes[i] for i in idx]
        L = min(len(q["p02"]) for q in P)
        p02 = sum(q["p02"][:L] for q in P)
        N = sum(q["N"] for q in P)
        xd, drift, fk = P[0]["xd"], P[0]["drift"], P[0]["fk"]   # Doppler from the strongest line of the group
        st, Lser, cens = stats(p02, N, xd, P[0]["frac_nb"], P[0]["Nw"])
        if st["snr_nb_db"] < 10:
            cls = "weak"
        elif kind == "fsk":
            cls = "fsk"
        elif st["censored"] >= 0.05 and st["abrupt_drops"] >= 3:
            cls = "keyed"
        elif st["K"] >= 20 and st["tcoh_s"] >= 10:
            cls = "local"
        else:
            cls = "sky"
        f_abs = round(base["centre_khz"] + P[0]["f0"] / 1000.0, 4)
        row = dict(base, tone_khz=f_abs, f_off_hz=round(P[0]["f0"], 2), n_lines=len(P), fsk_r=round(float(r), 3) if kind == "fsk" else "",
                   prominence_db=round(P[0]["prom"], 1), presence=round(P[0]["pres"], 3), cls=cls,
                   drift_hz_per_min=round(drift, 3), **st)
        rows.append(row)
        if series_dir and cls in ("sky", "local", "fsk"):
            os.makedirs(series_dir, exist_ok=True)
            np.savez_compressed(os.path.join(series_dir, f"{base['file']}_{P[0]['f0']:+.1f}.npz"),
                                level_db=Lser.astype(np.float32), censored=cens, drift_track=np.asarray(fk, np.float32))
    return rows


def safe(args):
    path, sd = args
    try:
        return process(path, sd)
    except Exception as e:
        print(f"SKIP {path}: {e!r}", file=sys.stderr, flush=True)
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="+"); ap.add_argument("--out", required=True)
    ap.add_argument("--series-dir"); ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--files", help="optional list of wav paths (one per line) instead of scanning dirs")
    a = ap.parse_args()
    import csv, multiprocessing as mp
    if a.files:
        files = [l.strip() for l in open(a.files) if l.strip()]
    else:
        files = sorted(p for d in a.dirs for p in glob.glob(os.path.join(d, "*_iq*.wav")) if os.path.exists(p[:-4] + ".json"))
    done = set(); fields = None
    donelog = a.out + ".done"
    if os.path.exists(donelog):
        done = set(open(donelog).read().split())
    files = [f for f in files if f not in done]
    print(f"{len(files)} captures to do ({len(done)} done)", flush=True)
    if os.path.exists(a.out) and os.path.getsize(a.out) > 0:
        with open(a.out) as fh:
            fields = csv.DictReader(fh).fieldnames
    w = None; n = 0
    with open(a.out, "a", newline="") as fh, open(donelog, "a") as dl, mp.Pool(a.workers) as pool:
        if fields:
            w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        for path, rows in zip(files, pool.imap(safe, [(f, a.series_dir) for f in files], chunksize=2)):
            for r in rows:
                if w is None:
                    w = csv.DictWriter(fh, fieldnames=list(r.keys()), extrasaction="ignore"); w.writeheader()
                w.writerow(r)
            dl.write(path + "\n")
            n += 1
            if n % 100 == 0:
                fh.flush(); dl.flush(); print(f"{n}/{len(files)}", flush=True)
    print("done", flush=True)


if __name__ == "__main__":
    main()
