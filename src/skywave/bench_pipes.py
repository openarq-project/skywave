#!/usr/bin/env python3
"""Shared channel-sim launcher for skywave harnesses.

Replaces each harness's duplicated launch_pipes() (the two
`arecord | noise_pipe_gain.py | aplay` relays) with ONE channel_sim.py process that owns
all four snd-aloop devices (A_TX/B_TX captures, A_RX/B_RX playbacks) and applies the
half-duplex channel transform in between. The harness passes config purely via the
environment (SIGMA / TXGAIN / NP_STATS / SEED, plus the later SIM_* keying/delay/fade
flags); this helper just spawns the sim as a session leader and hands back the handle.

Teardown: os.killpg(os.getpgid(p.pid), 9) on the returned Popen kills the sim AND its
arecord/aplay children (they inherit the sim's process group).
"""
import os
import sys
import time
import subprocess as sp

import skywave

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.join(HERE, "channel_sim.py")


def launch_channel_sim(extra_env=None):
    """Spawn the shared half-duplex channel sim. Returns the Popen (a session leader, so
    one os.killpg tears down the whole rig). Config is read from the environment by
    channel_sim.py; pass extra_env to override/add keys for this run.

    In SIM_PTT mode the sim's stdin is a pipe: the harness writes 'a 1'/'a 0'/'b 1'/'b 0'
    lines (relayed from each modem's host PTT ON/OFF) to gate the channel on real PTT."""
    env = skywave.child_env()               # src root on PYTHONPATH for a source checkout
    if extra_env:
        env.update({k: str(v) for k, v in extra_env.items()})
    stdin = sp.PIPE if env.get("SIM_PTT", "0").strip() == "1" else None
    # Redirect the sim's stdout/stderr to a FILE, never the inherited stdout pipe.
    # channel_sim is an os.setsid session leader and OWNS the arecord/aplay children,
    # so if it inherited the parent's stdout, an orphaned sim (e.g. after a
    # `timeout`-killed cell) would keep that pipe's write end open and WEDGE output
    # collection: goodput_sweep's communicate() — and a headless agent's Bash tool —
    # block forever waiting for EOF, surfacing as "exit 1, empty output" on a run that
    # actually completed. A log file preserves SIM_KEYLOG/diagnostics while breaking the
    # inherited-pipe leak. Overridable via SIM_LOG.
    #
    # The default is a SINGLE path truncated at every launch, so back-to-back cells
    # overwrite each other and only the last survives. That is fine for a one-off manual
    # run (the caller wants "the log"), but it silently destroys per-cell evidence across
    # a campaign -- notably the fade-schedule transition timestamps, which are the ground
    # truth for scoring mode-switch latency. sweep_runner therefore sets SIM_LOG per cell
    # (and calibrate_pep per ladder condition); any other campaign driver launching cells
    # in a loop should do the same rather than inherit this default.
    log_path = env.get("SIM_LOG", "/tmp/channel_sim.log")
    simlog = open(log_path, "wb")
    p = sp.Popen([sys.executable, "-u", SIM], env=env, stdin=stdin,
                 stdout=simlog, stderr=sp.STDOUT, preexec_fn=os.setsid)
    simlog.close()  # the child holds its own dup; the parent doesn't need it
    wait_sim_ready(p, log_path, float(env.get("SKYW_SIM_READY_S", "600") or "600"))
    return p


READY_MARK = b"channel_sim: READY"


def wait_sim_ready(p, log_path, timeout_s, poll_s=0.1):
    """Block until the sim has built its channel effects (its `channel_sim: READY` line in
    the log it was just given, truncated at launch so no stale mark), it exits (a config
    error: the caller's existing dead-sim handling reports it), or `timeout_s` passes
    (SKYW_SIM_READY_S, default 600; a warning, then the caller proceeds as before).
    Returns the seconds waited, or None on timeout/exit. A QRM-replay rig takes tens of
    seconds here; without the wait the stations started into a channel that was not yet
    pumping (2026-10-03)."""
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            with open(log_path, "rb") as f:
                if READY_MARK in f.read():
                    return time.time() - t0
        except OSError:
            pass
        if p.poll() is not None:
            return None
        time.sleep(poll_s)
    print(f"bench_pipes: channel_sim not READY after {timeout_s:g}s -- starting stations anyway",
          file=sys.stderr, flush=True)
    return None


def fwd_ptt(sim, station_label, line):
    """SIM_PTT mode: relay one modem's host PTT line to the channel sim's stdin as
    'a 1'/'a 0'/'b 1'/'b 0' so the sim gates half-duplex on real PTT instead of VOX.

    Handles both host-protocol token styles seen across harnesses:
      'PTT ON' / 'PTT OFF'    -- VARA, Mercury, and others
      'PTT TRUE' / 'PTT FALSE'-- ARDOP (ardopcf)
    station_label is 'A' or 'B' (A's TX is captured as sim source 'a', B's as 'b' --
    a mapping that holds for every harness here). No-op when sim has no stdin pipe
    (i.e. SIM_PTT was not requested), so it is safe to call unconditionally."""
    if sim is None or sim.stdin is None:
        return
    if "PTT ON" in line or "PTT TRUE" in line:
        v = "1"
    elif "PTT OFF" in line or "PTT FALSE" in line:
        v = "0"
    else:
        return
    st = "a" if station_label == "A" else "b"
    try:
        sim.stdin.write(f"{st} {v}\n".encode())
        sim.stdin.flush()
    except (BrokenPipeError, OSError):
        pass
