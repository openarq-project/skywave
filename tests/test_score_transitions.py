"""Tests for the scheduled-fade transition scorer.

A scheduled fade is the only cell that makes a modem switch modes, and its ground
truth (channel_sim's `[fade-schedule ...]` lines) has been landing in per-cell sim
logs since 2026-07-25 with nothing reading it. This is the reader.

The load-bearing property is the TIME AXIS. The transition is stamped in the sim's
AUDIO seconds; the modem's events are stamped in wall seconds since the adapter
launched; the delivery curve is transfer-relative. Joining them by pretending they
share an origin buries the (unbounded) sim-startup gap in every latency number, so
these tests build artifacts with deliberately DIFFERENT origins and assert the scorer
recovers the true offsets.

Run:  cd skywave && python3 -m pytest tests/test_score_transitions.py -q
"""
import csv

import pytest

from skywave import score_transitions as st
from skywave.results_schema import COLUMNS

# Wall origins, chosen so nothing lines up by accident:
CELL_T0 = 1_700_000_000.0      # adapter launched
AUDIO_T0 = CELL_T0 + 3.5       # sim's first audio block, 3.5 s later
XFER_WALL = CELL_T0 + 20.0     # transfer began, 20 s after launch
# => a transition at audio t=100 is at wall 1_700_000_103.5, i.e. 83.5 s into the
#    transfer. Any scorer that ignores an anchor gets 100 or 80 instead.
XFER_OFFSET = (AUDIO_T0 + 100.0) - XFER_WALL      # 83.5


def _sim_log(tmp_path, base, transitions, clock="wall", anchor=AUDIO_T0):
    lines = ["channel_sim[gen8]: transport=alsa  half-duplex keying=PTT  "
             "fade=schedule[good:100,poor:120,good:0]xf=1s block=1024f/21.3ms"
             + ("  clock=virt_time(max=600s)" if clock == "virt_time" else "")]
    if clock == "wall":
        for d in ("A->B", "B->A"):
            lines.append(f"channel_sim: [audio-clock {d}] t=0.000s wall={anchor:.3f}")
    for d, t, frm, to in transitions:
        lines.append(f"channel_sim: [fade-schedule {d}] t={t:.2f}s {frm} -> {to}")
    p = tmp_path / (base + ".sim.log")
    p.write_text("\n".join(lines) + "\n")
    return p


def _cell_log(tmp_path, base, modes=(), xfer=True):
    """A cell log with sweep_runner's stamps. `modes` is [(stamp_s, bps), ...]."""
    lines = [f"[+{0.0:8.3f}] cell_t0 wall={CELL_T0:.3f} attempt=0"]
    if xfer:
        lines.append(f"[+{20.0:8.3f}] XFER_START bench={XFER_WALL:.3f} "
                     f"wall={XFER_WALL:.3f}")
    for s, bps in modes:
        lines.append(f"[+{s:8.3f}]   <- B: BITRATE ({bps // 100}) {bps} BPS")
    p = tmp_path / (base + ".log")
    p.write_text("\n".join(lines) + "\n")
    return p


