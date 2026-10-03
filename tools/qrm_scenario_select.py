#!/usr/bin/env python3
"""qrm_scenario_select.py — cut one QRM replay manifest per real-world-campaign scenario (B2).

    qrm_scenario_select.py features.csv --out-dir DIR [--per 8] [--root ~/qrm-replay] [--seed 20261002]
    qrm_scenario_select.py --verify ROOT DIR/scenarios.json [--alias out=qrm-pilot-bench1-2026-09-11 ...] [--md5]

Writes DIR/scenarios.json (per scenario: filters, fading, ends, slice lists, a ready env block) and DIR/SCENARIOS.md.
Deterministic: same features.csv => same manifest. Re-run after each IQ mirror.

Design: openarq reviews/CHANGING-CONDITIONS-CAMPAIGN-DESIGN-2026-10-02.md §2/§3 (scenarios), §11 Q5 (moderate twins),
Q6 (two-site links). Strata, exclusions (ADC overload, peak cap) and the median-closest `pick` are
qrm_bench_select.py's, verbatim; this tool adds the scenario filters on top:

  S1  busy_gw   · 20 m · local 08-18 · weekday     fading good          ends SHARED (Q6: too thin per site)
  S2  busy_park · 20 m · local 08-18 · weekday     fading good          ends by rule
  S3  NATURAL DRAW · 30 m · local 08-18 · any day  fading good          ends by rule
  S4  busy_park · 40 m · local 08-18 · weekday     custom 0.05 Hz / 0.5 ms  ends by rule
  S5  busy_park · 40 m · local 20-04 · weekday     fading moderate      ends by rule
  S6  contest   · 40 m (CQ WW RTTY 09-26/27)       fading moderate      ends SHARED (Q6)
  S7  quiet     · all bands                        fading good          ends by rule
  S1m/S2m/S3m = S1/S2/S3's slice lists with Watterson moderate (Q5).

Hours: local_hour h in [8, 18) is "08-18"; [20, 24) or [0, 4) is "20-04" (wraps midnight).
S3 is a seeded RANDOM sample of the band-hour population as it occurs (not conditioned on a class, not the
median-closest pick), after the same exclusions, one slice per capture, EXCLUDING the 10133 kHz +2000 Hz dial
slice (FT8 at 10136 kHz: not a channel an ARQ session sits on). That exclusion is a definition the design's
§3 row does not spell out; it is recorded in the manifest.
laurelspringsNC is excluded from every scenario: an ungated alternate receiver whose 09-29/30 rows the
2026-09-30 ruling made legacy/informational (and it has <= 6 slices in any scenario).

Ends (Q6, ruled 2026-10-02). A cell has two receivers: station B hears SIM_QRM_REPLAY (the A->B list), station A
hears SIM_QRM_REPLAY_BA. Rule, deterministic: if coventryOH and the best NC site (most candidates; ties by name)
each have >= MIN_END (4) candidates, end A = coventryOH and end B = that NC site — the natural 600-700 km
Ohio-Carolinas 40/20 m path; else the two stations with the most candidates (ties by name) if both have
>= MIN_END; else one SHARED list for both directions (playlist seeds 33/44 still pick different files),
labelled. Each end takes up to --per slices from its own site. S1 and S6 are SHARED by the Q6 ruling whatever
the rule says (reported when the rule would differ).

No capture is used twice across S1..S7 (cut in that order: the thinnest stratum first, the all-band quiet
stratum last). Paths in the env block are `{root}/{dir}/{file}.wav@{dial}` — the Mac corpus layout
(~/qrm-carrier/out*, where features.csv was scored); stage that layout under --root on the bench box.
channel_sim expands `~`.
"""
import argparse, hashlib, json, os, sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qrm_bench_select import STRATA, pick, prepare  # noqa: E402

