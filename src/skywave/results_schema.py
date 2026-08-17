#!/usr/bin/env python3
"""results_schema -- the versioned campaign-corpus contract for the skywave harness.

sweep_runner writes one CSV row per (cell, rep). That ROW FORMAT is the harness's
external contract: scorers and other projects parse the corpus. Previously the contract
was IMPLICIT -- a column list inlined in sweep_runner with no version stamp, so a
consumer had no way to tell which schema a given corpus was written against, and a
column rename would silently break every downstream reader. This module makes the
contract EXPLICIT and VERSIONED:

  * COLUMNS / COLUMN_TYPES -- the single source of truth for the CSV shape. sweep_runner
    imports COLUMNS as its DictWriter fieldnames; a drift test asserts they stay equal.
  * RESULTS_SCHEMA -- the version tag ("results-schema/N"), bumped when COLUMNS change.
  * write_manifest() -- sweep_runner drops a `<out>.manifest.json` sidecar naming the
    schema, columns, types, and run provenance next to every corpus it writes.
  * read_manifest() / read_corpus() -- the READER side of the contract for external
    consumers: load the manifest, and iterate rows cast to their declared types.

The schema is GENERIC (no modem-specific fields), so results stay portable across
harnesses. Bump policy: ADDING a trailing column is
reader-tolerant and needs no bump; RENAMING, REMOVING, or retyping a column bumps the
integer -- record the change in the changelog below.

  results-schema/1 (2026-07-20): initial versioned contract. The 20-column row
    sweep_runner has emitted since the external-adapter registry work.
  results-schema/1 + connect_s (2026-07-21): trailing column `connect_s` appended --
    wall-clock seconds spent in link_connect (blank when the adapter did not report
    it, e.g. a fail_connect row or a pre-connect_s adapter). Append = no bump per
    the policy above; readers detect presence via the manifest's column list.
  results-schema/1 + label (2026-07-21): trailing column `label` appended -- the
    cell's spec "label" (sanitized), "" when the cell has none. Without it, rows
    from cells that differ only by their spec `env` (same sigma/watterson/payload)
    are indistinguishable in the corpus; (sigma, watterson, payload, label) is the
    full cell identity a scorer should group by.
  results-schema/1 + wall_s (2026-07-23): trailing column `wall_s` appended -- the
    transfer window in WALL seconds as measured by the adapter base class, "" when
    not reported (fail rows, pre-wall_s adapters). On real-time adapters it agrees
    with the RESULT's signal-time `in Xs`; on virtual-clock adapters it is the
    compressed wall duration, so speedup = (got/goodput) / wall_s. Added after the
    virtval-2026-07-23 campaign, where `elapsed` (wall, whole invocation) vs
    signal-time goodput ambiguity complicated the fidelity analysis.
  results-schema/1 + fade actuals (2026-07-25): trailing columns `fade_delay_ms` +
    `fade_doppler_hz` appended -- the fade channel_sim ACTUALLY applied, "" when no
    single static pair does (a scheduled fade sweeps through several; "off"). Filled
    for a named preset too (from watterson.PRESETS), so the pair means the same thing
    however the fade was requested. Append = no bump per the policy above.
    In the same change the `watterson` column's VALUE domain widened: it is no longer
    always a PRESETS name or "off". channel_sim silently overrides SIM_WATTERSON when
    a delay+doppler pair or SIM_FADE_SCHEDULE is set, so those rows now record
    "custom_<d>ms_<f>Hz" / "sched_<segs>" instead of naming a fade that never ran.
    No bump for that either: same name, position, and str type.
    READER NOTE: treat an unrecognised `watterson` value as a CUSTOM fade, never as
    unfaded -- a PRESETS.get(name) that falls back to "off"/None recreates the exact
    corpus lie this change fixed. Prefer the two numeric columns over parsing the
    descriptor string; its format is a human/filename label, not a contract.
  results-schema/1 + atten_db (2026-07-26): trailing column `atten_db` appended -- the
    cell's SIM_ATTEN_DB ACTUAL (path-loss dB applied by channel_sim, 0.0 when unset).
    `snr3k` already has this subtracted out by sweep_runner (act_rms is measured
    pre-attenuation, so it would otherwise overstate delivered SNR by exactly this
    amount) -- this column exists for provenance, not as a correction a reader must
    apply again. Added for the FRINGE deep-SNR campaign. Append = no bump.
  results-schema/1 + connected/time_to_connect (2026-07-26): trailing columns appended --
    ACQUISITION, independent of decode outcome. `status`/`connect_s`/`wall_s` all key off
    a "RESULT" line that only appears after a full transfer attempt; a row that connects
    and then never decodes (VARA at -16.5 dB in the FRINGE smoke: CONNECTED, then zero
    bytes for the full 600 s budget) has NO RESULT line, so those columns read exactly
    like a row that never connected at all. `connected` is a bool read off the adapters'
    shared `<- {A,B}: CONNECTED` handshake line (present regardless of whether a RESULT
    line follows); `time_to_connect` is the elapsed-seconds timestamp sweep_runner now
    stamps on every captured subprocess line, read straight off that same line ("" if
    connected is False, or for freedata, which has no handshake distinct from the
    transfer and is connected=True by construction). Below a modem's decode floor, Arms
    A and B of a fringe/deep-SNR sweep return identical zeros in every OTHER column --
    these two are what actually separates "didn't acquire" from "acquired but couldn't
    decode." Append = no bump.
  results-schema/1 + fade_units (2026-07-28): trailing column appended -- F.1487
    Annex 3 s6 statistical-coverage bookkeeping: independent fade states the row
    sampled, wall_s x fade_doppler_hz ("" for no-fade rows, schedules, or rows with
    no wall_s). Derivable from existing columns; made explicit so scorers can SUM
    it over reps against the ~3000-unit ensemble-convergence bar without re-deriving
    the rule, and so under-sampled absolute numbers are visible per row. Paired-seed
    A/B orderings do not need the bar (channel variance cancels between arms); it
    gates ABSOLUTE-number claims. Append = no bump.
  results-schema/1 + snr2k7 (2026-07-28): trailing column appended -- the row's
    SNR re-expressed in the ITU-R F.520-2 Annex 3 2.7 kHz noise reference
    bandwidth (snr3k + 10*log10(3000/2700) = snr3k + 0.46 dB). Pure derived
    convenience so cells can be quoted against ITU-convention instruments
    without the reader re-deriving the bandwidth correction ("" when snr3k
    is missing). Furman/App E use 3 kHz = the existing snr3k. Append = no bump.
  results-schema/1 + progress_log/stall_s (2026-07-29): trailing columns appended --
    the cell's byte-vs-time DELIVERY CURVE and its one summary statistic. Adapters
    emit `PROGRESS t=<s> bytes=<n>` ticks when run with SKYW_PROGRESS_S set (off by
    default, so every corpus collected before this reproduces byte-for-byte and both
    columns are ""); sweep_runner parks the parsed curve in `<log basename>.progress
    .csv` (t_s,bytes) and records its name in `progress_log`. Two things the row alone
    could never answer:
      * The transfer BUDGET stops being a COLLECTION parameter. read_progress() +
        bytes_at() re-score a cell at any budget B up to its own window, so raising a
        campaign's budget no longer breaks comparability with corpora collected at the
        old one -- changing your mind about B costs a scorer re-run, not a re-run of
        the campaign. (⚠ bytes_at answers DELIVERED BYTES, not intactness -- see its
        docstring for which adapters let the two stand in for each other.)
      * `stall_s` -- the longest span with no byte progress -- separates a STALLED
        transfer from a slow one, which are indistinguishable in every other column.
        A connect-then-no-decode row is flat for its whole window, so stall_s ~ wall_s;
        read it against wall_s, since a healthy transfer still reports about one tick
        interval. "" when the curve has fewer than two points.
    Append = no bump per the policy above.
  results-schema/1 + stopped_early/ceiling_s/peak_dbfs/papr_db (2026-08-17,
    GEN2 instrument): four trailing columns appended --
      * `stopped_early` -- the truncating no-progress early-out's provenance
        (sweep_runner StallWatch, armed by SKYW_STALL_S): "" = not armed,
        "false" = armed and did not fire, "true" = fired (the run was ended
        after SKYW_STALL_S of zero byte progress on the tick axis). THREE
        states on purpose: a scorer must distinguish "not armed" from "armed
        and quiet", and a censoring scorer must never read a truncated row as
        a confirmed non-delivery at budgets past its stop (GEN2 design §3.2).
        Stays str for the same bool("false") trap as `intact`/`connected`.
      * `ceiling_s` -- the transfer budget this row ACTUALLY ran under (the
        cell timeout after any adaptive-budget resolution), so a scorer never
        infers it from spec files.
      * `peak_dbfs` -- 20*log10(robust_peak/32767) from the row's npstats:
        the fair-PEP peak (cold-start transient excluded) the equal-PEP
        calibration targets, promoted so a shared calibration is VERIFIED per
        row instead of assumed (GEN2 design §5.1 peak gate). "" when stats
        are missing.
      * `papr_db` -- channel_sim's papr_db passed through. ⚠ upstream computes
        it from the RAW peak (cold-start included) over act_rms, so it can
        overstate PAPR on runs with an aloop cold-start pop; peak_dbfs is the
        robust one.
    All four "" on pre-existing corpora. Append = no bump per the policy above.
"""
import csv
import json
import os

