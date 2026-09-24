"""SIM_LISTEN — the lockstep rig served over TCP, one port, pairs of stations.

Drives channel_sim as a real process with python stand-ins for armstrong's
`--relay` station: first-come pairing (A then B), the delivered audio
byte-exact against the Link ground truth (TCP is a pure transport swap), a
third station turned away while a pair runs, re-pairing after a station
leaves, a station that hangs up before pairing, the stall timeout, and the
operator defaults (with explicit env winning).
"""
import os
import socket
import subprocess as sp
import sys
import time

import numpy as np
import pytest

import skywave
from conftest import REPO_ROOT, load_sim, make_link, feed, tone_block

from skywave import sock_frames


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_listen(sock_dir, port, **env_over):
    """Launch the sim on SIM_LISTEN=127.0.0.1:<port>; returns (proc, log path)
    once it is listening. stderr goes to a file so the test can poll it."""
    env = dict(os.environ)
    for k in ("NP_STATS", "SIM_TXDUMP", "SIM_KEYLOG", "SIM_SOCK_SHIM",
              "SIM_TRANSPORT", "SIM_CLOCK", "SIM_HALF_DUPLEX", "SIM_PTT"):
        env.pop(k, None)
    env.update({"SIM_LISTEN": f"127.0.0.1:{port}", "SIM_SOCK_DIR": sock_dir,
                "SIM_VIRT_MAX_RATIO": "0", "SIGMA": "0", "SEED": "777",
                "TXGAIN": "1.0", "SIM_NCH": "2", "SIM_BLOCK": "1024"})
    env.update({k: str(v) for k, v in env_over.items()})
    log = os.path.join(sock_dir, "sim.log")
    proc = sp.Popen([sys.executable, "-u", "-m", "skywave.channel_sim"],
                    env=skywave.child_env(env), cwd=REPO_ROOT,
                    stderr=open(log, "wb"), stdin=sp.DEVNULL)
    wait_log(proc, log, "listening on")
    return proc, log


