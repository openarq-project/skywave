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
    # label folded into the log basename (the clobber guard) AND recorded as its own
    # column -- (sigma, watterson, payload, label) is the full cell identity
    assert "jit20" in row["log"]
    assert row["label"] == "jit20"
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


# ---- signal-time budget parity (SIM_MAX_VIRTUAL_S) ------------------------------
# Without it, a virtual-clock leg's WALL timeout buys timeout x speedup of SIGNAL
# time, and every marginal cell flips optimistic (partial->ok) vs the real-time
# corpus -- the virtval-2026-07-23 deep-AWGN artifact. run_cell must bound every
# cell at its own timeout in VIRTUAL seconds (inert on real-time paths: only the
# lockstep sock loop reads SIM_MAX_VIRTUAL_S).

def _run_cell_captured_env(tmp_path, monkeypatch, cell):
    """Drive run_cell with a stubbed subprocess; return the env it launched with."""
    monkeypatch.setattr(sweep_runner, "LOGDIR", str(tmp_path))
    seen = {}

    def fake_run(argv, cwd=None, env=None, stdout=None, stderr=None, **kw):
        if argv and argv[0] == "pkill":
            class P:
                returncode = 1
            return P()
        seen.clear()
        seen.update(env or {})
        stdout.write(b"RESULT: 512/512 B in 1.0s intact=True goodput=512.0 B/s "
                     b"| peak_bitrate=0bps | SN_med=-99.0 | connect=0.1s | wall=1.0s\n")

        class P:
            returncode = 0
        return P()

    monkeypatch.setattr(sweep_runner.sp, "run", fake_run)
    out = tmp_path / "row.csv"
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        row = sweep_runner.run_cell("loopback", cell, 0, w, f, "spec")
    return seen, row


def test_virtual_budget_defaults_to_cell_timeout(tmp_path, monkeypatch):
    monkeypatch.delenv("SIM_MAX_VIRTUAL_S", raising=False)
    env, _ = _run_cell_captured_env(tmp_path, monkeypatch,
                                    {"sigma": 0, "payload": 512, "timeout": 30})
    assert env["SIM_MAX_VIRTUAL_S"] == "30"


def test_virtual_budget_operator_env_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("SIM_MAX_VIRTUAL_S", "900")
    env, _ = _run_cell_captured_env(tmp_path, monkeypatch,
                                    {"sigma": 0, "payload": 512, "timeout": 30})
    assert env["SIM_MAX_VIRTUAL_S"] == "900"


def test_virtual_budget_cell_env_wins(tmp_path, monkeypatch):
    monkeypatch.delenv("SIM_MAX_VIRTUAL_S", raising=False)
    env, _ = _run_cell_captured_env(tmp_path, monkeypatch,
                                    {"sigma": 0, "payload": 512, "timeout": 30,
                                     "env": {"SIM_MAX_VIRTUAL_S": "7"}})
    assert env["SIM_MAX_VIRTUAL_S"] == "7"


def test_wall_s_reaches_the_row(tmp_path, monkeypatch):
    """The RESULT wall= token lands in the corpus row; absent token -> blank column."""
    _, row = _run_cell_captured_env(tmp_path, monkeypatch,
                                    {"sigma": 0, "payload": 512, "timeout": 30})
    assert row["wall_s"] == 1.0
    # end-to-end through the real loopback adapter: the base class measures wall_s
    row2 = _run_one_cell(tmp_path, monkeypatch,
                         {"sigma": 0, "payload": 512, "timeout": 30})
    assert isinstance(row2["wall_s"], float) and row2["wall_s"] >= 0.0
