"""Tests for the CONSUMER half of the PROGRESS tick contract (2026-07-29).

tests/test_progress_ticks.py covers the emitter (ModemAdapter.progress). This file
covers what sweep_runner and results_schema do with the ticks, which is where the two
payoffs actually land:

  - the curve survives into a `<log basename>.progress.csv` sidecar named by the row,
    so a scorer can re-score the cell at any budget within its own window;
  - `stall_s` separates a stalled transfer from a slow one in the CSV alone;
  - a run with ticks OFF still writes the columns (blank) and no sidecar, so an old
    corpus and a new one stay readable by the same scorer.

Run:  cd skywave && python3 -m pytest tests/test_progress_corpus.py -q
"""
import csv

import pytest

from skywave import sweep_runner
from skywave.results_schema import COLUMNS, bytes_at, progress_path, read_progress

RESULT = ("RESULT: 512/512 B in 40.0s intact=True goodput=12.8 B/s "
          "| peak_bitrate=0bps | SN_med=-99.0 | connect=0.1s | wall=40.0s\n")


class _FakePopen:
    """sweep_runner streams p.stdout line by line, so a scripted list of lines is a
    whole adapter run. `lines` is set per-test via the `script` class attribute."""
    script = []

    def __init__(self, argv, cwd=None, env=None, stdout=None, stderr=None, **kw):
        self.stdout = iter(list(self.script))
        self.returncode = None

    def wait(self):
        self.returncode = 0
        return self.returncode


def _run_cell(tmp_path, monkeypatch, lines, cell=None):
    """Drive run_cell over a scripted adapter output; return (row, logdir)."""
    monkeypatch.setattr(sweep_runner, "LOGDIR", str(tmp_path))
    monkeypatch.setattr(sweep_runner.sp, "run",
                        lambda *a, **k: type("P", (), {"returncode": 1})())
    monkeypatch.setattr(sweep_runner.sp, "Popen",
                        lambda argv, **kw: type("F", (_FakePopen,),
                                                {"script": lines})(argv, **kw))
    out = tmp_path / "row.csv"
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        row = sweep_runner.run_cell(
            "loopback", cell or {"sigma": 0, "payload": 512, "timeout": 60},
            0, w, f, "spec")
    return row, str(tmp_path)


# ---- the pure parsing/summary functions -----------------------------------------

def test_parse_progress_reads_through_the_line_stamp():
    """run_cell stamps every captured line, so the pattern must not be ^-anchored."""
    txt = ("[+   0.000] PROGRESS t=0.0s bytes=0\n"
           "[+  10.004] PROGRESS t=10.0s bytes=512\n")
    assert sweep_runner.parse_progress(txt) == [(0.0, 0), (10.0, 512)]


def test_parse_progress_later_line_wins_at_a_shared_timestamp():
    """The terminal tick may land on the same t as the last cadence tick; it is the
    more accurate of the two, so it must not be dropped as a duplicate."""
    txt = "PROGRESS t=5.0s bytes=100\nPROGRESS t=5.0s bytes=512\n"
    assert sweep_runner.parse_progress(txt) == [(5.0, 512)]


def test_parse_progress_empty_without_ticks():
    assert sweep_runner.parse_progress("RESULT: 1/1 B in 1.0s intact=True\n") == []


def test_stall_seconds_spans_up_to_the_recovering_tick():
    """A stall that eventually recovers is still a stall of its full width: the span
    ENDING at the increase is time during which nothing was delivered."""
    curve = [(0.0, 0), (10.0, 0), (20.0, 0), (30.0, 100), (40.0, 200)]
    assert sweep_runner.stall_seconds(curve) == 30.0


def test_stall_seconds_of_a_healthy_transfer_is_one_tick_interval():
    curve = [(0.0, 0), (10.0, 128), (20.0, 256), (30.0, 512)]
    assert sweep_runner.stall_seconds(curve) == 10.0


def test_stall_seconds_of_a_dead_band_covers_the_whole_window():
    """Link up, zero bytes for the budget -- the row this column exists to name."""
    curve = [(t, 0) for t in (0.0, 60.0, 120.0, 180.0)]
    assert sweep_runner.stall_seconds(curve) == 180.0


