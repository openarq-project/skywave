"""Tests for the cell-spec surface of sweep_runner: the per-cell `env` passthrough
(SIM_*-whitelisted, fail-fast at spec load), the `label` log-name disambiguator, the
custom-fade provenance in the `watterson` column, and the connect_s column landing in
the corpus row.

The env whitelist is a provenance guard: a spec must be able to carry channel
impairments (SIM_SIGMA_AB, SIM_TR_JITTER_MS, SIM_QRM_*, ...) but must NOT be able to
silently override runner-owned vars (SEED, TXGAIN, NP_STATS) — those decide fairness
and are set per-rep by the runner. The label exists because two cells differing only by
`env` would otherwise write the same log/npstats basename and clobber each other.

The fade tests guard the same corpus-honesty property from the other side: a
fade_delay_ms+fade_doppler_hz pair and SIM_FADE_SCHEDULE each silently override
SIM_WATTERSON inside channel_sim, so the row must name the fade that RAN whichever
route it arrived by — and because that name is folded into the log basename, it must
also survive as a filename.

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


def test_basename_collision_fails_before_any_run(tmp_path, monkeypatch):
    # The FRINGE campaign's own bug (2026-07-26): two cells agreeing on every
    # basename-relevant field (sigma/watterson/payload/rep) but differing only by an
    # env-only knob (there, atten_db) with no "label" set write the SAME log/npstats
    # path and silently clobber each other -- only the last-run cell's artifacts
    # survive. This must be a load-time SystemExit, before any cell runs, not a
    # silently-corrupted corpus discovered after a multi-hour campaign.
    spec = _write_spec(tmp_path, [
        {"sigma": 12000, "watterson": "poor", "payload": 256, "reps": 1,
         "env": {"SIM_ATTEN_DB": "8"}},
        {"sigma": 12000, "watterson": "poor", "payload": 256, "reps": 1,
         "env": {"SIM_ATTEN_DB": "12"}},
    ])
    out = tmp_path / "out.csv"
    with pytest.raises(SystemExit, match="basename collision"):
        _run_main(monkeypatch, spec, str(out))
    assert not out.exists()


def test_label_disambiguates_the_collision(tmp_path, monkeypatch):
    # The actual fix: a distinguishing "label" on at least one cell (both, by
    # convention) clears the same collision the test above raises on.
    spec = _write_spec(tmp_path, [
        {"sigma": 12000, "watterson": "poor", "payload": 256, "reps": 1,
         "env": {"SIM_ATTEN_DB": "8"}, "label": "a8"},
        {"sigma": 12000, "watterson": "poor", "payload": 256, "reps": 1,
         "env": {"SIM_ATTEN_DB": "12"}, "label": "a12"},
    ])
    out = tmp_path / "out.csv"
    _run_main(monkeypatch, spec, str(out))
    assert out.exists()


def test_reps_of_the_same_cell_do_not_collide(tmp_path, monkeypatch):
    # A sanity check on the checker itself: reps of ONE cell must not false-positive
    # (each rep's `rep` index is part of the basename).
    spec = _write_spec(tmp_path, [{"sigma": 0, "payload": 512, "reps": 3}])
    out = tmp_path / "out.csv"
    _run_main(monkeypatch, spec, str(out))
    assert out.exists()


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

class _FakePopen:
    """Stand-in for sp.Popen(..., stdout=sp.PIPE): run_cell now streams+timestamps each
    line off `.stdout` (see the SIM_ATTEN_DB/connected work, 2026-07-26) instead of a
    blocking sp.run with a direct file redirect -- pkill still goes through sp.run
    unmodified, so that mock stays separate (below)."""
    def __init__(self, argv, cwd=None, env=None, stdout=None, stderr=None, **kw):
        self.seen = env or {}
        self.stdout = iter(["RESULT: 512/512 B in 1.0s intact=True goodput=512.0 B/s "
                             "| peak_bitrate=0bps | SN_med=-99.0 | connect=0.1s | "
                             "wall=1.0s\n"])
        self.returncode = None

    def wait(self):
        self.returncode = 0
        return self.returncode


def _run_cell_captured_env(tmp_path, monkeypatch, cell):
    """Drive run_cell with a stubbed subprocess; return the env it launched with."""
    monkeypatch.setattr(sweep_runner, "LOGDIR", str(tmp_path))
    seen = {}

    def fake_pkill_run(argv, cwd=None, env=None, stdout=None, stderr=None, **kw):
        class P:
            returncode = 1
        return P()

    def fake_popen(argv, **kw):
        p = _FakePopen(argv, **kw)
        seen.clear()
        seen.update(p.seen)
        return p

    monkeypatch.setattr(sweep_runner.sp, "run", fake_pkill_run)
    monkeypatch.setattr(sweep_runner.sp, "Popen", fake_popen)
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


# ---- custom-fade provenance (the `watterson` column) ----------------------------
# A fade_delay_ms+fade_doppler_hz pair overrides SIM_WATTERSON inside channel_sim (its
# `elif FADE_DOPPLER and FADE_DELAY` beats the named-preset branch), so the row must
# name what ACTUALLY ran -- and that name is folded into the log basename, so it must
# stay filename-safe. Both halves have drawn blood: an unnamed custom fade made faded
# rows indistinguishable from unfaded ones in the corpus, and a "/" in the descriptor
# made every such cell die at open() with ENOENT mid-campaign.

@pytest.fixture(autouse=True)
def _no_ambient_fade(monkeypatch):
    """fade_resolved reads the RESOLVED env, so any exported fade knob would leak into
    these rows -- scrub every rung of the ladder, not just the pair."""
    for k in ("SIM_FADE_SCHEDULE", "SIM_FADE_DELAY_MS", "SIM_FADE_DOPPLER_HZ",
              "SIM_WATTERSON"):
        monkeypatch.delenv(k, raising=False)


def test_custom_fade_is_named_in_the_row(tmp_path, monkeypatch):
    cell = {"sigma": 0, "payload": 512, "timeout": 30, "watterson": "off",
            "fade_delay_ms": 2.0, "fade_doppler_hz": 1.0}
    env, row = _run_cell_captured_env(tmp_path, monkeypatch, cell)
    assert env["SIM_FADE_DELAY_MS"] == "2.0" and env["SIM_FADE_DOPPLER_HZ"] == "1.0"
    # the applied fade, NOT the cell's literal watterson="off"
    assert row["watterson"] == "custom_2.0ms_1.0Hz"
    assert "custom_2.0ms_1.0Hz" in row["log"]


def test_custom_fade_descriptor_is_filename_safe(tmp_path, monkeypatch):
    # main() rejects this spec at load; run_cell is the belt-and-braces for a direct
    # caller -- whatever reaches the descriptor must not carry a path separator into
    # the basename (the ENOENT that killed every k3/k4 cell of a live campaign).
    cell = {"sigma": 0, "payload": 512, "timeout": 30,
            "fade_delay_ms": "2/0", "fade_doppler_hz": 1.0}
    _, row = _run_cell_captured_env(tmp_path, monkeypatch, cell)
    assert "/" not in row["watterson"] and "/" not in row["log"]


def test_empty_fade_fields_keep_the_preset_name(tmp_path, monkeypatch):
    # "" is channel_config's unset sentinel for these fields: the keys are PRESENT but
    # channel_sim's `and` gate is false, so the named preset runs. A key-presence test
    # would write "custom_ms_Hz" here and mislabel a genuinely poor-faded row.
    cell = {"sigma": 0, "payload": 512, "timeout": 30, "watterson": "poor",
            "fade_delay_ms": "", "fade_doppler_hz": ""}
    env, row = _run_cell_captured_env(tmp_path, monkeypatch, cell)
    assert env["SIM_WATTERSON"] == "poor"
    assert row["watterson"] == "poor"


def test_half_a_custom_fade_keeps_the_preset_name(tmp_path, monkeypatch):
    # Only one of the pair set: channel_sim falls through to the preset branch.
    cell = {"sigma": 0, "payload": 512, "timeout": 30, "watterson": "poor",
            "fade_delay_ms": 2.0}
    _, row = _run_cell_captured_env(tmp_path, monkeypatch, cell)
    assert row["watterson"] == "poor"


# The descriptor is resolved from the FINAL child env, not the cell dict, because all
# three fade routes can arrive via the cell `env` passthrough -- and SIM_FADE_SCHEDULE
# has no first-class cell field, so `env` is its ONLY route. Resolving from the cell
# recorded a request rather than what ran, which is how every scheduled-fade sweep
# landed in the corpus labelled "off".

def test_fade_schedule_via_cell_env_is_named(tmp_path, monkeypatch):
    cell = {"sigma": 0, "payload": 512, "timeout": 30,
            "env": {"SIM_FADE_SCHEDULE": "good:120,poor:180,good:0"}}
    env, row = _run_cell_captured_env(tmp_path, monkeypatch, cell)
    assert env["SIM_FADE_SCHEDULE"] == "good:120,poor:180,good:0"
    assert row["watterson"] == "sched_good120_poor180_good0"
    assert "sched_good120_poor180_good0" in row["log"]
    # a schedule sweeps through several pairs -- no single one describes the cell
    assert row["fade_delay_ms"] == "" and row["fade_doppler_hz"] == ""


def test_preset_via_cell_env_is_named(tmp_path, monkeypatch):
    cell = {"sigma": 0, "payload": 512, "timeout": 30, "env": {"SIM_WATTERSON": "poor"}}
    _, row = _run_cell_captured_env(tmp_path, monkeypatch, cell)
    assert row["watterson"] == "poor"          # not the cell's defaulted "off"


def test_custom_pair_via_cell_env_is_named(tmp_path, monkeypatch):
    cell = {"sigma": 0, "payload": 512, "timeout": 30,
            "env": {"SIM_FADE_DELAY_MS": "2.0", "SIM_FADE_DOPPLER_HZ": "1.0"}}
    _, row = _run_cell_captured_env(tmp_path, monkeypatch, cell)
    assert row["watterson"] == "custom_2.0ms_1.0Hz"
    assert (row["fade_delay_ms"], row["fade_doppler_hz"]) == (2.0, 1.0)


def test_schedule_beats_pair_beats_preset(tmp_path, monkeypatch):
    # channel_sim's ladder, top rung: a schedule wins over BOTH the explicit pair and
    # the named preset, so neither may name the row.
    cell = {"sigma": 0, "payload": 512, "timeout": 30, "watterson": "poor",
            "fade_delay_ms": 2.0, "fade_doppler_hz": 1.0,
            "env": {"SIM_FADE_SCHEDULE": "good:0"}}
    _, row = _run_cell_captured_env(tmp_path, monkeypatch, cell)
    assert row["watterson"] == "sched_good0"


def test_preset_fills_the_numeric_fade_columns(tmp_path, monkeypatch):
    # the pair means the same thing however the fade was requested, so a scorer can
    # read delay/doppler off any static-fade row without knowing PRESETS
    cell = {"sigma": 0, "payload": 512, "timeout": 30, "watterson": "poor"}
    _, row = _run_cell_captured_env(tmp_path, monkeypatch, cell)
    assert (row["fade_delay_ms"], row["fade_doppler_hz"]) == (2.0, 1.0)   # CCIR poor


def test_unfaded_cell_has_blank_fade_columns(tmp_path, monkeypatch):
    _, row = _run_cell_captured_env(tmp_path, monkeypatch,
                                    {"sigma": 0, "payload": 512, "timeout": 30})
    assert row["watterson"] == "off"
    assert row["fade_delay_ms"] == "" and row["fade_doppler_hz"] == ""


@pytest.mark.parametrize("cell", [
    {"sigma": 0, "fade_delay_ms": "2/0", "fade_doppler_hz": 1.0},
    {"sigma": 0, "fade_delay_ms": 2.0, "fade_doppler_hz": "fast"},
])
def test_non_numeric_fade_fails_fast(tmp_path, monkeypatch, cell):
    # channel_sim float()s these; a bad one must die at load, not at cell 40 of an
    # overnight campaign (the same doctrine as the SIM_ env whitelist above). The "2/0"
    # case is also a path separator headed for the log basename.
    spec = _write_spec(tmp_path, [cell])
    out = tmp_path / "out.csv"
    with pytest.raises(SystemExit, match="must be a number"):
        _run_main(monkeypatch, spec, str(out))
    assert not out.exists()


# ---- per-cell channel_sim log (SIM_LOG) -----------------------------------------
# bench_pipes defaults SIM_LOG to ONE /tmp/channel_sim.log and opens it "wb", so every
# cell truncates the previous cell's. The sim's stderr is the only record of what the
# CHANNEL did -- the resolved-fade banner, SIM_KEYLOG, and the fade-schedule transition
# timestamps that score mode-switch latency -- so a campaign kept only its last cell's.

def test_sim_log_is_per_cell(tmp_path, monkeypatch):
    monkeypatch.delenv("SIM_LOG", raising=False)
    env, row = _run_cell_captured_env(tmp_path, monkeypatch,
                                      {"sigma": 0, "payload": 512, "timeout": 30})
    # beside the cell's own log, sharing its basename so the pair is obvious on disk
    assert env["SIM_LOG"] == str(tmp_path / row["log"].replace(".log", ".sim.log"))
    assert env["SIM_LOG"] != "/tmp/channel_sim.log"


def test_sim_log_differs_per_rep_and_per_cell(tmp_path, monkeypatch):
    # the whole point: two cells (or reps) must not share one truncated file
    monkeypatch.delenv("SIM_LOG", raising=False)
    seen = set()
    for rep in range(2):
        monkeypatch.setattr(sweep_runner, "LOGDIR", str(tmp_path))
        out = tmp_path / f"r{rep}.csv"
        with open(out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS)
            w.writeheader()
            captured = {}

            def fake_pkill_run(argv, cwd=None, env=None, stdout=None, stderr=None, **kw):
                return type("P", (), {"returncode": 1})()

            def fake_popen(argv, **kw):
                p = _FakePopen(argv, **kw)
                captured.update(p.seen)
                p.stdout = iter(["RESULT: 512/512 B in 1.0s intact=True goodput=512.0 "
                                 "B/s\n"])
                return p

            monkeypatch.setattr(sweep_runner.sp, "run", fake_pkill_run)
            monkeypatch.setattr(sweep_runner.sp, "Popen", fake_popen)
            sweep_runner.run_cell("loopback", {"sigma": 0, "payload": 512, "timeout": 30},
                                  rep, w, f, "spec")
        seen.add(captured["SIM_LOG"])
    assert len(seen) == 2, f"reps shared a sim log: {seen}"
    # and a differing cell axis separates them too
    env_a, _ = _run_cell_captured_env(tmp_path, monkeypatch,
                                      {"sigma": 0, "payload": 512, "timeout": 30,
                                       "label": "a"})
    env_b, _ = _run_cell_captured_env(tmp_path, monkeypatch,
                                      {"sigma": 0, "payload": 512, "timeout": 30,
                                       "label": "b"})
    assert env_a["SIM_LOG"] != env_b["SIM_LOG"]


def test_sim_log_operator_export_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("SIM_LOG", "/tmp/pinned.log")
    env, _ = _run_cell_captured_env(tmp_path, monkeypatch,
                                    {"sigma": 0, "payload": 512, "timeout": 30})
    assert env["SIM_LOG"] == "/tmp/pinned.log"


def test_sim_log_cell_env_wins(tmp_path, monkeypatch):
    monkeypatch.delenv("SIM_LOG", raising=False)
    env, _ = _run_cell_captured_env(tmp_path, monkeypatch,
                                    {"sigma": 0, "payload": 512, "timeout": 30,
                                     "env": {"SIM_LOG": "/tmp/from_cell.log"}})
    assert env["SIM_LOG"] == "/tmp/from_cell.log"


def test_wall_s_reaches_the_row(tmp_path, monkeypatch):
    """The RESULT wall= token lands in the corpus row; absent token -> blank column."""
    _, row = _run_cell_captured_env(tmp_path, monkeypatch,
                                    {"sigma": 0, "payload": 512, "timeout": 30})
    assert row["wall_s"] == 1.0
    # end-to-end through the real loopback adapter: the base class measures wall_s
    row2 = _run_one_cell(tmp_path, monkeypatch,
                         {"sigma": 0, "payload": 512, "timeout": 30})
    assert isinstance(row2["wall_s"], float) and row2["wall_s"] >= 0.0


# ---- accumulate_extra: forensic strings must survive later empty batches ----

def test_accumulate_extra_sums_numerics_and_joins_strings():
    from skywave.vector_sweep import accumulate_extra
    extra = {}
    # numerics sum across batches (crc_evals)
    accumulate_extra(extra, "crc_evals", "40")
    accumulate_extra(extra, "crc_evals", "2")
    assert extra["crc_evals"] == 42
    # a forensic string is kept even when a LATER batch reports none —
    # the overwrite bug erased fd_detail exactly when the gate needed it
    accumulate_extra(extra, "fd_detail", "len=403:first4=00000006")
    accumulate_extra(extra, "fd_detail", "")
    assert extra["fd_detail"] == "len=403:first4=00000006"
    # two real records join
    accumulate_extra(extra, "fd_detail", "len=10:first4=AA")
    assert extra["fd_detail"] == "len=403:first4=00000006;len=10:first4=AA"
    # empty-only series still materializes the key as ''
    e2 = {}
    accumulate_extra(e2, "fd_detail", "")
    assert e2["fd_detail"] == ""


def test_extra_json_round_trips_through_json():
    """The sweep once serialized extras with a ';' item separator — INVALID
    JSON that only manifests with >=2 keys (a single-key dict has no
    separator), so every multi-key extra was silently unparseable and
    vector_report's extras() returned {}. Pin the round trip."""
    import json
    payload = {"always_cold": 1, "crc_evals": 42, "fd_detail": "len=1:first4=AA",
               "s_offset_db": "-0.120"}
    encoded = json.dumps(payload, separators=(",", ":"))
    assert json.loads(encoded) == payload
    # and the exact call pattern the sweep uses on its row dict
    from skywave import vector_sweep as vs
    import inspect
    src = inspect.getsource(vs)
    assert 'separators=(";"' not in src, "the invalid-JSON separator is back"
