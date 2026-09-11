#!/usr/bin/env python3
"""wspr_pull.py — pull a month of WSPR spot history for a set of real HF paths
and reduce it to the slow-envelope statistics skywave does not model.

Data source: wspr.live (ClickHouse over HTTP, table `wspr.rx`; the wsprnet
corpus).  Everything fetched is cached under --out so `analyze` is offline and
re-runnable; `manifest.json` records every query string verbatim (provenance).

    wspr_pull.py pull    --rx-grid FM0 --days 30 --bands 7,14 --out out/wspr-2026-09
    wspr_pull.py analyze --out out/wspr-2026-09 [--tx-power-dbm 43] [--knees D0:-14,D1:-9,D3:-4]
    wspr_pull.py run     ...both, same flags...

What it measures per path (rx station, tx station, band):

  * availability   — decoded slots / (decoded + censored) per UTC hour.
  * SNR envelope   — per-hour median (censored-aware: valid when < 50 % of the
                     hour's active slots are censored), p10/p90 of decodes.
  * decorrelation  — semivariogram over decoded pairs at lag k slots →
                     ACF(k) = 1 − γ(k)/var; τ_c = first lag with ACF < 1/e.
  * ΔSNR quantiles — |SNR(t+Δ) − SNR(t)| p50/p90 at Δ = 2, 10, 30, 60 min.
  * fade-out hazard — P(censored at t+Δ | decoded at t).
  * outage / dwell — wall durations of censored stretches and decoded stretches
                     measured over the TX's ACTIVE slots.
  * drift          — |WSPR drift| Hz/min quantiles (a weak Doppler proxy).
  * ladder view    — optional: project SNR to a modem TX power and count the
                     rung each slot would land on + the rung-change rate.

CENSORING RULE (read before trusting any number): WSPR beacons transmit on a
random ~20–30 % duty cycle, so an empty slot is ambiguous.  A slot counts as
ACTIVE for a TX if ANY receiver on that band spotted it in that slot; an active
slot the chosen receiver did not decode is CENSORED (below its decode floor,
about −28 dB in 2.5 kHz for WSPR-2).  Slots with no spot anywhere are UNKNOWN
and are excluded from every ratio.  Decoded-only means/medians are biased high
whenever availability < 1 — the report marks those.

Units: WSPR SNR is dB in 2.5 kHz.  SNR3000 ≈ SNR2500 − 0.8 dB.  SNR is
normalised to each TX's modal reported power before analysis.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict

import numpy as np

WSPR_LIVE = "https://db1.wspr.live/"
SLOT_S = 120
WSPR2_FLOOR_DB = -28.0          # nominal WSPR-2 decode floor, dB in 2.5 kHz
BW_CORR_DB = -0.8               # 2.5 kHz → 3 kHz noise bandwidth
DEFAULT_BINS = "200-800,800-2000,2000-4000,4000-20000"   # km; <200 = groundwave, skipped

# --- dataviz reference palette (light mode) -----------------------------------
SURFACE = "#fcfcfb"
INK, INK2, INK3 = "#0b0b0b", "#52514e", "#8a8985"
GRID = "#e6e5e1"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
BAND_FILL = "#cde2fb"           # sequential blue step 100


# ------------------------------------------------------------------ fetching --
def ch_query(sql: str, retries: int = 4, timeout: int = 300) -> str:
    url = WSPR_LIVE + "?" + urllib.parse.urlencode({"query": sql})
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return r.read().decode("utf-8")
        except urllib.error.HTTPError as e:            # ClickHouse puts the SQL error in the body
            body = e.read().decode("utf-8", "replace")
            raise SystemExit(f"wspr.live HTTP {e.code}: {body[:500]}\nSQL: {sql}")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
            time.sleep(2 ** attempt)
    raise SystemExit(f"wspr.live unreachable after {retries} tries: {last}")


def parse_tsv(text: str) -> list[dict]:
    lines = [ln for ln in text.splitlines() if ln]
    if not lines:
        return []
    hdr = lines[0].split("\t")
    return [dict(zip(hdr, ln.split("\t"))) for ln in lines[1:]]


def ts(d: dt.datetime) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S")


def parse_bins(spec: str) -> list[tuple[int, int]]:
    out = []
    for tok in spec.split(","):
        lo, hi = tok.split("-")
        out.append((int(lo), int(hi)))
    return out


def pull(args) -> None:
    os.makedirs(args.out, exist_ok=True)
    end = dt.datetime.strptime(args.end, "%Y-%m-%d") if args.end else \
        dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
    start = end - dt.timedelta(days=args.days)
    bands = [int(b) for b in args.bands.split(",")]
    bins = parse_bins(args.bins)
    manifest = {"source": WSPR_LIVE, "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "window": [ts(start), ts(end)], "bands": bands, "rx_grid": args.rx_grid,
                "rx_sign": args.rx_sign, "bins_km": bins, "per_bin": args.per_bin,
                "min_spots": args.min_spots, "queries": {}}

    rx_clause = f"rx_sign = '{args.rx_sign}'" if args.rx_sign else f"rx_loc LIKE '{args.rx_grid}%'"
    q_pairs = (f"SELECT rx_sign, tx_sign, band, any(distance) AS km, any(tx_loc) AS txloc, "
               f"any(rx_loc) AS rxloc, count() AS n, uniqExact(power) AS npow "
               f"FROM wspr.rx WHERE time >= '{ts(start)}' AND time < '{ts(end)}' "
               f"AND band IN ({','.join(map(str, bands))}) AND {rx_clause} AND code = 1 "
               f"GROUP BY rx_sign, tx_sign, band HAVING n >= {args.min_spots} "
               f"ORDER BY n DESC FORMAT TSVWithNames")
    manifest["queries"]["pairs"] = q_pairs
    print(f"[pull] pair census {ts(start)} → {ts(end)} bands={bands} rx={args.rx_sign or args.rx_grid + '*'}")
    pairs = parse_tsv(ch_query(q_pairs))
    with open(os.path.join(args.out, "pairs.tsv"), "w") as f:
        f.write("rx_sign\ttx_sign\tband\tkm\ttxloc\trxloc\tn\tnpow\n")
        for p in pairs:
            f.write("\t".join(p[k] for k in ("rx_sign", "tx_sign", "band", "km", "txloc", "rxloc", "n", "npow")) + "\n")
    print(f"[pull] {len(pairs)} candidate pairs with ≥ {args.min_spots} spots")

    # Selection: per band, per distance bin, the top --per-bin pairs by spot count.
    selected = []
    for band in bands:
        for lo, hi in bins:
            cands = [p for p in pairs if int(p["band"]) == band and lo <= int(p["km"]) < hi]
            for p in cands[:args.per_bin]:
                selected.append({"rx_sign": p["rx_sign"], "tx_sign": p["tx_sign"], "band": band,
                                 "km": int(p["km"]), "tx_loc": p["txloc"], "rx_loc": p["rxloc"],
                                 "n": int(p["n"]), "bin_km": [lo, hi]})
            if not cands:
                print(f"[pull]   band {band} bin {lo}-{hi} km: no pair meets min_spots")
    with open(os.path.join(args.out, "selection.json"), "w") as f:
        json.dump(selected, f, indent=2)
    for s in selected:
        print(f"[pull]   {s['band']:>3} MHz  {s['rx_sign']:<8} ← {s['tx_sign']:<8} {s['km']:>5} km  {s['n']} spots")

    os.makedirs(os.path.join(args.out, "spots"), exist_ok=True)
    os.makedirs(os.path.join(args.out, "active"), exist_ok=True)
    for s in selected:
        key = f"{s['band']}_{s['rx_sign']}_{s['tx_sign']}".replace("/", "-")
        path = os.path.join(args.out, "spots", key + ".tsv")
        if not os.path.exists(path):
            q = (f"SELECT time, snr, drift, power, frequency, code FROM wspr.rx "
                 f"WHERE time >= '{ts(start)}' AND time < '{ts(end)}' AND band = {s['band']} "
                 f"AND rx_sign = '{s['rx_sign']}' AND tx_sign = '{s['tx_sign']}' AND code = 1 "
                 f"ORDER BY time FORMAT TSVWithNames")
            manifest["queries"][f"spots:{key}"] = q
            open(path, "w").write(ch_query(q))
            print(f"[pull]   spots  {key}")
        akey = f"{s['band']}_{s['tx_sign']}".replace("/", "-")
        apath = os.path.join(args.out, "active", akey + ".tsv")
        if not os.path.exists(apath):
            q = (f"SELECT time, count() AS nrx, max(snr) AS best_snr FROM wspr.rx "
                 f"WHERE time >= '{ts(start)}' AND time < '{ts(end)}' AND band = {s['band']} "
                 f"AND tx_sign = '{s['tx_sign']}' AND code = 1 GROUP BY time ORDER BY time FORMAT TSVWithNames")
            manifest["queries"][f"active:{akey}"] = q
            open(apath, "w").write(ch_query(q))
            print(f"[pull]   active {akey}")
    with open(os.path.join(args.out, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"[pull] done → {args.out}")


# ------------------------------------------------------------------ analysis --
def to_epoch(s: str) -> int:
    return int(dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp())


def load_pair(out: str, sel: dict, t0: int, nslots: int, min_rx: int = 2) -> dict:
    """Return per-slot arrays: status (0 unknown, 1 censored, 2 decoded), snr (power-normalised), drift."""
    key = f"{sel['band']}_{sel['rx_sign']}_{sel['tx_sign']}".replace("/", "-")
    akey = f"{sel['band']}_{sel['tx_sign']}".replace("/", "-")
    spots = parse_tsv(open(os.path.join(out, "spots", key + ".tsv")).read())
    active = parse_tsv(open(os.path.join(out, "active", akey + ".tsv")).read())
    status = np.zeros(nslots, dtype=np.int8)
    snr = np.full(nslots, np.nan)
    drift = np.full(nslots, np.nan)
    powers = Counter(int(r["power"]) for r in spots)
    p_mode = powers.most_common(1)[0][0] if powers else 0
    # WSPR-2 slots start on even minutes; odd-minute rows are mis-clocked receivers (or another
    # mode) and would fabricate phantom active slots next to real ones — drop them, count them.
    def even(r):
        return int(r["time"][14:16]) % 2 == 0 and r["time"][17:19] == "00"
    dropped_active = sum(1 for r in active if not even(r))
    dropped_spots = sum(1 for r in spots if not even(r))
    # A slot reported by a single receiver is often that receiver's clock a whole slot late
    # (measured: 435/467 consecutive-slot 'actives' on a 6-min beacon were nrx=1) — require min_rx.
    dropped_single = sum(1 for r in active if even(r) and int(r["nrx"]) < min_rx)
    for r in active:
        if not even(r) or int(r["nrx"]) < min_rx:
            continue
        i = (to_epoch(r["time"]) - t0) // SLOT_S
        if 0 <= i < nslots:
            status[i] = max(status[i], 1)
    for r in spots:
        if not even(r):
            continue
        i = (to_epoch(r["time"]) - t0) // SLOT_S
        if not (0 <= i < nslots):
            continue
        v = float(r["snr"]) - (int(r["power"]) - p_mode)
        if np.isnan(snr[i]) or v > snr[i]:      # duplicate uploads: keep the best
            snr[i] = v
            drift[i] = float(r["drift"])
        status[i] = 2
    return {"key": key, "status": status, "snr": snr, "drift": drift, "power_mode_dbm": p_mode,
            "powers": dict(powers), "n_spots_rows": len(spots),
            "dropped_odd_minute": {"active": dropped_active, "spots": dropped_spots},
            "dropped_single_rx_active": dropped_single}


def hourly(status, snr, t0):
    hours = ((t0 + np.arange(len(status)) * SLOT_S) // 3600) % 24
    rows = []
    for h in range(24):
        m = hours == h
        act = m & (status >= 1)
        dec = m & (status == 2)
        n_act, n_dec = int(act.sum()), int(dec.sum())
        avail = n_dec / n_act if n_act else np.nan
        vals = snr[dec]
        # censored-aware median: censored slots sit below every decode
        cens_med = np.nan
        if n_act and avail > 0.5:
            full = np.concatenate([vals, np.full(n_act - n_dec, -np.inf)])
            cens_med = float(np.median(full))
        rows.append({"hour": h, "n_active": n_act, "n_decoded": n_dec, "availability": avail,
                     "median_censored": cens_med,
                     "p10_decoded": float(np.percentile(vals, 10)) if n_dec else np.nan,
                     "p50_decoded": float(np.median(vals)) if n_dec else np.nan,
                     "p90_decoded": float(np.percentile(vals, 90)) if n_dec else np.nan})
    return rows


def lag_stats(status, snr, max_lag_slots):
    """Semivariogram / ACF, ΔSNR quantiles, and fade-out hazard by lag over ACTIVE slots."""
    dec = status == 2
    act = status >= 1
    v = np.nanvar(snr[dec]) if dec.sum() > 2 else np.nan
    lags, acf, d50, d90, drms, hazard, npairs = [], [], [], [], [], [], []
    for k in range(1, max_lag_slots + 1):
        both = dec[:-k] & dec[k:]
        n = int(both.sum())
        d = snr[k:][both] - snr[:-k][both]
        gamma = 0.5 * np.mean(d * d) if n else np.nan
        haz_den = dec[:-k] & act[k:]
        haz_num = dec[:-k] & (status[k:] == 1)
        lags.append(k * SLOT_S / 60.0)
        npairs.append(n)
        acf.append(1 - gamma / v if (n >= 20 and v > 0) else np.nan)
        d50.append(float(np.percentile(np.abs(d), 50)) if n >= 20 else np.nan)
        d90.append(float(np.percentile(np.abs(d), 90)) if n >= 20 else np.nan)
        drms.append(float(np.sqrt(np.mean(d * d))) if n >= 20 else np.nan)
        hazard.append(float(haz_num.sum() / haz_den.sum()) if haz_den.sum() >= 20 else np.nan)
    acf_a = np.array(acf)
    tau_c = np.nan
    for i, a in enumerate(acf_a):
        if not np.isnan(a) and a < math.exp(-1):
            tau_c = lags[i]
            break
    return {"lag_min": lags, "acf": acf, "dsnr_p50": d50, "dsnr_p90": d90, "dsnr_rms": drms,
            "hazard": hazard, "npairs": npairs, "tau_c_min": tau_c, "var_decoded": float(v)}


def detrend(status, snr, half_window_slots=30, min_pts=10):
    """Residual after a centred running mean of decoded SNR (±half_window slots) — isolates the
    minutes-scale wander from the diurnal envelope.  NaN where the window holds < min_pts decodes."""
    dec = status == 2
    vals = np.where(dec, snr, 0.0)
    cnt = dec.astype(float)
    k = np.ones(2 * half_window_slots + 1)
    num = np.convolve(vals, k, mode="same")
    den = np.convolve(cnt, k, mode="same")
    trend = np.where(den >= min_pts, num / np.maximum(den, 1), np.nan)
    resid = snr - trend
    st = status.copy()
    st[dec & np.isnan(resid)] = 1        # decoded but no trend estimate → not usable as a decode here
    return st, resid


def runs(status, t0):
    """Outage and dwell durations in minutes, measured across the TX's ACTIVE slots only."""
    idx = np.nonzero(status >= 1)[0]
    if len(idx) < 3:
        return {"outage_min": [], "dwell_min": []}
    st = status[idx]
    outages, dwells = [], []
    # walk active slots; a run is a maximal stretch of equal status
    run_start = 0
    for j in range(1, len(idx) + 1):
        if j == len(idx) or st[j] != st[run_start]:
            first, last = idx[run_start], idx[j - 1]
            # duration: from the slot before the run to the slot after it (bounded by decodes/censors)
            prev_i = idx[run_start - 1] if run_start > 0 else first
            next_i = idx[j] if j < len(idx) else last
            if st[run_start] == 1 and run_start > 0 and j < len(idx):
                outages.append((next_i - prev_i) * SLOT_S / 60.0)     # decode → … → decode
            elif st[run_start] == 2:
                dwells.append((last - first + 1) * SLOT_S / 60.0)
            run_start = j
    return {"outage_min": outages, "dwell_min": dwells}


