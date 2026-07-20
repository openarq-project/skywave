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
import subprocess as sp

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.join(HERE, "channel_sim.py")


def launch_channel_sim(extra_env=None):
    """Spawn the shared half-duplex channel sim. Returns the Popen (a session leader, so
    one os.killpg tears down the whole rig). Config is read from the environment by
    channel_sim.py; pass extra_env to override/add keys for this run.

    In SIM_PTT mode the sim's stdin is a pipe: the harness writes 'a 1'/'a 0'/'b 1'/'b 0'
    lines (relayed from each modem's host PTT ON/OFF) to gate the channel on real PTT."""
    env = dict(os.environ)
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
    simlog = open(env.get("SIM_LOG", "/tmp/channel_sim.log"), "wb")
    p = sp.Popen(["python3", "-u", SIM], env=env, stdin=stdin,
                 stdout=simlog, stderr=sp.STDOUT, preexec_fn=os.setsid)
    simlog.close()  # the child holds its own dup; the parent doesn't need it
    return p


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
