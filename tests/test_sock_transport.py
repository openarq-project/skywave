"""Virtual-rig stage 1 — framed unix-socket transport (SIM_TRANSPORT=sock).

Golden-pins the wire format (sock_frames.py is a wire contract: channel_sim,
the sock<->ALSA shim, and a future native socket-backend station must all agree),
and proves the sock transport is a pure block-I/O swap: the delivered samples
are byte-identical to the alsa-transport Link.process ground truth, PTT rides
in-band block-exactly, and the full sim process runs end-to-end over sockets
with no ALSA devices.
"""
import os
import socket
import subprocess as sp
import sys
import threading
import time

import numpy as np

from conftest import REPO_ROOT, load_sim, make_link, feed, tone_block

import skywave
from skywave import sock_frames

PAYLOAD12 = bytes(range(12))   # 3 frames x NCH=2 x i16 = 12 sample bytes

# ---------------------------------------------------------------- wire golden

GOLD_SIM = bytes.fromhex(
    "1e000000"                  # u32 len = 18 hdr + 12 payload
    "0100000000000000"          # u64 seq = 1
    "0200000000000000"          # u64 virtual_now_ms = 2
    "0300"                      # u16 n = 3
    "000102030405060708090a0b")
GOLD_STA = bytes.fromhex(
    "17000000"                  # u32 len = 11 hdr + 12 payload
    "0100000000000000"          # u64 seq = 1
    "01"                        # u8 ptt = 1
    "0300"                      # u16 n = 3
    "000102030405060708090a0b")


def test_frame_goldens():
    assert sock_frames.pack_sim(1, 2, 3, PAYLOAD12) == GOLD_SIM
    assert sock_frames.pack_station(1, 1, 3, PAYLOAD12) == GOLD_STA
    assert sock_frames.PTT_UNKNOWN == 255


def test_frame_roundtrip_and_errors():
    import io
    out = bytearray(12)
    f = io.BytesIO(GOLD_STA + GOLD_STA)
    assert sock_frames.recv_into(f, sock_frames.HDR_STA, memoryview(out)) == (1, 1, 3)
    assert bytes(out) == PAYLOAD12
    # second frame, then clean EOF at the boundary
    assert sock_frames.recv_into(f, sock_frames.HDR_STA, memoryview(out)) == (1, 1, 3)
    assert sock_frames.recv_into(f, sock_frames.HDR_STA, memoryview(out)) is None
    # size mismatch (sim header parsed with station struct) is a protocol error
    try:
        sock_frames.recv_into(io.BytesIO(GOLD_SIM), sock_frames.HDR_STA,
                              memoryview(out))
        assert False, "expected ValueError"
    except ValueError:
        pass
    # torn frame is EOFError, not a silent None
    try:
        sock_frames.recv_into(io.BytesIO(GOLD_STA[:9]), sock_frames.HDR_STA,
                              memoryview(out))
        assert False, "expected EOFError"
    except EOFError:
        pass


# ------------------------------------------------- SockLink == Link, in-process

def socklink_pair(cs, **kw):
    """A SockLink A->B wired to two socketpairs; returns (link, tx_sock, rx_file)
    where the test writes station frames to tx_sock and reads sim frames from
    rx_file."""
    src_test, src_link = socket.socketpair()
    sink_link, sink_test = socket.socketpair()
    L = cs.SockLink("A->B", src_link, sink_link, 1, "", threading.Event(),
                    "a", "b", cs.Keys(), **kw)
    return L, src_test, sink_test.makefile("rb")


def run_link_thread(L):
    t = threading.Thread(target=L.run, daemon=True)
    t.start()
    return t


def test_socklink_matches_link_byte_exact_awgn():
    """The sock transport delivers byte-identical samples to the alsa-path
    Link.process ground truth (same env, same seed) — transport is I/O only."""
    cs = load_sim(SIGMA=200, TXGAIN=1.0)
    blocks = [tone_block(cs, block_index=i) for i in range(6)]
    # ground truth: direct Link.process drive (the alsa-path technique)
    ref = make_link(cs, seed=1)
    want = [feed(ref, b) for b in blocks]

    cs2 = load_sim(SIGMA=200, TXGAIN=1.0)
    L, tx, rxf = socklink_pair(cs2)
    t = run_link_thread(L)
    out = bytearray(cs2.NBYTES)
    got = []
    for i, b in enumerate(blocks):
        tx.sendall(sock_frames.pack_station(i, sock_frames.PTT_UNKNOWN,
                                            cs2.BLOCK, b.tobytes()))
        hdr = sock_frames.recv_into(rxf, sock_frames.HDR_SIM, memoryview(out))
        assert hdr is not None and hdr[0] == i and hdr[2] == cs2.BLOCK
        got.append(np.frombuffer(bytes(out), dtype="<i2"))
    tx.shutdown(socket.SHUT_WR)   # clean EOF -> link winds down
    t.join(timeout=5.0)
    assert not t.is_alive()
    for i, (w, g) in enumerate(zip(want, got)):
        assert np.array_equal(w, g), f"block {i} diverged over the sock transport"


