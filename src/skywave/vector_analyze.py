#!/usr/bin/env python3
"""Analysis for VectorAdapter campaigns: floors, Pareto frontier, capability chart.

Reads a `vector_sweep` CSV. Adapter-aware: when a sweep contains rows from more
than one adapter, modes are keyed `adapter:label` so armstrong and modem73 modes
can share one frontier — the payoff of a single contract and a single SNR
convention.

FLOOR METRIC (codec2-modefloor §11 amendment). Definitions are written out rather
than left to a function name, because that ambiguity is exactly what went wrong
before:

  scan the curve in ASCENDING SNR (FER falls as SNR rises)
  floor_db       FIRST SNR at which FER <= target   -- OPTIMISTIC
  floor_last_db  LAST crossing from >target to <=target -- CONSERVATIVE
                 (equal to floor_db on a monotone curve; higher otherwise)

Two gates guard the number:
  onset       min(FER) must reach --onset, else "never_worked"
  truncation  FER at the HIGHEST measured SNR must be <= target, else
              "truncated_high" -- the defect that contaminated an earlier run

PARETO FRONTIER: at each SNR, the mode with the highest goodput. A mode is on the
frontier if it wins at >= 1 SNR point. Everything strictly beneath it everywhere
is dominated and should never be selected.

  python3 -m skywave.vector_analyze --sweep out/joint.csv --outdir out
"""
import argparse
import csv
import os
import textwrap
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt          # noqa: E402

# Reference categorical palette, light mode, fixed order, never cycled.
# Validated: worst adjacent CVD dE 9.1, normal-vision 19.6 (all checks pass).
# Three slots sit below 3:1 on the light surface, so the relief rule applies --
# discharged by direct labels plus the CSVs as the table view.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
          "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
SURFACE, INK, INK2, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#b4b3ad"
MAX_SERIES = len(SERIES)

PRESET_ORDER = ["off", "good", "moderate", "poor"]
PRESET_TITLE = {
    "off": "AWGN (no fading)",
    "good": "CCIR good  0.5 ms / 0.1 Hz",
    "moderate": "CCIR moderate  1.0 ms / 0.5 Hz",
    "poor": "CCIR poor  2.0 ms / 1.0 Hz",
}


def load_sweep(path):
    """-> (curves, meta, multi_adapter). curves[(key,preset)] = [(snr,fer,gp,frames)]"""
    rows = list(csv.DictReader(open(path)))
    if not rows:
        raise SystemExit(f"vector_analyze: no rows in {path}")
    adapters = {r.get("adapter", "") for r in rows}
    multi = len(adapters) > 1
    curves, meta = defaultdict(list), {}
    for r in rows:
        key = f"{r['adapter']}:{r['label']}" if multi else r["label"]
        curves[(key, r["preset"])].append(
            (float(r["snr_db"]), float(r["fer"]), float(r["goodput_bps"]),
             int(r["frames"])))
        meta[key] = dict(
            adapter=r.get("adapter", ""), label=r["label"],
            family=r.get("family", ""),
            payload_bytes=int(r["payload_bytes"] or 0),
            air_s=float(r["air_s"] or 0),
            bandwidth_hz=int(r["bandwidth_hz"] or 0),
            sample_rate=r.get("sample_rate", ""),
            bw_hz=r.get("bw_hz", ""),
            nominal_bps=float(r["nominal_bps"] or 0),
            papr_db=r.get("papr_db", ""))
    for k in curves:
        curves[k].sort(key=lambda t: t[0])
    return curves, meta, multi


def interp_cross(s0, f0, s1, f1, target):
    if f0 == f1:
        return s1
    return s0 + ((f0 - target) / (f0 - f1)) * (s1 - s0)


#: Threshold comparisons need a tolerance, and it is load-bearing. With N frames
#: per cell the achievable FER is a multiple of 1/N, so an integer failure count
#: can land EXACTLY on a threshold: at N=150, 3/150 is precisely the 0.02 onset.
#: Such a curve passes only if the CSV rounded it to "0.020000" and fails if the
#: same value is recomputed as 1-147/150 (0.020000000000000018) -- same data,
#: opposite verdict, decided by CSV precision. Four of the modem73 deep corpus's
#: 256 curves sat on that boundary.
EPS = 1e-9


