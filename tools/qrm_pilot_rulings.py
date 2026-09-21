#!/usr/bin/env python3
"""qrm_pilot_rulings.py — evaluate the QRM pilot's pre-registered reads D1-D5 (pre-reg §0) from a scored run.

    qrm_pilot_rulings.py SCORE_DIR RECORD_DIR_PARENT --out OUT_DIR

SCORE_DIR is qrm_occupancy.py's output (channels/events/frames CSV); RECORD_DIR_PARENT holds A/ B/ C/ (record.log per site).
Writes OUT_DIR/RULINGS.md + d2_strata.csv + d4_pairs.csv. It only COMPUTES the pre-registered statistics — the rulings
themselves are dev-main's (the handoff says so).

Gateway channels (pre-reg §3, cluster edges from `pat rmslist`): the 100 Hz-grid windows nearest the listed centres.
D3's generator side is a Monte-Carlo of skywave.rig_effects.QrmGenerator's process (Poisson onsets, Exp(10 s) durations
clamped 0.1-120 s, one active at a time, level ~ N(inr, spread) capped) SCORED WITH THE SAME DETECTOR as the data
(passband INR = 10log10(1 + INR_lin * keying_duty) over the noise mean, busy >= 10 dB), matched to each real channel's
busy fraction — nominal occupancy is not what the scorer reads on generated CW (PARIS keying duty 22/50).
"""
import argparse, collections, datetime as dt, glob, os, re, sys
import numpy as np, pandas as pd

GATEWAY = {   # band kHz -> gateway centres kHz (pre-reg §3 clusters; 0.5 kHz stride inside a cluster to avoid double counting)
    7097: [7084.1, 7100.1, 7100.6, 7101.1, 7101.6, 7102.1, 7102.6],
    10138: [10127.5, 10144.8, 10145.3, 10145.8, 10146.3, 10146.8],
    14095: [14091.0, 14094.9, 14095.4, 14095.9, 14096.4, 14096.9, 14097.4, 14097.9],
}
PARIS_DUTY = 22 / 50.0        # keyed-on fraction of the PARIS word (sum of "on" units; edges ignored)
UTC_OFF = -4                  # US Eastern DST, all three receivers


def gw_channels(df):
    """{(band, width): set of channel_khz (rounded 0.1)} nearest each gateway centre."""
    out = {}
    for band, cs in GATEWAY.items():
        for width in (500, 2400):
            have = np.unique(df[(df.band_khz == band) & (df.width_hz == width)].channel_khz.round(1))
            out[(band, width)] = {float(have[np.argmin(np.abs(have - c))]) for c in cs if len(have)}
    return out


def gen_sim(occ_nom, inr=10.0, spread=6.0, cap=16.0, mean_dur=10.0, n_s=400_000, seed=1):
    """Monte-Carlo the generator at 1 s frames; return (measured busy frac, busy-run lengths s, idle-run lengths s)."""
    rng = np.random.default_rng(seed)
    lam = occ_nom / (mean_dur * (1 - occ_nom))
    t = 0.0; busy = np.zeros(n_s, bool)
    while t < n_s:
        t += rng.exponential(1 / lam) if lam > 0 else n_s          # onset after an idle gap (one active at a time)
        d = min(max(rng.exponential(mean_dur), 0.1), 120.0)
        lev = min(rng.normal(inr, spread), cap)
        det = 10 * np.log10(1 + 10 ** (lev / 10) * PARIS_DUTY)
        if det >= 10.0:
            a, b = int(round(t)), int(round(t + d))
            if b > a:
                busy[a:min(b, n_s)] = True
        t += d
    return busy


def runs(b):
    e = np.flatnonzero(np.diff(b.astype(np.int8))) + 1
    bd = np.concatenate([[0], e, [len(b)]])
    L = np.diff(bd); v = b[bd[:-1]]
    return L[v], L[~v]


