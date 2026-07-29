#!/usr/bin/env python3
"""score_transitions -- what the modem did when the CHANNEL changed underneath it.

A scheduled fade (`SIM_FADE_SCHEDULE=good:120,poor:180,good:0`) is the only cell that
exercises adaptive rate control: a static preset never makes a modem switch modes. The
sim already logs the transitions as ground truth --

    channel_sim: [fade-schedule A->B] t=120.01s  good -> poor

-- but a corpus row is one line for the WHOLE run, so a cell that switched cleanly and
one that never noticed produce the same row. This module is the scorer that closes that
gap: it joins each transition to what the modem was doing on either side of it.

WHAT IT MEASURES, per (cell, direction, transition):

  resume_s          seconds from the transition to the next byte delivered. The
                    recovery latency: how long the link carried NOTHING after the
                    channel moved. "" when it never resumed inside the window.
  rate_before/after mean delivered B/s over the window each side of the transition.
  switch_latency_s  seconds from the transition to the modem's first BITRATE change.
  overshoot_bps     did the rate controller step PAST where it settled? Settled = the
                    last bitrate seen in the segment; overshoot = |settled - extreme|
                    where extreme is the min (step-down) or max (step-up) reached
                    first. 0 = went straight there; >0 = it hunted.

THE TIME-AXIS PROBLEM, and why this needs three files. The transition timestamp is
AUDIO seconds -- samples the channel has processed since block 0. The modem's own
events are stamped in wall seconds since sweep_runner launched the adapter. Those are
different clocks with different origins, and joining them by pretending otherwise puts
the whole (unbounded) sim-startup gap straight into every latency number. So:

  <base>.sim.log       the transitions, plus the `[audio-clock <dir>] t=0.000s
                       wall=<unix>` anchor each Link emits from its FIRST block, plus
                       the banner (which names the clock -- see below).
  <base>.log           the modem's stamped lines, plus sweep_runner's `cell_t0
                       wall=<unix>` header, which makes those stamps absolute.
  <base>.progress.csv  the delivery curve (bytes vs transfer-relative seconds), whose
                       origin is the adapter's `XFER_START bench=.. wall=..` line.

⚠ SIM_CLOCK=virt_time corpora are scored on the DELIVERY CURVE ONLY. There the sim's
signal clock is virtual and the adapter's bench_time reads it directly, so transitions
and the curve share an origin exactly -- but the modem's log stamps are compressed wall
time with no fixed ratio to it, so the mode columns are left blank rather than filled
with numbers off the wrong axis. The banner's `clock=virt_time` is what selects this.

⚠ Requires the cell to have been run with SKYW_PROGRESS_S set. Without a delivery curve
there is nothing to measure a transition against, and such rows are reported as skipped
rather than silently dropped.

Usage: skywave-score-transitions <corpus.csv> [-o transitions.csv] [--logdir DIR]
                                 [--window S]
  logdir defaults to the corpus's own directory, then <BENCH_ROOT>/logs.
  window (default 30 s) is the span averaged each side of a transition; it is always
  clamped to the neighbouring transitions and to the transfer window, and the clamped
  value is reported per row so a squeezed window is never mistaken for a full one.
"""
import argparse
import csv
import os
import re
import sys

from skywave.results_schema import progress_path, read_corpus, read_progress

# ⚠ Both of these are applied with findall over the WHOLE text, and their fade-name
# groups are \w+ rather than \S+, on purpose. Until 2026-07-29 channel_sim logged these
# with print() from two threads, whose write(message)+write(newline) pair interleaved in
# ~12% of runs -- so logs already on disk can carry two records on one physical line
# ("good -> poorchannel_sim: [fade-schedule ..."). A per-line search() would see only the
# first, and a \S+ fade name would swallow the second record into the first's `to` field.
# The sim now writes each record atomically; this stays tolerant so the scheduled-fade
# logs collected since 2026-07-25 remain readable.
TRANSITION_RE = re.compile(
    r"\[fade-schedule ([^\]]+)\]\s+t=([\d.]+)s\s+(\w+)\s*->\s*"
    # The `to` name ends the record, so it is the field a collision runs into. Stop it
    # at the injected text's own prefix as well as at any non-word char: `poor` and
    # `poorchannel_sim` are both \w+, so a plain \w+ swallows the next record's start.
    r"(\w+?)(?=channel_sim|\W|$)")