MIN_END = 4
EXCLUDED_STATIONS = ("laurelspringsNC",)
OHIO = "coventryOH"
FADING = {
    "good": {"SIM_WATTERSON": "good"},
    "moderate": {"SIM_WATTERSON": "moderate"},
    "custom-0.05Hz-0.5ms": {"SIM_FADE_DOPPLER_HZ": "0.05", "SIM_FADE_DELAY_MS": "0.5"},
}
RULE = {n: r for n, _, r in STRATA}
KEY = {n: k for n, k, _ in STRATA}


def day(d):
    return (d.local_hour >= 8) & (d.local_hour < 18)


def night(d):
    return (d.local_hour >= 20) | (d.local_hour < 4)


def weekday(d):
    return d.weekend.eq(0)


# id, title, stratum (None = natural draw), filter, fading, forced ends ("shared" or None = rule)
SCENARIOS = [
    ("S1", "20 m day, Winlink gateway channel", "busy_gw",
     lambda d: d.band.eq("20m") & day(d) & weekday(d), "good", "shared"),
    ("S2", "20 m day, parking channel", "busy_park",
     lambda d: d.band.eq("20m") & day(d) & weekday(d), "good", None),
    ("S3", "30 m day, typical channel (natural draw)", None,
     lambda d: d.band.eq("30m") & day(d) & ~d.ft8, "good", None),
    ("S4", "40 m day, short path", "busy_park",
     lambda d: d.band.eq("40m") & day(d) & weekday(d), "custom-0.05Hz-0.5ms", None),
    ("S5", "40 m night", "busy_park",
     lambda d: d.band.eq("40m") & night(d) & weekday(d), "moderate", None),
    ("S6", "40 m contest evening (CQ WW RTTY)", "contest",
     lambda d: d.band.eq("40m"), "moderate", "shared"),
    ("S7", "quiet control", "quiet", lambda d: d.band.notna(), "good", None),
]
TWINS = [("S1m", "S1"), ("S2m", "S2"), ("S3m", "S3")]
FILTER_TEXT = {
    "S1": "busy_gw, 20m, local 08-18, weekday", "S2": "busy_park, 20m, local 08-18, weekday",
    "S3": "natural draw, 30m, local 08-18, any day, excl. the FT8 slice (10133 kHz @ +2000)",
    "S4": "busy_park, 40m, local 08-18, weekday", "S5": "busy_park, 40m, local 20-04, weekday",
    "S6": "contest (CQ WW RTTY 09-26/27), 40m", "S7": "quiet, all bands",
}


def candidates(ok, stratum, flt):
    c = ok[flt(ok)]
    if stratum is not None:
        c = c[RULE[stratum](c)]
    return c


def natural(c, n, used, seed):
    """Seeded random draw, one slice per capture, no capture already used."""
    c = c[~c.file.isin(used)].sort_values(["file", "dial_hz"])
    if c.empty:
        return c
    rng = np.random.default_rng(seed)
    c = c.iloc[rng.permutation(len(c))].drop_duplicates("file")
    return c.head(n)


def choose_ends(c):
    """(end_a_station, end_b_station) by the Q6 rule, or None (shared)."""
    n = c.station.value_counts()
    nc = sorted((s for s in n.index if s.endswith("NC")), key=lambda s: (-n[s], s))
    if n.get(OHIO, 0) >= MIN_END and nc and n[nc[0]] >= MIN_END:
        return OHIO, nc[0]
    top = sorted(n.index, key=lambda s: (-n[s], s))
    if len(top) >= 2 and n[top[1]] >= MIN_END:
        return top[0], top[1]
    return None


def take(c, stratum, n, used, seed):
    return natural(c, n, used, seed) if stratum is None else pick(c, KEY[stratum], n, used)


def entry(r):
    return dict(file=r.file, dir=r.dir, dial_hz=int(r.dial_hz), station=r.station, band=r.band,
                start_utc=r.start_utc, local_hour=int(r.local_hour), busy10=float(r.busy10),
                inr_p50=float(r.inr_p50), inr_p90=float(r.inr_p90), peak_sigma=float(r.peak_sigma),
                floor_dbm_hz=None if pd.isna(r.floor_dbm_hz) else float(r.floor_dbm_hz))


