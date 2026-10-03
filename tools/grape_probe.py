#!/usr/bin/env python3
"""grape_probe.py — HamSCI Grape-1 (AD8Y, Cleveland Hts OH, EN91fl) CHU 7.85 / WWV 10 MHz carrier records as
channel probes: 1 s level (fldigi Vpk, receiver with no AGC claimed — checked here) + Doppler.

Rules (fixed before reading results, 2026-10-02):
 - level L = 20 log10(Vpk) per second; NOISE per day = the 1st percentile of L (the carrier fades into the noise
   at some hour every day on these paths; if it never does, the estimate is high and fades read shallower).
 - AGC check: daily L range p99 - p1. A receiver with AGC compresses this to a few dB.
 - 2-min WINDOWS (aligned to the UTC even minute, like WSPR slots): used when every 1 s sample is >= noise + 10 dB
   and the window has no gap. GRAPE_RULE=relaxed (sensitivity arm, added after the first read — the strict rule drops
   every window with a deep fade): median >= noise + 15 dB, samples clipped at noise + 3 dB. Per window, the WSPR-IQ statistics at 1 s resolution: 10 s-mean |delta| at 10/30/60/90 s,
   in-window range of the 10 s mean, |delta| of the raw 1 s level at 1/2/5 s, envelope decorrelation time (power
   autocovariance falls to 1/e of its 1 s value), 12 s-window p10 re window mean.
 - LONG lags: 1-min mean level (minutes with >= 50 samples >= noise + 6 dB), |delta| at 2/10/30/60 min.
 - Doppler: per window, std of Freq about its linear trend (spread proxy, includes fldigi estimator noise) and trend Hz/min.
CAVEATS: one receiver, one path per station (CHU Ottawa -> Cleveland ~ 550 km; WWV Fort Collins -> Cleveland ~ 1800 km,
with WWVH co-channel on 10 MHz: two-station beating reads as fading); fldigi's Vpk filter bandwidth is undocumented.
"""
import sys, glob, os, numpy as np, pandas as pd


def load(path):
    """two formats: new (# metadata, 'UTC,Freq,Vpk' with ISO stamps) and old 2020 (a date line, then
    'UTC,Freq,Freq Err,Vpk,dBV(Vpk)' with time-only stamps)."""
    lines = [l for l in open(path) if not l.startswith("#")]
    hdr = next(i for i, l in enumerate(lines) if l.startswith("UTC,"))
    date = lines[0].split(",")[0].strip() if hdr == 1 else None
    from io import StringIO
    d = pd.read_csv(StringIO("".join(lines[hdr:])), skipinitialspace=True)
    d.columns = [c.strip() for c in d.columns]
    stamp = d.UTC.astype(str).str.strip()
    d["t"] = pd.to_datetime((date + "T" + stamp + "Z") if date else stamp, utc=True, errors="coerce")
    d = d.dropna(subset=["t"])
    d = d[d.Vpk > 0].set_index("t").sort_index()
    d = d[~d.index.duplicated(keep="first")]          # repeated stamps occur in the fldigi logs
    d["L"] = 20 * np.log10(d.Vpk)
    return d


def window_stats(L, F):
    p = 10 ** (L / 10)
    out = {}
    m10 = np.convolve(p, np.ones(10) / 10, mode="valid"); s10 = 10 * np.log10(m10)
    for lag in (10, 30, 60, 90):
        if lag < len(s10):
            dd = np.abs(s10[lag:] - s10[:-lag]); out[f"slow{lag}_p50"] = np.median(dd); out[f"slow{lag}_p90"] = np.percentile(dd, 90)
    out["slow_range"] = s10.max() - s10.min()
    for lag in (1, 2, 5):
        dd = np.abs(L[lag:] - L[:-lag]); out[f"d{lag}_p50"] = np.median(dd); out[f"d{lag}_p90"] = np.percentile(dd, 90)
    y = p - p.mean(); n = len(y)
    ac = np.fft.ifft(np.abs(np.fft.fft(y, 2 * n)) ** 2)[:n].real
    b = np.flatnonzero(ac[1:] < ac[1] / np.e) if ac[1] > 0 else []
    out["tenv_s"] = float(b[0] + 1) if len(b) else float(n)
    vals = [np.percentile(10 * np.log10(p[i:i + 12] / p[i:i + 12].mean()), 10) for i in range(0, n - 11, 12)]
    out["win12_p10"] = np.median(vals)
    t = np.arange(n); c = np.polyfit(t, F, 1)
    out["dop_std_hz"] = np.std(F - np.polyval(c, t)); out["drift_hz_min"] = c[0] * 60
    return out


