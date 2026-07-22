"""Tests for the cell-spec surface of sweep_runner: the per-cell `env` passthrough
(SIM_*-whitelisted, fail-fast at spec load), the `label` log-name disambiguator, and the
connect_s column landing in the corpus row.

The env whitelist is a provenance guard: a spec must be able to carry channel
impairments (SIM_SIGMA_AB, SIM_TR_JITTER_MS, SIM_QRM_*, ...) but must NOT be able to
silently override runner-owned vars (SEED, TXGAIN, NP_STATS) — those decide fairness
and are set per-rep by the runner. The label exists because two cells differing only by
`env` would otherwise write the same log/npstats basename and clobber each other.

Run:  cd skywave && python3 -m pytest tests/test_sweep_spec.py -q
"""
import csv
import json
import sys

import pytest

from skywave import sweep_runner
from skywave.results_schema import COLUMNS


def _write_spec(tmp_path, cells):
    spec = tmp_path / "cells.json"
    spec.write_text(json.dumps(cells))
    return str(spec)


def _run_main(monkeypatch, spec, out):
    monkeypatch.setattr(sys, "argv", ["sweep_runner", "loopback", spec, out, "t"])
    return sweep_runner.main()


def test_bad_env_key_fails_before_any_run(tmp_path, monkeypatch):
    # A non-SIM_ env key (here trying to override the runner-owned SEED) must kill the
    # run at spec load — BEFORE the CSV is created, i.e. before any cell has run.
    spec = _write_spec(tmp_path, [{"sigma": 0, "env": {"SEED": "1"}, "reps": 1}])
    out = tmp_path / "out.csv"
    with pytest.raises(SystemExit, match="SIM_"):
        _run_main(monkeypatch, spec, str(out))
    assert not out.exists()


def test_missing_sigma_fails_fast(tmp_path, monkeypatch):
    spec = _write_spec(tmp_path, [{"payload": 512}])
    out = tmp_path / "out.csv"
    with pytest.raises(SystemExit, match="sigma"):
        _run_main(monkeypatch, spec, str(out))
    assert not out.exists()


def _run_one_cell(tmp_path, monkeypatch, cell):
    """Drive run_cell against the in-process loopback adapter, logs into tmp_path."""
    monkeypatch.setattr(sweep_runner, "LOGDIR", str(tmp_path))
    out = tmp_path / "row.csv"
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        row = sweep_runner.run_cell("loopback", cell, 0, w, f, "spec")
    return row


def test_cell_env_and_label_reach_the_row(tmp_path, monkeypatch):
    cell = {"sigma": 0, "payload": 512, "timeout": 30, "label": "jit20",
            "env": {"SIM_TR_JITTER_MS": "20"}}
    row = _run_one_cell(tmp_path, monkeypatch, cell)
    assert row["status"] == "ok"
    # label folded into the log basename (the clobber guard)
    assert "jit20" in row["log"]
    # the base class timed link_connect; run_cell parsed it into the schema column
    assert isinstance(row["connect_s"], float) and row["connect_s"] >= 0.0


def test_label_absent_keeps_legacy_basename(tmp_path, monkeypatch):
    row = _run_one_cell(tmp_path, monkeypatch,
                        {"sigma": 0, "payload": 512, "timeout": 30})
    assert row["status"] == "ok"
    assert row["log"].startswith("spec_loopback_s0_")     # unchanged pre-label shape


def test_label_is_sanitized(tmp_path, monkeypatch):
    # a label is a filename fragment: path or shell metacharacters must not survive
    cell = {"sigma": 0, "payload": 512, "timeout": 30, "label": "a/b ../x$«y»"}
    row = _run_one_cell(tmp_path, monkeypatch, cell)
    assert "/" not in row["log"] and " " not in row["log"] and "$" not in row["log"]
    assert "ab..xy" in row["log"]
