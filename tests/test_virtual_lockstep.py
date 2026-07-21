"""Virtual-rig stage 2 — SIM_CLOCK=virt_time block-lockstep loop.

Drives channel_sim as a real process with two fake virtual-clock stations
(python stand-ins for a station's `--audio sock` backend): checks the barrier sequencing
(one RX frame per block, header virtual_now_ms = exact block end time), the
one-block channel latency (RX block k = transform of TX block k-1), in-band
PTT keying under half-duplex, byte-exactness against the wall-path
Link.process ground truth, and the SIM_MAX_VIRTUAL_S timeout marker.
"""
import os
import socket
import subprocess as sp
import sys
import time

import numpy as np

import skywave
from conftest import REPO_ROOT, load_sim, make_link, feed, tone_block

from skywave import sock_frames


def start_sim(sock_dir, **env_over):
    env = dict(os.environ)
    env.update({"SIM_TRANSPORT": "sock", "SIM_CLOCK": "virt_time",
                "SIM_SOCK_DIR": sock_dir, "SIM_SOCK_ACCEPT_S": "10",
                "SIGMA": "0", "SEED": "777", "TXGAIN": "1.0",
                "SIM_NCH": "2", "SIM_BLOCK": "1024"})
    env.update({k: str(v) for k, v in env_over.items()})
    for k in ("NP_STATS", "SIM_TXDUMP", "SIM_KEYLOG", "SIM_SOCK_SHIM"):
        env.pop(k, None)
    return sp.Popen([sys.executable, "-u", "-m", "skywave.channel_sim"],
                    env=skywave.child_env(env), cwd=REPO_ROOT, stderr=sp.PIPE)


def connect(sock_dir, name):
    path = os.path.join(sock_dir, name)
    deadline = time.monotonic() + 10.0
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


def stop_sim(sim):
    sim.terminate()
    try:
        return sim.communicate(timeout=5.0)[1] or b""
    except sp.TimeoutExpired:
        # Pre-existing shutdown race (observed 2026-07-13, across multiple
        # releases): the sim occasionally outlives SIGTERM past the window.
        # SIGKILL it and return the *stderr* that is already buffered in the
        # pipe — the old code returned index [0] (stdout, always None), which
        # turned this slow-shutdown flake into a bogus banner-assert failure.
        sim.kill()
        return sim.communicate()[1] or b""


def test_lockstep_barrier_latency_ptt_and_byte_exactness(sock_dir):
    cs = load_sim(SIGMA=150, SEED=777, SIM_HALF_DUPLEX=1, SIM_PTT=1)
    nblocks = 6
    tones = [tone_block(cs, block_index=i) for i in range(nblocks)]
    # Ground truth: the wall-path Link (a->b), keyed via PttState, same seed.
    ptt = cs.PttState()
    ptt.a = True
    ref = make_link(cs, seed=777 + 11, ptt=ptt)
    want = [feed(ref, b) for b in tones]

    sim = start_sim(sock_dir, SIGMA=150, SIM_HALF_DUPLEX=1, SIM_PTT=1)
    try:
        sa, sb = connect(sock_dir, "a.sock"), connect(sock_dir, "b.sock")
        fa, fb = sa.makefile("rb"), sb.makefile("rb")
        buf_a = bytearray(cs.NBYTES)
        buf_b = bytearray(cs.NBYTES)
        got_b = []
        silence = bytes(cs.NBYTES)
        for k in range(nblocks + 1):
            ha = sock_frames.recv_into(fa, sock_frames.HDR_SIM, memoryview(buf_a))
            hb = sock_frames.recv_into(fb, sock_frames.HDR_SIM, memoryview(buf_b))
            assert ha is not None and hb is not None
            # barrier sequencing + the exact virtual clock in the header
            assert ha[0] == k and hb[0] == k
            assert ha[1] == ((k + 1) * cs.BLOCK * 1000) // cs.FS
            got_b.append(bytes(buf_b))
            # station A transmits the tone (ptt=1); B idles (ptt=0)
            tx_a = tones[k].tobytes() if k < nblocks else silence
            sa.sendall(sock_frames.pack_station(k, 1 if k < nblocks else 0,
                                                cs.BLOCK, tx_a))
            sb.sendall(sock_frames.pack_station(k, 0, cs.BLOCK, silence))
        # RX block 0 is the cold-start silence primer (no TX has been heard
        # yet — the cable's one-block latency); RX block k+1 is the transform
        # of TX block k, byte-exact against the wall-path ground truth.
        assert not np.any(np.frombuffer(got_b[0], dtype="<i2"))
        for k in range(nblocks):
            g = np.frombuffer(got_b[k + 1], dtype="<i2")
            w = np.frombuffer(want[k].tobytes(), dtype="<i2")
            assert np.array_equal(g, w), f"RX block {k + 1} != transform(TX {k})"
        sa.close(); sb.close()
    finally:
        err = stop_sim(sim)
    assert b"clock=virt_time" in err