def floors(curve, target, onset):
    """-> (floor_db, floor_last_db, status). See the module docstring."""
    if len(curve) < 2:
        return (None, None, "too_few_points")
    if min(f for _, f, _, _ in curve) > onset + EPS:
        return (None, None, "never_worked")
    if curve[-1][1] > target + EPS:
        return (None, None, "truncated_high")
    first = last = None
    for i in range(1, len(curve)):
        s0, f0 = curve[i - 1][0], curve[i - 1][1]
        s1, f1 = curve[i][0], curve[i][1]
        if f0 > target >= f1:
            x = interp_cross(s0, f0, s1, f1, target)
            if first is None:
                first = x
            last = x
    if first is None:
        return (curve[0][0], curve[0][0], "truncated_low")
    return (first, last, "ok")


def pareto(curves, preset, max_fer=None):
    """Pareto frontier by goodput at each SNR.

    `max_fer` is a USABILITY BAR and it matters more than it looks. Goodput is
    `rate * (1 - FER)`, so a high-rate mode limping at FER 0.4 still books ~60%
    of a large number and can top a raw-goodput frontier -- while the floor table
    in the same report calls that mode `never_worked`, because it never reaches
    FER 0.10. Two outputs then contradict each other, and the frontier is the one
    that is wrong: nobody selects a mode that drops 2 frames in 5.

    Default bar is the FER target, matching armstrong's in-tree bench, which
    calls such a cell NOT USABLE rather than a datum.
    """
    per_snr = defaultdict(list)
    for (key, p), pts in curves.items():
        if p != preset:
            continue
        for snr, fer, gp, _ in pts:
            if max_fer is not None and fer > max_fer:
                continue
            per_snr[round(snr, 3)].append((gp, key))
    winners, order = {}, []
    for snr in sorted(per_snr, reverse=True):
        if not per_snr[snr]:
            continue
        gp, key = max(per_snr[snr], key=lambda t: (t[0], t[1]))
        if gp <= 0:
            continue
        winners[snr] = (key, gp)
        if key not in order:
            order.append(key)
    return winners, order


def _gp_series(pts):
    """Goodput with non-positive values masked to NaN: a cell at FER 1.0 has zero
    goodput, which on a log axis would plunge to the panel floor and draw a
    spurious cliff. Breaking the line is the honest mark."""
    return ([s for s, _, _, _ in pts],
            [gp if gp > 0 else float("nan") for _, _, gp, _ in pts])


