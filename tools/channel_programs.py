#!/usr/bin/env python3
"""channel_programs.py — the real-world campaign's moving-path-loss programs (B4) and their schedule emitter.

    channel_programs.py library --out DIR [--wspr-dir skywave/out/wspr-fm0-2026-09] [--seeds 3]
    channel_programs.py emit DIR/programs.json PROGRAM_ID --atten-at-0db A0 [--sub-db 0.5]
    channel_programs.py trace DIR/programs.json PROGRAM_ID [--dt 1]

A program is a piecewise-LINEAR SNR3000 path in dB against time (knots), referenced to the cell's 0 dB point.
`emit` turns it into SIM_ATTEN_SCHEDULE: atten = A0 - snr, where A0 is the staging calibration "the path loss
that gives 0 dB SNR3000 at this cell's sigma and this modem's equal-PEP TX gain"; a ramp becomes a staircase of
at most --sub-db per step (a ramp, not a jump: realistic steps are RAMPS over ~2 min, design section 1). `trace`
prints the same path at --dt seconds (t_s, snr_db) for the armstrong v2 Bench (a trace knob there is a follow-up;
its existing ARM_V2B_STEP is a held step and cannot carry a ramp).

Programs (design openarq reviews/CHANGING-CONDITIONS-CAMPAIGN-DESIGN-2026-10-02.md section 5; seeds paired across
modems, SEED = 1234 + rep*7, rep 0..seeds-1):
  P1-20m-s{SEED} / P1-40m-s{SEED}   wander: Ornstein-Uhlenbeck around 0 dB, 30 min, one realisation per seed
                                    (the same path for every modem); 20 m sigma 5 dB tau 8 min, 40 m sigma 3.5 dB
                                    tau 6 min (WSPR survey, detrended), sampled every 10 s, stationary start.
  P2dn / P2up                       drop / rise: 3 min lead at +5 (-5), a 2 min ramp to -5 (+5), hold 8 min.
  P4-{band}-{class}{k}              real-path walks: 30 min WSPR windows (2-min slots, every slot active),
                                    2 closing + 2 opening + 2 wandering per band, decoded slots shifted to mean
                                    0 dB, censored slots at -20 dB. Selection is a fixed rule, not a pick by eye:
                                    candidates are the non-overlapping 30-min windows of the band's 2-min-cadence
                                    paths with every slot active and >= 60 % decoded; delta = the OLS slope over
                                    decoded slots x 30 min; closing = the band's lowest decile of delta, opening =
                                    the highest decile, wandering = |delta| in the lowest third AND residual std
                                    above the band median; within a class the two windows nearest the class median
                                    (of delta, or of residual std for wandering), from different paths when
                                    possible, then different days.
  P5-2min / P5-6min                 fade-out + resume: 5 min at 0 dB, -20 dB for 2 (6) min, back to 0 dB for
                                    10 min (resume budget 120 s, scored by B5); steps are instantaneous (an
                                    outage, not a ramp).
P3 (grey line) is not built: it awaits its owner call (design section 11).
"""
import argparse, csv, glob, hashlib, json, math, os, sys

import numpy as np

SLOT_S = 120
WINDOW_S = 1800
CENSORED_DB = -20.0
SEED0, SEED_STEP = 1234, 7
OU = {"20m": dict(sigma_db=5.0, tau_s=480.0), "40m": dict(sigma_db=3.5, tau_s=360.0)}
BAND_OF = {"14": "20m", "7": "40m"}


# ----------------------------------------------------------------------------------------------- programs
def ou_knots(sigma_db, tau_s, seed, dur_s=WINDOW_S, dt=10.0):
    rng = np.random.default_rng(seed)
    a = math.exp(-dt / tau_s)
    b = sigma_db * math.sqrt(1.0 - a * a)
    x = rng.normal(0.0, sigma_db)
    out = []
    for k in range(int(dur_s / dt) + 1):
        out.append((round(k * dt, 3), round(float(x), 3)))
        x = a * x + b * rng.normal()
    return out


def step_knots(lead_db, end_db, lead_s=180.0, ramp_s=120.0, hold_s=480.0):
    return [(0.0, lead_db), (lead_s, lead_db), (lead_s + ramp_s, end_db), (lead_s + ramp_s + hold_s, end_db)]


def fadeout_knots(out_s, base_db=0.0, lead_s=300.0, tail_s=600.0, eps=1e-3):
    """Jumps at exactly t1 and t2 (the pre-jump knot sits eps BEFORE them), so a 1 s grid holds the outage
    for exactly out_s."""
    t1, t2 = lead_s, lead_s + out_s
    return [(0.0, base_db), (t1 - eps, base_db), (t1, CENSORED_DB), (t2 - eps, CENSORED_DB), (t2, base_db),
            (t2 + tail_s, base_db)]


