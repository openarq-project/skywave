#!/usr/bin/env python3
"""ArdopAdapter -- an ARDOP (ardopcf) adapter on the ModemAdapter base class.

Drives two ardopcf instances through skywave over the 4-card ALSA loopback rig,
via ardopcf's native host TCP interface (a command port plus a data port at
cmd + 1). ARQ connect is an ARQCALL from the caller; the receiver reads
length-prefixed data frames (a 2-byte big-endian length, an optional 3-byte
ARQ/FEC/ERR tag, then the payload bytes). ardopcf runs at 12 kHz, so plughw
resamples to the 8 kHz cable.

Set ARDOP_BIN to the ardopcf binary, then run it as the `ardop` modem:

  python3 sweep_runner.py ardop spec.json out.csv
"""
import os
import select
import socket
import struct
import subprocess as sp
import time

from modem_adapter import ModemAdapter, run_adapter


class ArdopAdapter(ModemAdapter):
    name = "ardop"
    A_CMD, A_DAT = 8515, 8516            # A = answerer/receiver
    B_CMD, B_DAT = 8517, 8518            # B = caller/sender
    ACALL, BCALL = "W1ARD", "W2ARD"
    ready_timeout_s = 20.0
    connect_timeout_s = 150.0

    def __init__(self, cfg):
        super().__init__(cfg)
        self.ardop = os.environ.get("ARDOP_BIN", "").strip() or "ardopcf"
        self.a = self.b = self.adat = self.bdat = None
        self.nm = {}
        self.buf = {}

    # ---- hooks ----
    def preclean_patterns(self):
        return ["ardopcf ", "arecord -D plughw", "aplay -D plughw"]

    def start_stations(self):
        self._launch(self.A_CMD, "3,1", "2,1", self.ACALL, listen=True)    # A answerer
        self._launch(self.B_CMD, "5,0", "4,0", self.BCALL, listen=False)   # B caller/sender

    def _launch(self, cmd_port, capture, playback, mycall, listen):
        hc = ("MYCALL {};PROTOCOLMODE ARQ;ARQBW 2000MAX".format(mycall)
              + (";LISTEN TRUE" if listen else ""))
        p = sp.Popen([self.ardop, str(cmd_port), f"plughw:{capture}", f"plughw:{playback}",
                      "-H", hc, "--nologfile"],
                     stdout=open(f"/tmp/ardop_{cmd_port}.log", "wb"), stderr=sp.STDOUT)
        self._stations.append(p)

    def wait_ready(self, deadline):
        return (self._wait_listen(self.A_CMD, deadline)
                and self._wait_listen(self.B_CMD, deadline))

    def _wait_listen(self, port, deadline):
        while time.time() < deadline:
            if any(p.poll() is not None for p in self._stations):
                return False          # a station died at startup: fail loudly
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                return True
            except OSError:
                time.sleep(0.3)
        return False

    def link_connect(self, deadline):
        time.sleep(2.0)
        self.a = socket.create_connection(("127.0.0.1", self.A_CMD)); self.a.setblocking(False)
        self.b = socket.create_connection(("127.0.0.1", self.B_CMD)); self.b.setblocking(False)
        self.adat = socket.create_connection(("127.0.0.1", self.A_DAT)); self.adat.setblocking(False)
        self.bdat = socket.create_connection(("127.0.0.1", self.B_DAT))
        self.nm = {self.a: "A", self.b: "B"}
        self.buf = {self.a: b"", self.b: b""}
        for attempt in range(1, 4):
            self._snd(self.b, f"ARQCALL {self.ACALL} 5")
            if self._pump(min(deadline, time.time() + 45),
                          stop=lambda t: "NEWSTATE ISS" in t or "NEWSTATE IRS" in t
                          or t.startswith("CONNECTED")):
                self._pump(time.time() + 1.0)      # settle; keep pumping so PTT relays
                return True
            print(f"  (ARQCALL {attempt}/3 no connect; retry)", flush=True)
            self._snd(self.b, "DISCONNECT"); time.sleep(3)
        return False

    def transfer(self, payload, deadline):
        # send the whole payload as one length-prefixed frame on B's data port
        self.bdat.sendall(struct.pack(">H", len(payload)) + payload)
        print(f"sent {len(payload)} B on B.data; reading A.data ...", flush=True)
        recv = bytearray()
        rbuf = b""
        while len(recv) < len(payload) and time.time() < deadline:
            r, _, _ = select.select([self.adat, self.a, self.b], [], [], 0.5)
            for s in r:
                if s is self.adat:
                    try:
                        d = self.adat.recv(8192)
                        if d:
                            rbuf += d
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
            # parse <2-byte len>[3-byte ARQ/FEC/ERR tag]<data> frames off the data stream
            while len(rbuf) >= 2:
                flen = struct.unpack(">H", rbuf[:2])[0]
                if len(rbuf) < 2 + flen:
                    break
                frame = rbuf[2:2 + flen]
                rbuf = rbuf[2 + flen:]
                recv += frame[3:] if frame[:3] in (b"ARQ", b"FEC", b"ERR") else frame
        return bytes(recv)

    def teardown_stations(self):
        try:
            if self.b is not None:
                self._snd(self.b, "DISCONNECT"); time.sleep(2)
        except OSError:
            pass
        super().teardown_stations()      # SIGTERM the ardopcf processes
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
                    self.on_line(self.nm[s], t)          # PTT relay (ardop: PTT TRUE/FALSE)
                    if not t or t.startswith("BUFFER"):
                        continue
                    print(f"  <- {self.nm[s]}: {t}", flush=True)
                    if stop and stop(t):
                        return True
        return False


if __name__ == "__main__":
    import sys
    sys.exit(run_adapter(ArdopAdapter))
