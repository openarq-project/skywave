#!/usr/bin/env python3
"""wspr_summary.py wspr.csv — summarise accepted WSPR transmissions (one row = one station-slot)."""
import sys, numpy as np, pandas as pd

pd.set_option("display.width", 250); pd.set_option("display.max_columns", 40)
d = pd.read_csv(sys.argv[1])
acc = d[d.accepted == 1].copy()
print(f"captures with candidates {d.file.nunique()}, candidates {len(d)}, ACCEPTED {len(acc)} transmissions "
      f"in {acc.file.nunique()} captures; rejects: snr<10 {int((d.snr7_db < 10).sum())}, width {int((d.frac_in_win < 0.7).sum())}, "
      f"not slot-gated {int((pd.to_numeric(d.gate_db, errors='coerce') > -6).sum())}, no edge in capture {int(d.gate_db.isna().sum())}")
e = pd.to_numeric(acc.edge_err_s, errors="coerce").dropna()
print(f"slot-edge timing (observed switch - expected): n {len(e)}, p10/p50/p90 {e.quantile(.1):.2f}/{e.median():.2f}/{e.quantile(.9):.2f} s")
print("by station:", acc.station.value_counts().to_dict())
t = pd.to_datetime(acc.start_utc)
acc["local_h"] = (t.dt.hour - 4) % 24
acc["period"] = np.where(acc.local_h.between(8, 17), "day 08-18", np.where(acc.local_h.between(18, 21), "eve 18-22", "night 22-08"))
acc["snr_bin"] = pd.cut(acc.snr7_db, [10, 13, 16, 20, 25, 99], right=False)
acc["slot_len"] = pd.cut(acc.in_slot_s, [40, 60, 80, 120], right=False)

COLS = ["fade_p10", "fade_p1", "win12p6_p10_med", "d0.5_p90", "d2_p90", "d10_p90", "slow10_p50", "slow10_p90", "slow30_p50",
        "slow30_p90", "slow60_p50", "slow60_p90", "slow90_p90", "slow_range_db", "lcr10_per_min", "fade10_mean_s", "K", "tenv_s", "drift_hz_per_min"]
q = lambda s: f"{s.quantile(.1):6.2f} {s.median():6.2f} {s.quantile(.9):6.2f}  (n {s.notna().sum()})"
print("\nALL accepted, p10 / p50 / p90 across transmissions:")
for c in ["snr7_db"] + COLS:
    print(f"  {c:18s} {q(acc[c])}")
print("\nby SNR bin (medians) — compare with the selftest floor (constant signal): fade_p10 -2.2/-1.6/-1.1/-0.7/-0.5,"
      " slow30_p90 0.5/0.45/0.26/0.17/0.11, d10_p90 3.7/2.6/1.9/1.3/0.9 at SNR 10/13/16/20/25 dB")
print(acc.groupby("snr_bin", observed=True)[["fade_p10", "win12p6_p10_med", "d10_p90", "slow30_p50", "slow30_p90", "slow60_p90", "slow_range_db", "tenv_s"]]
      .median().round(2).assign(n=acc.groupby("snr_bin", observed=True).size()))
print("\nby local time period (SNR >= 13 dB only, medians):")
h = acc[acc.snr7_db >= 13]
print(h.groupby("period")[["fade_p10", "win12p6_p10_med", "d10_p90", "slow30_p50", "slow30_p90", "slow60_p50", "slow60_p90", "slow_range_db", "tenv_s"]]
      .median().round(2).assign(n=h.groupby("period").size()))
print("\nby station (SNR >= 13 dB, medians):")
print(h.groupby("station")[["fade_p10", "win12p6_p10_med", "slow30_p90", "slow60_p90", "slow_range_db", "tenv_s"]].median().round(2).assign(n=h.groupby("station").size()))
print("\nslow-envelope |delta| vs lag, SNR >= 13 dB, p50-of-p50 / p50-of-p90 / p90-of-p90:")
for l in (10, 20, 30, 60, 90):
    a, b = h[f"slow{l}_p50"], h[f"slow{l}_p90"]
    print(f"  {l:3d} s: {a.median():5.2f} / {b.median():5.2f} / {b.quantile(.9):5.2f}   (n {b.notna().sum()})")
acc.to_csv("wspr_accepted.csv", index=False)