def parse_knees(spec: str) -> list[tuple[str, float]]:
    if not spec:
        return []
    out = [(tok.split(":")[0], float(tok.split(":")[1])) for tok in spec.split(",")]
    return sorted(out, key=lambda x: x[1])


def ladder_view(status, snr, p_mode_dbm, tx_power_dbm, knees):
    """Project WSPR SNR to a modem at tx_power_dbm on the same antenna/path and count rungs."""
    if not knees:
        return None
    gain = tx_power_dbm - p_mode_dbm + BW_CORR_DB
    proj = snr + gain
    floor_proj = WSPR2_FLOOR_DB + gain
    act = status >= 1
    names = [k for k, _ in knees]
    thr = np.array([v for _, v in knees])
    rung = np.full(len(status), -2, dtype=int)         # -2 unknown, -1 none, i = knees[i]
    dec = status == 2
    r = np.searchsorted(thr, proj[dec], side="right") - 1
    rung[dec] = r
    cens = status == 1
    rung[cens] = -1 if floor_proj < thr[0] else -2       # censored is 'none' only if the floor is below the lowest knee
    occ = {}
    n_act = int(act.sum())
    for i, nm in enumerate(names):
        occ[nm] = int((rung == i).sum()) / n_act if n_act else np.nan
    occ["none"] = int((rung == -1).sum()) / n_act if n_act else np.nan
    occ["unknown"] = int(((rung == -2) & act).sum()) / n_act if n_act else np.nan
    # rung-change rate between consecutive known-rung active slots, by lag
    change = {}
    known = rung >= -1
    for k in (1, 5, 15):
        both = known[:-k] & known[k:]
        change[f"{k * 2}min"] = float((rung[k:][both] != rung[:-k][both]).mean()) if both.sum() >= 20 else np.nan
    return {"gain_db": gain, "floor_projected_db": floor_proj, "occupancy": occ, "rung_change_rate": change,
            "knees": knees}


