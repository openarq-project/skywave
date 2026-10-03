#!/usr/bin/env python3
"""qrm_bench_select.py — pick a varied, real-world QRM replay bench from qrm_bench_features.py output.

    qrm_bench_select.py features.csv --out-dir DIR [--per 8]

Writes DIR/manifest.json ({stratum: [{file, dir, dial_hz, ...}]}), DIR/selected.csv and DIR/STRATA.md.
Deterministic: same features.csv ⇒ same bench. Re-run after each IQ mirror to re-cut.

Strata (definitions are the contract; the LBT bench's four are reused verbatim where they overlap):
  quiet      inr_p90 < 1 dB and no persistent tone (carrier_frac < 0.3)                    [LBT 'quiet']
  hidden     detect3 >= 0.99 and busy10 < 0.02 (the D5 +3..+10 dB population)               [LBT 'hidden', 100 % -> 99 %]
  busy_gw    Winlink gateway-cluster slice, 0.10 <= busy10 <= 0.73, not a contest day       [LBT 'busy' range]
  busy_park  parking slice (where an armstrong session would sit), same busy range, not FT8, not contest
  dense      busy10 > 0.73, not FT8, not contest
             (busy_gw/busy_park/dense also exclude slices whose busy-ness IS a persistent line — carrier_frac >= 0.6
             and >= half the busy seconds <= 100 Hz wide; those
             belong to 'carrier'; a steady two-line signal at 14097.1 kHz heard at ALL sites otherwise dominates;
             and slices that meet the 'broad' rule — wideband humps such as pilot site B's 40 m noise state, ruled a
             site-level term, not traffic — so busy_* / dense / broad are disjoint). Pilot site B (northernneckVA)
             40 m is excluded from busy_* / dense outright: OWNER-RULINGS D2 ruled it one lockstep wideband source,
             a site noise state, and its hump covers too little of the slice for the width rule to catch.
             busy_gw / busy_park use the strict tone test (carrier_frac < 0.6); dense keeps the narrow one, since
             any near-continuously busy slice lights some 25 Hz group most of the time.
  carrier    a steady narrow tone: one 25 Hz group >= +10 dB in >= 80 % of seconds, neighbours quiet,
             busy10 < 0.30 (the tone, not a pile-up), tone 150-2850 Hz inside the slice (in a modem's passband) — the real-world counterpart of the v2 CW wrongness cell
  ft8        the 10135-10138 kHz slice (FT8 at 10136), busy10 >= 0.30 — 15 s slot cadence
  contest    CQ WW RTTY weekend (UTC 09-26/27), busy10 >= 0.10, not FT8
  broad      events wider than the slice: bw_broad >= 0.2 or wide_frac >= 0.1, with busy10 >= 0.05, not FT8
Exclusions (all strata): ADC-overload flag (rate > max(2000/h, 3 x the station's median)); peak_sigma above the rail cap (default 40, i.e. a 60 sigma QrmReplay bound).
Carrier picks: at most 2 per 500 Hz tone bin (a steady two-line signal at 14097.1 kHz is heard at every site).
Picking: candidates are spread round-robin over (station, band), then by date, taking in each group the
capture closest to the stratum's median of its key statistic (typical, not extreme); no capture is used twice.
"""
import argparse, json, os
import numpy as np, pandas as pd

GW = {7101.9, 10146.4, 14097.0}


def adc_flags(d, per_hour=2000.0):
    """ADC overload: the sidecar's adc_ov is a receiver-wide cumulative counter read at capture start.
    Returns the counter's rate (/h) to the NEXT capture of that station; the caller flags a capture whose rate
    exceeds max(per_hour, 3 x that station's median) — site A's receiver overloads CHRONICALLY (~60 k/h median,
    132 ft end-fed), so an absolute bar would drop the whole site; the flag marks hours anomalous for THAT receiver."""
    f = d.drop_duplicates("file")[["file", "station", "start_utc", "adc_ov"]].copy()
    f["t"] = pd.to_datetime(f.start_utc)
    f["ov"] = pd.to_numeric(f.adc_ov, errors="coerce")
    f = f.sort_values(["station", "t"])
    f["rate"] = (f.groupby("station").ov.shift(-1) - f.ov) / (
        (f.groupby("station").t.shift(-1) - f.t).dt.total_seconds() / 3600)
    f.loc[f.rate < 0, "rate"] = np.nan                      # counter reset (receiver reboot)
    return f.set_index("file").rate


STRATA = [
    ("quiet", "inr_p90", lambda d: (d.inr_p90 < 1.0) & (d.carrier_frac < 0.3)),
    ("hidden", "detect3", lambda d: (d.detect3 >= 0.99) & (d.busy10 < 0.02)),
    ("busy_gw", "busy10", lambda d: d.role.eq("gw") & d.busy10.between(0.10, 0.73) & d.contest.eq(0)
     & (d.carrier_frac < 0.6) & ~d.hump & ~d.b40),
    ("busy_park", "busy10", lambda d: d.role.eq("park") & ~d.ft8 & d.busy10.between(0.10, 0.73) & d.contest.eq(0)
     & (d.carrier_frac < 0.6) & ~d.hump & ~d.b40),
    ("dense", "busy10", lambda d: ~d.ft8 & (d.busy10 > 0.73) & d.contest.eq(0) & ~d.tone & ~d.hump & ~d.b40),
    ("carrier", "carrier_frac", lambda d: (d.carrier_frac >= 0.8) & d.carrier_narrow.eq(1) & (d.busy10 < 0.30)
     & d.tone_pos.between(150, 2850)),
    ("ft8", "busy10", lambda d: d.ft8 & (d.busy10 >= 0.30)),
    ("contest", "busy10", lambda d: d.contest.eq(1) & ~d.ft8 & (d.busy10 >= 0.10)),
    ("broad", "wide_frac", lambda d: ((d.bw_broad >= 0.2) | (d.wide_frac >= 0.1)) & (d.busy10 >= 0.05) & ~d.ft8),
]