RESULTS_SCHEMA = "results-schema/1"

# Canonical CSV column order. sweep_runner.main() uses this list verbatim as its
# DictWriter fieldnames -- test_results_schema asserts the two never drift apart.
COLUMNS = [
    "modem", "tag", "sigma", "snr3k", "act_rms", "txgain",
    "watterson", "payload", "rep",
    "got", "total", "intact", "goodput", "peak_bps", "sn_med",
    "elapsed", "status", "rc", "log", "rig_gen",
    "connect_s", "label", "wall_s",
    "fade_delay_ms", "fade_doppler_hz",
    "atten_db",
    "connected", "time_to_connect",
    "fade_units", "snr2k7",
    "progress_log", "stall_s",
    "stopped_early", "ceiling_s",
    "peak_dbfs", "papr_db",
]

# Per-column caster for the READER side (read_corpus). Everything is stored as text in
# the CSV; this maps each column to the Python type a consumer wants. `intact` stays a
# str ("true"/"false", as the harness emits) -- callers compare .lower().
COLUMN_TYPES = {
    "modem": str, "tag": str, "sigma": float, "snr3k": float, "act_rms": float,
    "txgain": float, "watterson": str, "payload": int, "rep": int,
    "got": int, "total": int, "intact": str, "goodput": float, "peak_bps": int,
    "sn_med": float, "elapsed": float, "status": str, "rc": int, "log": str,
    "rig_gen": int, "connect_s": float, "label": str, "wall_s": float,
    "fade_delay_ms": float, "fade_doppler_hz": float,
    "atten_db": float,
    # connected stays str (mirrors `intact`): bool("False") == True in Python, so casting
    # this column to bool would silently invert every False row. Callers compare .lower().
    "connected": str, "time_to_connect": float,
    "fade_units": float, "snr2k7": float,
    "progress_log": str, "stall_s": float,
    # stopped_early stays str: three-state (""/"false"/"true"), and bool() would
    # invert "false" exactly like connected/intact above.
    "stopped_early": str, "ceiling_s": float,
    "peak_dbfs": float, "papr_db": float,
}


