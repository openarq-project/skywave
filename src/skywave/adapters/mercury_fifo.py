#!/usr/bin/env python3
"""MercuryFifoAdapter -- Mercury over its device-free `-x fifo` backend.

This runs the Mercury HF modem through skywave with NO soundcard and NO ALSA
loopback: each station reads/writes raw s32le 8 kHz PCM over a named pipe
(`mercury -x fifo -i <rx.fifo> -o <tx.fifo>`), and skywave bridges the two
directions through `hfchan` -- its codec2-`ch`-compatible one-way channel filter,
the SAME WattersonChannel + rig physics the ALSA path uses. So Mercury benchmarks
on macOS (and Linux) with no virtual audio device at all.

The control plane (TCP TNC, MYCALL/LISTEN/CONNECT, the payload transfer, telemetry
scrape) is identical to the ALSA MercuryAdapter and is inherited unchanged; only
the audio transport (fifo bridge) and process launch differ.

Bridge design mirrors Mercury's own `-x fifo` integration bridge: two independent
per-direction filters at 8 kHz (Mercury's native modem rate, so no resample -- just
s32le<->s16 by >>16 / <<16), with RX delivery PACED at the sample rate. The pacing
is load-bearing: Mercury writes a whole burst to its TX fifo faster than real time
while holding PTT, so the peer's RX must be dripped at 8 kHz or it would decode and
reply while the sender still has PTT up (a half-duplex turnaround the modem would
then discard).

Set MERCURY_BIN to the Mercury binary, then run as the `mercury_fifo` modem:

  skywave-sweep mercury_fifo spec.json out.csv
"""
import errno
import os
import subprocess as sp
import sys
import threading
import time

import numpy as np

import skywave
from skywave.adapters.mercury import MercuryAdapter
from skywave.modem_adapter import ModemAdapter, run_adapter

FS = 8000                 # Mercury's native modem rate; the fifo carries raw s32le at this rate
PACE_BLOCK = 160          # RX pacing granularity: 160 samples = 20 ms per block


class _FifoBridge:
    """Bridges Mercury's four s32le/8 kHz fifos through two `hfchan` filters.

    a_tx -> hfchan(fwd) -> b_rx   (A's transmit reaches B's receive)
    b_tx -> hfchan(rev) -> a_rx   (and vice versa)
    """

    def __init__(self, fifos, cfg, hfchan_argv):
        self.fifos = fifos
        self.cfg = cfg
        self.hfchan_argv = hfchan_argv
        self._stop = threading.Event()
        self.procs = []
        self.threads = []

    def _hfchan_args(self, seed):
        a = list(self.hfchan_argv) + [
            "-", "-", "--Fs", str(FS), "--quiet",
            "--block", str(PACE_BLOCK), "--seed", str(seed),
            "--gain", str(self.cfg.txgain),
        ]
        # skywave-native noise scale (int16 LSB std), the same knob channel_sim's
        # SIGMA uses; falls through to hfchan's clean --No default when unset/0.
        if self.cfg.sigma and self.cfg.sigma not in ("0", "0.0"):
            a += ["--sigma", str(self.cfg.sigma)]
        if self.cfg.watterson and self.cfg.watterson not in ("off", ""):
            a += ["--fade", self.cfg.watterson]
        return a

    def start(self):
        # per-direction seeds, offset like channel_sim's SEED+11 / SEED+22
        self._spawn(self.fifos["a_tx"], self.fifos["b_rx"], self.cfg.seed + 11)
        self._spawn(self.fifos["b_tx"], self.fifos["a_rx"], self.cfg.seed + 22)

    def _spawn(self, tx_path, rx_path, seed):
        proc = sp.Popen(self._hfchan_args(seed), stdin=sp.PIPE, stdout=sp.PIPE,
                        stderr=sp.DEVNULL, env=skywave.child_env())
        self.procs.append(proc)
        for target, args in ((self._tx_pump, (tx_path, proc)),
                             (self._rx_pump, (proc, rx_path))):
            t = threading.Thread(target=target, args=args, daemon=True)
            t.start()
            self.threads.append(t)

    def _tx_pump(self, tx_path, proc):
        """Mercury TX fifo (s32le) -> s16 -> hfchan stdin. A whole burst arrives
        much faster than real time; just forward it, hfchan/the RX pump pace it."""
        fd = os.open(tx_path, os.O_RDONLY | os.O_NONBLOCK)   # RDONLY fifo open never blocks
        carry = b""
        try:
            while not self._stop.is_set():
                try:
                    chunk = os.read(fd, 64 * 1024)
                except BlockingIOError:
                    time.sleep(0.002)
                    continue
                if not chunk:                     # writer gone/idle: no EOF on a fifo we hold
                    time.sleep(0.002)
                    continue
                raw = carry + chunk
                whole = len(raw) & ~3             # s32le sample alignment
                carry = raw[whole:]
                if whole:
                    x = np.frombuffer(raw[:whole], dtype="<i4")
                    s16 = (x >> 16).astype("<i2").tobytes()
                    try:
                        proc.stdin.write(s16)
                        proc.stdin.flush()
                    except (BrokenPipeError, ValueError, OSError):
                        break
        finally:
            os.close(fd)
            try:
                proc.stdin.close()
            except OSError:
                pass

    def _rx_pump(self, proc, rx_path):
        """hfchan stdout (s16) -> s32 -> Mercury RX fifo, PACED at 8 kHz."""
        fd = self._open_write_retry(rx_path)
        if fd < 0:
            return
        nbytes = PACE_BLOCK * 2                    # s16 bytes per paced block
        out = proc.stdout
        deadline = None
        try:
            while not self._stop.is_set():
                buf = out.read(nbytes)             # exactly one block, or short at EOF
                if not buf:
                    break
                x = np.frombuffer(buf[:len(buf) & ~1], dtype="<i2")
                s32 = (x.astype("<i4") << 16).tobytes()
                now = time.monotonic()             # absolute-clock pacing: one block per n/Fs s
                if deadline is None or deadline < now:
                    deadline = now                 # idle gap: restart the pacing clock
                if deadline > now:
                    time.sleep(deadline - now)
                deadline += len(x) / float(FS)
                self._write_all(fd, s32)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def _open_write_retry(self, path):
        """O_WRONLY|O_NONBLOCK on a fifo raises ENXIO until Mercury opens the read
        end; retry until it does (or we are torn down)."""
        while not self._stop.is_set():
            try:
                return os.open(path, os.O_WRONLY | os.O_NONBLOCK)
            except OSError as e:
                if e.errno in (errno.ENXIO, errno.ENOENT):
                    time.sleep(0.02)
                    continue
                raise
        return -1

    def _write_all(self, fd, data):
        mv = memoryview(data)
        while mv and not self._stop.is_set():
            try:
                mv = mv[os.write(fd, mv):]
            except BlockingIOError:
                time.sleep(0.001)
            except OSError:
                return

    def stop(self):
        self._stop.set()
        for p in self.procs:                       # kill hfchan so blocked stdout reads unblock
            try:
                p.kill()
            except OSError:
                pass
        for t in self.threads:
            t.join(timeout=1.0)