def pick(c, key, n, used, div=None, div_max=2):
    """div: optional column; at most div_max picks share a value (e.g. one tone frequency heard at every site)."""
    c = c[~c.file.isin(used)].copy()
    if c.empty:
        return c
    med = c[key].median()
    c["dist"] = (c[key] - med).abs()
    c["day"] = c.start_utc.str[:10]
    c = c.sort_values(["dist", "file"])
    groups = [g for _, g in c.groupby(["station", "band"], sort=True)]
    out, seen_files, seen_days, divs = [], set(), set(), []
    while len(out) < n and any(len(g) for g in groups):
        for i, g in enumerate(groups):
            if len(out) >= n or g.empty:
                continue
            if div is not None:
                full = [v for v in set(divs) if divs.count(v) >= div_max]
                g = g[~g[div].isin(full)]; groups[i] = g
            fresh = g[~g.day.isin(seen_days) & ~g.file.isin(seen_files)]
            row = (fresh if len(fresh) else g[~g.file.isin(seen_files)]).head(1)
            if row.empty:
                groups[i] = g.iloc[0:0]; continue
            out.append(row); seen_files.add(row.file.iloc[0]); seen_days.add(row.day.iloc[0])
            if div is not None:
                divs.append(row[div].iloc[0])
            groups[i] = g[g.file != row.file.iloc[0]]
    return pd.concat(out) if out else c.iloc[0:0]


def prepare(d, adc_per_hour=2000.0):
    """The derived columns every stratum rule reads, plus the ADC-overload flag (shared with qrm_scenario_select)."""
    d = d.copy()
    d["ft8"] = (d.centre_khz == 10133.0) & (d.dial_hz == 2000)
    d["tone"] = (d.carrier_frac >= 0.6) & (d.bw_carrier >= 0.5)   # a persistent line that IS the busy-ness (the 14097.1 kHz pair)
    d["b40"] = d.station.eq("northernneckVA") & d.band.eq("40m")    # OWNER-RULINGS D2: a site noise state, not traffic
    d["hump"] = (d.wide_frac >= 0.1) | (d.bw_broad >= 0.2)          # wider than the slice: belongs to 'broad'
    d["tone_bin"] = (d.carrier_khz * 2).round() / 2                 # 500 Hz tone bins, for carrier diversity
    d["tone_pos"] = (d.carrier_khz - d.centre_khz) * 1000 - d.dial_hz   # Hz inside the slice
    d["adc_rate"] = d.file.map(adc_flags(d, adc_per_hour))
    med = d.drop_duplicates("file").groupby("station").adc_rate.median()
    d["adc_flag"] = d.adc_rate > np.maximum(adc_per_hour, 3 * d.station.map(med))
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("features"); ap.add_argument("--out-dir", required=True)
    ap.add_argument("--per", type=int, default=8); ap.add_argument("--peak-cap", type=float, default=40.0)
    ap.add_argument("--adc-per-hour", type=float, default=2000.0)
    a = ap.parse_args()
    os.makedirs(a.out_dir, exist_ok=True)
    d = prepare(pd.read_csv(a.features), a.adc_per_hour)
    ok = d[~d.adc_flag & (d.peak_sigma <= a.peak_cap)]
    L = [f"# QRM replay bench — strata (from {os.path.basename(a.features)}: {d.file.nunique()} captures, {len(d)} slices)\n",
         f"Excluded: ADC overload > max({a.adc_per_hour:g}/h, 3x station median) on {int(d.drop_duplicates('file').adc_flag.sum())} captures; "
         f"peak > {a.peak_cap:g} sigma on {int((d.peak_sigma > a.peak_cap).sum())} slices.\n",
         "| stratum | candidates | picked | stations | bands | busy10 p50 | inr_p90 p50 | peak sigma max | QrmReplay bound max |",
         "|---|---|---|---|---|---|---|---|---|"]
    used, man, sel = set(), {}, []
    for name, key, rule in STRATA:
        c = ok[rule(ok)]
        p = pick(c, key, a.per, used, div="tone_bin" if name == "carrier" else None)
        used |= set(p.file)
        p = p.assign(stratum=name)
        sel.append(p)
        man[name] = [dict(file=r.file, dir=r.dir, dial_hz=int(r.dial_hz), slice_khz=r.slice_khz, station=r.station,
                          start_utc=r.start_utc, busy10=r.busy10, inr_p90=r.inr_p90, peak_sigma=r.peak_sigma)
                     for r in p.itertuples()]
        L.append(f"| {name} | {len(c)} | {len(p)} | {', '.join(sorted(p.station.unique()))} | "
                 f"{', '.join(sorted(p.band.unique()))} | {p.busy10.median():.2f} | {p.inr_p90.median():.1f} | "
                 f"{p.peak_sigma.max():.1f} | {1.5 * p.peak_sigma.max():.0f} |")
    pd.concat(sel).drop(columns=["dist"], errors="ignore").to_csv(os.path.join(a.out_dir, "selected.csv"), index=False)
    json.dump(man, open(os.path.join(a.out_dir, "manifest.json"), "w"), indent=1)
    open(os.path.join(a.out_dir, "STRATA.md"), "w").write("\n".join(L) + "\n\n" + __doc__)
    print("\n".join(L))


if __name__ == "__main__":
    main()
