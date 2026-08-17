"""Tests for the truncating no-progress early-out (GEN2 design §3) + the
peak_dbfs/papr_db schema promotion (§5.1).

The early-out ENDS a run that has flatlined; it never grants extra time. It is
what makes a deep budget ceiling affordable (19 of 168 fringe runs were
flatlined and ate 37% of that campaign producing nothing). Contract:

  * armed by SKYW_STALL_S (seconds on the TICK axis, campaign-wide); off when
    unset/0 -- a run without it reproduces the prior corpus byte-for-byte.
  * requires ticks: SKYW_STALL_S without SKYW_PROGRESS_S is a loud config
    error (a stall watch with no ticks NEVER fires -- an inert gate, worse
    than none), as is a window <= 2x the tick cadence (every tick gap would
    graze the window).
  * judged on TICK TIMESTAMPS (bench/signal axis), not wall time: a
    virtual-clock transport's stall window is signal seconds, same axis its
    budget is on. A dead adapter stops ticking entirely; that is the hard
    `timeout` backstop's job, not the stall watch's.
  * a trip SIGTERMs the adapter (same signal path as the hard timeout),
    stamps STALL_EARLYOUT into the cell log (scoring ground truth), and the
    row records stopped_early=true. ceiling_s always records the budget the
    cell ACTUALLY ran under; stopped_early is ""/false/true for
    unarmed/armed-quiet/fired (three states -- a scorer must be able to tell
    "not armed" from "armed and did not fire").

Run:  cd skywave && python3 -m pytest tests/test_stall_earlyout.py -q
"""
import csv

import pytest

from skywave import sweep_runner
from skywave.results_schema import COLUMNS
from skywave.sweep_runner import StallWatch

RESULT = ("RESULT: 512/512 B in 40.0s intact=True goodput=12.8 B/s "
          "| peak_bitrate=0bps | SN_med=-99.0 | connect=0.1s | wall=40.0s\n")


# ---- StallWatch: the pure trip rule ----------------------------------------------

def test_no_trip_while_bytes_advance():
    w = StallWatch(30.0)
    assert not any(w.feed(t, n) for t, n in
                   [(0, 0), (10, 100), (20, 200), (45, 300), (70, 400)])


def test_trips_on_a_flatline_of_the_window_width():
    w = StallWatch(30.0)
    assert not w.feed(0, 0)
    assert not w.feed(10, 0)
    assert not w.feed(29.9, 0)
    assert w.feed(30.0, 0)          # boundary inclusive: >= window is a stall


def test_a_gain_resets_the_window():
    w = StallWatch(30.0)
    w.feed(0, 0)
    assert not w.feed(25, 100)      # gain at 25 re-anchors
    assert not w.feed(54.9, 100)
    assert w.feed(55.0, 100)


def test_dead_band_after_delivery_still_trips():
    """The FRINGE signature: link up, some bytes, then nothing for the window."""
    w = StallWatch(60.0)
    w.feed(0, 0)
    w.feed(20, 4096)
    assert not w.feed(79.9, 4096)
    assert w.feed(80.0, 4096)


def test_byte_regression_never_counts_as_progress():
    """A tick reporting FEWER bytes (adapter restart artifact) must not reset
    the window -- only an increase is progress."""
    w = StallWatch(30.0)
    w.feed(0, 0)
    w.feed(10, 500)
    w.feed(20, 400)                 # regression: not a gain
    assert w.feed(40.0, 450)        # still below 500 peak; 30 s since t=10 gain


# ---- config guards ---------------------------------------------------------------

def test_stall_without_ticks_is_a_loud_config_error(monkeypatch):
    monkeypatch.setenv("SKYW_STALL_S", "180")
    monkeypatch.delenv("SKYW_PROGRESS_S", raising=False)
    with pytest.raises(SystemExit):
        sweep_runner.stall_config()


def test_stall_window_must_clear_twice_the_cadence(monkeypatch):
    monkeypatch.setenv("SKYW_STALL_S", "10")
    monkeypatch.setenv("SKYW_PROGRESS_S", "5")
    with pytest.raises(SystemExit):
        sweep_runner.stall_config()


