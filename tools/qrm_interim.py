#!/usr/bin/env python3
"""Interim read of the QRM four-week run: (1) D2 per-stratum day-to-day sigma of busy fraction on the
gateway channels, (2) week-over-week stability of the generative-fit targets (busy fraction, busy-run p50,
INR p50, bandwidth-class mix), weekdays only and the one weekend split out. Also the pilot's site A week
as a third independent week for coventryOH (same receiver)."""
import sys, numpy as np, pandas as pd

GATEWAY = {7097: [7084.1, 7100.1, 7100.6, 7101.1, 7101.6, 7102.1, 7102.6],
           10138: [10127.5, 10144.8, 10145.3, 10145.8, 10146.3, 10146.8],
           14095: [14091.0, 14094.9, 14095.4, 14095.9, 14096.4, 14096.9, 14097.4, 14097.9]}
UTC_OFF = -4
SITES = ["coventryOH", "elizabethcityNC", "youngsvilleNC"]
COLS = ["site", "band_khz", "width_hz", "channel_khz", "start_utc", "len_s", "level_db", "bw_hz"]


def gw_channels(ch):
    out = {}
    for band, cs in GATEWAY.items():
        for width in (500, 2400):
            have = np.unique(ch[(ch.band_khz == band) & (ch.width_hz == width)].channel_khz.round(1))
            out[(band, width)] = {float(have[np.argmin(np.abs(have - c))]) for c in cs if len(have)}
    return out


def load(score, events_name, sites):
    ch = pd.read_csv(f"{score}/channels.csv", usecols=["site", "band_khz", "width_hz", "channel_khz"])
    gw = gw_channels(ch)
    fr = pd.read_csv(f"{score}/frames.csv", usecols=["site", "band_khz", "utc"])
    fr = fr[fr.site.isin(sites)]
    fr["hour"] = pd.to_datetime(fr.utc, utc=True).dt.floor("h")
    frames_h = fr.groupby(["site", "band_khz", "hour"]).size().rename("frames").reset_index()
    parts = []
    for c in pd.read_csv(f"{score}/{events_name}", chunksize=1_000_000, usecols=COLS):
        c = c[c.site.isin(sites)]
        keep = np.zeros(len(c), bool)
        for (band, width), chs in gw.items():
            keep |= ((c.band_khz == band) & (c.width_hz == width) & c.channel_khz.round(1).isin(chs)).values
        parts.append(c[keep])
    ev = pd.concat(parts, ignore_index=True)
    ev["t"] = pd.to_datetime(ev.start_utc, utc=True)
    ev["hour"] = ev.t.dt.floor("h")
    ev["chan"] = ev.channel_khz.round(1)
    return gw, frames_h, ev


