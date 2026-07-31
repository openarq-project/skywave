#!/usr/bin/env python3
"""Pre-registered readout and validation gates for a VectorAdapter campaign.

Run this before interpreting a sweep. It answers "is this corpus trustworthy?"
mechanically, so a shepherd (or a cheaper model) reports output rather than
deciding what numbers mean.

  A  COMPLETENESS   every cell present, uniform frames, single arch
  B  INVALIDATION   instrument failures that void the run
  C  FRONTIER       Pareto membership per channel + family/adapter census
  D  QUESTIONS      floor movement AWGN->fading, and any --watch family census
  E  FLOOR TABLE    floor_db per frontier mode per channel, under the measured
                    equal-average-power lens AND the derived equal-PEP lens
  F  PAIRED SERIES  (--pairs) pre-registered pairs on FER-at-matched-SNR and
                    floor deltas; config-identical pairs carry a Wilson tie
                    gate -- a gap there is an instrument fault, not a result

Exit code: 0 = clean, 1 = an INVALIDATION gate tripped (escalate, do not
interpret the science), 2 = incomplete (still running, or cells missing).

  python3 -m skywave.vector_report --sweep out/joint.csv
  python3 -m skywave.vector_report --sweep out/m73.csv --watch rdm,x2
"""
import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict

TARGET = 0.10
ONSET = 0.02
PRESET_ORDER = ["off", "good", "moderate", "poor"]

#: Comparisons against TARGET/ONSET need a tolerance, and it is load-bearing
#: rather than decorative. With N frames per cell the achievable FER is a
#: multiple of 1/N, so an integer failure count can land EXACTLY on a threshold:
#: at N=150, 3/150 is precisely the 0.02 onset. Such a curve passes only if the
#: CSV rounded it to "0.020000" and fails if the same value is recomputed as
#: 1-147/150 (0.020000000000000018). Same data, opposite verdict, decided by CSV
#: precision. Four of the modem73 deep corpus's 256 curves sat on that boundary.
EPS = 1e-9

#: Significance level for the false_decode gate, and the multiplicity policy.
#: The gate SCANS every mode looking for an anomaly, so an uncorrected per-mode
#: test trips on roughly m*alpha modes by construction; Bonferroni over the modes
#: actually tested is the conservative choice. This matters concretely: modem73's
#: one observed false accept scored a raw p of 0.016, which an uncorrected
#: alpha=0.05 test would have called an invalidation purely because 56 modes were
#: examined.
FD_ALPHA = 0.01


def poisson_sf(k, lam):
    """P(X >= k) for X ~ Poisson(lam). Exact for the small k ever seen here."""
    if k <= 0:
        return 1.0
    if lam <= 0.0:
        return 0.0
    cdf, term = 0.0, math.exp(-lam)
    for i in range(k):
        if i:
            term *= lam / i
        cdf += term
    return max(0.0, 1.0 - cdf)


def median(xs):
    """True median -- averages the two middle values for even n. Naming a
    non-median 'median' is exactly the ambiguity this report exists to remove."""
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def wilson(k, n, z=1.96):
    """95% Wilson score interval for a proportion k/n. Chosen over normal
    approximation because tie-gate comparisons live exactly where p is near 0
    or 1, which is where the normal interval is wrong."""
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    d = 1.0 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def disjoint(a, b):
    """True when two intervals do not overlap -- the conservative 'gap beyond
    the Wilson interval' test: it under-calls real differences rather than
    over-calling chance ones, which is the right polarity for a gate whose
    trip means 'the instrument is broken'."""
    return a[0] > b[1] + EPS or b[0] > a[1] + EPS


def load_or_exit2(path):
    """A missing sweep file is INCOMPLETE (exit 2), never exit 1 -- exit 1 is
    reserved for 'an invalidation gate tripped', and a shepherd told to escalate
    on 1 must not be sent down that path by a mistyped filename."""
    try:
        with open(path) as f:
            return list(csv.DictReader(f))
    except OSError as e:
        print(f"REPORT: cannot read {path}: {e.strerror}")
        print("VERDICT: INCOMPLETE -- no sweep file. Has it been copied back yet?")
        sys.exit(2)


def fnum(r, k, d=0.0):
    try:
        return float(r[k])
    except (KeyError, ValueError, TypeError):
        return d


def inum(r, k, d=0):
    try:
        return int(r[k])
    except (KeyError, ValueError, TypeError):
        return d