ANCHOR_RE = re.compile(r"\[audio-clock ([^\]]+)\]\s+t=0\.000s\s+wall=([\d.]+)")
SCHEDULE_RE = re.compile(r"fade=schedule\[([^\]]*)\]")
CELL_T0_RE = re.compile(r"cell_t0 wall=([\d.]+)")
XFER_START_RE = re.compile(r"XFER_START bench=([\d.]+) wall=([\d.]+)")
# The stamp sweep_runner puts on every captured line, and the adapters' shared
# telemetry token `BITRATE (<mode>) <bps> BPS`. Mode is read from that because it is
# what every adapter in the tree already scans into self.modes -- this scorer adds no
# new adapter obligation. Case-insensitive on the unit: VARA emits "bps", the rest
# "BPS". An adapter that reports no bitrate simply yields no mode timeline.
STAMPED_BITRATE_RE = re.compile(
    r"^\[\+\s*([\d.]+)\].*?\bBITRATE\s*\(\d+\)\s+(\d+)\s*bps", re.M | re.I)

DEFAULT_WINDOW_S = 30.0

COLUMNS = [
    "modem", "tag", "label", "sigma", "snr3k", "watterson", "rep", "log",
    "direction", "idx", "from_fade", "to_fade",
    "t_audio_s", "t_xfer_s", "window_s",
    "bytes_before", "bytes_after", "rate_before_bps", "rate_after_bps",
    "resume_s", "mode_before_bps", "mode_after_bps",
    "switch_latency_s", "overshoot_bps", "clock",
]


def parse_sim_log(path):
    """(schedule, clock, anchors, transitions) from a cell's channel_sim log.

    schedule    the raw `good:120,poor:180,good:0` text from the banner ("" if absent)
    clock       "virt_time" or "wall" -- which axis the transitions can be joined on
    anchors     {direction: wall_seconds_at_audio_t0}
    transitions [(direction, t_audio_s, from_fade, to_fade), ...] in log order
    """
    schedule, clock, anchors, transitions = "", "wall", {}, []
    if not path or not os.path.exists(path):
        return schedule, clock, anchors, transitions
    txt = open(path, errors="replace").read()
    m = SCHEDULE_RE.search(txt)
    if m:
        schedule = m.group(1)
    if "clock=virt_time" in txt:
        clock = "virt_time"
    for direction, wall in ANCHOR_RE.findall(txt):
        anchors[direction] = float(wall)
    for direction, t, frm, to in TRANSITION_RE.findall(txt):
        transitions.append((direction, float(t), frm, to))
    return schedule, clock, anchors, transitions


def parse_cell_log(path):
    """(cell_t0_wall, xfer_bench, xfer_wall, modes) from a cell's adapter log.

    modes is [(stamp_s, bitrate_bps), ...] -- the observed mode timeline, on the
    stamp axis (seconds since the adapter launched). Any of the three scalars is
    None when its line is absent: a pre-anchor log, or a run without ticks.
    """
    cell_t0 = xfer_bench = xfer_wall = None
    modes = []
    if not path or not os.path.exists(path):
        return cell_t0, xfer_bench, xfer_wall, modes
    txt = open(path, errors="replace").read()
    m = CELL_T0_RE.search(txt)
    if m:
        cell_t0 = float(m.group(1))
    m = XFER_START_RE.search(txt)
    if m:
        xfer_bench, xfer_wall = float(m.group(1)), float(m.group(2))
    for stamp, bps in STAMPED_BITRATE_RE.findall(txt):
        modes.append((float(stamp), int(bps)))
    return cell_t0, xfer_bench, xfer_wall, modes


def transition_offsets(clock, anchors, transitions, xfer_bench, xfer_wall):
    """Each transition's position on the DELIVERY CURVE's axis (transfer-relative
    seconds), or None where it cannot be placed.

    virt_time: the sim's signal clock IS the adapter's bench clock, so the offset is
    just the transition's audio time minus the transfer's start on that clock.
    wall: the transition's audio time is turned absolute with that direction's
    audio-clock anchor, then made transfer-relative with the transfer's start wall.
    A direction with no anchor yields None rather than a guess.
    """
    out = []
    for direction, t_audio, frm, to in transitions:
        if clock == "virt_time":
            off = None if xfer_bench is None else t_audio - xfer_bench
        else:
            a = anchors.get(direction)
            off = None if (a is None or xfer_wall is None) else \
                (a + t_audio) - xfer_wall
        out.append(off)
    return out