def test_socklink_inband_ptt_gates_half_duplex():
    """ptt=1/0 in the station frame header keys the channel block-exactly;
    ptt=255 leaves PttState to the stdin relay (untouched)."""
    cs = load_sim(SIGMA=0, SIM_HALF_DUPLEX=1, SIM_PTT=1, SIM_HANG_MS=0)
    ptt = cs.PttState()
    L, tx, rxf = socklink_pair(cs, ptt=ptt)
    t = run_link_thread(L)
    out = bytearray(cs.NBYTES)
    sig = tone_block(cs)

    def xfer(ptt_v, i):
        tx.sendall(sock_frames.pack_station(i, ptt_v, cs.BLOCK, sig.tobytes()))
        assert sock_frames.recv_into(rxf, sock_frames.HDR_SIM,
                                     memoryview(out)) is not None
        return np.frombuffer(bytes(out), dtype="<i2")

    assert not np.any(xfer(0, 0)), "unkeyed station must deliver silence"
    assert ptt.a is False
    assert np.any(xfer(1, 1)), "keyed station must deliver the signal"
    assert ptt.a is True
    prev = ptt.a
    xfer(sock_frames.PTT_UNKNOWN, 2)
    assert ptt.a is prev, "ptt=255 must not touch PttState"
    tx.shutdown(socket.SHUT_WR)
    t.join(timeout=5.0)


def test_socklink_rejects_wrong_block_size():
    cs = load_sim(SIGMA=0)
    L, tx, rxf = socklink_pair(cs)
    t = run_link_thread(L)
    # header claims BLOCK-1 frames but carries a full payload -> length mismatch
    tx.sendall(sock_frames.pack_station(0, 255, cs.BLOCK - 1,
                                        bytes(cs.NBYTES)))
    t.join(timeout=5.0)
    assert not t.is_alive(), "malformed frame must wind the link down"


# ----------------------------------------------- full sim process over sockets

def test_channel_sim_end_to_end_over_sockets(tmp_path):
    """channel_sim as a real process, SIM_TRANSPORT=sock, no ALSA anywhere:
    station A sends a tone + station B silence; B's delivered frames are
    byte-identical to the Link.process ground truth (AWGN seeded SEED+11)."""
    cs = load_sim(SIGMA=150, SEED=777)
    blocks = [tone_block(cs, block_index=i) for i in range(4)]
    ref = make_link(cs, seed=777 + 11)
    want = [feed(ref, b) for b in blocks]

    env = dict(os.environ)
    env.update({"SIM_TRANSPORT": "sock", "SIM_SOCK_DIR": str(tmp_path),
                "SIM_SOCK_ACCEPT_S": "10", "SIGMA": "150", "SEED": "777",
                "TXGAIN": "1.0", "SIM_NCH": "2", "SIM_BLOCK": "1024"})
    for k in ("NP_STATS", "SIM_TXDUMP", "SIM_KEYLOG", "SIM_HALF_DUPLEX",
              "SIM_PTT", "SIM_SOCK_SHIM"):
        env.pop(k, None)
    sim = sp.Popen([sys.executable, "-u", "-m", "skywave.channel_sim"],
                   env=skywave.child_env(env), cwd=REPO_ROOT, stderr=sp.PIPE)
    try:
        def connect(name):
            path = os.path.join(str(tmp_path), name)
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

        sa, sb = connect("a.sock"), connect("b.sock")
        rxb = sb.makefile("rb")
        out = bytearray(cs.NBYTES)
        got = []
        for i, b in enumerate(blocks):
            sa.sendall(sock_frames.pack_station(i, sock_frames.PTT_UNKNOWN,
                                                cs.BLOCK, b.tobytes()))
            hdr = sock_frames.recv_into(rxb, sock_frames.HDR_SIM, memoryview(out))
            assert hdr is not None and hdr[2] == cs.BLOCK
            got.append(np.frombuffer(bytes(out), dtype="<i2"))
        sa.close(); sb.close()
        for i, (w, g) in enumerate(zip(want, got)):
            assert np.array_equal(w, g), f"block {i} diverged (end-to-end)"
    finally:
        sim.terminate()
        try:
            sim.wait(timeout=5.0)
        except sp.TimeoutExpired:
            sim.kill()
            sim.wait()


def test_channel_sim_sock_accept_timeout_exits_2(tmp_path):
    """Nobody connects -> the sim exits 2 with a clear message, no hang."""
    env = dict(os.environ)
    env.update({"SIM_TRANSPORT": "sock", "SIM_SOCK_DIR": str(tmp_path),
                "SIM_SOCK_ACCEPT_S": "0.3", "SIGMA": "0", "TXGAIN": "1.0",
                "SIM_NCH": "2", "SIM_BLOCK": "1024", "SEED": "1"})
    for k in ("NP_STATS", "SIM_SOCK_SHIM"):
        env.pop(k, None)
    r = sp.run([sys.executable, "-m", "skywave.channel_sim"],
               env=skywave.child_env(env), cwd=REPO_ROOT, capture_output=True, timeout=15)
    assert r.returncode == 2
    assert b"did not connect" in r.stderr