# --------------------------------------------------------------------- plots --
def style_axes(ax):
    ax.set_facecolor(SURFACE)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.grid(True, color=GRID, linewidth=0.8, linestyle="-")
    ax.set_axisbelow(True)
    ax.tick_params(colors=INK2, labelsize=8)
    ax.xaxis.label.set_color(INK2)
    ax.yaxis.label.set_color(INK2)
    ax.title.set_color(INK)


def plot_pair(res, out_png):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    st, snr, t0 = res["status"], res["snr"], res["t0"]
    n = len(st)
    days = (np.arange(n) * SLOT_S) / 86400.0
    fig = plt.figure(figsize=(13, 13), facecolor=SURFACE)
    gs = fig.add_gridspec(4, 2, height_ratios=[1.2, 1, 1, 1], hspace=0.5, wspace=0.28)
    ax = fig.add_subplot(gs[0, :]); style_axes(ax)
    dec, cens = st == 2, st == 1
    ax.scatter(days[cens], np.full(cens.sum(), WSPR2_FLOOR_DB - 2), s=3, marker="|", color=INK3, linewidths=0.6,
               label="censored (TX active, not decoded here)")
    ax.scatter(days[dec], snr[dec], s=4, color=SERIES[0], linewidths=0, label="decoded")
    ax.set_xlabel(f"days since {res['window'][0]} UTC"); ax.set_ylabel("SNR, dB in 2.5 kHz")
    ax.set_title(f"{res['sel']['tx_sign']} → {res['sel']['rx_sign']}  {res['sel']['band']} MHz  {res['sel']['km']} km   "
                 f"(availability {res['availability']:.0%}, τ_c {res['lag']['tau_c_min']:.0f} min)", fontsize=10, loc="left")
    ax.legend(loc="upper right", fontsize=8, frameon=False, labelcolor=INK2)

    hrs = res["hourly"]
    h = [r["hour"] for r in hrs]
    ax = fig.add_subplot(gs[1, 0]); style_axes(ax)
    p10 = np.array([r["p10_decoded"] for r in hrs]); p90 = np.array([r["p90_decoded"] for r in hrs])
    med = np.array([r["median_censored"] for r in hrs]); p50d = np.array([r["p50_decoded"] for r in hrs])
    ax.fill_between(h, p10, p90, color=BAND_FILL, linewidth=0, label="decoded p10–p90")
    ax.plot(h, p50d, color=SERIES[0], linewidth=1.2, alpha=0.5, label="decoded median (biased high)")
    ax.plot(h, med, color=SERIES[0], linewidth=2, label="censored-aware median")
    ax.set_xlabel("UTC hour"); ax.set_ylabel("SNR, dB in 2.5 kHz"); ax.set_xlim(0, 23)
    ax.set_title("Diurnal envelope", fontsize=10, loc="left")
    ax.legend(fontsize=7, frameon=False, labelcolor=INK2, loc="lower right")

    ax = fig.add_subplot(gs[1, 1]); style_axes(ax)
    av = np.array([r["availability"] for r in hrs])
    ax.bar(h, av, width=0.8, color=SERIES[0], linewidth=0)
    ax.set_ylim(0, 1); ax.set_xlim(-0.5, 23.5)
    ax.set_xlabel("UTC hour"); ax.set_ylabel("decoded / active slots")
    ax.set_title("Availability by hour", fontsize=10, loc="left")

    lg = res["lag"]
    ax = fig.add_subplot(gs[2, 0]); style_axes(ax)
    ax.plot(lg["lag_min"], lg["dsnr_p50"], color=SERIES[0], linewidth=2, label="|ΔSNR| p50")
    ax.plot(lg["lag_min"], lg["dsnr_p90"], color=SERIES[1], linewidth=2, label="|ΔSNR| p90")
    ax.set_xscale("log"); ax.set_xlabel("lag, min"); ax.set_ylabel("dB")
    ax.set_title("SNR change vs lag (decoded pairs)", fontsize=10, loc="left")
    ax.legend(fontsize=8, frameon=False, labelcolor=INK2, loc="upper left")

    ax = fig.add_subplot(gs[3, 0]); style_axes(ax)
    ax.plot(lg["lag_min"], lg["acf"], color=SERIES[0], linewidth=2, label="raw")
    lf = res["lag_fast"]
    ax.plot(lf["lag_min"], lf["acf"], color=SERIES[1], linewidth=2, label="detrended (2 h running mean removed)")
    ax.legend(fontsize=7, frameon=False, labelcolor=INK2, loc="upper right")
    ax.axhline(math.exp(-1), color=INK3, linewidth=0.8)
    ax.text(lg["lag_min"][-1], math.exp(-1) + 0.03, "1/e", color=INK3, fontsize=7, ha="right")
    ax.set_xscale("log"); ax.set_ylim(-0.2, 1.05); ax.set_xlabel("lag, min"); ax.set_ylabel("ACF of SNR")
    ax.set_title("SNR autocorrelation (semivariogram estimate)", fontsize=10, loc="left")

    ax = fig.add_subplot(gs[3, 1]); style_axes(ax)
    ax.plot(lg["lag_min"], lg["hazard"], color=SERIES[1], linewidth=2)
    ax.set_xscale("log"); ax.set_ylim(0, 1); ax.set_xlabel("lag, min"); ax.set_ylabel("P(censored | decoded now)")
    ax.set_title("Fade-out hazard vs lag", fontsize=10, loc="left")

    ax = fig.add_subplot(gs[2, 1]); style_axes(ax)
    o = np.array(res["runs"]["outage_min"]); d = np.array(res["runs"]["dwell_min"])
    bins = np.logspace(math.log10(2), math.log10(max(60.0, o.max() if o.size else 60, d.max() if d.size else 60)), 24)
    if o.size:
        ax.hist(o, bins=bins, color=SERIES[1], alpha=0.85, linewidth=0, label=f"outage (n={o.size})")
    if d.size:
        ax.hist(d, bins=bins, color=SERIES[0], alpha=0.6, linewidth=0, label=f"decoded dwell (n={d.size})")
    ax.set_xscale("log"); ax.set_xlabel("duration, min (active-slot bounded)"); ax.set_ylabel("count")
    ax.set_title("Outage and dwell durations", fontsize=10, loc="left")
    ax.legend(fontsize=8, frameon=False, labelcolor=INK2)
    fig.savefig(out_png, dpi=110, facecolor=SURFACE)
    plt.close(fig)