def test_stall_off_by_default(monkeypatch):
    monkeypatch.delenv("SKYW_STALL_S", raising=False)
    monkeypatch.delenv("SKYW_PROGRESS_S", raising=False)
    assert sweep_runner.stall_config() == 0.0
    monkeypatch.setenv("SKYW_PROGRESS_S", "5")     # ticks without the watch: fine
    assert sweep_runner.stall_config() == 0.0


def test_valid_config_returns_the_window(monkeypatch):
    monkeypatch.setenv("SKYW_STALL_S", "180")
    monkeypatch.setenv("SKYW_PROGRESS_S", "5")
    assert sweep_runner.stall_config() == 180.0


# ---- end-to-end through run_cell (scripted adapter) ------------------------------

class _FakePopen:
    script = []
    terminated = []              # class-level: the instance is created inside run_cell

    def __init__(self, argv, cwd=None, env=None, stdout=None, stderr=None, **kw):
        self.stdout = iter(list(self.script))
        self.returncode = None

    def terminate(self):
        type(self).terminated.append(True)

    def wait(self):
        self.returncode = 0
        return self.returncode


def _run_cell(tmp_path, monkeypatch, lines, cell=None):
    monkeypatch.setattr(sweep_runner, "LOGDIR", str(tmp_path))
    monkeypatch.setattr(sweep_runner.sp, "run",
                        lambda *a, **k: type("P", (), {"returncode": 1})())
    fake = type("F", (_FakePopen,), {"script": lines, "terminated": []})
    monkeypatch.setattr(sweep_runner.sp, "Popen", lambda argv, **kw: fake(argv, **kw))
    out = tmp_path / "row.csv"
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        row = sweep_runner.run_cell(
            "loopback", cell or {"sigma": 0, "payload": 512, "timeout": 60},
            0, w, f, "spec")
    return row, fake