def _curve(tmp_path, base, points):
    p = tmp_path / (base + ".progress.csv")
    with open(p, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_s", "bytes"])
        for t, n in points:
            w.writerow([f"{t:.1f}", n])
    return p


def _row(base, **over):
    row = {"modem": "armstrong", "tag": "t", "label": "", "sigma": 4000.0,
           "snr3k": 12.0, "watterson": "sched_good100_poor120_good0", "rep": 0,
           "log": base + ".log", "progress_log": base + ".progress.csv"}
    row.update(over)
    return row


# ---- the parsers ------------------------------------------------------------------

def test_parse_sim_log_reads_schedule_anchor_and_transitions(tmp_path):
    _sim_log(tmp_path, "c", [("A->B", 100.0, "good", "poor"),
                             ("B->A", 100.02, "good", "poor")])
    sched, clock, anchors, trans = st.parse_sim_log(str(tmp_path / "c.sim.log"))
    assert sched == "good:100,poor:120,good:0"
    assert clock == "wall"
    assert anchors == {"A->B": AUDIO_T0, "B->A": AUDIO_T0}
    assert trans == [("A->B", 100.0, "good", "poor"),
                     ("B->A", 100.02, "good", "poor")]


def test_parse_sim_log_detects_the_virtual_clock(tmp_path):
    _sim_log(tmp_path, "c", [("A->B", 100.0, "good", "poor")], clock="virt_time")
    _, clock, anchors, _ = st.parse_sim_log(str(tmp_path / "c.sim.log"))
    assert clock == "virt_time"
    assert anchors == {}          # run_lockstep emits no anchor; none is needed


def test_parse_sim_log_survives_two_records_on_one_physical_line(tmp_path):
    """channel_sim logged these with print() from two threads until 2026-07-29, whose
    write(message)+write(newline) pair interleaved in ~12% of runs. Logs already on
    disk carry the collision, so the parser must still read BOTH records -- and must
    not let the first one's `to` field swallow the second."""
    (tmp_path / "c.sim.log").write_text(
        "channel_sim[gen8]: transport=alsa fade=schedule[good:1,poor:0]xf=1s\n"
        "channel_sim: [fade-schedule B->A] t=1.00s good -> "
        "poorchannel_sim: [fade-schedule A->B] t=1.00s good -> poor\n")
    _, _, _, trans = st.parse_sim_log(str(tmp_path / "c.sim.log"))
    assert trans == [("B->A", 1.0, "good", "poor"), ("A->B", 1.0, "good", "poor")]


def test_parse_cell_log_reads_anchors_and_the_mode_timeline(tmp_path):
    _cell_log(tmp_path, "c", modes=[(30.0, 600), (95.0, 300)])
    t0, bench, wall, modes = st.parse_cell_log(str(tmp_path / "c.log"))
    assert (t0, bench, wall) == (CELL_T0, XFER_WALL, XFER_WALL)
    assert modes == [(30.0, 600), (95.0, 300)]


def test_parse_cell_log_tolerates_a_pre_anchor_log(tmp_path):
    (tmp_path / "c.log").write_text("[+   1.000]   <- B: CONNECTED\n")
    assert st.parse_cell_log(str(tmp_path / "c.log")) == (None, None, None, [])


# ---- the axis join, which is the whole point --------------------------------------

def test_transition_lands_at_the_true_transfer_offset(tmp_path):
    """audio t=100 with a +3.5 s anchor and a transfer starting at +20 s is 83.5 s
    into the transfer -- not 100 (ignoring the transfer origin) and not 80 (ignoring
    the audio-clock anchor)."""
    base = "cell"
    _sim_log(tmp_path, base, [("A->B", 100.0, "good", "poor")])
    _cell_log(tmp_path, base)
    _curve(tmp_path, base, [(t, t * 100) for t in range(0, 181, 20)])
    (out,) = st.score_row(_row(base), str(tmp_path))
    assert out["t_audio_s"] == 100.0
    assert out["t_xfer_s"] == pytest.approx(XFER_OFFSET, abs=0.05)
    assert out["t_xfer_s"] not in (100.0, 80.0)


def test_virt_time_uses_the_shared_signal_clock(tmp_path):
    """On virt_time the adapter's bench_time IS the sim's clock, so the offset is a
    plain difference -- and the wall anchor must not be applied on top of it."""
    base = "cell"
    _sim_log(tmp_path, base, [("A->B", 100.0, "good", "poor")], clock="virt_time")
    lines = [f"[+{0.0:8.3f}] cell_t0 wall={CELL_T0:.3f} attempt=0",
             f"[+{2.0:8.3f}] XFER_START bench=40.000 wall={XFER_WALL:.3f}"]
    (tmp_path / (base + ".log")).write_text("\n".join(lines) + "\n")
    _curve(tmp_path, base, [(t, t * 100) for t in range(0, 121, 20)])
    (out,) = st.score_row(_row(base), str(tmp_path))
    assert out["clock"] == "virt_time"
    assert out["t_xfer_s"] == pytest.approx(60.0)     # 100 audio - 40 bench


def test_a_transition_without_an_anchor_is_reported_unplaced(tmp_path):
    """A pre-anchor sim log must yield the transition with blank metrics, never a
    number computed off a guessed origin."""
    base = "cell"
    _sim_log(tmp_path, base, [("A->B", 100.0, "good", "poor")], clock="wall")
    p = tmp_path / (base + ".sim.log")
    p.write_text("\n".join(l for l in p.read_text().splitlines()
                           if "audio-clock" not in l) + "\n")
    _cell_log(tmp_path, base)
    _curve(tmp_path, base, [(0.0, 0), (100.0, 1000)])
    (out,) = st.score_row(_row(base), str(tmp_path))
    assert out["from_fade"] == "good" and out["to_fade"] == "poor"
    assert out["t_xfer_s"] == "" and out["resume_s"] == ""


# ---- the metrics ------------------------------------------------------------------

def _scored(tmp_path, curve_pts, modes=(), transitions=None, window=30.0):
    base = "cell"
    _sim_log(tmp_path, base, transitions or [("A->B", 100.0, "good", "poor")])
    _cell_log(tmp_path, base, modes=modes)
    _curve(tmp_path, base, curve_pts)
    return st.score_row(_row(base), str(tmp_path), window_s=window)


def test_rate_collapse_across_a_transition_is_visible(tmp_path):
    """Delivery at 100 B/s before the fade turns poor, 10 B/s after."""
    pts = [(t, t * 100) for t in range(0, 84, 10)]           # 100 B/s up to 83.5
    pts += [(83.5, 8350)] + [(83.5 + d, 8350 + d * 10) for d in range(10, 41, 10)]
    (out,) = _scored(tmp_path, pts)
    assert out["rate_before_bps"] == pytest.approx(100, abs=8)
    assert out["rate_after_bps"] == pytest.approx(10, abs=2)


def test_resume_s_is_the_recovery_latency(tmp_path):
    """Nothing delivered for 25 s after the transition, then bytes resume."""
    pts = [(t, t * 100) for t in range(0, 84, 10)]
    pts += [(83.5, 8350), (95.0, 8350), (108.5, 8350), (110.0, 9000)]
    (out,) = _scored(tmp_path, pts)
    assert out["resume_s"] == pytest.approx(26.5, abs=0.1)


def test_resume_s_blank_when_it_never_recovers(tmp_path):
    pts = [(t, t * 100) for t in range(0, 84, 10)] + [(83.5, 8350), (140.0, 8350)]
    (out,) = _scored(tmp_path, pts)
    assert out["resume_s"] == ""
    assert out["rate_after_bps"] == 0.0     # measured, not missing: it delivered zero


def test_switch_latency_and_overshoot(tmp_path):
    """The controller drops 600 -> 150 twelve seconds after the fade turns poor, then
    climbs back to 300: latency 12 s, overshoot 150 (it stepped past where it settled)."""
    pts = [(t, t * 100) for t in range(0, 121, 10)]
    modes = [(30.0, 600), (XFER_OFFSET + 20.0 + 12.0, 150),
             (XFER_OFFSET + 20.0 + 25.0, 300)]
    (out,) = _scored(tmp_path, pts, modes=modes)
    assert out["mode_before_bps"] == 600
    assert out["mode_after_bps"] == 150
    assert out["switch_latency_s"] == pytest.approx(12.0, abs=0.1)
    assert out["overshoot_bps"] == 150


def test_a_clean_step_has_zero_overshoot(tmp_path):
    pts = [(t, t * 100) for t in range(0, 121, 10)]
    modes = [(30.0, 600), (XFER_OFFSET + 20.0 + 8.0, 300),
             (XFER_OFFSET + 20.0 + 25.0, 300)]
    (out,) = _scored(tmp_path, pts, modes=modes)
    assert out["switch_latency_s"] == pytest.approx(8.0, abs=0.1)
    assert out["overshoot_bps"] == 0


def test_a_modem_that_never_switched_reports_no_latency(tmp_path):
    """The row this scorer exists to separate from a clean switch: same corpus row,
    same got/goodput -- and nothing in the mode timeline moved."""
    pts = [(t, t * 100) for t in range(0, 121, 10)]
    modes = [(30.0, 600), (XFER_OFFSET + 20.0 + 10.0, 600)]
    (out,) = _scored(tmp_path, pts, modes=modes)
    assert out["mode_before_bps"] == out["mode_after_bps"] == 600
    assert out["switch_latency_s"] == "" and out["overshoot_bps"] == ""


def test_virt_time_leaves_mode_columns_blank_rather_than_wrong(tmp_path):
    """Compressed wall stamps have no fixed ratio to the virtual signal clock, so the
    mode timeline is dropped -- the curve-based metrics still land."""
    base = "cell"
    _sim_log(tmp_path, base, [("A->B", 100.0, "good", "poor")], clock="virt_time")
    lines = [f"[+{0.0:8.3f}] cell_t0 wall={CELL_T0:.3f} attempt=0",
             f"[+{2.0:8.3f}] XFER_START bench=40.000 wall={XFER_WALL:.3f}",
             f"[+{9.0:8.3f}]   <- B: BITRATE (6) 600 BPS"]
    (tmp_path / (base + ".log")).write_text("\n".join(lines) + "\n")
    _curve(tmp_path, base, [(t, t * 100) for t in range(0, 121, 20)])
    (out,) = st.score_row(_row(base), str(tmp_path))
    assert out["mode_before_bps"] == "" and out["switch_latency_s"] == ""
    assert out["rate_before_bps"] != ""


def test_window_clamps_to_the_neighbouring_transition(tmp_path):
    """Averaging across the NEXT transition would attribute its channel to this one,
    so the window shrinks and says so."""
    trans = [("A->B", 100.0, "good", "poor"), ("A->B", 112.0, "poor", "good")]
    pts = [(t, t * 100) for t in range(0, 121, 2)]
    first, second = _scored(tmp_path, pts, transitions=trans, window=30.0)
    assert first["window_s"] == pytest.approx(12.0, abs=0.1)
    assert second["window_s"] == pytest.approx(12.0, abs=0.1)


def test_windows_are_bounded_by_separate_directions(tmp_path):
    """A B->A transition must not clamp an A->B window: they are different fades."""
    trans = [("A->B", 100.0, "good", "poor"), ("B->A", 100.5, "good", "poor")]
    pts = [(t, t * 100) for t in range(0, 181, 10)]
    ab, ba = _scored(tmp_path, pts, transitions=trans, window=30.0)
    assert ab["direction"] == "A->B" and ba["direction"] == "B->A"
    assert ab["window_s"] == pytest.approx(30.0, abs=0.1)


# ---- corpus level -----------------------------------------------------------------

def _corpus(tmp_path, rows):
    p = tmp_path / "out.csv"
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in COLUMNS})
    return str(p)