def plot_summary(results, out_png):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    results = sorted(results, key=lambda r: (r["sel"]["band"], r["sel"]["km"]))[:8]
    fig, axes = plt.subplots(1, 3, figsize=(16, 5.2), facecolor=SURFACE)
    for ax in axes:
        style_axes(ax)
    for i, r in enumerate(results):
        lab = f"{r['sel']['band']} MHz {r['sel']['km']} km {r['sel']['tx_sign']}"
        axes[0].plot(r["lag"]["lag_min"], r["lag"]["acf"], color=SERIES[i], linewidth=1.8, label=lab)
        axes[1].plot(r["lag_fast"]["lag_min"], r["lag_fast"]["acf"], color=SERIES[i], linewidth=1.8, label=lab)
        h = [x["hour"] for x in r["hourly"]]
        axes[2].plot(h, [x["availability"] for x in r["hourly"]], color=SERIES[i], linewidth=1.8, label=lab)
    for ax, title in ((axes[0], "SNR autocorrelation, raw"), (axes[1], "SNR autocorrelation, detrended (2 h)")):
        ax.axhline(math.exp(-1), color=INK3, linewidth=0.8)
        ax.set_xscale("log"); ax.set_xlabel("lag, min"); ax.set_ylabel("ACF of SNR")
        ax.set_ylim(-0.2, 1.05); ax.set_title(title, fontsize=10, loc="left")
    axes[2].set_xlabel("UTC hour"); axes[2].set_ylabel("availability"); axes[2].set_ylim(0, 1); axes[2].set_xlim(0, 23)
    axes[2].set_title("Availability by hour", fontsize=10, loc="left")
    fig.legend(*axes[2].get_legend_handles_labels(), loc="lower center", ncol=4, fontsize=8, frameon=False,
               labelcolor=INK2, bbox_to_anchor=(0.5, 0.0))
    fig.tight_layout(rect=(0, 0.12, 1, 1))
    fig.savefig(out_png, dpi=110, facecolor=SURFACE)
    plt.close(fig)


