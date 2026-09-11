#!/usr/bin/env python3
"""qrm_occupancy.py — score KiwiSDR waterfall captures (kiwi_wf_record.py) for the QRM pilot (pre-reg §5).

    qrm_occupancy.py DIR [DIR ...] --out REPORT_DIR [--utc-offset -4] [--busy-db 10] [--detect-db 6]

Per capture file: dBm/Hz per bin = byte − 255 + wf_cal − 10·log10(rbw). Floor per FILE = noise mean from the
per-bin 10th percentile over time, then the 25th percentile across bins (see file_floor; the per-frame p25
floor is recorded alongside as a diagnostic). Channels = 500 Hz and 2400 Hz windows on a
100 Hz grid; channel level = window peak − floor (dB). Detected: level ≥ detect-db (3); BUSY:
level ≥ busy-db (10) (the instrument's threshold, not the modem's). Outputs:
  channels.csv   one row per (site, band, channel centre, width, stratum): busy fraction, runs, levels,
                 bandwidth classes, cadence
  events.csv     one row per busy run (site, band, channel, start UTC, length s, censored, level dB, bw Hz)
  frames.csv     one row per frame: floor dBm/Hz (median, p25), fraction of bins > floor+busy
  REPORT.md + occupancy.png
No demodulation, no decoding: bandwidth and cadence only.
"""
import argparse, collections, datetime as dt, glob, json, os

import numpy as np

BW_CLASSES = [(0, 100, "carrier"), (100, 600, "narrow"), (600, 3000, "voice_wide"), (3000, 1e9, "broad")]


def q(v, p):
    return float(np.percentile(v, p)) if len(v) else float("nan")


def load(path_npy):
    side = json.load(open(path_npy[:-7] + ".json"))
    fr = np.load(path_npy).astype(np.float32)
    ts = np.load(path_npy[:-7] + ".t.npy")
    rbw = side["rbw_hz"]
    dbm_hz = fr + side.get("dbm_offset", -255) + side.get("wf_cal", -13) - 10 * np.log10(rbw)
    dbm_hz[:, :2] = np.nan                                     # DC notch bins
    f_khz = side["centre_khz"] - side["span_khz"] / 2 + (np.arange(fr.shape[1]) + 0.5) * side["span_khz"] / 1024
    return side, dbm_hz, ts, f_khz, rbw


EXP_P25 = -np.log(0.75)          # p25 of an exponential (single-FFT noise bin) in units of its mean: 0.2877
SEG_DB = 10.0                    # segmentation threshold over the noise MEAN per bin (false-bin rate e^-10 per bin)


EXP_P10 = -np.log(0.9)           # p10 of an exponential in units of its mean: 0.1054


def file_floor(dbm_hz):
    """Per-FILE noise floor (dBm/Hz, mean power): per bin the 10th percentile over TIME (/0.1054 → that bin's noise
    mean, valid while the bin is signal-free ≥ 10 % of the dwell), then the 25th percentile across bins. Smoke
    2026-09-11: agrees with the S-meter slot-gap anchor to 0.2 dB on a dead band and within +1…+3 dB on the
    signal-dense 20/30/40 m digital segments (the per-FRAME p25 floor sat 4–5 dB high there)."""
    lin = 10 ** (dbm_hz / 10)
    per_bin = np.nanpercentile(lin, 10, axis=0) / EXP_P10
    return float(10 * np.log10(np.nanpercentile(per_bin, 25)))


def frame_floor(dbm_hz):
    """Per-frame noise floor as a MEAN power (dBm/Hz) from the 25th percentile of the bins, assuming exponential
    (single-FFT) noise bins: mean = p25 / 0.2877. Robust until > 75 % of the bins carry signal. Also returns the
    shape diagnostic median − p25 (dB; 3.8 for exponential noise, smaller if the receiver averages frames)."""
    lin = 10 ** (dbm_hz / 10)
    p25 = np.nanpercentile(lin, 25, axis=1); med = np.nanmedian(lin, axis=1)
    return 10 * np.log10(p25 / EXP_P25), 10 * np.log10(med / p25)