def test_skips_are_reported_not_silently_dropped(tmp_path):
    """A campaign that scored nothing because its cells ran WITHOUT ticks must not
    look like a campaign whose modem never switched."""
    base = "cell"
    _sim_log(tmp_path, base, [("A->B", 100.0, "good", "poor")])
    _cell_log(tmp_path, base)
    corpus = _corpus(tmp_path, [_row(base, progress_log="")])
    rows, skips = st.score_corpus(corpus, str(tmp_path))
    assert rows == []
    assert skips and "SKYW_PROGRESS_S" in skips[0][1]


def test_static_fade_rows_are_not_reported_as_skips(tmp_path):
    """Every campaign has non-scheduled cells; they are out of scope, not failures."""
    corpus = _corpus(tmp_path, [_row("c", watterson="poor", progress_log="")])
    rows, skips = st.score_corpus(corpus, str(tmp_path))
    assert rows == [] and skips == []


def test_main_writes_a_csv_of_every_scored_transition(tmp_path, capsys):
    base = "cell"
    _sim_log(tmp_path, base, [("A->B", 100.0, "good", "poor"),
                              ("B->A", 100.0, "good", "poor")])
    _cell_log(tmp_path, base, modes=[(30.0, 600)])
    _curve(tmp_path, base, [(t, t * 100) for t in range(0, 181, 20)])
    corpus = _corpus(tmp_path, [_row(base)])
    out = tmp_path / "trans.csv"
    assert st.main([corpus, "-o", str(out), "--logdir", str(tmp_path)]) == 0
    got = list(csv.DictReader(open(out)))
    assert [r["direction"] for r in got] == ["A->B", "B->A"]
    assert all(r["from_fade"] == "good" and r["to_fade"] == "poor" for r in got)
    assert "2 transitions scored" in capsys.readouterr().err