def wait_log(proc, log, needle, count=1, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = open(log, errors="replace").read()
        if text.count(needle) >= count:
            return text
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    raise AssertionError(f"no {needle!r} x{count} in sim log:\n"
                         + open(log, errors="replace").read())


def stop(proc):
    proc.terminate()
    try:
        proc.wait(timeout=5.0)
    except sp.TimeoutExpired:
        proc.kill()
        proc.wait()


class Station:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.f = self.sock.makefile("rb")

    def recv(self, nbytes):
        buf = bytearray(nbytes)
        hdr = sock_frames.recv_into(self.f, sock_frames.HDR_SIM, memoryview(buf))
        return hdr, bytes(buf)

    def send(self, seq, ptt, nframes, payload):
        self.sock.sendall(sock_frames.pack_station(seq, ptt, nframes, payload))

    def closed_by_peer(self, timeout=5.0):
        """True once the sim has closed us: drains frames until EOF."""
        self.sock.settimeout(timeout)
        try:
            while self.f.read(65536):
                pass
            return True
        except (socket.timeout, OSError):
            return False

    def close(self):
        self.f.close()
        self.sock.close()


def test_parse_listen():
    cs = load_sim()
    assert cs.parse_listen("8340") == ("127.0.0.1", 8340)
    assert cs.parse_listen("0.0.0.0") == ("0.0.0.0", 8340)
    assert cs.parse_listen("0.0.0.0:9000") == ("0.0.0.0", 9000)
    assert cs.parse_listen(":9000") == ("127.0.0.1", 9000)
    assert cs.parse_listen("simhost") == ("simhost", 8340)
    assert cs.parse_listen("[::]:9000") == ("::", 9000)
    assert cs.parse_listen("[::1]") == ("::1", 8340)
    assert cs.parse_listen("::") == ("::", 8340)
    for bad in ("host:port", "0.0.0.0:0", "0.0.0.0:70000", "[::]x", "[::]:",
                "[::1"):
        with pytest.raises(ValueError):
            cs.parse_listen(bad)


def test_listen_operator_defaults_and_explicit_env_wins():
    try:
        cs = load_sim(SIM_LISTEN="9000")
        assert (cs.TRANSPORT, cs.SIM_CLOCK) == ("sock", "virt_time")
        assert cs.HALF_DUPLEX and cs.SIM_PTT and cs.VIRT_MAX_RATIO == 1.0
        assert cs.LISTEN_ADDR == ("127.0.0.1", 9000)
        cs = load_sim(SIM_LISTEN="9000", SIM_HALF_DUPLEX=0, SIM_VIRT_MAX_RATIO=0)
        assert not cs.HALF_DUPLEX and cs.VIRT_MAX_RATIO == 0.0
    finally:
        load_sim()      # drop SIM_LISTEN and its setdefaults from os.environ


def test_listen_pairs_turns_away_and_repairs(sock_dir):
    cs = load_sim(SIGMA=150, SEED=777, SIM_HALF_DUPLEX=1, SIM_PTT=1)
    nblocks = 6
    tones = [tone_block(cs, block_index=i) for i in range(nblocks)]
    ptt = cs.PttState()
    ptt.a = True
    ref = make_link(cs, seed=777 + 11, ptt=ptt)
    want = [feed(ref, b) for b in tones]

    port = free_port()
    sim, log = start_listen(sock_dir, port, SIGMA=150)
    try:
        a = Station(port)
        wait_log(sim, log, "waiting for a second")
        b = Station(port)
        wait_log(sim, log, "paired A=")
        silence = bytes(cs.NBYTES)
        got_b = []
        for k in range(nblocks + 1):
            ha, _ = a.recv(cs.NBYTES)
            hb, rx_b = b.recv(cs.NBYTES)
            assert ha[0] == k and hb[0] == k
            assert ha[1] == ((k + 1) * cs.BLOCK * 1000) // cs.FS
            got_b.append(rx_b)
            tx_a = tones[k].tobytes() if k < nblocks else silence
            a.send(k, 1 if k < nblocks else 0, cs.BLOCK, tx_a)
            b.send(k, 0, cs.BLOCK, silence)
        # The first-come station is A: B hears A's tone through the A->B
        # transform, byte-exact, one block late (block 0 is the primer).
        assert not np.any(np.frombuffer(got_b[0], dtype="<i2"))
        for k in range(nblocks):
            g = np.frombuffer(got_b[k + 1], dtype="<i2")
            w = np.frombuffer(want[k].tobytes(), dtype="<i2")
            assert np.array_equal(g, w), f"RX block {k + 1} != transform(TX {k})"

        c = Station(port)
        assert c.closed_by_peer(), "a third station must be turned away"
        wait_log(sim, log, "turned away 127.0.0.1")

        a.close()                           # A leaves: the pair ends for B too
        assert b.closed_by_peer()
        wait_log(sim, log, "pair ended")

        d, e = Station(port), Station(port)
        wait_log(sim, log, "paired A=", count=2)
        for st in (d, e):                   # a fresh rig: the clock restarts
            hdr, _ = st.recv(cs.NBYTES)
            assert hdr[0] == 0 and hdr[1] == (cs.BLOCK * 1000) // cs.FS
        for st in (b, c, d, e):
            st.close()
    finally:
        stop(sim)
    text = open(log, errors="replace").read()
    assert "transport=tcp(127.0.0.1:" in text
    assert "half-duplex keying=PTT" in text     # the operator defaults


def test_listen_drops_a_station_that_left_before_pairing(sock_dir):
    cs = load_sim()
    port = free_port()
    sim, log = start_listen(sock_dir, port)
    try:
        gone = Station(port)
        wait_log(sim, log, "waiting for a second")
        gone.close()
        time.sleep(0.2)
        b, c = Station(port), Station(port)
        wait_log(sim, log, "left before pairing")
        wait_log(sim, log, "paired A=")
        for st in (b, c):
            hdr, _ = st.recv(cs.NBYTES)
            assert hdr[0] == 0
        b.close(); c.close()
    finally:
        stop(sim)


def test_listen_stall_ends_the_pair(sock_dir):
    cs = load_sim()
    port = free_port()
    sim, log = start_listen(sock_dir, port, SIM_LISTEN_STALL_S=1, SIM_LISTEN_ONCE=1)
    try:
        a, b = Station(port), Station(port)
        a.recv(cs.NBYTES)
        b.recv(cs.NBYTES)
        a.send(0, 0, cs.BLOCK, bytes(cs.NBYTES))   # B never answers
        t0 = time.monotonic()
        assert a.closed_by_peer(timeout=10.0)
        assert time.monotonic() - t0 < 5.0
        assert sim.wait(timeout=10.0) == 0         # SIM_LISTEN_ONCE: exits
        a.close(); b.close()
    finally:
        if sim.poll() is None:
            stop(sim)


def test_listen_rejects_a_conflicting_transport(sock_dir):
    load_sim()
    port = free_port()
    env = dict(os.environ, SIM_LISTEN=f"127.0.0.1:{port}", SIM_TRANSPORT="alsa",
               SIM_SOCK_DIR=sock_dir)
    r = sp.run([sys.executable, "-u", "-m", "skywave.channel_sim"],
               env=skywave.child_env(env), cwd=REPO_ROOT, stdin=sp.DEVNULL,
               capture_output=True, timeout=30)
    assert r.returncode == 2
    assert b"SIM_LISTEN needs SIM_TRANSPORT=sock" in r.stderr