def test_stall_seconds_blank_without_a_span():
    assert sweep_runner.stall_seconds([]) == ""
    assert sweep_runner.stall_seconds([(0.0, 0)]) == ""


# ---- the corpus row + sidecar ----------------------------------------------------

def test_row_carries_the_sidecar_and_the_curve_round_trips(tmp_path, monkeypatch):
    lines = ["PROGRESS t=0.0s bytes=0\n", "PROGRESS t=20.0s bytes=256\n",
             "PROGRESS t=40.0s bytes=512\n", RESULT]
    row, logdir = _run_cell(tmp_path, monkeypatch, lines)
    assert row["progress_log"].endswith(".progress.csv")
    # named off the same basename as the log, so the two are findable together
    assert row["progress_log"] == row["log"][:-len(".log")] + ".progress.csv"
    curve = read_progress(progress_path(row, logdir))
    assert curve == [(0.0, 0), (20.0, 256), (40.0, 512)]


def test_budget_becomes_a_scorer_parameter(tmp_path, monkeypatch):
    """The payoff: a cell collected at a 60 s budget answers for any B inside it,
    without re-running the campaign."""
    lines = ["PROGRESS t=0.0s bytes=0\n", "PROGRESS t=20.0s bytes=256\n",
             "PROGRESS t=40.0s bytes=512\n", RESULT]
    row, logdir = _run_cell(tmp_path, monkeypatch, lines)
    curve = read_progress(progress_path(row, logdir))
    assert bytes_at(curve, 5) == 0             # before the first delivery
    assert bytes_at(curve, 20) == 256          # exactly on a tick
    assert bytes_at(curve, 39.9) == 256        # between ticks: last known, not lerped
    assert bytes_at(curve, 600) == 512         # past the run: its final count


def test_stall_lands_in_the_row(tmp_path, monkeypatch):
    lines = ["PROGRESS t=0.0s bytes=0\n", "PROGRESS t=30.0s bytes=0\n",
             "PROGRESS t=40.0s bytes=512\n", RESULT]
    row, _ = _run_cell(tmp_path, monkeypatch, lines)
    assert row["stall_s"] == 40.0

    lines = ["PROGRESS t=%.1fs bytes=%d\n" % (t, t * 12.8) for t in (0.0, 20.0, 40.0)]
    row, _ = _run_cell(tmp_path, monkeypatch, lines + [RESULT])
    assert row["stall_s"] == 20.0


def test_ticks_off_writes_no_sidecar_and_blank_columns(tmp_path, monkeypatch):
    """The default. An old corpus and a new one must stay readable by one scorer."""
    row, logdir = _run_cell(tmp_path, monkeypatch, [RESULT])
    assert row["progress_log"] == ""
    assert row["stall_s"] == ""
    assert progress_path(row, logdir) is None
    assert read_progress(None) == []
    assert not list(tmp_path.glob("*.progress.csv"))


def test_a_stalled_cell_is_distinguishable_from_a_slow_one(tmp_path, monkeypatch):
    """Both rows report the same got/goodput/status -- stall_s is the only column
    that tells them apart, which is the whole reason it exists."""
    partial = ("RESULT: 256/512 B in 60.0s intact=False goodput=4.3 B/s "
               "| peak_bitrate=0bps | SN_med=-99.0 | connect=0.1s | wall=60.0s\n")
    slow = ["PROGRESS t=%.1fs bytes=%d\n" % (t, t * 4.3) for t in range(0, 61, 10)]
    stalled = (["PROGRESS t=0.0s bytes=0\n", "PROGRESS t=10.0s bytes=256\n"]
               + ["PROGRESS t=%.1fs bytes=256\n" % t for t in range(20, 61, 10)])
    slow_row, _ = _run_cell(tmp_path, monkeypatch, slow + [partial])
    stall_row, _ = _run_cell(tmp_path, monkeypatch, stalled + [partial])
    assert slow_row["got"] == stall_row["got"] == 256
    assert slow_row["status"] == stall_row["status"] == "partial"
    assert slow_row["stall_s"] == 10.0
    assert stall_row["stall_s"] == 50.0


@pytest.mark.parametrize("col", ["progress_log", "stall_s"])
def test_columns_are_in_the_schema(col):
    assert col in COLUMNS