def _rate(curve, t_lo, t_hi):
    """(bytes, B/s) delivered between the ticks that BRACKET [t_lo, t_hi].

    Divided by the span of the ticks actually used, not by the requested window: the
    curve is sampled on a cadence, so the last tick at or before t_lo generally sits
    earlier than t_lo, and charging its bytes against the shorter nominal window
    inflates the rate by up to one tick interval's worth (a 100 B/s link reads as 112
    at a 10 s cadence in a 30 s window). ("", "") when the bracket has no span.
    """
    if t_hi <= t_lo or not curve:
        return "", ""
    lo = hi = curve[0]
    for pt in curve:
        if pt[0] <= t_lo:
            lo = pt
        if pt[0] <= t_hi:
            hi = pt
    if hi[0] <= lo[0]:
        # One tick brackets the whole window. The curve is monotone, so its value is
        # that tick's at both ends: exactly zero bytes moved. That is a MEASUREMENT --
        # the dead-band case this scorer exists for -- not a missing one.
        return 0, 0.0
    delivered = max(0, hi[1] - lo[1])
    return delivered, round(delivered / (hi[0] - lo[0]), 2)


def resume_seconds(curve, t_at, until):
    """Seconds from `t_at` to the next tick showing MORE bytes than were delivered at
    `t_at`. This is the recovery latency: the span the link carried nothing after the
    channel moved. "" when no tick in (t_at, until] ever increases -- the transfer
    ended, or it never recovered at all, and the caller can tell those apart from the
    curve's own extent."""
    at = 0
    for t, n in curve:
        if t <= t_at:
            at = n
    for t, n in curve:
        if t_at < t <= until and n > at:
            return round(t - t_at, 1)
    return ""


def mode_metrics(modes, t_at, t_hi):
    """(mode_before, mode_after, switch_latency_s, overshoot_bps) around a transition.

    `modes` is on the same axis as t_at/t_hi. before = the last bitrate at or before
    the transition; after = the first one strictly after it. switch_latency is when the
    first CHANGE landed -- a repeat of the same bitrate is telemetry, not a switch.

    overshoot compares the extreme the controller reached against where it SETTLED
    (the last bitrate in the window): stepping down to 300 and climbing back to 600 is
    an overshoot of 300, while stepping straight to 600 is 0. It answers "did it hunt",
    which a before/after pair alone cannot. Blank without a `before` to set the
    direction of travel, or when nothing changed at all -- neither is an overshoot of
    zero, and reporting one as such would read as a clean step.
    """
    before = after = switch = overshoot = ""
    prior = [b for t, b in modes if t <= t_at]
    post = [(t, b) for t, b in modes if t_at < t <= t_hi]
    if prior:
        before = prior[-1]
    if not post:
        return before, after, switch, overshoot
    after = post[0][1]
    if not prior:
        return before, after, switch, overshoot
    changed = [(t, b) for t, b in post if b != before]
    if changed:
        switch = round(changed[0][0] - t_at, 1)
        settled = post[-1][1]
        extreme = (min(b for _, b in post) if changed[0][1] < before
                   else max(b for _, b in post))
        overshoot = abs(settled - extreme)
    return before, after, switch, overshoot


def score_row(row, logdir, window_s=DEFAULT_WINDOW_S):
    """Per-transition score rows for one corpus row, or [] when it has nothing to
    score. A row is scorable when it ran a scheduled fade AND carries a delivery
    curve; `skipped()` explains the ones that are not."""
    if skipped(row, logdir):
        return []
    log = os.path.join(logdir, row["log"])
    sim_log = log[:-len(".log")] + ".sim.log"
    curve = read_progress(progress_path(row, logdir))
    _, clock, anchors, transitions = parse_sim_log(sim_log)
    cell_t0, xfer_bench, xfer_wall, modes = parse_cell_log(log)
    offsets = transition_offsets(clock, anchors, transitions, xfer_bench, xfer_wall)
    # The modem's stamped lines share the curve's axis only on a real-time rig: there
    # bench_time IS the wall clock, so a stamp made absolute (cell_t0) and shifted by
    # the transfer's start wall is transfer-relative. On virt_time they are compressed
    # wall time against a virtual signal clock with no fixed ratio between them, so the
    # mode timeline is DROPPED rather than rebased onto an axis it does not live on --
    # blank mode columns, not wrong ones. See the module docstring.
    if clock == "wall" and cell_t0 is not None and xfer_wall is not None:
        modes = [(cell_t0 + s - xfer_wall, b) for s, b in modes]
    else:
        modes = []
    span_end = curve[-1][0] if curve else 0.0
    out = []
    for i, ((direction, t_audio, frm, to), off) in enumerate(zip(transitions, offsets)):
        # Same-direction neighbours bound the windows: averaging across another
        # transition would attribute the next segment's channel to this one.
        nbrs = [o for j, (o, (d, *_)) in enumerate(zip(offsets, transitions))
                if d == direction and o is not None and j != i]
        rec = {c: "" for c in COLUMNS}
        rec.update({k: row.get(k, "") for k in
                    ("modem", "tag", "label", "sigma", "snr3k", "watterson", "rep", "log")})
        rec.update({"direction": direction, "idx": i, "from_fade": frm, "to_fade": to,
                    "t_audio_s": round(t_audio, 2), "clock": clock})
        if off is None or not curve:
            out.append(rec)          # placed nowhere: report the transition, not a number
            continue
        lo_bound = max([0.0] + [o for o in nbrs if o < off])
        hi_bound = min([span_end] + [o for o in nbrs if o > off])
        w = round(min(window_s, off - lo_bound, hi_bound - off), 1)
        t_lo, t_hi = off - w, off + w
        bb, rb = _rate(curve, t_lo, off)
        ba, ra = _rate(curve, off, t_hi)
        mb, ma, sw, ov = mode_metrics(modes, off, t_hi) if modes else ("", "", "", "")
        rec.update({"t_xfer_s": round(off, 1), "window_s": w,
                    "bytes_before": bb, "bytes_after": ba,
                    "rate_before_bps": rb, "rate_after_bps": ra,
                    "resume_s": resume_seconds(curve, off, hi_bound),
                    "mode_before_bps": mb, "mode_after_bps": ma,
                    "switch_latency_s": sw, "overshoot_bps": ov})
        out.append(rec)
    return out