def extras(r):
    try:
        return json.loads(r.get("extra_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def curves(rows, multi):
    c = defaultdict(list)
    for r in rows:
        key = f"{r['adapter']}:{r['label']}" if multi else r["label"]
        c[(key, r["preset"])].append((fnum(r, "snr_db"), fnum(r, "fer"),
                                      fnum(r, "goodput_bps")))
    for k in c:
        c[k].sort()
    return c


def floor_of(pts, target=TARGET, onset=ONSET, last=False):
    """-> (floor_db, status). `last=True` returns the LAST crossing instead of the
    first: the first is OPTIMISTIC, the last CONSERVATIVE, and they differ only
    when the curve re-crosses the target. Reporting the gap is how the
    optimism caveat gets discharged or quantified rather than assumed."""
    if len(pts) < 2:
        return None, "too_few_points"
    if min(f for _, f, _ in pts) > onset + EPS:
        return None, "never_worked"
    if pts[-1][1] > target + EPS:
        return None, "truncated_high"
    hit = None
    for i in range(1, len(pts)):
        s0, f0, _ = pts[i - 1]
        s1, f1, _ = pts[i]
        if f0 > target >= f1:
            t = (f0 - target) / (f0 - f1) if f0 != f1 else 1.0
            x = s0 + t * (s1 - s0)
            if hit is None:
                hit = x
            if not last:
                return x, "ok"
            hit = x
    if hit is None:
        return pts[0][0], "truncated_low"
    return hit, "ok"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sweep", required=True)
    ap.add_argument("--expect-cells", type=int, default=0)
    ap.add_argument("--frontier-exclude-class", default="bench",
                    help="comma-separated mode_class values kept OFF the primary "
                         "frontier (default: bench). They are still scored and "
                         "reported, just in a separate arm -- an ablation that "
                         "outranks a shipping mode must not be quotable as one.")
    ap.add_argument("--frontier-exclude-family", default="",
                    help="comma-separated families kept off the primary frontier, "
                         "for classes the instrument cannot rank fairly (e.g. "
                         "'plh', whose preamble airtime is charged with no credit "
                         "for the acquisition it buys).")
    ap.add_argument("--watch", default="",
                    help="comma-separated substrings naming candidate groups "
                         "whose FADING-frontier membership is pre-registered "
                         "(e.g. 'rdm,x2'). A zero census is a REPORTABLE "
                         "NEGATIVE, not a missing result.")
    ap.add_argument("--pep-ref", default="",
                    help="series label used as the PAPR reference for the "
                         "derived equal-PEP floor lens (floor + papr - "
                         "papr_ref). The reference is a pure additive offset "
                         "and cannot change any ordering; default is the "
                         "lowest-PAPR frontier-eligible series, stated in the "
                         "output.")
    ap.add_argument("--pairs", default="",
                    help="JSON file naming pre-registered series pairs: "
                         '[{"a": LABEL, "b": LABEL, "config_identical": bool, '
                         '"note": str}]. Pair membership and config identity '
                         "are MODEM knowledge -- the campaign supplies them; "
                         "this report only scores them. config_identical "
                         "pairs carry a tie gate: their payload waveforms are "
                         "the same bytes, so any per-SNR FER gap beyond the "
                         "Wilson interval is an instrument fault, not a mode "
                         "property.")
    a = ap.parse_args(argv)

    rows = load_or_exit2(a.sweep)
    if not rows:
        print("REPORT: no rows in", a.sweep)
        return 2
    multi = len({r.get("adapter", "") for r in rows}) > 1
    fail, incomplete = [], []

    # -------------------------------------------------- A COMPLETENESS
    print("=" * 72)
    print("A. COMPLETENESS")
    print("=" * 72)
    keys = sorted({(f"{r['adapter']}:{r['label']}" if multi else r["label"])
                   for r in rows})
    pres = [p for p in PRESET_ORDER if any(r["preset"] == p for r in rows)]
    snrs = sorted({fnum(r, "snr_db") for r in rows})
    print(f"  cells          {len(rows)}")
    print(f"  modes          {len(keys)}")
    print(f"  adapters       {dict(Counter(r.get('adapter','') for r in rows))}")
    print(f"  presets        {', '.join(pres)}")
    print(f"  SNR points     {len(snrs)}  ({min(snrs):+.1f} .. {max(snrs):+.1f} dB)")
    print(f"  reference BW   {dict(Counter(r.get('bw_hz','') for r in rows))}")
    print(f"  sample rates   {dict(Counter(r.get('sample_rate','') for r in rows))}")
    expect = a.expect_cells or len(keys) * len(pres) * len(snrs)
    print(f"  expected cells {expect}")
    if len(rows) < expect:
        print(f"  -> INCOMPLETE: {expect - len(rows)} cell(s) missing")
        incomplete.append("missing cells")
    else:
        print("  -> complete")

    bw = {r.get("bw_hz", "") for r in rows}
    if len(bw) > 1:
        print("  -> INVALIDATION: rows measured at different reference "
              f"bandwidths {sorted(bw)}. SNR means a different thing in each; "
              "do not pool or compare floors.")
        fail.append("mixed reference BW")

    fr = Counter(inum(r, "frames") for r in rows)
    print(f"  frames/cell    {dict(fr)}")
    if len(fr) > 1:
        print("  -> WARNING mixed frame counts; cells carry unequal weight")
    arches = Counter(r.get("arch", "") for r in rows)
    print(f"  host/arch      {dict(Counter(r.get('host','') for r in rows))} / "
          f"{dict(arches)}")
    known = {k for k in arches if k}
    if len(known) > 1:
        print(f"  -> INVALIDATION: {len(known)} architectures ({sorted(known)}). "
              "Float results are not bit-identical across arches; do not pool.")
        fail.append("mixed arch")

    # host+arch pin the machine but not the executable. Two drivers in one corpus
    # is a pooling error the other gates cannot see.
    drv = Counter(r.get("driver_id", "") for r in rows)
    print(f"  driver_id      {dict(drv)}")
    # Option-A transition: per-mode old-S/new-S conversion offsets, so floors
    # from pre-convention corpora remain convertible while both exist.
    offs = {}
    for r in rows:
        v = extras(r).get("s_offset_db")
        if v not in (None, ""):
            offs.setdefault(r["label"], v)
    if offs:
        print("  S-convention   Option-A payload-region S; new-S − old-S per "
              "mode (dB): "
              + ", ".join(f"{k} {v}" for k, v in sorted(offs.items())))
    seen_drv = {d for d in drv if d}
    if len(seen_drv) > 1:
        print(f"  -> INVALIDATION: {len(seen_drv)} distinct driver binaries "
              f"({sorted(seen_drv)}) in one corpus. Do not pool rows produced by "
              "different builds.")
        fail.append("mixed driver binary")
    elif drv.get("", 0):
        print(f"  -> NOTE {drv['']} row(s) carry no driver_id (adapter did not "
              "report one, or predate the column). Not invalidating; say so when "
              "reporting.")

    # -------------------------------------------------- B INVALIDATION
    print()
    print("=" * 72)
    print("B. INVALIDATION GATES")
    print("=" * 72)
    # ---- false_decode: per-mode Poisson upper tail against the mode's CRC width.
    #
    # Zero tolerance was the original rule and it is wrong for any mode space
    # whose payload checks differ in width -- it measures CRC width, not
    # instrument health. A CRC-16 mode admits ~1 in 65,536 of the corrupt frames
    # that reach its check, so at the few thousand failed checks a campaign arm
    # accumulates it trips several percent of the time by arithmetic, while a
    # CRC-32 mode essentially never does.
    #
    # Denominator: a CRC evaluation on corrupt data either fails (crc_errors) or
    # is wrongly admitted (false_decode), so n = crc_errors + false_decode.
    # Frames that never synced never reached the check and must not be counted.
    fd_cells = [r for r in rows if inum(r, "false_decode") > 0]
    per_mode, per_width = {}, defaultdict(lambda: [0, 0])
    for r in rows:
        key = f"{r['adapter']}:{r['label']}" if multi else r["label"]
        w = inum(r, "crc_bits", 0) or None
        e = per_mode.setdefault(key, {"n": 0, "k": 0, "w": w})
        e["n"] += inum(r, "crc_errors") + inum(r, "false_decode")
        e["k"] += inum(r, "false_decode")
        if w:
            per_width[w][0] += inum(r, "crc_errors") + inum(r, "false_decode")
            per_width[w][1] += inum(r, "false_decode")
    tested = sum(1 for e in per_mode.values() if e["n"] > 0)
    m = max(1, tested)
    tot_fd = sum(e["k"] for e in per_mode.values())

    print(f"  false_decode       {tot_fd} event(s) in {len(fd_cells)} cell(s) "
          f"of {len(rows)}")
    print(f"  gate               per-mode Poisson upper tail vs CRC width, "
          f"alpha={FD_ALPHA}, Bonferroni x{m} mode(s) tested")
    for w in sorted(per_width):
        n, k = per_width[w]
        lam = n * 2.0 ** -w
        p = poisson_sf(k, lam)
        print(f"    CRC-{w:<2d} evals={n:<8d} expected={lam:.4g}  observed={k}"
              f"  p={p:.4g}")
        # One aggregate test per width, uncorrected: catches a DIFFUSE fault that
        # every per-mode test would individually absorb.
        if p < FD_ALPHA:
            print(f"    -> INVALIDATION: the CRC-{w} arm as a whole admits more "
                  f"false decodes than its width explains (p={p:.4g} < "
                  f"{FD_ALPHA}).")
            fail.append(f"false decodes (CRC-{w} aggregate)")
    for key, e in sorted(per_mode.items(), key=lambda kv: -kv[1]["k"]):
        if e["k"] == 0:
            continue
        if not e["w"]:
            print(f"    {key}: adapter reported no crc_bits -> ZERO TOLERANCE")
            print("    -> INVALIDATION: cannot bound the collision rate without "
                  "a CRC width. Have the adapter report crc_bits for this mode.")
            fail.append(f"false decodes ({key}, unknown CRC width)")
            continue
        lam = e["n"] * 2.0 ** -e["w"]
        p = poisson_sf(e["k"], lam)
        padj = min(1.0, p * m)
        verdict = ("INVALIDATION" if padj < FD_ALPHA
                   else "within CRC-width chance")
        print(f"    {key} (CRC-{e['w']}): evals={e['n']} expected={lam:.4g} "
              f"observed={e['k']}  p={p:.4g} p_adj={padj:.4g}  -> {verdict}")
        for r in fd_cells:
            k2 = f"{r['adapter']}:{r['label']}" if multi else r["label"]
            if k2 == key:
                det = (extras(r).get("fd_detail") or "").strip()
                print(f"        at {r['preset']} {r['snr_db']} dB "
                      f"x{r['false_decode']}"
                      + (f"  [{det}]" if det else "  [no forensics recorded]"))
        if padj < FD_ALPHA:
            fail.append(f"false decodes ({key})")
    if tot_fd:
        print("    NOTE a delivered payload of the WRONG LENGTH also counts as a "
              "false_decode, and that is a framing fault rather than a CRC "
              "collision. The counter cannot separate them -- read the forensics "
              "before accepting a chance verdict.")

    wf = sum(inum(r, "wrong_frame") for r in rows)
    print(f"  wrong_frame        {wf}")
    if wf:
        print("  -> unexpected outside --cold runs; report it.")
        fail.append("wrong frame")

    ex_tot = Counter()
    for r in rows:
        for k, v in extras(r).items():
            if isinstance(v, int):
                ex_tot[k] += v
    if ex_tot:
        print(f"  adapter counters   {dict(ex_tot)}")
    if ex_tot.get("sticky_syncs"):
        print("  -> CAVEAT (not invalidating): a warm decoder reused a previous "
              "frame's mode through a damaged header, which inflates decode "
              "rate at the knee. A --cold re-run of those modes is the check.")

    cs = curves(rows, multi)
    # Per-series level/airtime metadata and per-point counts, for the equal-PEP
    # lens and the paired section. papr_db is measured BY THE DRIVER per row
    # (recomputing it host-side was tried and abandoned: two defensible RMS
    # windows disagreed by 1.1 dB on the same waveform); it should be constant
    # within a series, so the median is a formality.
    ser_papr, ser_air, ser_fails = {}, {}, defaultdict(dict)
    _papr_acc = defaultdict(list)
    for r in rows:
        key = f"{r['adapter']}:{r['label']}" if multi else r["label"]
        p = r.get("papr_db", "")
        if p not in ("", None):
            try:
                _papr_acc[key].append(float(p))
            except ValueError:
                pass
        if key not in ser_air:
            try:
                ser_air[key] = float(r.get("air_s") or 0.0) or None
            except ValueError:
                ser_air[key] = None
        n = inum(r, "frames")
        dec = inum(r, "decoded", -1)
        if dec < 0:  # older corpora: recover the count from fer
            dec = round((1.0 - fnum(r, "fer")) * n)
        ser_fails[(key, r["preset"])][round(fnum(r, "snr_db"), 3)] = (n - dec, n)
    for k, v in _papr_acc.items():
        ser_papr[k] = median(v)

    trunc = sum(1 for pts in cs.values() if floor_of(pts)[1] == "truncated_high")
    print(f"  truncated_high     {trunc} curve(s) of {len(cs)}")
    if trunc > 0.5 * len(cs):
        print("  -> INVALIDATION: over half the curves never reach FER<=0.10 at "
              "the top of the sweep. The range did not cover the working "
              "regime; floors are not trustworthy.")
        fail.append("range truncated")
    elif trunc:
        print("  -> expected for the weakest modes on the hardest channels.")

    # -------------------------------------------------- C FRONTIER
    print()
    print("=" * 72)
    print(f"C. FRONTIER (Pareto by goodput, cells with FER <= {TARGET:g})")
    print("=" * 72)
    meta = {}
    for r in rows:
        key = f"{r['adapter']}:{r['label']}" if multi else r["label"]
        meta[key] = (r.get("adapter", ""), r.get("family", ""))
    excl_class = {c.strip() for c in a.frontier_exclude_class.split(",") if c.strip()}
    excl_fam = {f.strip().lower() for f in a.frontier_exclude_family.split(",") if f.strip()}
    row_class, row_fam = {}, {}
    for r in rows:
        k = f"{r['adapter']}:{r['label']}" if multi else r["label"]
        row_class[k] = (r.get("mode_class") or "production").strip()
        row_fam[k] = (r.get("family") or "").strip().lower()

    def eligible(key):
        return (row_class.get(key, "production") not in excl_class
                and row_fam.get(key, "") not in excl_fam)

    held = sorted(k for k in keys if not eligible(k))
    if held:
        by = Counter(f"{row_class.get(k,'?')}/{row_fam.get(k,'?')}" for k in held)
        print(f"  frontier eligibility: {len(held)} of {len(keys)} series HELD OFF "
              f"the primary frontier  {dict(by)}")
        print(f"    excluded class={sorted(excl_class) or 'none'} "
              f"family={sorted(excl_fam) or 'none'}")
        print(f"    held: {', '.join(held[:8])}"
              + (f" (+{len(held)-8} more)" if len(held) > 8 else ""))
        print("    they are still scored; they are reported as a separate arm and "
              "in section E.")
    else:
        print("  frontier eligibility: all series eligible")
    print()

    def hull_of(p, max_fer, only_eligible=True):
        """max_fer=TARGET is the USABILITY BAR; None reproduces the raw
        goodput-max frontier. Goodput is rate*(1-FER), so a high-rate mode
        limping at FER 0.4 books ~60% of a large number and would top a raw
        frontier while the floor table calls it never_worked. Nobody selects a
        mode that drops 2 frames in 5 -- but the raw frontier is still the right
        answer to a DIFFERENT question (best mode under ideal retransmission),
        so both are reported and labelled."""
        per_snr = defaultdict(list)
        for (key, pp), pts in cs.items():
            if pp != p:
                continue
            if only_eligible and not eligible(key):
                continue
            for snr, fer, gp in pts:
                if max_fer is not None and fer > max_fer + EPS:
                    continue
                per_snr[round(snr, 3)].append((gp, key, fer))
        order, wins = [], {}
        for snr in sorted(per_snr, reverse=True):
            gp, key, fer = max(per_snr[snr], key=lambda t: (t[0], t[1]))
            if gp <= 0:
                continue
            wins[snr] = (key, gp, fer)
            if key not in order:
                order.append(key)
        return order, wins

    hulls, raw_hulls = {}, {}
    for p in pres:
        order, _ = hull_of(p, TARGET)
        raw_order, raw_wins = hull_of(p, None)
        hulls[p], raw_hulls[p] = order, raw_order
        ad = Counter(meta[k][0] for k in order)
        fam = Counter(meta[k][1] for k in order)
        print(f"  {p:9s} frontier {len(order):3d} of {len(keys):3d}   "
              f"adapters {dict(ad)}  families {dict(fam)}")
        print(f"            top: {order[0] if order else '(none)'}")
        # Reconcile against section E. The bar is the TARGET while the floor
        # metric ALSO demands the ONSET, so a mode can be a legitimate frontier
        # member and still have no floor: it plateaus between the two. That is an
        # irreducible error floor -- more SNR does not help -- which under slow
        # fading is what a fade outlasting a frame looks like. Name it, or "on the
        # frontier" sitting beside "never_worked" reads as self-contradiction.
        # Classify by STATUS, not by min-FER alone. A frontier member without an
        # 'ok' floor is one of three quite different things, and conflating them
        # produced the nonsense "irreducible error floor (best 0.000)":
        #   never_worked  + best <= bar : genuine plateau between onset and target
        #   never_worked  + best >  bar : impossible under the bar -> scorer bug
        #   truncated_low               : already under target at the lowest SNR,
        #                                 so the floor is at or BELOW the grid
        #   truncated_high              : range never covered the working regime
        plateau, contra, low, high = [], [], [], []
        for k in order:
            pts = cs.get((k, p), [])
            _, st = floor_of(pts)
            if st == "ok":
                continue
            mn = min(f for _, f, _ in pts) if pts else 1.0
            if st == "truncated_low":
                low.append(k)
            elif st == "truncated_high":
                high.append(k)
            elif mn <= TARGET + EPS:
                plateau.append((k, mn))
            else:
                contra.append(k)
        for k, mn in plateau:
            print(f"            note: {k} clears FER<={TARGET:g} but never "
                  f"reaches the {ONSET:g} onset (best {mn:.3f}) -- irreducible "
                  f"error floor, no floor in section E")
        if low:
            print(f"            note: floor at or BELOW the sweep's lowest SNR "
                  f"for {', '.join(low)} -- extend the grid downward to place it")
        if high:
            print(f"            note: sweep never reached the working regime for "
                  f"{', '.join(high)} -- extend the grid upward")
        if contra:
            print(f"            !! {len(contra)} member(s) on the frontier with "
                  f"best FER above the bar -- scorer inconsistency, investigate: "
                  f"{', '.join(contra)}")
        if held:
            h_order, _ = hull_of(p, TARGET, only_eligible=False)
            gained = [k for k in h_order if not eligible(k)]
            if gained:
                print(f"            held-off arm: {len(gained)} excluded series "
                      f"would have made this frontier: {', '.join(gained[:5])}")
        unusable = sum(1 for _, (_, _, f) in raw_wins.items() if f > TARGET + EPS)
        print(f"            raw (ideal-retransmission) frontier "
              f"{len(raw_order):3d}, top: "
              f"{raw_order[0] if raw_order else '(none)'}"
              f"  [{unusable}/{len(raw_wins)} of its winners exceed "
              f"FER {TARGET:g}]")

    # -------------------------------------------------- D QUESTIONS
    print()
    print("=" * 72)
    print("D. PRE-REGISTERED QUESTIONS")
    print("=" * 72)
    fading = [p for p in pres if p != "off"]

    for token in [t.strip() for t in a.watch.split(",") if t.strip()]:
        hit = {p: [k for k in hulls[p] if token.lower() in k.lower()]
               for p in fading}
        raw = {p: [k for k in raw_hulls[p] if token.lower() in k.lower()]
               for p in fading}
        total = sum(len(v) for v in hit.values())
        rtotal = sum(len(v) for v in raw.values())
        print(f"  Q: does '{token}' reach any FADING frontier?")
        for p in fading:
            print(f"       {p:9s} {len(hit[p])} member(s)"
                  + (f": {', '.join(hit[p][:4])}" if hit[p] else ""))
        # Answer off both frontiers so the reader can see whether the verdict
        # depends on the usability bar rather than having to trust that it does not.
        print(f"       [robustness: {total} barred slot(s) vs {rtotal} raw -- "
              f"verdict "
              f"{'UNCHANGED' if (total > 0) == (rtotal > 0) else 'FLIPS'} "
              f"under the bar]")
        if total:
            print(f"     ANSWER: YES -- {total} frontier slot(s).")
        else:
            print("     ANSWER: NO -- dominated on every channel measured. This "
                  "is a REPORTABLE NEGATIVE: a group screened out on the easy "
                  "channel and absent under fading has been measured, not "
                  "overlooked.")
        print()

    # ---- Censoring first. EVERY floor comparison below is over the modes that
    # HAVE a floor, and that population shrinks hard with channel difficulty. A
    # per-channel statistic quoted without this census compares different sets of
    # modes to each other and means nothing: on the modem73 deep corpus the naive
    # read spanned 55 modes on the mildest channel against 11 on the harshest, and
    # made the fading penalty look like it SHRANK as the channel worsened.
    fl = {(k, p): floor_of(cs.get((k, p), []))[0] for k in keys for p in pres}
    print("  CENSORING CENSUS (read before any floor comparison below)")
    for p in pres:
        ok = [k for k in keys if fl[(k, p)] is not None]
        fam = Counter(meta[k][1] for k in ok)
        print(f"       {p:9s} {len(ok):3d}/{len(keys)} modes reach "
              f"FER<={TARGET:g} anywhere   {dict(fam)}")
    common = [k for k in keys if all(fl[(k, p)] is not None for p in pres)]
    cfam = Counter(meta[k][1] for k in common)
    print(f"       common subset (a floor on ALL {len(pres)} channels): "
          f"n={len(common)}  {dict(cfam)}")
    if pres and len(pres) > 1:
        print("     !! survival count and mean-floor point in OPPOSITE "
              "directions: by survival the mildest channel is EASIEST (most "
              "modes work),")
        print("        by floor among survivors it can look HARDEST. Different "
              "populations, not a contradiction. Never quote one alone.")
    print()

    print("  Q: how far do floors move from AWGN to fading?")
    if "off" in pres:
        print("     (a) NAIVE -- every mode with a floor on both. Populations "
              "DIFFER per row; rows are NOT comparable to each other.")
        any_naive = False
        for p in fading:
            d = [fl[(k, p)] - fl[(k, "off")] for k in keys
                 if fl[(k, p)] is not None and fl[(k, "off")] is not None]
            if d:
                any_naive = True
                print(f"       {p:9s} median {median(d):+.1f} dB, range "
                      f"{min(d):+.1f} .. {max(d):+.1f} (n={len(d)})")
        if not any_naive:
            print("       (no mode has an 'ok' floor on both AWGN and fading)")
        print("     (b) PAIRED -- the common subset only. THIS is the comparable "
              "row.")
        if common:
            for p in fading:
                d = [fl[(k, p)] - fl[(k, "off")] for k in common]
                print(f"       {p:9s} median {median(d):+.1f} dB, range "
                      f"{min(d):+.1f} .. {max(d):+.1f} (n={len(d)}, {dict(cfam)})")
            if len(cfam) == 1:
                print(f"     !! the common subset is ENTIRELY "
                      f"'{next(iter(cfam))}' -- a within-family result, not a "
                      f"mode-space result.")
        else:
            print("       (empty: no mode has a floor on every channel)")
    else:
        print("       (no AWGN arm; skipped)")

    # -------------------------------------------------- E FLOOR TABLE
    print()
    print("=" * 72)
    print("E. FLOOR TABLE (frontier modes, floor dB per channel)")
    print("=" * 72)
    union = []
    for p in pres:
        for k in hulls[p]:
            if k not in union:
                union.append(k)
    print(f"  {'mode':<34}" + "".join(f"{p:>11}" for p in pres))
    for key in union:
        line = f"  {key:<34}"
        for p in pres:
            f, st = floor_of(cs.get((key, p), []))
            line += f"{f:>11.1f}" if f is not None else f"{st[:10]:>11}"
        print(line)

    # ---- E2: the same floors under the DERIVED equal-PEP lens.
    #
    # The sweep normalizes to equal AVERAGE power (sigma from the vector's own
    # signal power), which structurally favours high-PAPR modes against a
    # PEP-limited transmitter. The equal-PEP floor is derived, not re-measured:
    #   floor_equalPEP = floor_equalAvgPower + (papr_mode - papr_ref)
    # The reference is a pure additive offset shared by every entry, so it can
    # never change an ordering -- but the PER-MODE papr term can, and the pairs
    # whose order flips between the lenses are exactly the ones that have
    # earned a MEASURED clip arm rather than a derivation.
    #
    # Load-bearing caveat, stated where the numbers are: the +PAPR term assumes
    # a linear PA at full backoff. Prior measured work found the real-PA error
    # is neither small nor single-signed (clip distortion x multipath breaks
    # equalisers -- low PAPR is double-edged under fading), so this lens ranks
    # candidates; it does not settle them.
    print()
    if not ser_papr:
        print("  equal-PEP lens: SKIPPED -- no papr_db in this corpus (predates "
              "the column). The primary table above is equal-average-power only.")
    else:
        ref = ""
        if a.pep_ref:
            hits = [k for k in keys
                    if k == a.pep_ref or k.split(":", 1)[-1] == a.pep_ref]
            if len(hits) == 1 and hits[0] in ser_papr:
                ref = hits[0]
            else:
                print(f"  equal-PEP lens: --pep-ref '{a.pep_ref}' "
                      f"{'is ambiguous' if len(hits) > 1 else 'not in corpus or has no papr_db'};"
                      " falling back to auto.")
        if not ref:
            cands = [k for k in union if k in ser_papr and eligible(k)] \
                or [k for k in ser_papr]
            ref = min(cands, key=lambda k: (ser_papr[k], k))
        print(f"  EQUAL-PEP floor table (derived: floor + papr - papr_ref; "
              f"ref {ref} @ {ser_papr[ref]:.2f} dB PAPR)")
        # Frontier modes first (in frontier order), then every other ELIGIBLE
        # series: this lens exists for ladder ORDER, and a ladder candidate
        # need not have won a frontier cell.
        pep_rows = union + sorted(k for k in keys
                                  if eligible(k) and k not in union)
        print(f"  {'mode':<28}{'papr':>7}" + "".join(f"{p:>11}" for p in pres))
        for key in pep_rows:
            pk = ser_papr.get(key)
            line = (f"  {key:<28}"
                    + (f"{pk:>7.2f}" if pk is not None else f"{'?':>7}"))
            for p in pres:
                f, st = floor_of(cs.get((key, p), []))
                if f is not None and pk is not None:
                    line += f"{f + pk - ser_papr[ref]:>11.1f}"
                elif f is not None:
                    line += f"{'no_papr':>11}"
                else:
                    line += f"{st[:10]:>11}"
            print(line)
        flips = []
        for p in pres:
            # All ELIGIBLE series, not just the frontier union: the escalation
            # rule cares about ladder-candidate order, and a mode can be a
            # ladder candidate without winning a frontier cell.
            ranked = [(k, floor_of(cs.get((k, p), []))[0]) for k in keys
                      if eligible(k) and k in ser_papr
                      and floor_of(cs.get((k, p), []))[1] == "ok"]
            for i in range(len(ranked)):
                for j in range(i + 1, len(ranked)):
                    (ka, fa), (kb, fb) = ranked[i], ranked[j]
                    da = fa - fb
                    dp = da + ser_papr[ka] - ser_papr[kb]
                    if da * dp < 0 and abs(da) > 0.01 and abs(dp) > 0.01:
                        flips.append((p, ka, kb, da, dp))
        if flips:
            print(f"  LENS DISAGREEMENTS ({len(flips)} pair(s) whose floor "
                  f"order FLIPS between the lenses -- per the pre-registered "
                  f"escalation rule, ladder-adjacent ones earn a measured "
                  f"clip-on/clip-off arm):")
            for p, ka, kb, da, dp in flips:
                print(f"    {p:9s} {ka} vs {kb}: equal-avg-power "
                      f"{da:+.1f} dB, equal-PEP {dp:+.1f} dB")
        else:
            print("  lens agreement: no floor-order flips between "
                  "equal-average-power and equal-PEP.")

    # Monotonicity. floor_db takes the FIRST crossing (optimistic); the LAST is
    # conservative. They differ only when a curve re-crosses the target, in which
    # case a single number silently picked a side. Report the gap so the caveat is
    # discharged or quantified -- never assumed either way.
    gaps = []
    for k2, pts in cs.items():
        first, st = floor_of(pts)
        if st != "ok":
            continue
        last, st2 = floor_of(pts, last=True)
        if st2 == "ok" and last is not None and abs(last - first) > EPS:
            gaps.append((last - first, k2))
    print()
    print(f"  monotonicity: {len(gaps)} of {len(cs)} curves re-cross "
          f"FER={TARGET:g} (first-crossing != last-crossing)")
    if not gaps:
        print("  -> the optimistic/conservative floor distinction is VACUOUS "
              "here; both crossings agree on every curve.")
    else:
        worst = max(gaps)
        print(f"  -> CAVEAT: floors on those curves are optimistic by up to "
              f"{worst[0]:+.2f} dB ({worst[1][0]} on {worst[1][1]}). Quote the "
              f"conservative crossing for them.")

    # -------------------------------------------------- F PAIRED SERIES
    tie_caveats = []
    if a.pairs:
        print()
        print("=" * 72)
        print("F. PAIRED SERIES (pre-registered pairs)")
        print("=" * 72)
        print("  Pairs compare on FER-at-matched-SNR and floor dB under BOTH "
              "lenses -- NEVER on goodput or frontier membership: when a "
              "variant carries acquisition airtime the instrument charges but "
              "does not credit, a goodput read reports the framing, not the "
              "mode.")
        try:
            with open(a.pairs) as f:
                pair_spec = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"  cannot read pairs file {a.pairs}: {e}")
            print("  -> INCOMPLETE: the paired section was pre-registered but "
                  "cannot be scored.")
            incomplete.append("pairs file unreadable")
            pair_spec = []

        def resolve(name):
            hits = [k for k in keys
                    if k == name or k.split(":", 1)[-1] == name]
            return hits[0] if len(hits) == 1 else None

        for spec in pair_spec:
            ka, kb = resolve(spec.get("a", "")), resolve(spec.get("b", ""))
            ident = bool(spec.get("config_identical"))
            tag = ("[CONFIG-IDENTICAL -- payload waveform is the same bytes; "
                   "pre-registered expectation: TIE within CI]" if ident
                   else "[different configs -- a real A/B]")
            print()
            print(f"  pair: {spec.get('a','?')} vs {spec.get('b','?')}  {tag}")
            if spec.get("note"):
                print(f"    note: {spec['note']}")
            if ka is None or kb is None:
                missing = [n for n, k in ((spec.get('a'), ka),
                                          (spec.get('b'), kb)) if k is None]
                print(f"    -> NOT IN CORPUS (or ambiguous): {missing}. "
                      "Scored nothing; say so rather than dropping the pair "
                      "silently.")
                continue
            aa, ab = ser_air.get(ka), ser_air.get(kb)
            if aa and ab:
                print(f"    airtime  {aa:.3f} vs {ab:.3f} s  "
                      f"({(aa / ab - 1) * 100:+.1f}% -- charged, uncredited)")
            pa, pb = ser_papr.get(ka), ser_papr.get(kb)
            if pa is not None and pb is not None:
                print(f"    papr     {pa:.2f} vs {pb:.2f} dB  (delta {pa - pb:+.2f})")
            print(f"    {'preset':<10}{'floor A':>9}{'floor B':>9}"
                  f"{'d(avgpwr)':>11}{'d(PEP)':>9}{'worst dFER@SNR':>17}"
                  f"{'CI-disjoint':>13}")
            pair_disjoint = 0
            for p in pres:
                fa, sta = floor_of(cs.get((ka, p), []))
                fb, stb = floor_of(cs.get((kb, p), []))
                fx = ser_fails.get((ka, p), {})
                fy = ser_fails.get((kb, p), {})
                common = sorted(set(fx) & set(fy))
                worst, ndis = (0.0, None), 0
                for s in common:
                    kfa, na = fx[s]
                    kfb, nb = fy[s]
                    d = kfa / na - kfb / nb if na and nb else 0.0
                    if abs(d) > abs(worst[0]):
                        worst = (d, s)
                    if disjoint(wilson(kfa, na), wilson(kfb, nb)):
                        ndis += 1
                pair_disjoint += ndis
                cf = (f"{fa:>9.2f}" if fa is not None else f"{sta[:8]:>9}")
                cg = (f"{fb:>9.2f}" if fb is not None else f"{stb[:8]:>9}")
                dv = (f"{fa - fb:>+11.2f}" if fa is not None and fb is not None
                      else f"{'--':>11}")
                dp = (f"{fa - fb + pa - pb:>+9.2f}"
                      if None not in (fa, fb, pa, pb) else f"{'--':>9}")
                w = (f"{worst[0]:+.3f} @ {worst[1]:+.1f}" if worst[1] is not None
                     else "--")
                print(f"    {p:<10}{cf}{cg}{dv}{dp}{w:>17}"
                      f"{ndis:>7}/{len(common):<5}")
            if ident:
                if pair_disjoint == 0:
                    print("    TIE GATE: PASS -- no SNR point separates the "
                          "pair beyond the 95% Wilson interval.")
                else:
                    print(f"    TIE GATE: FAILED -- {pair_disjoint} SNR "
                          f"point(s) separate a byte-identical payload "
                          f"waveform beyond the 95% Wilson interval. That is "
                          f"an INSTRUMENT fault (the head charged to the "
                          f"payload, or sidecar/framing misalignment), not a "
                          f"mode property. Investigate before interpreting "
                          f"ANY result in this section.")
                    tie_caveats.append(
                        f"{spec.get('a')} vs {spec.get('b')} "
                        f"({pair_disjoint} pt(s))")

    print()
    print("=" * 72)
    if fail:
        print("VERDICT: INVALIDATION GATE TRIPPED ->", "; ".join(fail))
        print("Report sections A and B. Do NOT interpret C/D/E.")
        return 1
    if incomplete:
        print("VERDICT: INCOMPLETE ->", "; ".join(incomplete))
        return 2
    if tie_caveats:
        print("CAVEAT (not invalidating, per the pre-registered gate list): "
              "config-identical tie gate FAILED for", "; ".join(tie_caveats))
        print("The paired section is not interpretable until the instrument "
              "fault is resolved. Other sections stand.")
    print("VERDICT: CLEAN -- all gates passed. Report C, D and E.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