# -------------------------------------------------------------------- report --
def f1(x, unit=""):
    return "–" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.1f}{unit}"


def pick(lg, minutes):
    i = min(range(len(lg["lag_min"])), key=lambda j: abs(lg["lag_min"][j] - minutes))
    return lg["dsnr_p50"][i], lg["dsnr_p90"][i], lg["hazard"][i]


def analyze(args) -> None:
    man = json.load(open(os.path.join(args.out, "manifest.json")))
    sel = json.load(open(os.path.join(args.out, "selection.json")))
    t0, t1 = to_epoch(man["window"][0]), to_epoch(man["window"][1])
    nslots = (t1 - t0) // SLOT_S
    knees = parse_knees(args.knees)
    max_lag = int(args.max_lag_min * 60 // SLOT_S)
    results = []
    os.makedirs(os.path.join(args.out, "plots"), exist_ok=True)
    for s in sel:
        pr = load_pair(args.out, s, t0, nslots, args.min_rx)
        st, snr = pr["status"], pr["snr"]
        n_act, n_dec = int((st >= 1).sum()), int((st == 2).sum())
        res = {"sel": s, "key": pr["key"], "t0": t0, "window": man["window"], "status": st, "snr": snr,
               "n_active": n_act, "n_decoded": n_dec, "availability": n_dec / n_act if n_act else np.nan,
               "duty_cycle": n_act / nslots, "power_mode_dbm": pr["power_mode_dbm"], "powers": pr["powers"],
               "hourly": hourly(st, snr, t0), "lag": lag_stats(st, snr, max_lag), "runs": runs(st, t0),
               "lag_fast": lag_stats(*detrend(st, snr), int(120 * 60 // SLOT_S)),
               "dropped_odd_minute": pr["dropped_odd_minute"],
               "drift_abs_p50": float(np.nanpercentile(np.abs(pr["drift"]), 50)) if n_dec else np.nan,
               "drift_abs_p90": float(np.nanpercentile(np.abs(pr["drift"]), 90)) if n_dec else np.nan,
               "ladder": ladder_view(st, snr, pr["power_mode_dbm"], args.tx_power_dbm, knees)}
        # censored-aware overall median
        if n_act and res["availability"] > 0.5:
            res["median_censored"] = float(np.median(np.concatenate([snr[st == 2], np.full(n_act - n_dec, -np.inf)])))
        else:
            res["median_censored"] = np.nan
        results.append(res)
        plot_pair(res, os.path.join(args.out, "plots", pr["key"] + ".png"))
        # per-slot series for downstream use (trace-mode seed): epoch, status, snr
        with open(os.path.join(args.out, "spots", pr["key"] + ".series.tsv"), "w") as f:
            f.write("epoch\tstatus\tsnr_norm_db\n")
            for i in range(nslots):
                if st[i]:
                    f.write(f"{t0 + i * SLOT_S}\t{int(st[i])}\t{'' if np.isnan(snr[i]) else f'{snr[i]:.0f}'}\n")
        print(f"[analyze] {pr['key']:<28} active {n_act:>5} decoded {n_dec:>5} avail {res['availability']:.2f} "
              f"τ_c {f1(res['lag']['tau_c_min'], ' min')} fast τ_c {f1(res['lag_fast']['tau_c_min'], ' min')} "
              f"dropped odd-minute {pr['dropped_odd_minute']} single-rx {pr['dropped_single_rx_active']}")
    plot_summary(results, os.path.join(args.out, "plots", "summary.png"))

    # ---- summary.json (no arrays of slots) + REPORT.md
    slim = []
    for r in results:
        d = {k: v for k, v in r.items() if k not in ("status", "snr", "runs", "hourly", "lag", "lag_fast")}
        d["hourly"] = r["hourly"]
        d["lag"] = {k: v for k, v in r["lag"].items()}
        d["lag_fast"] = {k: v for k, v in r["lag_fast"].items()}
        d["outage_min_quantiles"] = {q: float(np.percentile(r["runs"]["outage_min"], q)) for q in (50, 90)} if r["runs"]["outage_min"] else {}
        d["dwell_min_quantiles"] = {q: float(np.percentile(r["runs"]["dwell_min"], q)) for q in (50, 90)} if r["runs"]["dwell_min"] else {}
        slim.append(d)
    json.dump({"manifest": man, "tx_power_dbm": args.tx_power_dbm, "knees": knees, "paths": slim},
              open(os.path.join(args.out, "summary.json"), "w"), indent=1, default=lambda o: None if isinstance(o, float) and math.isnan(o) else o)

    L = []
    L.append(f"# WSPR slow-envelope survey — {man['window'][0]} → {man['window'][1]} UTC\n")
    L.append(f"Source wspr.live, fetched {man['fetched_at']}; receivers `{man['rx_sign'] or man['rx_grid'] + '*'}`; "
             f"bands {man['bands']} MHz; {len(results)} paths. SNR is dB in 2.5 kHz, normalised to each TX's modal "
             f"reported power (SNR3000 ≈ −0.8 dB). Slots are 2 min.\n")
    L.append("**Censoring rule.** A slot is ACTIVE for a TX when any receiver on the band spotted it; an active slot "
             "the chosen receiver did not decode is CENSORED (below ≈ −28 dB). Availability = decoded/active. "
             "The censored-aware median is reported only where < 50 % of active slots are censored; the decoded-only "
             "median is biased high whenever availability < 1.\n")
    L.append("## Paths\n")
    L.append("| band | path | km | duty | active | avail | median (cens.) | p50/p90 dec | τ_c raw | τ_c detrended | drift p90 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in results:
        dec = r["snr"][r["status"] == 2]
        L.append(f"| {r['sel']['band']} | {r['sel']['tx_sign']} → {r['sel']['rx_sign']} | {r['sel']['km']} | "
                 f"{r['duty_cycle']:.0%} | {r['n_active']} | {r['availability']:.0%} | {f1(r['median_censored'], ' dB')} | "
                 f"{f1(float(np.median(dec)) if dec.size else np.nan)}/{f1(float(np.percentile(dec, 90)) if dec.size else np.nan)} | "
                 f"{f1(r['lag']['tau_c_min'], ' min')} | {f1(r['lag_fast']['tau_c_min'], ' min')} | {f1(r['drift_abs_p90'], ' Hz/min')} |")
    L.append("\n## SNR change and fade-out hazard vs lag\n")
    L.append("|ΔSNR| p50 / p90 in dB over decoded pairs; hazard = P(censored at t+Δ | decoded at t).\n")
    L.append("| band | path | 2 min | 10 min | 30 min | 60 min | hazard 2/10/30 min | outage p50/p90 | dwell p50/p90 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for r in results:
        cells, haz = [], []
        for m in (2, 10, 30, 60):
            p50, p90, hz = pick(r["lag"], m)
            cells.append(f"{f1(p50)} / {f1(p90)}")
            if m != 60:
                haz.append(f"{hz:.0%}" if not math.isnan(hz) else "–")
        o, d = r["runs"]["outage_min"], r["runs"]["dwell_min"]
        oq = f"{f1(float(np.percentile(o, 50)))} / {f1(float(np.percentile(o, 90)))} min" if o else "–"
        dq = f"{f1(float(np.percentile(d, 50)))} / {f1(float(np.percentile(d, 90)))} min" if d else "–"
        L.append(f"| {r['sel']['band']} | {r['sel']['tx_sign']} → {r['sel']['rx_sign']} | " + " | ".join(cells) +
                 f" | {'/'.join(haz)} | {oq} | {dq} |")
    if knees:
        L.append(f"\n## Ladder projection at {args.tx_power_dbm} dBm TX (same antenna and path as the beacon — a strong assumption)\n")
        L.append("Occupancy = fraction of ACTIVE slots landing on each rung; 'none' = below the lowest knee; "
                 "'unknown' = censored but the projected WSPR floor sits above the lowest knee. "
                 "Change rate = fraction of slot pairs at that lag whose rung differs.\n")
        names = [k for k, _ in knees] + ["none", "unknown"]
        L.append("| band | path | gain | " + " | ".join(names) + " | change 2/10/30 min |")
        L.append("|---|---|---|" + "---|" * len(names) + "---|")
        for r in results:
            lv = r["ladder"]
            occ = " | ".join(f"{lv['occupancy'][n]:.0%}" for n in names)
            ch = "/".join(f"{v:.0%}" if not math.isnan(v) else "–" for v in lv["rung_change_rate"].values())
            L.append(f"| {r['sel']['band']} | {r['sel']['tx_sign']} → {r['sel']['rx_sign']} | {lv['gain_db']:+.0f} dB | {occ} | {ch} |")
    L.append("\n## Reading it\n")
    L.append("* τ_c raw is the ACF 1/e lag of the whole series (diurnal envelope included); τ_c detrended is the "
             "same after removing a centred 2-h running mean, i.e. the minutes-scale wander alone. The detrended ACF's "
             "negative lobe near 40–60 min is the running-mean window, not the channel.")
    L.append("* WSPR's own SNR estimate carries ~1 dB of noise per decode, so the 2-min |ΔSNR| step includes ~1.4 dB "
             "of measurement noise (RMS); the growth from 2 → 10 → 30 min is the channel.")
    L.append("* τ_c against the modem's adaptation time (ladder round trip ≈ 6 s, CQR seed per burst) says whether the "
             "channel moves faster or slower than the modem reacts. WSPR's 2-min floor bounds τ_c from below only.")
    L.append("* The 2-min |ΔSNR| p90 is the smallest step the envelope takes between consecutive looks; skywave's "
             "scheduled fade has no step this small and no continuous drift at all.")
    L.append("* Outage durations are bounded by the beacon's duty cycle — they are upper bounds on the true fade-out.")
    L.append("* Drift is a frequency-drift proxy (Hz/min), dominated by TX oscillators on most paths; do not read it as Doppler spread.\n")
    L.append(f"Plots: `plots/summary.png`, one `plots/<key>.png` per path. Per-slot series: `spots/<key>.series.tsv`.\n")
    open(os.path.join(args.out, "REPORT.md"), "w").write("\n".join(L))
    print(f"[analyze] → {os.path.join(args.out, 'REPORT.md')}")


# ---------------------------------------------------------------------- main --
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_pull(p):
        p.add_argument("--rx-grid", default="FM0", help="Maidenhead prefix for receivers (default FM0 = central NC)")
        p.add_argument("--rx-sign", default="", help="pin one receiver callsign instead of a grid prefix")
        p.add_argument("--days", type=int, default=30)
        p.add_argument("--end", default="", help="window end YYYY-MM-DD UTC (default: today 00:00)")
        p.add_argument("--bands", default="7,14", help="wspr.live band codes = MHz (7, 14, 3, 10, 21…)")
        p.add_argument("--bins", default=DEFAULT_BINS, help="distance bins km, lo-hi,lo-hi,…")
        p.add_argument("--per-bin", type=int, default=1, help="paths per band per bin (top by spot count)")
        p.add_argument("--min-spots", type=int, default=500)

    def add_an(p):
        p.add_argument("--tx-power-dbm", type=float, default=43.0, help="modem TX power for the ladder projection (43 = 20 W)")
        p.add_argument("--knees", default="", help="rung floors in SNR3000 dB, e.g. D0:-14,D1:-9,D3:-4 (enables the ladder view)")
        p.add_argument("--max-lag-min", type=float, default=360)
        p.add_argument("--min-rx", type=int, default=2, help="receivers that must report a slot for it to count as ACTIVE")

    p = sub.add_parser("pull"); add_pull(p); p.add_argument("--out", required=True)
    p = sub.add_parser("analyze"); add_an(p); p.add_argument("--out", required=True)
    p = sub.add_parser("run"); add_pull(p); add_an(p); p.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    if args.cmd in ("pull", "run"):
        pull(args)
    if args.cmd in ("analyze", "run"):
        analyze(args)


if __name__ == "__main__":
    main()
