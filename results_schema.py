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
]

# Per-column caster for the READER side (read_corpus). Everything is stored as text in
# the CSV; this maps each column to the Python type a consumer wants. `intact` stays a
# str ("true"/"false", as the harness emits) -- callers compare .lower().
COLUMN_TYPES = {
    "modem": str, "tag": str, "sigma": float, "snr3k": float, "act_rms": float,
    "txgain": float, "watterson": str, "payload": int, "rep": int,
    "got": int, "total": int, "intact": str, "goodput": float, "peak_bps": int,
    "sn_med": float, "elapsed": float, "status": str, "rc": int, "log": str,
    "rig_gen": int,
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