def tag(df, col):
    loc = df[col] + pd.Timedelta(hours=UTC_OFF)
    df["day"] = loc.dt.floor("D").dt.tz_localize(None)
    df["block"] = (loc.dt.hour // 4) * 4
    df["daytype"] = np.where(loc.dt.weekday >= 5, "weekend", "weekday")
    return df


def grid_of(gw, frames_h, ev):
    busy_h = ev.groupby(["site", "band_khz", "width_hz", "chan", "hour"]).len_s.sum().rename("busy_s").reset_index()
    rows = []
    for (band, width), chs in gw.items():
        for c in chs:
            g = frames_h[frames_h.band_khz == band].copy(); g["width_hz"] = width; g["chan"] = c
            rows.append(g)
    grid = pd.concat(rows, ignore_index=True).merge(busy_h, how="left").fillna({"busy_s": 0})
    grid["bf"] = (grid.busy_s / grid.frames).clip(upper=1.0)
    grid = tag(grid, "hour")
    return grid[grid.frames >= 200]


def bwclass(b):
    return pd.cut(b, [-1, 100, 600, 3000, 1e9], labels=["carrier", "narrow", "wide", "broad"])


def targets(grid, ev, label):
    """Generative-fit targets pooled over gateway channels, per site x width."""
    out = []
    for (site, width), g in grid.groupby(["site", "width_hz"]):
        e = ev[(ev.site == site) & (ev.width_hz == width)]
        bf = (g.busy_s.sum() / g.frames.sum())
        mix = bwclass(e.bw_hz).value_counts(normalize=True)
        out.append(dict(period=label, site=site, width=width, frames=int(g.frames.sum()), events=len(e),
                        busy=round(bf, 4), run_p50=e.len_s.median(), run_p90=e.len_s.quantile(.9),
                        inr_p50=round(e.level_db.median(), 1), bw_p50=e.bw_hz.median(),
                        carrier=round(mix.get("carrier", 0), 3), narrow=round(mix.get("narrow", 0), 3),
                        wide=round(mix.get("wide", 0), 3), broad=round(mix.get("broad", 0), 4)))
    return out


def main():
    score4 = sys.argv[1]; pilot = sys.argv[2]
    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30); pd.set_option("display.max_rows", 200)
    gw, fh, ev = load(score4, "events.csv", SITES)
    ev = tag(ev, "t")
    grid = grid_of(gw, fh, ev)
    days = sorted(grid.day.unique())
    print("local days covered:", days[0], "->", days[-1], f"({len(days)} days)")
    print("days per daytype:", grid.groupby("daytype").day.nunique().to_dict())

    # ---- D2
    dayblk = grid.groupby(["site", "band_khz", "width_hz", "chan", "block", "daytype", "day"]).bf.mean().rename("bf").reset_index()
    st = dayblk.groupby(["site", "band_khz", "width_hz", "chan", "block", "daytype"]).bf.agg(["mean", "std", "count"]).reset_index()
    st = st[st["count"] >= 2]
    st["n_days"] = (st["std"] / 0.05) ** 2
    st["have"] = st["count"]
    st["resolved"] = st["n_days"] <= st["have"]
    # half-width of the mean's ~95% CI with the days in hand
    st["ci95"] = 1.96 * st["std"] / np.sqrt(st["count"])
    print("\n== D2 (gateway channels; resolved = days needed for +-0.05 <= days in hand)")
    s = st.groupby(["width_hz", "daytype"]).agg(strata=("std", "size"), days_have=("have", "median"),
                                                 sd_p50=("std", "median"), sd_p90=("std", lambda x: x.quantile(.9)),
                                                 need_p50=("n_days", "median"), need_p90=("n_days", lambda x: x.quantile(.9)),
                                                 need_max=("n_days", "max"), resolved=("resolved", "mean"),
                                                 ci95_p90=("ci95", lambda x: x.quantile(.9)))
    print(s.round(3))
    print("\nby site x band (500 Hz):")
    s2 = st[st.width_hz == 500].groupby(["site", "band_khz", "daytype"]).agg(
        strata=("std", "size"), need_p90=("n_days", lambda x: x.quantile(.9)), need_max=("n_days", "max"),
        resolved=("resolved", "mean"), mean_busy=("mean", "mean"))
    print(s2.round(3))
    print("\nworst unresolved weekday strata (500 Hz):")
    print(st[(st.width_hz == 500) & (st.daytype == "weekday") & ~st.resolved].sort_values("n_days", ascending=False).head(12).round(3))
    st.to_csv("d2_interim.csv", index=False)

    # ---- week-over-week fit targets (weekdays only), plus the weekend
    wk1 = (grid.day >= pd.Timestamp("2026-09-21")) & (grid.day <= pd.Timestamp("2026-09-25"))
    wk2 = (grid.day >= pd.Timestamp("2026-09-28")) & (grid.day <= pd.Timestamp("2026-10-02"))
    we1 = (grid.day >= pd.Timestamp("2026-09-26")) & (grid.day <= pd.Timestamp("2026-09-27"))
    ewk1 = (ev.day >= pd.Timestamp("2026-09-21")) & (ev.day <= pd.Timestamp("2026-09-25"))
    ewk2 = (ev.day >= pd.Timestamp("2026-09-28")) & (ev.day <= pd.Timestamp("2026-10-02"))
    ewe1 = (ev.day >= pd.Timestamp("2026-09-26")) & (ev.day <= pd.Timestamp("2026-09-27"))
    rows = (targets(grid[wk1], ev[ewk1], "4wk wkdays 09-21..25") + targets(grid[wk2], ev[ewk2], "4wk wkdays 09-28..10-02")
            + targets(grid[we1], ev[ewe1], "4wk weekend 09-26/27"))

    # pilot site A (same receiver) as an independent third week
    gwp, fhp, evp = load(pilot, "events.csv.gz", ["coventryOH"])
    evp = tag(evp, "t")
    gridp = grid_of(gwp, fhp, evp)
    rows += targets(gridp[gridp.daytype == "weekday"], evp[evp.daytype == "weekday"], "pilot wkdays 09-11..18")
    rows += targets(gridp[gridp.daytype == "weekend"], evp[evp.daytype == "weekend"], "pilot weekend 09-12/13")
    T = pd.DataFrame(rows).sort_values(["width", "site", "period"])
    print("\n== generative-fit targets, gateway channels pooled (bars: busy +-0.03, run p50 +-25 %, INR p50 +-2 dB)")
    print(T.to_string(index=False))
    T.to_csv("targets_interim.csv", index=False)

    # per-stratum busy (band x block) weekday wk1 vs wk2, 500 Hz, gateway mean
    print("\n== per-stratum gateway busy fraction, weekday wk1 vs wk2 (500 Hz): |diff| > 0.03 rows")
    g5 = grid[grid.width_hz == 500]
    a = g5[wk1[grid.width_hz == 500]].groupby(["site", "band_khz", "block"]).bf.mean()
    b = g5[wk2[grid.width_hz == 500]].groupby(["site", "band_khz", "block"]).bf.mean()
    d = pd.DataFrame({"wk1": a, "wk2": b}); d["diff"] = d.wk2 - d.wk1
    print(f"strata {len(d)}, |diff|>0.03: {(d['diff'].abs() > 0.03).sum()}, |diff| p50/p90/max "
          f"{d['diff'].abs().median():.3f}/{d['diff'].abs().quantile(.9):.3f}/{d['diff'].abs().max():.3f}")
    print(d[d["diff"].abs() > 0.03].round(3))


if __name__ == "__main__":
    main()