def spec(entries, root):
    return ",".join(f"{root}/{e['dir']}/{e['file']}.wav@{e['dial_hz']}" for e in entries)


def summary(entries):
    if not entries:
        return {}
    df = pd.DataFrame(entries)
    return dict(n=len(df), stations=sorted(df.station.unique()), days=int(df.start_utc.str[:10].nunique()),
                busy10_p50=round(float(df.busy10.median()), 3), inr_p50_p50=round(float(df.inr_p50.median()), 2),
                peak_sigma_max=round(float(df.peak_sigma.max()), 2),
                floor_dbm_hz_p50=None if df.floor_dbm_hz.isna().all() else round(float(df.floor_dbm_hz.median()), 1))


def cut(d, per, root, seed):
    ok = d[~d.adc_flag & (d.peak_sigma <= 40.0) & ~d.station.isin(EXCLUDED_STATIONS)]
    used, out, notes = set(), {}, []
    for sid, title, stratum, flt, fading, forced in SCENARIOS:
        c = candidates(ok, stratum, flt)
        ends = choose_ends(c)
        rule_label = "two-site" if ends else "shared"
        if forced == "shared":
            if ends:
                notes.append(f"{sid}: the >= {MIN_END}-per-site rule would make it two-site "
                             f"({ends[0]} / {ends[1]}); SHARED per the Q6 ruling")
            ends = None
        sseed = seed + int(sid[1:])
        if ends:
            a = take(c[c.station.eq(ends[0])], stratum, per, used, sseed)
            used |= set(a.file)
            b = take(c[c.station.eq(ends[1])], stratum, per, used, sseed + 100)
            used |= set(b.file)
            ab, ba = [entry(r) for r in b.itertuples()], [entry(r) for r in a.itertuples()]
            mode = "two-site"
        else:
            p = take(c, stratum, per, used, sseed)
            used |= set(p.file)
            ab, ba = [entry(r) for r in p.itertuples()], None
            mode = "shared"
        env = dict(FADING[fading])
        env["SIM_QRM_REPLAY"] = spec(ab, root)
        if ba is not None:
            env["SIM_QRM_REPLAY_BA"] = spec(ba, root)
        out[sid] = dict(
            title=title, stratum=stratum or "natural", filter=FILTER_TEXT[sid], fading=fading, ends=mode,
            ends_by_rule=rule_label,
            station_b_hears=sorted({e["station"] for e in ab}),
            station_a_hears=sorted({e["station"] for e in (ba if ba is not None else ab)}),
            candidates=dict(n=int(len(c)), stations={k: int(v) for k, v in c.station.value_counts().sort_index().items()},
                            days=int(c.start_utc.str[:10].nunique())),
            ab=ab, ba=ba, ab_summary=summary(ab), ba_summary=summary(ba) if ba is not None else None, env=env)
    for twin, base in TWINS:
        o = dict(out[base])
        o = {k: v for k, v in o.items() if k not in ("ab", "ba")}
        o.update(title=out[base]["title"] + " — moderate twin", fading="moderate", alias_of=base)
        env = {k: v for k, v in out[base]["env"].items() if k.startswith("SIM_QRM_")}
        env.update(FADING["moderate"])
        o["env"] = env
        out[twin] = o
    return out, notes


def md5(path, chunk=1 << 20):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def verify(root, manifest, aliases, want_md5):
    """List missing captures (and their sidecars) under root; optionally md5 the present ones."""
    m = json.load(open(manifest))
    root = os.path.expanduser(root)
    alias = dict(a.split("=", 1) for a in aliases)
    seen, missing, sums = set(), [], {}
    for sid, s in m["scenarios"].items():
        for e in (s.get("ab") or []) + (s.get("ba") or []):
            top, sub = e["dir"].split("/", 1)
            p = os.path.join(root, alias.get(top, top), sub, e["file"] + ".wav")
            if p in seen:
                continue
            seen.add(p)
            if not os.path.exists(p):
                missing.append(p)
            elif want_md5:
                sums[e["dir"] + "/" + e["file"] + ".wav"] = md5(p)
            if os.path.exists(p) and not os.path.exists(p[:-4] + ".json"):
                missing.append(p[:-4] + ".json")
    print(f"{len(seen)} captures referenced; {len(missing)} missing under {root}")
    for p in missing:
        print("  MISSING", p)
    if want_md5:
        for k in sorted(sums):
            print(sums[k], k)
    return 1 if missing else 0