def skipped(row, logdir):
    """Why this corpus row cannot be scored, or "" when it can. Callers report these
    rather than dropping rows: a campaign that scored zero transitions because its
    cells ran without SKYW_PROGRESS_S must not look like a campaign whose modem never
    switched."""
    if not str(row.get("watterson", "")).startswith("sched_"):
        return "not a scheduled fade"
    if not row.get("log"):
        return "no cell log"
    if not row.get("progress_log"):
        return "no delivery curve (run with SKYW_PROGRESS_S)"
    log = os.path.join(logdir, row["log"])
    if not os.path.exists(log):
        return f"cell log missing: {row['log']}"
    sim_log = log[:-len(".log")] + ".sim.log"
    if not os.path.exists(sim_log):
        return f"sim log missing: {os.path.basename(sim_log)}"
    return ""


def resolve_logdir(corpus_csv, explicit=None):
    """Where the per-cell artifacts live: an explicit --logdir, else the corpus's own
    directory (campaign corpora are written beside their logs), else <cwd>/logs."""
    if explicit:
        return explicit
    here = os.path.dirname(os.path.abspath(corpus_csv))
    if os.path.isdir(os.path.join(here, "logs")):
        return os.path.join(here, "logs")
    return here


def score_corpus(corpus_csv, logdir=None, window_s=DEFAULT_WINDOW_S):
    """(score_rows, skips) for a whole corpus. skips is [(log, reason), ...]."""
    logdir = resolve_logdir(corpus_csv, logdir)
    rows, skips = [], []
    for row in read_corpus(corpus_csv):
        why = skipped(row, logdir)
        if why:
            if why != "not a scheduled fade":
                skips.append((row.get("log", "?"), why))
            continue
        rows.extend(score_row(row, logdir, window_s))
    return rows, skips


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="skywave-score-transitions",
        description="Score a scheduled-fade corpus: what the modem did at each "
                    "channel transition.")
    ap.add_argument("corpus", help="campaign CSV written by skywave-sweep")
    ap.add_argument("-o", "--out", help="output CSV (default: stdout)")
    ap.add_argument("--logdir", help="per-cell artifact dir (default: beside the corpus)")
    ap.add_argument("--window", type=float, default=DEFAULT_WINDOW_S,
                    help=f"seconds averaged each side of a transition "
                         f"(default {DEFAULT_WINDOW_S:g}; always clamped to the "
                         f"neighbouring transitions and the transfer window)")
    a = ap.parse_args(argv)
    logdir = resolve_logdir(a.corpus, a.logdir)
    rows, skips = score_corpus(a.corpus, logdir, a.window)
    f = open(a.out, "w", newline="") if a.out else sys.stdout
    try:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    finally:
        if a.out:
            f.close()
    for log, why in skips:
        print(f"skywave-score-transitions: skipped {log}: {why}", file=sys.stderr)
    print(f"skywave-score-transitions: {len(rows)} transitions scored"
          + (f", {len(skips)} rows skipped" if skips else "")
          + (f" -> {a.out}" if a.out else ""), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
