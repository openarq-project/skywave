#!/usr/bin/env python3
"""Stage-1 sock<->ALSA shim for the virtual-rig transport (one per station).

Bridges one station's side of the snd-aloop cable to channel_sim's framed unix
socket (SIM_TRANSPORT=sock), so the REAL modem binaries run over the new
transport at real time — the ALSA clock still paces everything; the sim itself
is no longer wall-aware. It exists to prove the socket
framing carries the cable bit-exactly before stage 2 swaps the stations for
a station's native socket backend and drops ALSA entirely.

    modem TX -> aloop -> [arecord CAP] -> station frames -> sim a/b.sock
    sim a/b.sock -> sim frames -> [aplay PLAY] -> aloop -> modem RX

The shim cannot see the modem's PTT, so every TX frame carries ptt=255
(PTT_UNKNOWN) and the sim's stdin PTT relay keeps governing keying, unchanged.
Device map, format and block size mirror channel_sim (same env: SIM_NCH,
SIM_BLOCK); the two directions run on independent threads for the same reason
the sim's do — a half-duplex modem stalls its own TX capture when receiving.

Spawned by channel_sim when SIM_SOCK_SHIM=1 (inheriting its process group, so
the harness's killpg teardown reaches the arecord/aplay children as before).

Usage: sock_alsa_shim.py --station a|b --sock PATH [--cap DEV] [--play DEV]
"""
import argparse
import os
import socket
import subprocess as sp
import sys
import threading
import time

from skywave import sock_frames

FS = 48000
NCH = int(os.environ.get("SIM_NCH", "2").strip() or "2")
BLOCK = int(os.environ.get("SIM_BLOCK", "1024").strip() or "1024")
NBYTES = BLOCK * NCH * 2
BUF = ["--buffer-time=60000", "--period-time=15000"]

# Same cable endpoints as channel_sim's alsa transport (its CAP_*/PLAY_* map):
# station a: TX captured at plughw:2,0, RX played at plughw:3,0
# station b: TX captured at plughw:4,1, RX played at plughw:5,1
DEV = {"a": ("plughw:2,0", "plughw:3,0"), "b": ("plughw:4,1", "plughw:5,1")}


def connect(path, timeout_s=15.0):
    """Connect to the sim's station socket, retrying until it is bound."""
    deadline = time.monotonic() + timeout_s
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


def read_exact(f, mv):
    n, L = 0, len(mv)
    while n < L:
        k = f.readinto(mv[n:])
        if not k:
            return n
        n += k
    return n


def write_all(fd, buf):
    mv = memoryview(buf)
    while mv:
        try:
            k = os.write(fd, mv)
        except (BrokenPipeError, OSError):
            return False
        mv = mv[k:]
    return True


def uplink(cap, sock, stop):
    """arecord -> station frames (TX). Runs until capture EOF or send failure."""
    buf = bytearray(NBYTES)
    mv = memoryview(buf)
    seq = 0
    try:
        while not stop.is_set():
            if read_exact(cap.stdout, mv) < NBYTES:
                break
            sock.sendall(sock_frames.pack_station(
                seq, sock_frames.PTT_UNKNOWN, BLOCK, buf))
            seq += 1
    except OSError:
        pass
    stop.set()


def downlink(sock, play, stop):
    """sim frames -> aplay (RX). Runs until socket EOF or playback failure."""
    buf = bytearray(NBYTES)
    mv = memoryview(buf)
    f = sock.makefile("rb")
    try:
        while not stop.is_set():
            hdr = sock_frames.recv_into(f, sock_frames.HDR_SIM, mv)
            if hdr is None or hdr[2] != BLOCK:
                break
            if not write_all(play.stdin.fileno(), buf):
                break
    except (EOFError, ValueError, OSError):
        pass
    stop.set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--station", choices=("a", "b"), required=True)
    ap.add_argument("--sock", required=True)
    ap.add_argument("--cap", default=None)
    ap.add_argument("--play", default=None)
    a = ap.parse_args()
    cap_dev, play_dev = DEV[a.station]
    cap_dev = a.cap or cap_dev
    play_dev = a.play or play_dev

    sock = connect(a.sock)
    cap = sp.Popen(["arecord", "-D", cap_dev, "-f", "S16_LE", "-r", str(FS),
                    "-c", str(NCH), *BUF], stdout=sp.PIPE, stderr=sp.DEVNULL,
                   bufsize=0)
    play = sp.Popen(["aplay", "-D", play_dev, "-f", "S16_LE", "-r", str(FS),
                     "-c", str(NCH), *BUF], stdin=sp.PIPE, stderr=sp.DEVNULL,
                    bufsize=0)
    print(f"sock_alsa_shim[{a.station}]: cap={cap_dev} play={play_dev} "
          f"sock={a.sock} block={BLOCK}f nch={NCH}", file=sys.stderr, flush=True)

    stop = threading.Event()
    t_up = threading.Thread(target=uplink, args=(cap, sock, stop), daemon=True)
    t_dn = threading.Thread(target=downlink, args=(sock, play, stop), daemon=True)
    t_up.start(); t_dn.start()
    try:
        while not stop.is_set():
            stop.wait(0.5)
    finally:
        stop.set()
        for p in (cap, play):
            try:
                p.kill()
            except (OSError, ProcessLookupError):
                pass
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()
        t_up.join(timeout=2.0); t_dn.join(timeout=2.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