def read_series(path):
    with open(path) as f:
        r = csv.DictReader(f, delimiter="\t")
        return [(int(x["epoch"]), int(x["status"]), float(x["snr_norm_db"]) if x["snr_norm_db"] else math.nan)
                for x in r]


def wspr_windows(wspr_dir):
    """Every candidate 30-min window, per band: list of dicts (path, start epoch, slots, delta, resid std)."""
    out = {"20m": [], "40m": []}
    n = WINDOW_S // SLOT_S
    for p in sorted(glob.glob(os.path.join(wspr_dir, "spots", "*.series.tsv"))):
        key = os.path.basename(p)[:-len(".series.tsv")]
        band = BAND_OF.get(key.split("_")[0])
        rows = read_series(p)
        if band is None or len(rows) < n:
            continue
        ep = np.array([r[0] for r in rows])
        if np.median(np.diff(ep)) != SLOT_S:
            continue                                      # a 6-min-duty beacon cannot make a 2-min walk
        by_t = {r[0]: r for r in rows}
        t = ep[0] - ep[0] % WINDOW_S
        while t + WINDOW_S <= ep[-1] + SLOT_S:
            # slot epochs sit on even minutes; take the n slots starting at the first on/after t
            slots = [by_t.get(t + i * SLOT_S) for i in range(n)]
            t += WINDOW_S
            if any(s is None or s[1] < 1 for s in slots):
                continue
            dec = [(i, s[2]) for i, s in enumerate(slots) if s[1] == 2 and not math.isnan(s[2])]
            if len(dec) < 0.6 * n:
                continue
            x = np.array([i for i, _ in dec], float) * SLOT_S
            y = np.array([v for _, v in dec])
            slope, icpt = np.polyfit(x, y, 1)
            resid = y - (slope * x + icpt)
            out[band].append(dict(path=key, start=int(slots[0][0]), slots=[[s[1], s[2]] for s in slots],
                                  delta=float(slope * WINDOW_S), resid_std=float(resid.std()),
                                  mean_dec=float(y.mean()), n_dec=len(dec)))
    return out