def channel_levels(dbm_hz, f_khz, rbw, width_hz, step_khz=0.1, floor_db=None):
    """Per frame, per channel on an exact step_khz grid: passband INR = mean linear power in the window over the
    frame's noise-mean floor (dB). Noise-only level ≈ 0 dB ± 1/sqrt(nbins) (0.24 for 500 Hz, 0.1 for 2400 Hz).
    Returns (centres_khz, level[frames, ch], floor_dbm_hz[frames])."""
    if floor_db is None:
        floor_db, _ = frame_floor(dbm_hz)
    floor_db = np.broadcast_to(np.asarray(floor_db, dtype=float), (dbm_hz.shape[0],)).copy()
    lin = np.nan_to_num(10 ** (dbm_hz / 10), nan=0.0)
    lin[:, :2] = 10 ** (floor_db[:, None] / 10)              # DC notch bins → floor
    cs = np.concatenate([np.zeros((lin.shape[0], 1)), np.cumsum(lin, axis=1)], axis=1)
    nb = max(1, int(round(width_hz / rbw)))
    f0 = f_khz[0] - rbw / 2000
    centres = np.arange(np.ceil((f_khz[0] + width_hz / 2000) * 10) / 10, f_khz[-1] - width_hz / 2000, step_khz)
    starts = np.clip(np.round((centres - width_hz / 2000 - f0) / (rbw / 1000)).astype(int), 0, lin.shape[1] - nb)
    mean_win = (cs[:, starts + nb] - cs[:, starts]) / nb
    level = 10 * np.log10(np.maximum(mean_win, 1e-30)) - floor_db[:, None]
    return np.round(centres, 1), level, floor_db


def segments(frame_dbm, floor_db, rbw, thr_db=SEG_DB, gap_bins=1):
    """Contiguous runs of bins ≥ floor_mean + thr_db (gaps ≤ gap_bins merged): list of (lo, hi, width_hz, peak_db)."""
    above = np.nan_to_num(frame_dbm, nan=-999) >= floor_db + thr_db
    above[:2] = False
    idx = np.flatnonzero(above)
    if len(idx) == 0:
        return []
    out = []
    lo = hi = idx[0]
    for i in idx[1:]:
        if i - hi <= gap_bins + 1:
            hi = i
        else:
            out.append((lo, hi)); lo = hi = i
    out.append((lo, hi))
    return [(lo, hi, (hi - lo + 1) * rbw, float(np.nanmax(frame_dbm[lo:hi + 1]) - floor_db)) for lo, hi in out]


def stratum_of(t0, utc_offset):
    loc = dt.datetime.fromtimestamp(t0, dt.timezone.utc) + dt.timedelta(hours=utc_offset)
    return f"{(loc.hour // 4) * 4:02d}-{(loc.hour // 4) * 4 + 4:02d}", "weekend" if loc.weekday() >= 5 else "weekday"


def runs_of(b):
    """Runs of a boolean vector: list of (start, length, value, censored_left, censored_right)."""
    out = []
    if len(b) == 0:
        return out
    edges = np.flatnonzero(np.diff(b.astype(np.int8))) + 1
    bounds = np.concatenate([[0], edges, [len(b)]])
    for s, e in zip(bounds[:-1], bounds[1:]):
        out.append((int(s), int(e - s), bool(b[s]), s == 0, e == len(b)))
    return out


def bw_class(width_hz):
    for lo, hi, name in BW_CLASSES:
        if lo <= width_hz < hi:
            return name
    return "broad"


def cadence(b, max_lag=20):
    """Autocorrelation of the busy sequence at lags 1..max_lag (1 s frames); returns (lag, height) of the strongest peak."""
    x = b.astype(np.float64) - b.mean()
    if x.std() == 0 or len(x) < 3 * max_lag:
        return None, None
    ac = np.array([np.dot(x[:-k], x[k:]) / np.dot(x, x) for k in range(1, max_lag + 1)])
    # a peak = local max above both neighbours
    best = None
    for k in range(1, len(ac) - 1):
        if ac[k] > ac[k - 1] and ac[k] >= ac[k + 1] and ac[k] > 0.1 and (best is None or ac[k] > ac[best]):
            best = k
    return (best + 1, float(ac[best])) if best is not None else (None, None)