def test_earlyout_fires_on_a_flatlined_run(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYW_PROGRESS_S", "5")
    monkeypatch.setenv("SKYW_STALL_S", "20")
    lines = [f"PROGRESS t={t:.1f}s bytes=100\n" for t in (0, 5, 10, 15, 20, 25)]
    row, fake = _run_cell(tmp_path, monkeypatch, lines,
                          cell={"sigma": 0, "payload": 512, "timeout": 600})
    assert fake.terminated, "stall trip must SIGTERM the adapter"
    assert row["stopped_early"] == "true"
    assert row["ceiling_s"] == 600.0
    assert row["status"] in ("partial", "fail")      # never a new status string
    log_txt = open(tmp_path / row["log"]).read()
    assert "STALL_EARLYOUT" in log_txt               # scoring ground truth


def test_no_trip_on_a_slow_but_moving_run(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYW_PROGRESS_S", "5")
    monkeypatch.setenv("SKYW_STALL_S", "20")
    lines = [f"PROGRESS t={t:.1f}s bytes={b}\n"
             for t, b in ((0, 0), (15, 128), (30, 256), (45, 512))] + [RESULT]
    row, fake = _run_cell(tmp_path, monkeypatch, lines)
    assert not fake.terminated
    assert row["stopped_early"] == "false"           # armed, did not fire
    assert row["intact"] == "True" or row["intact"] == "true"
    # The harm counter must reach a REAL emitted row, not just its unit test:
    # this run recovered from 15 s gaps under a 20 s window -- exactly the
    # "close to W" shape the recovered-gap gate exists to catch.
    assert row["max_recovered_gap"] == 15.0


def test_unarmed_rows_record_blank_stopped_early(tmp_path, monkeypatch):
    monkeypatch.delenv("SKYW_STALL_S", raising=False)
    monkeypatch.delenv("SKYW_PROGRESS_S", raising=False)
    row, fake = _run_cell(tmp_path, monkeypatch, [RESULT])
    assert not fake.terminated
    assert row["stopped_early"] == ""                # unarmed != armed-and-quiet
    assert row["ceiling_s"] == 60.0                  # ceiling recorded regardless


# ---- peak_dbfs / papr_db promotion (§5.1) ----------------------------------------

def test_peak_cols_from_stats():
    cols = sweep_runner.peak_cols({"robust_peak": 32767, "papr_db": 7.816})
    assert cols["peak_dbfs"] == 0.0
    assert cols["papr_db"] == 7.82
    half = sweep_runner.peak_cols({"robust_peak": 16384, "papr_db": 0.0})
    assert -6.03 <= half["peak_dbfs"] <= -6.01
    assert half["papr_db"] == 0.0


def test_peak_cols_blank_without_stats():
    cols = sweep_runner.peak_cols({})
    assert cols["peak_dbfs"] == "" and cols["papr_db"] == ""


def test_schema_carries_the_new_columns():
    for c in ("stopped_early", "ceiling_s", "peak_dbfs", "papr_db"):
        assert c in COLUMNS


# ---- max_recovered_gap: the early-out's always-on harm counter -------------------
#
# Landed 2026-08-17 after the synthetic blackout wrongness cell was retired
# (three failed constructions, then DIAG-RESUME measured 0/8 resumes -- the
# cell's premise was vacuous for this modem population). The safety obligation
# moved from a pre-run gate to this counter on every real campaign row:
# max_recovered_gap >= SKYW_STALL_S anywhere means the early-out would have
# truncated a transfer that came back.

def test_recovered_gap_is_one_tick_on_a_healthy_transfer():
    """A curve that gains every tick recovers only from the sampling interval."""
    curve = [(0.0, 0), (5.0, 100), (10.0, 200), (15.0, 300)]
    assert sweep_runner.recovered_gap_seconds(curve) == 5.0


def test_recovered_gap_reports_a_stall_the_transfer_came_back_from():
    curve = [(0.0, 0), (5.0, 100), (105.0, 200), (110.0, 300)]
    assert sweep_runner.recovered_gap_seconds(curve) == 100.0


def test_trailing_dead_span_is_not_a_recovered_gap():
    """THE discriminator vs stall_s: the span that KILLED a transfer was never
    recovered from, so it must not appear here. A truncated row's big flat
    tail is exactly that span."""
    curve = [(0.0, 0), (5.0, 100), (10.0, 200)] + [(t, 200) for t in range(15, 300, 5)]
    assert sweep_runner.stall_seconds(curve) > 200          # the tail dominates
    assert sweep_runner.recovered_gap_seconds(curve) == 5.0  # nothing came back


def test_counter_can_differ_from_stall_s_in_both_directions():
    """Negative control: prove the two columns are not the same number wearing
    two names (a counter that always equals stall_s would settle nothing)."""
    recovered = [(0.0, 0), (5.0, 10), (95.0, 20), (100.0, 30)]
    died = [(0.0, 0), (5.0, 10), (10.0, 20), (200.0, 20)]
    assert sweep_runner.recovered_gap_seconds(recovered) == 90.0
    assert sweep_runner.stall_seconds(recovered) == 90.0     # same here...
    assert sweep_runner.recovered_gap_seconds(died) == 5.0   # ...and apart here
    assert sweep_runner.stall_seconds(died) == 190.0


def test_leading_ramp_counts_because_the_trip_rule_counts_it():
    """StallWatch anchors at the FIRST TICK, so a slow first byte is just as
    truncatable as a mid-transfer stall. The counter must use the same
    convention or the comparison against SKYW_STALL_S is meaningless."""
    curve = [(0.0, 0), (50.0, 0), (100.0, 0), (150.0, 500), (155.0, 600)]
    assert sweep_runner.recovered_gap_seconds(curve) == 150.0
    w = StallWatch(150.0)
    assert not w.feed(0.0, 0)
    assert not w.feed(50.0, 0)
    assert not w.feed(100.0, 0)
    assert w.feed(150.0, 500) is False   # a GAINING tick never trips...
    w2 = StallWatch(150.0)
    w2.feed(0.0, 0)
    assert w2.feed(150.0, 0)             # ...but the same gap without the gain does


def test_recovered_gap_blank_when_nothing_was_ever_delivered():
    """A connect-then-no-decode row recovered from nothing; its story is stall_s."""
    assert sweep_runner.recovered_gap_seconds([(t, 0) for t in range(0, 100, 5)]) == ""
    assert sweep_runner.recovered_gap_seconds([(0.0, 0)]) == ""
    assert sweep_runner.recovered_gap_seconds([]) == ""


def test_byte_regression_is_not_a_recovery():
    """Same rule as the trip: only an INCREASE past the running peak counts."""
    curve = [(0.0, 0), (5.0, 500), (60.0, 400), (120.0, 450), (125.0, 600)]
    assert sweep_runner.recovered_gap_seconds(curve) == 120.0


def test_max_recovered_gap_is_a_trailing_schema_column():
    assert COLUMNS[-1] == "max_recovered_gap"