def pick_two(c, key, centre):
    """The two windows nearest `centre` in `key`, from different paths if possible, then different days."""
    c = sorted(c, key=lambda w: (abs(w[key] - centre), w["path"], w["start"]))
    got = []
    for rule in (lambda w: all(w["path"] != g["path"] for g in got),
                 lambda w: all(w["start"] // 86400 != g["start"] // 86400 for g in got),
                 lambda w: True):
        for w in c:
            if len(got) < 2 and w not in got and rule(w):
                got.append(w)
    return got


def classify(windows):
    if len(windows) < 10:
        raise SystemExit(f"too few WSPR windows ({len(windows)}) to classify")
    d = np.array([w["delta"] for w in windows])
    lo, hi = np.percentile(d, 10), np.percentile(d, 90)
    third = np.percentile(np.abs(d), 100 / 3)
    rs_med = float(np.median([w["resid_std"] for w in windows]))
    closing = [w for w in windows if w["delta"] <= lo]
    opening = [w for w in windows if w["delta"] >= hi]
    wander = [w for w in windows if abs(w["delta"]) <= third and w["resid_std"] > rs_med]
    sel = {"closing": pick_two(closing, "delta", float(np.median([w["delta"] for w in closing]))),
           "opening": pick_two(opening, "delta", float(np.median([w["delta"] for w in opening]))),
           "wandering": pick_two(wander, "resid_std", float(np.median([w["resid_std"] for w in wander])))}
    rule = dict(n_windows=len(windows), delta_p10=float(lo), delta_p90=float(hi), absdelta_p33=float(third),
                resid_std_p50=rs_med, n_closing=len(closing), n_opening=len(opening), n_wandering=len(wander))
    return sel, rule


def walk_knots(w):
    """Decoded slots at slot centres shifted to mean 0 dB; censored at CENSORED_DB; linear in between."""
    m = w["mean_dec"]
    knots = []
    for i, (st, v) in enumerate(w["slots"]):
        snr = round(v - m, 3) if st == 2 and not math.isnan(v) else CENSORED_DB
        knots.append((i * SLOT_S + SLOT_S / 2, snr))
    return [(0.0, knots[0][1])] + knots + [(float(WINDOW_S), knots[-1][1])]


def library(wspr_dir, seeds):
    progs = {}
    for rep in range(seeds):
        seed = SEED0 + rep * SEED_STEP
        for band, p in OU.items():
            progs[f"P1-{band}-s{seed}"] = dict(kind="wander", band=band, seed=seed, **p,
                                               knots=ou_knots(p["sigma_db"], p["tau_s"], seed))
    progs["P2dn"] = dict(kind="drop", knots=step_knots(5.0, -5.0))
    progs["P2up"] = dict(kind="rise", knots=step_knots(-5.0, 5.0))
    rules = {}
    for band, ws in wspr_windows(wspr_dir).items():
        sel, rules[band] = classify(ws)
        for cls, picks in sel.items():
            for k, w in enumerate(picks, 1):
                progs[f"P4-{band}-{cls}{k}"] = dict(
                    kind="walk", band=band, cls=cls, source=w["path"], start_epoch=w["start"],
                    delta_db=round(w["delta"], 2), resid_std_db=round(w["resid_std"], 2),
                    n_decoded=w["n_dec"], knots=walk_knots(w))
    for mins in (2, 6):
        progs[f"P5-{mins}min"] = dict(kind="fadeout", out_s=60 * mins, knots=fadeout_knots(60.0 * mins))
    return progs, rules


# ------------------------------------------------------------------------------------------------- emitter
def sample(knots, dt):
    t = np.arange(0.0, knots[-1][0] + 1e-9, dt)
    return t, np.interp(t, [k[0] for k in knots], [k[1] for k in knots])


def atten_schedule(knots, a0, sub_db=0.5, dt=1.0):
    """SIM_ATTEN_SCHEDULE for atten = a0 - snr, quantised to sub_db, run-length encoded on a dt grid. Jumps stay
    jumps (an outage), ramps become staircases of <= sub_db per step. The last segment holds (seconds 0)."""
    t, snr = sample(knots, dt)
    q = np.round((a0 - snr) / sub_db) * sub_db
    if (q < 0).any():
        raise ValueError(f"atten would go negative (a gain) at snr {snr.max():.1f} dB with A0 {a0:g}: "
                         "the rail gate assumes path LOSS; raise A0 or lower the program")
    segs, start = [], 0
    for i in range(1, len(q) + 1):
        if i == len(q) or q[i] != q[start]:
            segs.append((float(q[start]), (i - start) * dt))
            start = i
    segs[-1] = (segs[-1][0], 0.0)
    return ",".join(f"{db:g}:{secs:g}" for db, secs in segs)


def md5(path):
    return hashlib.md5(open(path, "rb").read()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    lb = sub.add_parser("library")
    lb.add_argument("--out", required=True)
    lb.add_argument("--wspr-dir", default=os.path.expanduser("~/tools/skywave/out/wspr-fm0-2026-09"))
    lb.add_argument("--seeds", type=int, default=3)
    em = sub.add_parser("emit")
    em.add_argument("programs"); em.add_argument("pid")
    em.add_argument("--atten-at-0db", type=float, required=True)
    em.add_argument("--sub-db", type=float, default=0.5)
    tr = sub.add_parser("trace")
    tr.add_argument("programs"); tr.add_argument("pid")
    tr.add_argument("--dt", type=float, default=1.0)
    a = ap.parse_args()
    if a.cmd == "library":
        progs, rules = library(a.wspr_dir, a.seeds)
        os.makedirs(a.out, exist_ok=True)
        series = sorted(glob.glob(os.path.join(a.wspr_dir, "spots", "*.series.tsv")))
        man = dict(meta=dict(tool_md5=md5(os.path.abspath(__file__)), wspr_dir=a.wspr_dir,
                             wspr_series_md5={os.path.basename(p): md5(p) for p in series}, seeds=a.seeds,
                             p4_rule=rules, censored_db=CENSORED_DB,
                             reference="SNR3000 dB relative to the cell's 0 dB point; atten = A0 - snr"),
                   programs=progs)
        p = os.path.join(a.out, "programs.json")
        json.dump(man, open(p, "w"), indent=1)
        for pid, pr in progs.items():
            ks = pr["knots"]
            print(f"{pid:<26} {pr['kind']:<8} {ks[-1][0] / 60:5.1f} min  snr {min(k[1] for k in ks):+6.1f} .. "
                  f"{max(k[1] for k in ks):+5.1f} dB" + (f"  {pr['source']} delta {pr['delta_db']:+.1f}"
                                                          if pr["kind"] == "walk" else ""))
        print(f"programs.json md5 {md5(p)}")
    else:
        pr = json.load(open(a.programs))["programs"][a.pid]
        if a.cmd == "emit":
            print(atten_schedule(pr["knots"], a.atten_at_0db, a.sub_db))
        else:
            for t, v in zip(*sample(pr["knots"], a.dt)):
                print(f"{t:g}\t{v:.3f}")


if __name__ == "__main__":
    main()
