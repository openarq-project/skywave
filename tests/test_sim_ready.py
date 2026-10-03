"""The sim READY handshake (2026-10-03): bench_pipes.launch_channel_sim returns only once channel_sim has built its
channel effects (a QRM-replay rig renders every capture first, tens of seconds), so stations never start into a
channel that is not yet pumping. Exits and timeouts fall through to the caller's existing handling."""
import os
import sys
import time

from skywave import bench_pipes


def fake_sim(tmp_path, body):
    p = tmp_path / "fake_sim.py"
    p.write_text("import sys, time\n" + body)
    return str(p)


def launch(monkeypatch, tmp_path, body, **env):
    monkeypatch.setattr(bench_pipes, "SIM", fake_sim(tmp_path, body))
    t0 = time.time()
    p = bench_pipes.launch_channel_sim(dict(SIM_LOG=str(tmp_path / "sim.log"), SIM_PTT="0", **env))
    return p, time.time() - t0


def kill(p):
    try:
        os.killpg(os.getpgid(p.pid), 9)
    except (OSError, ProcessLookupError):
        pass


def test_launch_waits_for_ready(monkeypatch, tmp_path):
    p, dt = launch(monkeypatch, tmp_path, "time.sleep(1.5)\n"
                   "print('channel_sim: READY effects built in 1.5s', file=sys.stderr, flush=True)\n"
                   "time.sleep(30)\n")
    try:
        assert 1.4 <= dt < 10 and p.poll() is None
    finally:
        kill(p)


def test_launch_returns_when_the_sim_exits(monkeypatch, tmp_path):
    p, dt = launch(monkeypatch, tmp_path, "sys.exit(2)\n")
    assert dt < 5 and p.wait(5) == 2


def test_launch_times_out_and_proceeds(monkeypatch, tmp_path, capsys):
    p, dt = launch(monkeypatch, tmp_path, "time.sleep(30)\n", SKYW_SIM_READY_S="1")
    try:
        assert 0.9 <= dt < 5
        assert "not READY" in capsys.readouterr().err
    finally:
        kill(p)


def test_real_channel_sim_says_ready_before_its_transport_waits(tmp_path):
    """The real sim prints READY after building effects and BEFORE the sock transport waits for stations: with no
    station ever connecting, the log holds READY and then the accept timeout, in that order."""
    import subprocess as sp
    env = dict(os.environ, SIM_TRANSPORT="sock", SIM_SOCK_DIR=str(tmp_path / "s"), SIM_SOCK_ACCEPT_S="2",
               SIGMA="400", SIM_LOG=str(tmp_path / "real.log"), SIM_PTT="0")
    env["PYTHONPATH"] = os.path.dirname(os.path.dirname(bench_pipes.__file__)) + os.pathsep + env.get("PYTHONPATH", "")
    p = bench_pipes.launch_channel_sim({k: env[k] for k in ("SIM_TRANSPORT", "SIM_SOCK_DIR", "SIM_SOCK_ACCEPT_S",
                                                            "SIGMA", "SIM_LOG", "SIM_PTT")})
    try:
        p.wait(60)
    finally:
        kill(p)
    log = open(env["SIM_LOG"]).read()
    i = log.find("channel_sim: READY")
    assert i >= 0, log[-2000:]
    later = log[i:]
    assert "did not connect" in later or "station" in later, log[-2000:]
