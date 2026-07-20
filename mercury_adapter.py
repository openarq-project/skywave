#!/usr/bin/env python3
"""MercuryAdapter -- a Mercury adapter on the ModemAdapter base class.

Drives the Mercury HF modem (https://github.com/Rhizomatica/mercury) through skywave:
the 4-card ALSA device map, its TCP host protocol, a 3-try connect, the pump/PTT-relay
loop, and teardown, all expressed as the base class's hooks. A worked example of a real
modem on the ModemAdapter contract -- copy it as a starting point for another modem.

Set MERCURY_BIN to the Mercury binary, then run it via the harness as the `mercury` modem:

  sweep_runner.py mercury spec.json out.csv
"""
import os
import re
import select
import signal
import socket
import subprocess as sp
import time

from modem_adapter import ModemAdapter, run_adapter


class MercuryAdapter(ModemAdapter):
    name = "mercury"
    A_PORT, B_PORT = 8300, 8310          # A = answerer, B = caller
    ready_timeout_s = 15.0
    connect_timeout_s = 150.0

    def __init__(self, cfg):
        super().__init__(cfg)
        self.merc = os.environ.get("MERCURY_BIN", "").strip() or "mercury"
        self.a = self.b = self.adat = self.bdat = None
        self.nm = {}
        self.buf = {}

    # ---- hooks ----
    def preclean_patterns(self):
        return ["mercury .*-p 83", "arecord -D plughw", "aplay -D plughw"]

    def start_stations(self):
        self._launch("2,1", "3,1", self.A_PORT, 8100, "/tmp/mpA.log")   # A answerer
        self._launch("4,0", "5,0", self.B_PORT, 8110, "/tmp/mpB.log")   # B caller/sender

    def _launch(self, tx, rx, port, bcast, log):
        p = sp.Popen([self.merc, "-x", "alsa", "-o", f"plughw:{tx}", "-i", f"plughw:{rx}",
                      "-p", str(port), "-b", str(bcast)],
                     stdout=open(log, "wb"), stderr=sp.STDOUT)
        self._stations.append(p)

    def wait_ready(self, deadline):
        return (self._wait_listen(self.A_PORT, deadline)
                and self._wait_listen(self.B_PORT, deadline))

    def _wait_listen(self, port, deadline):
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                return True
            except OSError:
                time.sleep(0.3)
        return False

    def link_connect(self, deadline):
        time.sleep(1.0)
        self.a = socket.create_connection(("127.0.0.1", self.A_PORT)); self.a.setblocking(False)
        self.b = socket.create_connection(("127.0.0.1", self.B_PORT)); self.b.setblocking(False)
        self.adat = socket.create_connection(("127.0.0.1", self.A_PORT + 1)); self.adat.setblocking(False)
        self.bdat = socket.create_connection(("127.0.0.1", self.B_PORT + 1))
        self.nm = {self.a: "A", self.b: "B"}
        self.buf = {self.a: b"", self.b: b""}
        self._snd(self.a, "MYCALL W1ABC"); self._snd(self.b, "MYCALL W2XYZ")
        self._pump(time.time() + 1)
        self._snd(self.a, "LISTEN ON"); time.sleep(0.5)
        for attempt in range(1, 4):
            self._snd(self.b, "CONNECT W2XYZ W1ABC")
            if self._pump(min(deadline, time.time() + 45),
                          stop=lambda t: t.startswith("CONNECTED")):
                return True
            print(f"  (connect {attempt}/3 failed; retry)", flush=True)
            self._snd(self.b, "ABORT"); time.sleep(3)
            self._snd(self.a, "LISTEN ON"); time.sleep(0.5)
        return False

    def transfer(self, payload, deadline):
        recv = bytearray()
        self.bdat.sendall(payload)
        print(f"sent {len(payload)} B; reading A.data ...", flush=True)
        while len(recv) < len(payload) and time.time() < deadline:
            r, _, _ = select.select([self.adat, self.a, self.b], [], [], 0.5)
            for s in r:
                if s is self.adat:
                    try:
                        d = self.adat.recv(8192)
                        if d:
                            recv += d
                    except OSError:
                        pass
                else:
                    try:
                        d = s.recv(4096)
                    except OSError:
                        continue
                    if not d:
                        continue
                    self.buf[s] += d
                    while b"\r" in self.buf[s]:
                        ln, self.buf[s] = self.buf[s].split(b"\r", 1)
                        self.on_line(self.nm[s], ln.decode(errors="replace").strip())
        return bytes(recv)

    def scan_telemetry(self, station, line):
        m = re.search(r"BITRATE \(\d+\) (\d+) BPS", line)
        if m:
            self.modes.append(int(m.group(1)))
        s = re.search(r"\bSN ([0-9.]+)", line)
        if s:
            self.snrs.append(float(s.group(1)))

    def teardown_stations(self):
        try:
            if self.b is not None:
                self._snd(self.b, "DISCONNECT"); time.sleep(2)
        except OSError:
            pass
        super().teardown_stations()      # SIGTERM the mercury processes
        for pat in ["arecord -D plughw", "aplay -D plughw"]:
            sp.run(["pkill", "-9", "-f", pat], stdout=sp.DEVNULL, stderr=sp.DEVNULL)

    # ---- helpers ----
    def _snd(self, s, c):
        s.sendall((c + "\r").encode())
        print(f"  -> {self.nm[s]}: {c}", flush=True)

    def _pump(self, deadline, stop=None):
        while time.time() < deadline:
            r, _, _ = select.select([self.a, self.b], [], [], 0.3)
            for s in r:
                try:
                    d = s.recv(4096)
                except OSError:
                    continue
                if not d:
                    continue
                self.buf[s] += d
                while b"\r" in self.buf[s]:
                    ln, self.buf[s] = self.buf[s].split(b"\r", 1)
                    t = ln.decode(errors="replace").strip()
                    self.on_line(self.nm[s], t)          # PTT relay + telemetry scan
                    if not t or t in ("PTT ON", "PTT OFF", "IAMALIVE", "BUFFER 0"):
                        continue
                    print(f"  <- {self.nm[s]}: {t}", flush=True)
                    if stop and stop(t):
                        return True
        return False


if __name__ == "__main__":
    import sys
    sys.exit(run_adapter(MercuryAdapter))