def match_sim(target_frac, **kw):
    """Nominal occupancy giving the SAME measured busy fraction; returns (nominal, busy p50, idle p50)."""
    lo, hi = 1e-4, 0.95
    for _ in range(18):
        mid = np.sqrt(lo * hi)
        f = gen_sim(mid, n_s=200_000, **kw).mean()
        lo, hi = (mid, hi) if f < target_frac else (lo, mid)
    nom = float(np.sqrt(lo * hi))
    b = gen_sim(nom, n_s=1_500_000, seed=2, **kw)
    bl, il = runs(b)
    return nom, float(np.median(bl)) if len(bl) else np.nan, float(np.median(il)) if len(il) else np.nan, b.mean()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("score"); ap.add_argument("rec"); ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    L = []
    P = L.append

    # ---------- D1
    P("## D1 feasibility — landed vs scheduled (record.log)\n")
    P("Scheduled per site (nominal, 168.0 h): 9 waterfall dwells/h (3 cycles × 3 bands) = 1512, and 6 IQ captures/h = 1008.\n")
    P("| site | wf dwells landed | of 1512 | IQ landed | of 1008 | wf % (bar 90) | IQ % (bar 80) | pass |\n|---|---|---|---|---|---|---|---|")
    d1 = {}
    for s in "ABC":
        txt = open(os.path.join(a.rec, s, "record.log")).read()
        wf = len(re.findall(r"frames over", txt)); iq = len(re.findall(r"iq .* s @", txt))
        first = re.search(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", txt, re.M).group(1)
        last = re.findall(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", txt, re.M)[-1]
        hrs = (dt.datetime.fromisoformat(last) - dt.datetime.fromisoformat(first)).total_seconds() / 3600
        swf, siq = 9 * 168, 6 * 168
        d1[s] = (wf, iq, hrs)
        P(f"| {s} | {wf} | {swf} | {iq} | {siq} | {100*wf/swf:.1f} | {100*iq/siq:.1f} | {'PASS' if wf/swf >= .9 and iq/siq >= .8 else 'FAIL'} |")
    P("")

    # ---------- load scored data
    ch = pd.read_csv(os.path.join(a.score, "channels.csv"))
    gw = gw_channels(ch)
    fr = pd.read_csv(os.path.join(a.score, "frames.csv"), usecols=["site", "band_khz", "utc"])
    fr["t"] = pd.to_datetime(fr.utc, utc=True)
    fr["hour"] = fr.t.dt.floor("h")
    ev_parts = []
    for c in pd.read_csv(os.path.join(a.score, "events.csv"), chunksize=2_000_000,
                         usecols=["site", "band_khz", "width_hz", "channel_khz", "start_utc", "len_s", "censored", "level_db"]):
        keep = np.zeros(len(c), bool)
        for (band, width), chs in gw.items():
            keep |= ((c.band_khz == band) & (c.width_hz == width) & c.channel_khz.round(1).isin(chs)).values
        ev_parts.append(c[keep])
    ev = pd.concat(ev_parts, ignore_index=True)
    ev["t"] = pd.to_datetime(ev.start_utc, utc=True)
    ev["hour"] = ev.t.dt.floor("h")
    ev["chan"] = ev.channel_khz.round(1)
    frames_h = fr.groupby(["site", "band_khz", "hour"]).size().rename("frames").reset_index()

    # ---------- D5
    P("## D5 level tail — detected-but-not-busy / detected, gateway channels (bar 20 %)\n")
    P("| width | sites | frames | detect frac | busy frac | **D5 tail** |\n|---|---|---|---|---|---|")
    d5 = {}
    for width in (500, 2400):
        for label, sites in (("all", ["coventryOH", "northernneckVA", "youngsvilleNC"]),
                             ("A+B (C excluded: waterfall noise shape 3.0-3.2 dB ≠ 3.8, floor unvalidated)", ["coventryOH", "northernneckVA"]),
                             ("A", ["coventryOH"]), ("B", ["northernneckVA"]), ("C", ["youngsvilleNC"])):
            sel = ch[(ch.block == "all") & (ch.width_hz == width) & ch.site.isin(sites)]
            m = np.zeros(len(sel), bool)
            for band in GATEWAY:
                m |= ((sel.band_khz == band) & sel.channel_khz.round(1).isin(gw[(band, width)])).values
            sel = sel[m]
            n = sel.frames.sum(); det = (sel.detect_frac * sel.frames).sum(); bz = (sel.busy_frac * sel.frames).sum()
            tail = 1 - bz / det if det else np.nan
            d5[(width, label)] = tail
            P(f"| {width} | {label} | {n:,.0f} | {det/n:.3f} | {bz/n:.3f} | **{tail:.3f}** |")
    P("")

    # ---------- D2/D4 per (site, band, channel, hour) busy fraction
    busy_h = (ev.groupby(["site", "band_khz", "width_hz", "chan", "hour"]).len_s.sum().rename("busy_s").reset_index())
    # full grid of (site, band, channel, hour) with frames>0
    rows = []
    for (band, width), chs in gw.items():
        for c in chs:
            g = frames_h[frames_h.band_khz == band].copy(); g["width_hz"] = width; g["chan"] = c
            rows.append(g)
    grid = pd.concat(rows, ignore_index=True)
    grid = grid.merge(busy_h, on=["site", "band_khz", "width_hz", "chan", "hour"], how="left").fillna({"busy_s": 0})
    grid["bf"] = (grid.busy_s / grid.frames).clip(upper=1.0)
    loc = grid.hour + pd.Timedelta(hours=UTC_OFF)
    grid["block"] = (loc.dt.hour // 4) * 4
    grid["day"] = loc.dt.floor("D")
    grid["daytype"] = np.where(loc.dt.weekday >= 5, "weekend", "weekday")
    grid = grid[grid.frames >= 200]                                      # a real dwell (300 nominal)

    # D2: day-to-day sd of the block-mean busy fraction per (site, band, width, channel, block, daytype)
    P("## D2 campaign length — day-to-day σ of busy fraction per stratum (n = (σ/0.05)² days; bar: > 8 weeks ⇒ drop/merge)\n")
    dayblk = grid.groupby(["site", "band_khz", "width_hz", "chan", "block", "daytype", "day"]).bf.mean().rename("bf").reset_index()
    st = dayblk.groupby(["site", "band_khz", "width_hz", "chan", "block", "daytype"]).bf.agg(["mean", "std", "count"]).reset_index()
    st = st[st["count"] >= 2]
    st["n_days"] = (st["std"] / 0.05) ** 2
    st["weeks"] = st.n_days / np.where(st.daytype == "weekday", 5, 2)
    st.to_csv(os.path.join(a.out, "d2_strata.csv"), index=False)
    for width in (500, 2400):
        s = st[st.width_hz == width]
        P(f"**{width} Hz windows** — {len(s)} strata with ≥2 days: σ p50/p90/max = "
          f"{s['std'].median():.3f}/{s['std'].quantile(.9):.3f}/{s['std'].max():.3f}; weeks needed p50/p90/max = "
          f"{s.weeks.median():.2f}/{s.weeks.quantile(.9):.2f}/{s.weeks.max():.1f}; strata needing > 8 weeks: "
          f"{int((s.weeks > 8).sum())} ({100*(s.weeks > 8).mean():.1f} %); needing > 4 weeks: {int((s.weeks > 4).sum())} "
          f"({100*(s.weeks > 4).mean():.1f} %)\n")
    P("| width | band | daytype | strata | σ p50 | σ p90 | weeks p50 | weeks p90 | > 8 wk |\n|---|---|---|---|---|---|---|---|---|")
    for (w, b, d), g in st.groupby(["width_hz", "band_khz", "daytype"]):
        P(f"| {w} | {b} | {d} | {len(g)} | {g['std'].median():.3f} | {g['std'].quantile(.9):.3f} | {g.weeks.median():.2f} | "
          f"{g.weeks.quantile(.9):.2f} | {int((g.weeks > 8).sum())} |")
    P("\nWeekday strata have 5 samples/week, weekend 2 (the pilot had ONE weekend: 09-12/13 local); σ from ≤ 5 (weekday) or "
      "2 (weekend) daily values is itself very noisy — treat weekend rows as indicative only.\n")

    # D4: cross-site correlation of per-dwell busy fraction (same channel, same band, same UTC hour)
    P("## D4 site axis — cross-site correlation of busy fraction per channel-hour, gateway channels (bar: r < 0.5 ⇒ SITE axis needed)\n")
    P("| width | pair | channel-hours | Pearson r | Spearman ρ | r on channels with mean busy > 0.02 |\n|---|---|---|---|---|---|")
    pv = grid.pivot_table(index=["band_khz", "width_hz", "chan", "hour"], columns="site", values="bf")
    pairs = [("coventryOH", "northernneckVA"), ("coventryOH", "youngsvilleNC"), ("northernneckVA", "youngsvilleNC")]
    d4rows = []
    for width in (500, 2400):
        pw = pv.xs(width, level="width_hz")
        for x, y in pairs:
            d = pw[[x, y]].dropna()
            act = d.groupby(level=["band_khz", "chan"]).transform("mean")
            da = d[(act[x] > 0.02) | (act[y] > 0.02)]
            r = d[x].corr(d[y]); rho = d[x].corr(d[y], method="spearman"); ra = da[x].corr(da[y])
            P(f"| {width} | {x[:4]}–{y[:4]} | {len(d):,} | {r:.3f} | {rho:.3f} | {ra:.3f} |")
            d4rows.append((width, x, y, len(d), r, rho, ra))
    pd.DataFrame(d4rows, columns=["width", "a", "b", "n", "pearson", "spearman", "pearson_active"]).to_csv(
        os.path.join(a.out, "d4_pairs.csv"), index=False)
    P("\nPer band (2400 Hz windows, Pearson):\n\n| band | A–B | A–C | B–C |\n|---|---|---|---|")
    pw = pv.xs(2400, level="width_hz")
    for band in GATEWAY:
        p = pw.xs(band, level="band_khz")
        P(f"| {band} | " + " | ".join(f"{p[[x, y]].dropna()[x].corr(p[[x, y]].dropna()[y]):.3f}" for x, y in pairs) + " |")
    P("")

    # ---------- D3
    P("## D3 model regime — measured busy-run length vs the Poisson-CW generator at the SAME measured busy fraction\n")
    P("Measured: per (site, band, gateway channel), pooled all strata; busy fraction from channels.csv 'all' rows; run p50 from "
      "events.csv (1 s frames; runs censored at the 5 min dwell counted at their censored length). Generator: Monte-Carlo, same "
      "detector, nominal occupancy solved so the MEASURED busy fraction matches (nominal → scorer-read factor shown).\n")
    P("| width | site | band | channel | measured busy | runs | meas busy-run p50 s | gen busy-run p50 s | ratio | meas idle-run p50 s | gen idle-run p50 s | idle ratio | nominal occ needed |\n|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    d3 = []
    cache = {}
    allrows = ch[ch.block == "all"]
    for width in (2400, 500):
        for (site, band, c), g in ev[ev.width_hz == width].groupby(["site", "band_khz", "chan"]):
            row = allrows[(allrows.site == site) & (allrows.band_khz == band) & (allrows.width_hz == width)
                          & (allrows.channel_khz.round(1) == c)]
            if row.empty:
                continue
            bf = float(row.busy_frac.iloc[0]); n = len(g)
            if bf < 0.005 or n < 100:
                continue
            key = round(bf, 3)
            if key not in cache:
                cache[key] = match_sim(bf)
            nom, gb, gi, gf = cache[key]
            mb = float(g.len_s.median()); mi = float(row.idle_run_p50_s.iloc[0])
            d3.append((width, site, band, c, bf, n, mb, gb, mb / gb, mi, gi, mi / gi, nom))
            P(f"| {width} | {site[:5]} | {band} | {c} | {bf:.3f} | {n} | {mb:.1f} | {gb:.1f} | {mb/gb:.2f} | {mi:.1f} | {gi:.1f} | {mi/gi:.2f} | {nom:.3f} |")
    d3 = pd.DataFrame(d3, columns=["width", "site", "band", "chan", "bf", "n", "mb", "gb", "rb", "mi", "gi", "ri", "nom"])
    P("")
    for width in (2400, 500):
        s = d3[d3.width == width]
        if len(s):
            P(f"**{width} Hz:** {len(s)} channel-rows; busy-run ratio measured/generator median {s.rb.median():.2f} "
              f"(p10 {s.rb.quantile(.1):.2f}, p90 {s.rb.quantile(.9):.2f}); idle-run ratio median {s.ri.median():.2f}; "
              f"nominal occupancy the generator needs to READ the measured busy fraction: median {s.nom.median():.3f} vs measured "
              f"{s.bf.median():.3f} (×{(s.nom/s.bf).median():.1f}).\n")
    d3.to_csv(os.path.join(a.out, "d3_rows.csv"), index=False)
    open(os.path.join(a.out, "RULINGS.md"), "w").write("# QRM pilot — pre-registered reads D1–D5 (computed, not ruled)\n\n" + "\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