def manifest(**provenance):
    """Build the manifest dict: the versioned schema declaration + run provenance."""
    m = {
        "schema": RESULTS_SCHEMA,
        "generated_by": "sweep_runner",
        "columns": list(COLUMNS),
        "column_types": {k: COLUMN_TYPES[k].__name__ for k in COLUMNS},
    }
    m.update(provenance)
    return m


def manifest_path(csv_path):
    """The sidecar path for a corpus CSV: `<csv_path>.manifest.json`."""
    return str(csv_path) + ".manifest.json"


def write_manifest(csv_path, **provenance):
    """Drop `<csv_path>.manifest.json` naming the schema + provenance next to a corpus.
    Idempotent (overwrites) so it is safe to call once per run, resumed or fresh."""
    path = manifest_path(csv_path)
    with open(path, "w") as f:
        json.dump(manifest(**provenance), f, indent=2, sort_keys=True)
        f.write("\n")
    return path


def read_manifest(csv_path):
    """Load a corpus's manifest, or None if it has none (a pre-B4 corpus)."""
    path = manifest_path(csv_path)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def cast_row(row):
    """Cast one CSV DictReader row to its declared column types. Unknown columns pass
    through as str; a blank field or a failed cast falls back to the raw string, so a
    tolerant reader never crashes on a partial/odd row."""
    out = {}
    for k, v in row.items():
        caster = COLUMN_TYPES.get(k, str)
        try:
            out[k] = caster(v) if v != "" else v
        except (TypeError, ValueError):
            out[k] = v
    return out


def read_corpus(csv_path):
    """READER-side contract: yield rows from a corpus CSV cast to their declared types.
    External consumers use this instead of hand-rolling a DictReader + per-column casts."""
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            yield cast_row(row)


def progress_path(row, logdir):
    """The delivery-curve sidecar for a corpus row, or None when it has none.

    Resolved from the row's `progress_log` column rather than by deriving a name off
    `log`: the mapping between the two is sweep_runner's, and a scorer re-deriving it
    by string surgery is exactly the implicit contract this module exists to remove.
    """
    name = (row.get("progress_log") or "").strip()
    return os.path.join(logdir, name) if name else None


def read_progress(path):
    """A cell's delivery curve as [(t_s, bytes), ...] from its `.progress.csv` sidecar.
    Returns [] for a missing/empty file, so a corpus with ticks off reads as "no curve"
    rather than raising."""
    if not path or not os.path.exists(path):
        return []
    out = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            try:
                out.append((float(r["t_s"]), int(r["bytes"])))
            except (KeyError, TypeError, ValueError):
                continue
    return out


def bytes_at(curve, budget_s):
    """Bytes delivered by `budget_s`: the last tick at or before it (0 before the first).

    This is what makes the transfer budget a SCORER parameter instead of a collection
    one -- a cell collected at a 600 s budget can be re-scored at any B within its own
    window without re-running the campaign.

    ⚠ It answers DELIVERED BYTES, not intactness. Adapters that deliver in ORDER
    (armstrong/vara/mercury/ardop each append to one stream) let `bytes_at >= payload`
    stand in for "would have completed by B". A selective-repeat or count-only adapter
    (modem73, freedata) can hold the same byte count with holes in it, so an
    intact-at-B claim for those needs the transfer to have COMPLETED by B -- i.e. the
    curve reaching payload AND the row's own `intact`.
    """
    got = 0
    for t, n in curve:
        if t > budget_s:
            break
        got = n
    return got