def plot(curves, presets, hulls, outpath, target, fer_floor, subtitle):
    n = len(presets)
    fig_w = max(5.0 * n, 9.0)
    fig, axes = plt.subplots(2, n, figsize=(fig_w, 8.8), squeeze=False)
    fig.patch.set_facecolor(SURFACE)
    handles, seen = [], []

    # Colour follows the ENTITY, not its rank in a panel: assigning per-panel
    # would give one hue to different modes in different panels and make the
    # shared legend ambiguous.
    # Allocate colour slots ROUND-ROBIN across panels, not first-come from the
    # union. Taken in preset order the easiest channel's frontier consumes every
    # slot and the hardest panel -- the interesting one -- ends up with no
    # coloured members: a real run hid the mode topping CCIR poor inside the grey
    # cloud while the text reported it as the headline.
    ranked = [list(hulls[p][1]) for p in presets]
    union, i = [], 0
    while i <= max((len(r) for r in ranked), default=0):
        for r in ranked:
            if i < len(r) and r[i] not in union:
                union.append(r[i])
        i += 1
    colour = {k: SERIES[j] for j, k in enumerate(union[:MAX_SERIES])}
    folded = len(union) - len(colour)

    for col, preset in enumerate(presets):
        winners, order = hulls[preset]
        shown = [k for k in order if k in colour]
        for row, metric in enumerate(("goodput", "fer")):
            ax = axes[row][col]
            ax.set_facecolor(SURFACE)
            for sp in ("top", "right"):
                ax.spines[sp].set_visible(False)
            for sp in ("left", "bottom"):
                ax.spines[sp].set_color(MUTED)
            ax.tick_params(colors=INK2, labelsize=9, length=3)
            ax.grid(True, color=MUTED, linewidth=0.5, alpha=0.45)
            ax.set_axisbelow(True)

            for (key, p), pts in curves.items():
                if p != preset or key in colour:
                    continue
                xs, ys = (_gp_series(pts) if metric == "goodput"
                          else ([s for s, _, _, _ in pts],
                                [max(f, fer_floor) for _, f, _, _ in pts]))
                ax.plot(xs, ys, color=MUTED, linewidth=0.7, alpha=0.4, zorder=1)

            for key in shown:
                pts = curves[(key, preset)]
                xs, ys = (_gp_series(pts) if metric == "goodput"
                          else ([s for s, _, _, _ in pts],
                                [max(f, fer_floor) for _, f, _, _ in pts]))
                ln, = ax.plot(xs, ys, color=colour[key], linewidth=2.0,
                              zorder=3, solid_capstyle="round",
                              marker="o", markersize=4.0)
                if key not in seen:
                    seen.append(key)
                    handles.append(ln)

            if metric == "goodput":
                ex = sorted(winners)
                if ex:
                    ln, = ax.plot(ex, [winners[s][1] for s in ex], color=INK,
                                  linewidth=2.2, alpha=0.9, zorder=5,
                                  marker="o", markersize=3.5,
                                  linestyle=(0, (6, 3)))
                    if "best achievable" not in seen:
                        seen.append("best achievable")
                        handles.append(ln)
                ax.set_yscale("log")
                if col == 0:
                    ax.set_ylabel("goodput (bps, log)", color=INK2, fontsize=10)
                ax.set_title(PRESET_TITLE.get(preset, preset), color=INK,
                             fontsize=10.5, pad=8)
                x0, x1 = ax.get_xlim()
                for key in shown[:1]:
                    pts = curves[(key, preset)]
                    best = max(pts, key=lambda t: t[2])
                    if best[2] > 0:
                        right = best[0] > (x0 + x1) / 2
                        ax.annotate(key, (best[0], best[2]),
                                    textcoords="offset points",
                                    xytext=(-5 if right else 5, 5),
                                    ha="right" if right else "left",
                                    fontsize=7, color=INK2, annotation_clip=True)
            else:
                ax.set_yscale("log")
                ax.set_ylim(fer_floor * 0.7, 1.6)
                ax.axhline(target, color=INK2, linewidth=1.0,
                           linestyle=(0, (4, 3)), alpha=0.8, zorder=2)
                if col == 0:
                    ax.set_ylabel("FER (log)", color=INK2, fontsize=10)
                    ax.annotate(f"FER {target:g}", (ax.get_xlim()[0], target),
                                textcoords="offset points", xytext=(4, 4),
                                fontsize=7, color=INK2)
                ax.set_xlabel("SNR (dB)", color=INK2, fontsize=10)

    fig.suptitle("PHY mode capability: goodput frontier and FER waterfalls",
                 color=INK, fontsize=12.5, y=0.985)
    cap = (f"{subtitle}  One-way PHY only - no ARQ, CSMA or turnaround airtime. "
           f"Independent block fading per frame. FER target {target:g}; zero-FER "
           f"cells drawn at the axis floor ({fer_floor:.3f} = 0.5/frames). "
           f"Grey = all other modes measured.")
    if folded:
        cap += (f" {folded} further frontier member(s) beyond {MAX_SERIES} "
                f"colour slots are in the grey cloud; see frontier.txt.")
    lines = textwrap.wrap(cap, max(60, int(fig_w * 9.5)))
    fig.text(0.5, 0.955, "\n".join(lines), ha="center", va="top",
             color=INK2, fontsize=8.5, linespacing=1.5)
    if handles:
        fig.legend(handles, seen, loc="lower center", frameon=False,
                   ncol=min(5, len(handles)), fontsize=8, labelcolor=INK2,
                   bbox_to_anchor=(0.5, 0.0))
    fig.tight_layout(rect=(0, 0.065, 1, 0.945 - 0.019 * len(lines)))
    fig.savefig(outpath, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    return folded


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sweep", required=True)
    ap.add_argument("--outdir", default="out")
    ap.add_argument("--target", type=float, default=0.10)
    ap.add_argument("--onset", type=float, default=0.02)
    ap.add_argument("--frontier-max-fer", type=float, default=-1.0,
                    help="usability bar for frontier membership; "
                         "default = --target. Use 1.0 for a raw "
                         "goodput frontier (rarely what you want).")
    ap.add_argument("--png", default="")
    a = ap.parse_args(argv)

    curves, meta, multi = load_sweep(a.sweep)
    os.makedirs(a.outdir, exist_ok=True)
    presets = [p for p in PRESET_ORDER if any(k[1] == p for k in curves)]
    presets += sorted({k[1] for k in curves} - set(presets))

    fpath = os.path.join(a.outdir, "floors.csv")
    status = defaultdict(int)
    with open(fpath, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["key", "adapter", "label", "family", "preset", "floor_db",
                    "floor_last_db", "status", "peak_goodput_bps",
                    "payload_bytes", "air_s", "bandwidth_hz", "sample_rate",
                    "bw_hz", "nominal_bps", "points", "frames_per_point",
                    "papr_db"])
        for (key, preset), pts in sorted(curves.items()):
            lo, hi, st = floors(pts, a.target, a.onset)
            status[st] += 1
            m = meta[key]
            w.writerow([key, m["adapter"], m["label"], m["family"], preset,
                        f"{lo:.2f}" if lo is not None else "",
                        f"{hi:.2f}" if hi is not None else "", st,
                        f"{max(p[2] for p in pts):.1f}", m["payload_bytes"],
                        f"{m['air_s']:.4f}", m["bandwidth_hz"],
                        m["sample_rate"], m["bw_hz"],
                        f"{m['nominal_bps']:.1f}", len(pts),
                        min(p[3] for p in pts), m["papr_db"]])

    bar = a.target if a.frontier_max_fer < 0 else a.frontier_max_fer
    hulls = {p: pareto(curves, p, bar) for p in presets}
    raw = {p: pareto(curves, p, None) for p in presets}
    union = []
    for p in presets:
        for k in hulls[p][1]:
            if k not in union:
                union.append(k)
    with open(os.path.join(a.outdir, "frontier.txt"), "w") as f:
        f.write("# Pareto frontier members (union over presets), first-win order\n")
        for k in union:
            f.write(k + "\n")
    with open(os.path.join(a.outdir, "frontier.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["preset", "snr_db", "best_mode", "goodput_bps"])
        for p in presets:
            win, _ = hulls[p]
            for snr in sorted(win, reverse=True):
                k, gp = win[snr]
                w.writerow([p, f"{snr:.2f}", k, f"{gp:.1f}"])

    min_frames = min(p[3] for pts in curves.values() for p in pts)
    fer_floor = max(1e-4, 0.5 / max(min_frames, 1))
    adapters = sorted({m["adapter"] for m in meta.values()})
    bws = sorted({m["bw_hz"] for m in meta.values()})
    subtitle = (f"adapters: {', '.join(adapters)} | reference BW "
                f"{', '.join(bws)} Hz | frontier requires FER <= {bar:g}.")
    png = a.png or os.path.join(a.outdir, "capability.png")
    folded = plot(curves, presets, hulls, png, a.target, fer_floor, subtitle)

    modes = {k[0] for k in curves}
    print(f"vector_analyze: {len(modes)} modes x {len(presets)} preset(s) = "
          f"{len(curves)} curves | adapters: {', '.join(adapters)}")
    print("vector_analyze: floor status ->", dict(status))
    for p in presets:
        _, order = hulls[p]
        _, rorder = raw[p]
        print(f"vector_analyze: {p:9s} frontier {len(order):3d} of {len(modes):3d}"
              + (f"  top: {order[0]}" if order else ""))
        if rorder != order:
            print(f"vector_analyze: {p:9s}   (raw-goodput frontier would be "
                  f"{len(rorder)} with top {rorder[0] if rorder else None} -- "
                  f"differs because some cells sit above FER {bar:g}; the "
                  "usability bar is what removed them)")
    if folded:
        print(f"vector_analyze: {folded} frontier member(s) beyond {MAX_SERIES} "
              "colour slots drawn in the grey cloud")
    print(f"vector_analyze: wrote {fpath}, frontier.txt, frontier.csv, {png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