def write_md(path, man):
    L = [f"# QRM replay scenarios — real-world channel campaign (B2)\n",
         f"From `{man['meta']['features']}` (md5 `{man['meta']['features_md5']}`), per {man['meta']['per']}, "
         f"seed {man['meta']['seed']}, root `{man['meta']['root']}`.\n",
         "| id | filter | fading | ends | candidates (stations) | B hears | A hears | n (B/A) | days | busy10 p50 | "
         "INR p50 | peak σ max |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for sid, s in man["scenarios"].items():
        if "alias_of" in s:
            L.append(f"| {sid} | = {s['alias_of']} | {s['fading']} | {s['ends']} | | | | | | | | |")
            continue
        ab, ba = s["ab_summary"], s["ba_summary"] or s["ab_summary"]
        cand = ", ".join(f"{k} {v}" for k, v in s["candidates"]["stations"].items())
        n = f"{ab['n']}/{ba['n']}" if s["ends"] == "two-site" else f"{ab['n']} shared"
        L.append(f"| {sid} | {s['filter']} | {s['fading']} | {s['ends']} | {s['candidates']['n']} ({cand}) | "
                 f"{', '.join(s['station_b_hears'])} | {', '.join(s['station_a_hears'])} | {n} | "
                 f"{ab['days']}/{ba['days']} | {ab['busy10_p50']}/{ba['busy10_p50']} | "
                 f"{ab['inr_p50_p50']}/{ba['inr_p50_p50']} | {max(ab['peak_sigma_max'], ba['peak_sigma_max'])} |")
    if man["meta"]["notes"]:
        L += ["", "Notes:"] + [f"- {n}" for n in man["meta"]["notes"]]
    open(path, "w").write("\n".join(L) + "\n\n```\n" + __doc__ + "```\n")
    return L


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("features", nargs="?")
    ap.add_argument("--out-dir")
    ap.add_argument("--per", type=int, default=8)
    ap.add_argument("--root", default="~/qrm-replay")
    ap.add_argument("--seed", type=int, default=20261002)
    ap.add_argument("--adc-per-hour", type=float, default=2000.0)
    ap.add_argument("--verify", metavar="ROOT")
    ap.add_argument("--alias", action="append", default=[], help="TOP=DIR: read {ROOT}/DIR for manifest dir TOP/...")
    ap.add_argument("--md5", action="store_true")
    a = ap.parse_args()
    if a.verify:
        sys.exit(verify(a.verify, a.features, a.alias, a.md5))
    if not (a.features and a.out_dir):
        ap.error("features.csv and --out-dir are required")
    os.makedirs(a.out_dir, exist_ok=True)
    d = prepare(pd.read_csv(a.features), a.adc_per_hour)
    scen, notes = cut(d, a.per, a.root.rstrip("/"), a.seed)
    man = dict(meta=dict(features=os.path.basename(a.features), features_md5=md5(a.features), per=a.per,
                         root=a.root, seed=a.seed, min_end=MIN_END, excluded_stations=list(EXCLUDED_STATIONS),
                         tool_md5=md5(os.path.abspath(__file__)), notes=notes,
                         ends_note="SIM_QRM_REPLAY = A->B = what station B hears; "
                                   "SIM_QRM_REPLAY_BA = B->A = what station A hears"),
               scenarios=scen)
    mp = os.path.join(a.out_dir, "scenarios.json")
    json.dump(man, open(mp, "w"), indent=1)
    L = write_md(os.path.join(a.out_dir, "SCENARIOS.md"), man)
    print("\n".join(L))
    print(f"\nscenarios.json md5 {md5(mp)}")


if __name__ == "__main__":
    main()