# ---- the format the scorer parses is the format the sim actually emits -----------
# Every test above builds artifacts by hand, which cannot catch the parsers drifting
# from channel_sim's real output. This one runs the sim for real (sockets, no ALSA)
# with a short fade schedule and parses its own log.

def test_parsers_match_a_real_channel_sim_log(sock_dir, tmp_path):
    import os
    import socket
    import subprocess as sp
    import sys
    import time

    import skywave
    from skywave import sock_frames

    # Built from a SIM_-free base, not from os.environ: conftest's load_sim() rewrites
    # os.environ globally (no monkeypatch), so whichever test ran before this one
    # decides what leaks into the child -- SIM_FS above all, which sets how many blocks
    # a scheduled second takes and therefore whether the boundary is ever crossed. That
    # made this test pass alone and fail about half the time inside the suite.
    env = {k: v for k, v in os.environ.items() if not k.startswith("SIM_")}
    for k in ("NP_STATS", "SIGMA", "SEED", "TXGAIN"):
        env.pop(k, None)
    env.update({"SIM_TRANSPORT": "sock", "SIM_SOCK_DIR": sock_dir,
                "SIM_SOCK_ACCEPT_S": "10", "SIGMA": "150", "SEED": "777",
                "TXGAIN": "1.0", "SIM_NCH": "2", "SIM_BLOCK": "1024",
                "SIM_FS": "48000",
                # 1 s of `good`, then `poor` for the rest: one transition per
                # direction. Short fade banks keep construction cheap.
                "SIM_FADE_SCHEDULE": "good:1,poor:0", "SIM_FADE_DUR_S": "20",
                "SIM_FADE_XFADE_S": "0.2"})
    wall_before = time.time()
    sim = sp.Popen([sys.executable, "-u", "-m", "skywave.channel_sim"],
                   env=skywave.child_env(env),
                   cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   stderr=sp.PIPE)
    try:
        def connect(name):
            path = os.path.join(sock_dir, name)
            deadline = time.monotonic() + 20.0
            while True:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    s.connect(path)
                    return s
                except (FileNotFoundError, ConnectionRefusedError):
                    s.close()
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.05)

        sa, sb = connect("a.sock"), connect("b.sock")
        rxa, rxb = sa.makefile("rb"), sb.makefile("rb")
        block = bytes(2 * 1024 * 2)                   # silence, NCH=2, int16
        out = bytearray(2 * 1024 * 2)
        # AUDIO time is what the schedule is measured in, and a 1024-frame block is
        # 21.3 ms at the default 48 kHz -- so ~70 blocks buys the 1.5 s needed to
        # cross a 1 s boundary. (Feeding 16, i.e. a block count read off an 8 kHz
        # assumption, sends 0.34 s and no transition ever fires.)
        # Each delivered block must be DRAINED: the sim writes every direction's
        # output to the peer socket, so an unread socket fills its buffer and the sim
        # stops processing -- the audio clock then stalls short of the boundary.
        for i in range(70):
            for s in (sa, sb):
                s.sendall(sock_frames.pack_station(i, sock_frames.PTT_UNKNOWN,
                                                   1024, block))
            for rx in (rxa, rxb):
                sock_frames.recv_into(rx, sock_frames.HDR_SIM, memoryview(out))
        sa.close(); sb.close()
    finally:
        sim.terminate()
        try:
            log = sim.communicate(timeout=10.0)[1].decode("utf-8", "replace")
        except sp.TimeoutExpired:
            sim.kill()
            log = sim.communicate()[1].decode("utf-8", "replace")
    wall_after = time.time()

    p = tmp_path / "real.sim.log"
    p.write_text(log)
    p = str(p)
    sched, clock, anchors, trans = st.parse_sim_log(p)
    assert sched == "good:1,poor:0", log
    assert clock == "wall"
    assert set(anchors) == {"A->B", "B->A"}, log
    # the anchor is a real wall clock taken inside this test's own window
    for w in anchors.values():
        assert wall_before <= w <= wall_after
    # one transition per direction, at the scheduled 1 s of AUDIO time (not wall:
    # the sock feed above is faster than real time)
    assert sorted(d for d, *_ in trans) == ["A->B", "B->A"], log
    for _, t_audio, frm, to in trans:
        assert (frm, to) == ("good", "poor")
        assert t_audio == pytest.approx(1.0, abs=0.2)
