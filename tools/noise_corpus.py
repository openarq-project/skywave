#!/usr/bin/env python3
"""noise_corpus.py — extract a real HF noise corpus from fixed-gain FT8 campaign captures.

FT8 transmits for 12.64 s of every 15 s slot; the remaining ~2.4 s is the band with no FT8 in it.
For each capture (needs `agc_gain` + `rssi_gap_dbm` in the sidecar and the decodes in est/), cut
the gap segments that no DECODED signal extends into, resample to 12 kHz, and store them with an
absolute level anchor: the slot-gap S-meter reading (dBm in 3 kHz) is the power of exactly this
kind of segment, so each capture gets a file-units → dBm scale from its own gap RMS.

Per segment: level (dBm/Hz), amplitude kurtosis (Gaussian = 3; impulsive ≫ 3), fraction of
samples beyond 4σ, spectral tilt (dB across 300–2700 Hz, LS fit of the log spectrum), and the
tone fraction (share of power in the 20 strongest 6.25 Hz bins — carriers/birdies).

    noise_corpus.py extract --campaign DIR [--out DIR/noise]
    noise_corpus.py report  --out DIR/noise
Output: <out>/segments/<band>_<utcstamp>_s<k>.npy (int16 @12 kHz), index.csv, REPORT.md, noise.png.
Caveat: undecoded weak signals and non-FT8 users can still be present; kurtosis/tone fraction flag them.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import glob
import json
import math
import os
import wave

import numpy as np
from math import gcd
from scipy.signal import resample_poly

FS = 12000
SIG_END_S = 0.5 + 79 * 0.16        # 13.14 s after the slot boundary for dt = 0


def load_12k(path):
    with wave.open(path) as w:
        fs = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(float) / 32768
    if fs != FS:
        g = gcd(int(fs), FS)
        x = resample_poly(x, FS // g, int(fs) // g)
    return x


def seg_stats(x):
    x = x - np.mean(x)
    s = np.std(x)
    kurt = float(np.mean(x ** 4) / s ** 4) if s > 0 else float("nan")
    frac4 = float(np.mean(np.abs(x) > 4 * s)) if s > 0 else float("nan")
    n = len(x)
    P = np.abs(np.fft.rfft(x * np.hanning(n))) ** 2
    fr = np.fft.rfftfreq(n, 1 / FS)
    m = (fr >= 300) & (fr <= 2700)
    lp = 10 * np.log10(P[m] + 1e-30)
    slope = np.polyfit(fr[m], lp, 1)[0] * 2400            # dB across the passband
    # tone fraction: power in the top-20 6.25 Hz bins vs total, on 0.16 s blocks
    nb = int(0.16 * FS)
    blocks = x[: (n // nb) * nb].reshape(-1, nb)
    Pb = np.mean(np.abs(np.fft.rfft(blocks, axis=1)) ** 2, axis=0)
    fb = np.fft.rfftfreq(nb, 1 / FS)
    mb = (fb >= 300) & (fb <= 2700)
    top = np.sort(Pb[mb])[::-1]
    tone_frac = float(top[:20].sum() / top.sum())
    return {"rms": float(np.sqrt(np.mean(x ** 2))), "kurtosis": kurt, "frac_gt4sigma": frac4,
            "tilt_db": float(slope), "tone_frac": tone_frac}


def extract(args):
    out = args.out or os.path.join(args.campaign, "noise")
    os.makedirs(os.path.join(out, "segments"), exist_ok=True)
    rows = []
    for jpath in sorted(glob.glob(os.path.join(args.campaign, "raw", "*.json"))):
        side = json.load(open(jpath))
        if side.get("agc_gain") is None or side.get("rssi_gap_dbm") is None or "slot_phase_s" not in side:
            continue
        wav = jpath[:-5] + ".wav"
        est = os.path.join(args.campaign, "est", os.path.basename(jpath)[:-5] + ".jsonl")
        decs = [json.loads(l) for l in open(est)] if os.path.exists(est) else []
        x = load_12k(wav)
        t0 = dt.datetime.fromisoformat(side["t0_utc"])
        b = side["slot_phase_s"]
        nslots = side["slots"]
        # per-slot latest signal end and earliest next-slot start, from the decodes
        by_slot = {}
        for d in decs:
            k = int(round((dt.datetime.fromisoformat(d["slot_utc"]) - t0).total_seconds() - b) / 15)
            by_slot.setdefault(k, []).append(d["start_s"])          # start_s = dt (WSJT-X convention)
        # calibration: the gap S-meter is the power of a gap segment in the 3 kHz passband
        gap_rms = []
        segs = []
        for k in range(nslots):
            t_start = b + 15 * k + SIG_END_S + max([0.0] + [s for s in by_slot.get(k, [])]) + 0.05
            t_end = b + 15 * (k + 1) + 0.5 + min([0.0] + [s for s in by_slot.get(k + 1, [])]) - 0.05
            if t_end - t_start < 1.0 or t_end * FS > len(x):
                continue
            seg = x[int(t_start * FS):int(t_end * FS)]
            st = seg_stats(seg)
            gap_rms.append(st["rms"])
            segs.append((k, t_start, seg, st))
        if not segs:
            continue
        # file-units → dBm: the median gap RMS corresponds to rssi_gap_dbm (3 kHz passband)
        cal_db = side["rssi_gap_dbm"] - 20 * math.log10(np.median(gap_rms))
        for k, t_start, seg, st in segs:
            name = f"{side['band_khz']}_{t0.strftime('%Y%m%dT%H%M%S')}_s{k}"
            np.save(os.path.join(out, "segments", name + ".npy"), np.clip(seg * 32768, -32768, 32767).astype(np.int16))
            level_dbm_hz = 20 * math.log10(st["rms"]) + cal_db - 10 * math.log10(3000)
            rows.append({"segment": name, "band_khz": side["band_khz"], "utc": (t0 + dt.timedelta(seconds=t_start)).isoformat(),
                         "utc_hour": round(((t0 + dt.timedelta(seconds=t_start)).hour + (t0 + dt.timedelta(seconds=t_start)).minute / 60), 2),
                         "seconds": round(len(seg) / FS, 2), "level_dbm_hz": round(level_dbm_hz, 2),
                         "kurtosis": round(st["kurtosis"], 3), "frac_gt4sigma": round(st["frac_gt4sigma"], 5),
                         "tilt_db": round(st["tilt_db"], 2), "tone_frac": round(st["tone_frac"], 4),
                         "rssi_gap_dbm": side["rssi_gap_dbm"], "kiwi": side["kiwi"]})
    with open(os.path.join(out, "index.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    print(f"{len(rows)} segments, {sum(r['seconds'] for r in rows) / 60:.1f} min of noise → {out}")


SURFACE, INK, INK2, INK3, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#8a8985", "#e6e5e1"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]


def q(v, p):
    return float(np.percentile(v, p)) if len(v) else float("nan")


def report(args):
    rows = list(csv.DictReader(open(os.path.join(args.out, "index.csv"))))
    for r in rows:
        for k in ("band_khz", "utc_hour", "seconds", "level_dbm_hz", "kurtosis", "frac_gt4sigma", "tilt_db", "tone_frac"):
            r[k] = float(r[k])
    bands = sorted(set(int(r["band_khz"]) for r in rows))
    L = [f"# HF noise corpus — {args.out}\n",
         f"{len(rows)} slot-gap segments, {sum(r['seconds'] for r in rows) / 60:.1f} min, bands {bands} kHz, "
         f"{min(r['utc'] for r in rows)} → {max(r['utc'] for r in rows)} UTC. Level = dBm/Hz anchored to the receiver's "
         "slot-gap S-meter; kurtosis 3 = Gaussian; tilt = dB change across 300–2700 Hz; tone fraction = share of power "
         "in the 20 strongest 6.25 Hz bins (0.02 for white noise).\n",
         "| band | UTC h | segs | level p50 | level p10/p90 | kurtosis p50 | kurtosis p90 | >4σ p90 | tilt p50 | tone frac p50/p90 |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for b in bands:
        for h in range(24):
            rr = [r for r in rows if int(r["band_khz"]) == b and int(r["utc_hour"]) == h]
            if not rr:
                continue
            L.append(f"| {b} | {h:02d} | {len(rr)} | {q([r['level_dbm_hz'] for r in rr], 50):.1f} | "
                     f"{q([r['level_dbm_hz'] for r in rr], 10):.1f}/{q([r['level_dbm_hz'] for r in rr], 90):.1f} | "
                     f"{q([r['kurtosis'] for r in rr], 50):.2f} | {q([r['kurtosis'] for r in rr], 90):.2f} | "
                     f"{q([r['frac_gt4sigma'] for r in rr], 90):.5f} | {q([r['tilt_db'] for r in rr], 50):+.1f} | "
                     f"{q([r['tone_frac'] for r in rr], 50):.3f}/{q([r['tone_frac'] for r in rr], 90):.3f} |")
    open(os.path.join(args.out, "REPORT.md"), "w").write("\n".join(L) + "\n")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(3, 1, figsize=(12, 9), facecolor=SURFACE, sharex=True)
    for ax in axes:
        ax.set_facecolor(SURFACE); ax.grid(True, color=GRID, linewidth=0.8); ax.set_axisbelow(True)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        ax.tick_params(colors=INK2, labelsize=8)
    t_ref = min(dt.datetime.fromisoformat(r["utc"]) for r in rows)
    for i, b in enumerate(bands):
        rr = [r for r in rows if int(r["band_khz"]) == b]
        th = [(dt.datetime.fromisoformat(r["utc"]) - t_ref).total_seconds() / 3600 for r in rr]
        axes[0].scatter(th, [r["level_dbm_hz"] for r in rr], s=8, color=SERIES[i], linewidths=0, label=f"{b} kHz")
        axes[1].scatter(th, [r["kurtosis"] for r in rr], s=8, color=SERIES[i], linewidths=0)
        axes[2].scatter(th, [r["tone_frac"] for r in rr], s=8, color=SERIES[i], linewidths=0)
    axes[0].set_ylabel("noise, dBm/Hz"); axes[0].set_title("Slot-gap noise density", fontsize=10, loc="left")
    axes[0].legend(fontsize=8, frameon=False, labelcolor=INK2)
    axes[1].set_ylabel("kurtosis"); axes[1].set_yscale("log"); axes[1].axhline(3, color=INK3, linewidth=0.8)
    axes[1].set_title("Amplitude kurtosis (3 = Gaussian)", fontsize=10, loc="left")
    axes[2].set_ylabel("tone fraction"); axes[2].set_ylim(0, 1); axes[2].set_title("Share of power in the 20 strongest 6.25 Hz bins", fontsize=10, loc="left")
    axes[2].set_xlabel(f"hours since {t_ref.strftime('%Y-%m-%d %H:%M')} UTC")
    fig.tight_layout(); fig.savefig(os.path.join(args.out, "noise.png"), dpi=110, facecolor=SURFACE); plt.close(fig)
    print(f"report → {os.path.join(args.out, 'REPORT.md')}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("extract"); p.add_argument("--campaign", required=True); p.add_argument("--out", default="")
    p = sub.add_parser("report"); p.add_argument("--out", required=True)
    a = ap.parse_args()
    {"extract": extract, "report": report}[a.cmd](a)


if __name__ == "__main__":
    main()
