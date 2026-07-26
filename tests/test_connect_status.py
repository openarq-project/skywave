"""`connected` / `time_to_connect`: acquisition, independent of decode outcome.

Owner review of the FRINGE campaign (2026-07-26): VARA at -16.5 dB CONNECTED and then
scored zero bytes for the full 600 s budget. `status`/`connect_s`/`wall_s` all key off a
"RESULT" line that never appears on such a row, so they read exactly like a row that
never connected at all -- Arm B's "connect-dominated, approximates connect-only"
justification only holds ABOVE the decode floor. These two columns answer the
acquisition question directly, in both arms, without a re-run: `connected` off the
adapters' shared `<- {A,B}: CONNECTED` handshake line (present whether or not a RESULT
line follows), `time_to_connect` off the elapsed-seconds timestamp run_cell now stamps
on every captured subprocess line.

Run:  cd skywave && python3 -m pytest tests/test_connect_status.py -q
"""
import csv

from skywave import sweep_runner
from skywave.results_schema import COLUMNS


class _FakePopen:
    def __init__(self, lines, env=None):
        self.seen = env or {}
        self.stdout = iter(lines)
        self.returncode = None

    def wait(self):
        self.returncode = 0
        return self.returncode


def _run(tmp_path, monkeypatch, lines, modem="loopback"):
    monkeypatch.setattr(sweep_runner, "LOGDIR", str(tmp_path))

    def fake_pkill_run(argv, cwd=None, env=None, stdout=None, stderr=None, **kw):
        return type("P", (), {"returncode": 1})()

    def fake_popen(argv, cwd=None, env=None, **kw):
        return _FakePopen(lines, env)

    monkeypatch.setattr(sweep_runner.sp, "run", fake_pkill_run)
    monkeypatch.setattr(sweep_runner.sp, "Popen", fake_popen)
    out = tmp_path / "row.csv"
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        return sweep_runner.run_cell(modem, {"sigma": 0, "payload": 512, "timeout": 30},
                                      0, w, f, "spec")


def test_connect_then_no_decode_is_connected_true_no_result_line(tmp_path, monkeypatch):
    # The exact VARA-at-(-16.5 dB) shape: CONNECTED, then nothing -- no RESULT line at
    # all (a bare timeout kill). status/connect_s must stay blank/timeout-like, but
    # `connected` must be True: this row DID acquire, it just never decoded.
    row = _run(tmp_path, monkeypatch,
               ["  -> B: CONNECT N4FPV-2 N4FPV-1\n",
                "  <- A: CONNECTED N4FPV-2 N4FPV-1 2300\n"])
    assert row["connected"] is True
    assert row["connect_s"] == ""              # no RESULT line -> still blank
    assert row["status"] in ("fail", "timeout")
    assert isinstance(row["time_to_connect"], float)


def test_never_connected_is_connected_false(tmp_path, monkeypatch):
    row = _run(tmp_path, monkeypatch,
               ["  -> B: CONNECT N4FPV-2 N4FPV-1\n",
                "  <- B: DISCONNECTED\n"])
    assert row["connected"] is False
    assert row["time_to_connect"] == ""


def test_time_to_connect_reads_the_stamped_elapsed_seconds(tmp_path, monkeypatch):
    # run_cell stamps "[+<elapsed>] " on every captured line itself (not the adapter),
    # so a fake subprocess needs no special formatting -- just emit the CONNECTED line
    # and let run_cell's own capture loop do the timestamping.
    row = _run(tmp_path, monkeypatch,
               ["  <- A: CONNECTED N4FPV-2 N4FPV-1 2300\n"])
    assert row["connected"] is True
    assert row["time_to_connect"] >= 0.0


def test_successful_transfer_is_connected_true_too(tmp_path, monkeypatch):
    row = _run(tmp_path, monkeypatch,
               ["  <- A: CONNECTED N4FPV-2 N4FPV-1 2300\n",
                "RESULT: 512/512 B in 1.0s intact=True goodput=512.0 B/s "
                "| peak_bitrate=0bps | SN_med=-99.0 | connect=0.1s | wall=1.0s\n"])
    assert row["connected"] is True
    assert row["status"] == "ok"
    assert row["connect_s"] == 0.1             # RESULT's own connect= still populated


def test_freedata_is_connected_by_construction(tmp_path, monkeypatch):
    # freedata's link_connect has no handshake distinct from the transfer itself (always
    # returns True), so there is no log marker to key off -- it's a special case in
    # run_cell, not a log-parsing gap. Verified even with NO connect-shaped log text.
    row = _run(tmp_path, monkeypatch, ["some unrelated freedata status line\n"],
               modem="freedata")
    assert row["connected"] is True
    assert row["time_to_connect"] == ""