def score(args):
    files = sorted(sum([glob.glob(os.path.join(d, "*.wf.npy")) for d in args.dirs], []))
    if not files:
        raise SystemExit("no .wf.npy files")
    os.makedirs(args.out, exist_ok=True)
    frames_rows, events, per = [], [], collections.defaultdict(list)       # per[(site, band, width, ch)] -> list of per-file dicts
    segrows = collections.defaultdict(collections.Counter)                # (site, band, block, daytype) -> class -> bin-seconds
    for p in files:
        side, dbm, ts, f_khz, rbw = load(p)
        site = side.get("station", "kiwi"); band = int(round(side["centre_khz"]))
        t0 = ts[0]
        ffloor = file_floor(dbm)
        frame_p25, shape_db = frame_floor(dbm)
        floor_db = np.full(len(ts), ffloor)
        segs = [segments(dbm[i], ffloor, rbw) for i in range(len(ts))]
        for i in range(len(ts)):
            occ = sum(sg[1] - sg[0] + 1 for sg in segs[i]) / (dbm.shape[1] - 2)
            frames_rows.append((site, band, dt.datetime.fromtimestamp(ts[i], dt.timezone.utc).isoformat(timespec="seconds"),
                                round(ffloor, 1), round(float(frame_p25[i]), 1), round(float(shape_db[i]), 1), round(float(occ), 3)))
            for sg in segs[i]:
                segrows[(site, band) + stratum_of(ts[i], args.utc_offset)][bw_class(sg[2])] += 1
                segrows[(site, band, "all", "all")][bw_class(sg[2])] += 1
        for width in (500, 2400):
            centres, lev, floor = channel_levels(dbm, f_khz, rbw, width, floor_db=ffloor)
            busy = lev >= args.busy_db; det = lev >= args.detect_db
            for j, c in enumerate(centres):
                b = busy[:, j]
                lag, h = cadence(b)
                # adjacent coupling: P(busy at ±3 kHz | this channel idle)
                adj = []
                for off in (-3.0, 3.0):
                    k = int(np.argmin(np.abs(centres - (c + off))))
                    if abs(centres[k] - (c + off)) < 0.15:
                        adj.append(busy[:, k])
                idle = ~b
                p_adj = float(np.mean(np.any(np.stack(adj), axis=0)[idle])) if adj and idle.sum() else float("nan")
                ev = []
                for s, n, val, cl, cr in runs_of(b):
                    if not val:
                        continue
                    seg = lev[s:s + n, j]
                    ipk = s + int(np.argmax(seg))
                    # peak bin inside the window at the peak frame
                    fr = np.nan_to_num(dbm[ipk], nan=-999)
                    w0 = int(np.argmin(np.abs(f_khz - (c - width / 2000)))); w1 = w0 + max(1, int(round(width / rbw)))
                    ib = w0 + int(np.argmax(fr[w0:w1]))
                    inside = [sg for sg in segs[ipk] if sg[0] <= ib <= sg[1]]
                    bw_hz = inside[0][2] if inside else rbw
                    ev.append((n, cl or cr, float(seg.max()), bw_hz))
                    events.append((site, band, width, round(float(c), 1), dt.datetime.fromtimestamp(t0 + ts[s] - ts[0], dt.timezone.utc)
                                   .isoformat(timespec="seconds"), n, int(cl or cr), round(float(seg.max()), 1), int(bw_hz)))
                idle_runs = [(n, cl or cr) for s, n, val, cl, cr in runs_of(b) if not val]
                per[(site, band, width, round(float(c), 1))].append({
                    "t0": t0, "n": len(b), "busy": int(b.sum()), "det": int(det[:, j].sum()),
                    "det_below_busy": int((det[:, j] & ~b).sum()),
                    "levels": lev[det[:, j], j].tolist(), "ev": ev, "idle": idle_runs, "lag": lag, "h": h, "p_adj": p_adj})
    # ---- aggregate per stratum
    def stratum(t0):
        return stratum_of(t0, args.utc_offset)
    rows = []
    for (site, band, width, c), lst in sorted(per.items()):
        groups = collections.defaultdict(list)
        for d in lst:
            groups[stratum(d["t0"])].append(d)
        groups[("all", "all")] = lst
        for (blk, dty), g in groups.items():
            n = sum(d["n"] for d in g); busy = sum(d["busy"] for d in g); det = sum(d["det"] for d in g)
            ev = sum((d["ev"] for d in g), []); idle = sum((d["idle"] for d in g), [])
            lv = np.array(sum((d["levels"] for d in g), []))
            bl = [e[0] for e in ev]; bcen = sum(e[1] for e in ev); il = [e[0] for e in idle]
            bws = collections.Counter(bw_class(e[3]) for e in ev)
            lags = [(d["lag"], d["h"]) for d in g if d["lag"]]
            rows.append({"site": site, "band_khz": band, "width_hz": width, "channel_khz": c, "block": blk, "daytype": dty,
                         "frames": n, "busy_frac": busy / n, "detect_frac": det / n,
                         "d5_tail": (det - busy) / det if det else float("nan"),
                         "n_busy_runs": len(bl), "busy_run_p50_s": q(bl, 50), "busy_run_p90_s": q(bl, 90), "busy_runs_censored": bcen,
                         "idle_run_p50_s": q(il, 50), "idle_run_p90_s": q(il, 90), "idle_runs_censored": sum(e[1] for e in idle),
                         "level_p50_db": q(lv, 50), "level_p90_db": q(lv, 90),
                         "bw_carrier": bws["carrier"], "bw_narrow": bws["narrow"], "bw_voice_wide": bws["voice_wide"], "bw_broad": bws["broad"],
                         "cadence_lag_s": collections.Counter(l for l, _ in lags).most_common(1)[0][0] if lags else "",
                         "cadence_files_frac": len(lags) / len(g),
                         "p_adjacent_busy_given_idle": float(np.nanmean(pa)) if (pa := [d["p_adj"] for d in g if d["p_adj"] == d["p_adj"]]) else float("nan")})
    import csv
    with open(os.path.join(args.out, "channels.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    with open(os.path.join(args.out, "events.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["site", "band_khz", "width_hz", "channel_khz", "start_utc", "len_s", "censored", "level_db", "bw_hz"]); w.writerows(events)
    with open(os.path.join(args.out, "frames.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["site", "band_khz", "utc", "floor_dbm_hz", "floor_frame_p25_dbm_hz", "noise_shape_db", "frac_bins_in_segments"]); w.writerows(frames_rows)
    with open(os.path.join(args.out, "segments.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["site", "band_khz", "block", "daytype", "carrier", "narrow", "voice_wide", "broad"])
        for k, c in sorted(segrows.items()):
            w.writerow(list(k) + [c["carrier"], c["narrow"], c["voice_wide"], c["broad"]])
    report(args, rows, frames_rows, files, segrows)
    plot(args, rows)
    print(f"{len(files)} files, {len(frames_rows)} frames, {len(events)} busy runs → {args.out}")


def report(args, rows, frames_rows, files, segrows):
    L = [f"# QRM occupancy — {len(files)} captures, {len(frames_rows)} frames ({args.busy_db} dB busy / {args.detect_db} dB detect passband INR over the per-frame noise-mean floor)\n"]
    fl = collections.defaultdict(list)
    for r in frames_rows:
        fl[(r[0], r[1])].append((r[3], r[4], r[5]))
    L.append("## Floor per site × band (noise MEAN, dBm/Hz; per-file time-p10 → bin-p25 estimator)\n\n| site | band kHz | frames | file floor p10 / p50 / p90 | per-frame p25 floor − file floor (dB) | noise shape median−p25 dB (3.8 = single-FFT exponential) | segments carrier/narrow/wide/broad (bin·s) |\n|---|---|---|---|---|---|---|")
    for (s, b), v in sorted(fl.items()):
        a = np.array(v); c = segrows[(s, b, "all", "all")]
        L.append(f"| {s} | {b} | {len(a)} | {q(a[:, 0], 10):.1f} / {q(a[:, 0], 50):.1f} / {q(a[:, 0], 90):.1f} | {np.mean(a[:, 1] - a[:, 0]):+.1f} | {np.mean(a[:, 2]):.1f} | "
                 f"{c['carrier']}/{c['narrow']}/{c['voice_wide']}/{c['broad']} |")
    for width in (500, 2400):
        L.append(f"\n## {width} Hz channels, all strata — the busiest 12 per site × band\n")
        L.append("| site | band | channel kHz | busy | detect | D5 tail | busy run p50/p90 s (cens.) | idle run p50/p90 s | level p50/p90 dB | bw carrier/narrow/wide/broad | cadence s | adj |")
        L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for (s, b) in sorted({(r["site"], r["band_khz"]) for r in rows}):
            sel = [r for r in rows if r["site"] == s and r["band_khz"] == b and r["width_hz"] == width and r["block"] == "all"]
            for r in sorted(sel, key=lambda r: -r["busy_frac"])[:12]:
                L.append(f"| {s} | {b} | {r['channel_khz']} | {r['busy_frac']:.2f} | {r['detect_frac']:.2f} | {r['d5_tail']:.2f} | "
                         f"{r['busy_run_p50_s']:.0f}/{r['busy_run_p90_s']:.0f} ({r['busy_runs_censored']}/{r['n_busy_runs']}) | "
                         f"{r['idle_run_p50_s']:.0f}/{r['idle_run_p90_s']:.0f} | {r['level_p50_db']:.0f}/{r['level_p90_db']:.0f} | "
                         f"{r['bw_carrier']}/{r['bw_narrow']}/{r['bw_voice_wide']}/{r['bw_broad']} | {r['cadence_lag_s']} | {r['p_adjacent_busy_given_idle']:.2f} |")
    L.append("\n## Strata (500 Hz, band mean busy fraction over channels)\n\n| site | band | block (local) | daytype | channels | mean busy | max busy | D5 tail (pooled) |\n|---|---|---|---|---|---|---|---|")
    g = collections.defaultdict(list)
    for r in rows:
        if r["width_hz"] == 500 and r["block"] != "all":
            g[(r["site"], r["band_khz"], r["block"], r["daytype"])].append(r)
    for k, v in sorted(g.items()):
        d5 = [r["d5_tail"] for r in v if r["d5_tail"] == r["d5_tail"]]
        L.append(f"| {k[0]} | {k[1]} | {k[2]} | {k[3]} | {len(v)} | {np.mean([r['busy_frac'] for r in v]):.3f} | "
                 f"{max(r['busy_frac'] for r in v):.2f} | {np.mean(d5) if d5 else float('nan'):.2f} |")
    L.append("\nLevel = passband INR: mean power in the channel window over the frame's noise mean (dB). D5 tail = detected-but-not-busy frames / detected frames (passband INR between the detect and busy thresholds: the hidden-node population).")
    L.append("Runs are censored at the dwell boundary (count shown); censored lengths enter the quantiles as lower bounds.")
    open(os.path.join(args.out, "REPORT.md"), "w").write("\n".join(L) + "\n")


def plot(args, rows):
    import matplotlib; matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    combos = sorted({(r["site"], r["band_khz"]) for r in rows})
    fig, axes = plt.subplots(len(combos), 1, figsize=(11, 2.6 * len(combos)), squeeze=False)
    for ax, (s, b) in zip(axes[:, 0], combos):
        sel = [r for r in rows if r["site"] == s and r["band_khz"] == b and r["width_hz"] == 500 and r["block"] == "all"]
        sel.sort(key=lambda r: r["channel_khz"])
        x = [r["channel_khz"] for r in sel]; y = [r["busy_frac"] for r in sel]
        ax.bar(x, y, width=0.1, color="#3B6EA5", linewidth=0)
        ax.set_ylim(0, 1); ax.set_ylabel("busy fraction"); ax.set_title(f"{s} — {b} kHz, 500 Hz channels", fontsize=10, loc="left")
        ax.grid(axis="y", color="#E5E5E5", linewidth=0.6); ax.set_axisbelow(True)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
    axes[-1, 0].set_xlabel("channel centre, kHz")
    fig.tight_layout(); fig.savefig(os.path.join(args.out, "occupancy.png"), dpi=110); plt.close(fig)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dirs", nargs="+"); ap.add_argument("--out", required=True)
    ap.add_argument("--utc-offset", type=float, default=-4, help="receiver-local hours vs UTC (US Eastern DST = -4)")
    ap.add_argument("--busy-db", type=float, default=10, help="passband INR for BUSY"); ap.add_argument("--detect-db", type=float, default=3)
    score(ap.parse_args())


if __name__ == "__main__":
    main()