def main():
    rows, days, longs = [], [], []
    for path in sorted(glob.glob(sys.argv[1])):
        tag = os.path.basename(path).split("_")[0]
        d = load(path)
        if len(d) < 40000:
            continue
        noise = np.percentile(d.L, 1)
        days.append(dict(tag=tag, file=os.path.basename(path), n=len(d), noise_db=noise, p50_db=np.median(d.L),
                         range_db=np.percentile(d.L, 99) - np.percentile(d.L, 1),
                         above10=float((d.L >= noise + 10).mean())))
        # 2-min windows
        for t0, gg in d.groupby(d.index.floor("2min")):
            g = gg.L
            if os.environ.get("GRAPE_RULE") == "relaxed":
                # sensitivity arm: keep fading windows; samples within 3 dB of the noise are CLIPPED (censored low)
                if len(g) < 118 or np.median(g) < noise + 15:
                    continue
                g = g.clip(lower=noise + 3)
            elif len(g) < 118 or (g < noise + 10).any():
                continue
            st = window_stats(g.values.astype(float), gg.Freq.values.astype(float))
            st.update(tag=tag, t0=t0, snr_db=float(np.median(g) - noise))
            rows.append(st)
        # long lags on 1-min means
        good = d[d.L >= noise + 6]
        m = good.L.resample("1min").agg(["mean", "count"])
        m = m[m["count"] >= 50]["mean"]
        m = m.reindex(pd.date_range(d.index[0].floor("1min"), d.index[-1].floor("1min"), freq="1min"))
        for lag in (2, 10, 30, 60):
            dd = (m.shift(-lag) - m).abs().dropna()
            longs.append(dict(tag=tag, lag_min=lag, n=len(dd), p50=dd.median(), p90=dd.quantile(.9)))
    D = pd.DataFrame(days); W = pd.DataFrame(rows); G = pd.DataFrame(longs)
    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)
    print("== per-day (AGC check: range_db; noise = p1 of the day's level)")
    print(D.groupby("tag")[["n", "noise_db", "p50_db", "range_db", "above10"]].describe(percentiles=[.5]).round(2).T.to_string())
    print("\n== 2-min windows with every sample >= noise + 10 dB: count", W.groupby("tag").size().to_dict())
    W["local_h"] = (W.t0.dt.hour - 4) % 24
    cols = ["snr_db", "win12_p10", "d1_p90", "d5_p90", "slow10_p50", "slow10_p90", "slow30_p50", "slow30_p90", "slow60_p50", "slow60_p90",
            "slow90_p90", "slow_range", "tenv_s", "dop_std_hz", "drift_hz_min"]
    for tag, g in W.groupby("tag"):
        print(f"\n-- {tag}: {len(g)} windows on {g.t0.dt.date.nunique()} days; p10 / p50 / p90 across windows")
        for c in cols:
            print(f"   {c:14s} {g[c].quantile(.1):7.2f} {g[c].median():7.2f} {g[c].quantile(.9):7.2f}")
        g = g.assign(tbin=pd.cut(g.tenv_s, [0, 1.5, 3, 5, 9, 200]))
        print("   by envelope decorrelation (Rayleigh ref s30p50 @tenv 1.0/2.2/4.0/7.0 s = 1.65/2.50/3.29/4.39):")
        print(g.groupby("tbin", observed=True)[["tenv_s", "slow30_p50", "slow30_p90", "slow_range"]].median().round(2)
              .assign(n=g.groupby("tbin", observed=True).size()).to_string())
        print("   by local hour block (medians):")
        g["blk"] = (g.local_h // 4) * 4
        print(g.groupby("blk")[["snr_db", "slow30_p50", "slow30_p90", "slow_range", "tenv_s", "dop_std_hz"]].median().round(2)
              .assign(n=g.groupby("blk").size()).to_string())
    print("\n== long lags on 1-min means (pooled over days): |delta| p50 / p90 dB")
    print(G.groupby(["tag", "lag_min"])[["p50", "p90"]].median().round(2).assign(n=G.groupby(["tag", "lag_min"]).n.sum()).to_string())
    sfx = "_relaxed" if os.environ.get("GRAPE_RULE") == "relaxed" else ""
    W.to_csv(f"grape_windows{sfx}.csv", index=False); D.to_csv("grape_days.csv", index=False)


if __name__ == "__main__":
    main()