class MercuryFifoAdapter(MercuryAdapter):
    name = "mercury_fifo"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.sockdir = (os.environ.get("SIM_SOCK_DIR", "").strip()
                        or f"/tmp/skywave-mercfifo-{os.getpid()}")
        os.makedirs(self.sockdir, exist_ok=True)
        self.fifos = {k: os.path.join(self.sockdir, f"{k}.s32le.fifo")
                      for k in ("a_rx", "a_tx", "b_rx", "b_tx")}
        self._bridge = None

    # ---- channel: device-free fifo bridge, no channel_sim/ALSA ----
    def preclean_patterns(self):
        return ["mercury .*-x fifo"]

    def launch_channel(self):
        for p in self.fifos.values():
            try:
                os.mkfifo(p, 0o600)
            except FileExistsError:
                pass
        self._bridge = _FifoBridge(self.fifos, self.cfg,
                                   [sys.executable, "-u", "-m", "skywave.hfchan"])
        self._bridge.start()
        # self._sim stays None -> the base's PTT relay / teardown_channel are no-ops

    def teardown_channel(self):
        if self._bridge is not None:
            self._bridge.stop()
        for p in self.fifos.values():
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass

    # ---- stations: mercury -x fifo, not -x alsa ----
    def start_stations(self):
        self._launch(self.fifos["a_rx"], self.fifos["a_tx"], self.A_PORT, 8100, "/tmp/mfA.log")  # A answerer
        self._launch(self.fifos["b_rx"], self.fifos["b_tx"], self.B_PORT, 8110, "/tmp/mfB.log")  # B caller

    def _launch(self, rx, tx, port, bcast, log):
        p = sp.Popen([self.merc, "-x", "fifo", "-i", rx, "-o", tx,
                      "-p", str(port), "-b", str(bcast), "-m", "1"],
                     stdout=open(log, "wb"), stderr=sp.STDOUT)
        self._stations.append(p)

    def teardown_stations(self):
        try:
            if self.b is not None:
                self._snd(self.b, "DISCONNECT"); time.sleep(2)
        except OSError:
            pass
        ModemAdapter.teardown_stations(self)         # SIGTERM stations; skip the base's ALSA pkill
        sp.run(["pkill", "-9", "-f", "mercury .*-x fifo"],
               stdout=sp.DEVNULL, stderr=sp.DEVNULL)


if __name__ == "__main__":
    sys.exit(run_adapter(MercuryFifoAdapter))
