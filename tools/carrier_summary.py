#!/usr/bin/env python3
"""carrier_summary.py tones.csv — summarise carrier_probe.py output per tone (station x 10 Hz), equal weight per tone."""
import sys, numpy as np, pandas as pd

pd.set_option("display.width", 250); pd.set_option("display.max_columns", 40); pd.set_option("display.max_rows", 200)
d = pd.read_csv(sys.argv[1])
d["tone_id"] = d.station + "@" + (d.tone_khz * 100).round().div(100).map(lambda x: f"{x:.2f}")
print(f"captures with >=1 line: {d.file.nunique()}, line rows: {len(d)}")
print("\nclass counts (rows / distinct tones):")
print(pd.DataFrame({"rows": d.cls.value_counts(), "tones": d.groupby("cls").tone_id.nunique()}))

LAG = ["d0.5", "d1", "d2", "d5", "d10", "d20", "d30", "d60", "d90"]
SLOW = ["slow10", "slow20", "slow30", "slow60", "slow90"]
COLS = (["snr_nb_db", "frac_in_nb", "censored", "K", "tcoh_s", "doppler_2sigma_hz", "drift_hz_per_min",
         "fade_p10", "fade_p1", "win12p6_p10_med", "lcr6_per_min", "lcr10_per_min", "fade6_mean_s", "fade10_mean_s",
         "slow_range_db"] + [f"{l}_p50" for l in LAG] + [f"{l}_p90" for l in LAG]
        + [f"{s}_p50" for s in SLOW] + [f"{s}_p90" for s in SLOW])


def per_tone(x):
    g = x.groupby("tone_id")
    t = g[COLS].median()
    t["captures"] = g.size()
    t["band"] = g.band.first(); t["station"] = g.station.first()
    return t


for cls in ("sky", "local"):
    x = d[d.cls == cls]
    if x.empty:
        continue
    t = per_tone(x)
    print(f"\n==== {cls}: {len(t)} tones, {len(x)} captures; captures/tone p50 {t.captures.median():.0f}, max {t.captures.max()}")
    print("tones by band x station:"); print(t.groupby(["band", "station"]).size().unstack(fill_value=0))
    q = lambda c: f"{t[c].quantile(.1):.2f} / {t[c].median():.2f} / {t[c].quantile(.9):.2f}"
    for c in ["snr_nb_db", "frac_in_nb", "censored", "K", "tcoh_s", "doppler_2sigma_hz", "drift_hz_per_min", "fade_p10",
              "fade_p1", "win12p6_p10_med", "lcr10_per_min", "fade10_mean_s", "slow_range_db"]:
        print(f"  {c:20s} p10/p50/p90 over tones: {q(c)}")
    print("  |delta level| vs lag (0.5 s grid; per-tone median of per-capture p50 and p90), tone p50 [p10..p90]:")
    for l in LAG:
        print(f"    {l[1:]:>4s} s : p50 {t[l + '_p50'].median():5.2f} [{t[l + '_p50'].quantile(.1):.2f}..{t[l + '_p50'].quantile(.9):.2f}]"
              f"   p90 {t[l + '_p90'].median():5.2f} [{t[l + '_p90'].quantile(.1):.2f}..{t[l + '_p90'].quantile(.9):.2f}]")
    print("  SLOW envelope (10 s mean) |delta| vs lag:")
    for s in SLOW:
        print(f"    {s[4:]:>4s} s : p50 {t[s + '_p50'].median():5.2f} [{t[s + '_p50'].quantile(.1):.2f}..{t[s + '_p50'].quantile(.9):.2f}]"
              f"   p90 {t[s + '_p90'].median():5.2f} [{t[s + '_p90'].quantile(.1):.2f}..{t[s + '_p90'].quantile(.9):.2f}]")
    if cls == "sky":
        print("  by band (tone medians):")
        print(t.groupby("band")[["tcoh_s", "doppler_2sigma_hz", "fade_p10", "win12p6_p10_med", "d10_p90", "d60_p90", "slow60_p50",
                                 "slow60_p90", "slow_range_db"]].median().round(2).assign(tones=t.groupby("band").size()))
        t.to_csv("sky_tones.csv")