def test_lockstep_unkeyed_station_delivers_noise_floor_only(sock_dir):
    cs = load_sim(SIGMA=0, SEED=777, SIM_HALF_DUPLEX=1, SIM_PTT=1)
    sim = start_sim(sock_dir, SIGMA=0, SIM_HALF_DUPLEX=1, SIM_PTT=1)
    try:
        sa, sb = connect(sock_dir, "a.sock"), connect(sock_dir, "b.sock")
        fa, fb = sa.makefile("rb"), sb.makefile("rb")
        buf = bytearray(cs.NBYTES)
        tone = tone_block(cs).tobytes()
        silence = bytes(cs.NBYTES)
        for k in range(4):
            assert sock_frames.recv_into(fa, sock_frames.HDR_SIM,
                                         memoryview(buf)) is not None
            hb = sock_frames.recv_into(fb, sock_frames.HDR_SIM, memoryview(buf))
            assert hb is not None
            if k >= 2:
                # A transmits but ptt=0 -> half-duplex gate must block it
                assert not np.any(np.frombuffer(bytes(buf), dtype="<i2")), \
                    f"unkeyed TX leaked into RX block {k}"
            sa.sendall(sock_frames.pack_station(k, 0, cs.BLOCK, tone))
            sb.sendall(sock_frames.pack_station(k, 0, cs.BLOCK, silence))
        sa.close(); sb.close()
    finally:
        stop_sim(sim)


def test_lockstep_virtual_timeout_marker(sock_dir):
    cs = load_sim(SIGMA=0)
    sim = start_sim(sock_dir, SIM_MAX_VIRTUAL_S="0.5")
    sa, sb = connect(sock_dir, "a.sock"), connect(sock_dir, "b.sock")
    fa, fb = sa.makefile("rb"), sb.makefile("rb")
    buf = bytearray(cs.NBYTES)
    silence = bytes(cs.NBYTES)
    # keep answering until the sim ends the run itself
    while True:
        try:
            if sock_frames.recv_into(fa, sock_frames.HDR_SIM,
                                     memoryview(buf)) is None:
                break
            if sock_frames.recv_into(fb, sock_frames.HDR_SIM,
                                     memoryview(buf)) is None:
                break
            k = 0
            sa.sendall(sock_frames.pack_station(k, 0, cs.BLOCK, silence))
            sb.sendall(sock_frames.pack_station(k, 0, cs.BLOCK, silence))
        except (OSError, EOFError, ValueError):
            break
    out, err = sim.communicate(timeout=10.0), b""
    stderr = out[1] or b""
    assert sim.returncode == 0
    assert b"VIRTUAL-TIMEOUT at 0.5" in stderr
    sa.close(); sb.close()


def test_clock_virtual_requires_sock_transport(tmp_path):
    env = dict(os.environ)
    env.update({"SIM_TRANSPORT": "alsa", "SIM_CLOCK": "virt_time",
                "SIGMA": "0", "TXGAIN": "1.0", "SIM_NCH": "2",
                "SIM_BLOCK": "1024", "SEED": "1"})
    r = sp.run([sys.executable, "-m", "skywave.channel_sim"],
               env=skywave.child_env(env), cwd=REPO_ROOT, capture_output=True, timeout=15)
    assert r.returncode == 2
    assert b"SIM_CLOCK=virt_time requires" in r.stderr
