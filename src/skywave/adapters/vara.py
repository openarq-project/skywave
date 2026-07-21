#!/usr/bin/env python3
"""VaraAdapter -- a VARA HF adapter on the ModemAdapter base class.

Drives two VARA HF instances (VARA.exe, typically under Wine) through skywave's shared
half-duplex channel_sim via VARA's TCP command/data ports. A faithful port of a
hand-written VARA TNC driver onto the ModemAdapter contract, in the same shape as
adapters/mercury.py (a TCP TNC that speaks CR-terminated commands).

EXTERNAL STATION LIFECYCLE (the one way this differs from mercury/ardop). This adapter
does NOT launch the modem processes. VARA is proprietary and slow to start, so the two
VARA.exe instances are brought up ONCE per campaign by an
external up/down step and PERSIST across cells; each must already be listening on its
command port (A=8300, B=8310) with the data port at +1 (8301/8311), wired to the correct
audio devices. Therefore:

  * `start_stations` is a no-op -- the instances are already running.
  * `wait_ready` polls the already-listening command ports.
  * `teardown_stations` DISCONNECTs the link but does NOT kill VARA.exe (it must survive
    to the next cell).

A campaign runner that wants VARA to persist across a sweep brings the instances up before
the first cell and down after the last (a `vara_up()`/`vara_down()` hook that also excludes
`VARA.exe` from the between-cell kill patterns). Wiring such a hook into skywave's
sweep_runner is a separate follow-up; this adapter owns only the per-cell connect /
transfer / disconnect.

The channel IS launched per cell (the shared `channel_sim`, base default) -- only the
modem stations persist. SIGMA/TXGAIN/NP_STATS/SIM_* pass through to channel_sim untouched
(the base + channel_sim read them from the environment).

Callsigns default to generic test calls; a REGISTERED VARA (full-speed) needs its
licensed callsign -- set VARA_ACALL / VARA_BCALL, or edit ACALL/BCALL below.

Run (with two VARA.exe instances already up on 8300/8310):
  skywave-sweep vara spec.json out.csv
"""
import os
import re
import select
import socket
import subprocess as sp
import time

from skywave.modem_adapter import ModemAdapter, run_adapter


class VaraAdapter(ModemAdapter):
    name = "vara"
    A_PORT, B_PORT = 8300, 8310          # A = answerer, B = caller; data ports are +1
    ACALL = os.environ.get("VARA_ACALL", "").strip() or "W1ABC"
    BCALL = os.environ.get("VARA_BCALL", "").strip() or "W2XYZ"
    ready_timeout_s = 20.0
    connect_timeout_s = 250.0

    def __init__(self, cfg):
        super().__init__(cfg)
        self.a = self.b = self.adat = self.bdat = None
        self.nm = {}
        self.buf = {}

    # ---- hooks ----
    def preclean_patterns(self):
        # Channel-side helpers only; VARA.exe is deliberately NOT matched (it persists,
        # managed by the external lifecycle). None of these match this adapter's cmdline.
        return ["arecord -D plughw", "aplay -D plughw", "noise_pipe"]

    def start_stations(self):
        # No-op: the two VARA.exe instances are launched by the external up/down
        # lifecycle and persist across cells. See the module docstring.
        pass

    def wait_ready(self, deadline):
        # VARA permits ONE client per command port and treats a client disconnect as a
        # session event. The base contract splits "is it listening?" (wait_ready) from
        # "open the link" (link_connect); a connect-CLOSE probe here followed by a
        # re-open in link_connect therefore hits VARA's command port TWICE at machine
        # speed, and the second open races VARA's teardown of the first -- tripping a
        # reset on the real link right after MYCALL (deterministic, box-independent).
        # The proven original driver opens each command socket EXACTLY ONCE. Match that:
        # establish the persistent A/B command sockets HERE and let link_connect reuse
        # them (no probe, no re-open).
        self.a = self._connect(self.A_PORT, deadline)
        self.b = self._connect(self.B_PORT, deadline)
        if self.a is None or self.b is None:
            return False
        self.a.setblocking(False); self.b.setblocking(False)
        self.nm = {self.a: "A", self.b: "B"}
        self.buf = {self.a: b"", self.b: b""}
        return True

    def _connect(self, port, deadline):
        while time.time() < deadline:
            try:
                return socket.create_connection(("127.0.0.1", port), timeout=1)
            except OSError:
                time.sleep(0.3)
        return None

    def link_connect(self, deadline):
        # Command sockets (self.a/self.b) were opened ONCE in wait_ready; reuse them.
        for s, call in ((self.a, self.ACALL), (self.b, self.BCALL)):
            for c in (f"MYCALL {call}", "COMPRESSION OFF", "BW2300"):
                self._snd(s, c); time.sleep(0.2)
        self._pump(time.time() + 1.5)
        self._snd(self.a, "LISTEN ON"); time.sleep(0.7)
        # 3 attempts like every other adapter (a single attempt is a connect-rate
        # handicap); inter-attempt waits PUMP so the PTT relay never stalls.
        for attempt in range(1, 4):
            self._snd(self.b, f"CONNECT {self.BCALL} {self.ACALL}")
            if self._pump(min(deadline, time.time() + 75),
                          stop=lambda t: t.startswith("CONNECTED")):
                return True
            print(f"  (connect {attempt}/3 failed; retry)", flush=True)
            self._snd(self.b, "ABORT"); self._pump(time.time() + 3)
            self._snd(self.a, "LISTEN ON"); self._pump(time.time() + 0.7)
        return False

    def transfer(self, payload, deadline):
        self.adat = socket.create_connection(("127.0.0.1", self.A_PORT + 1)); self.adat.setblocking(False)
        self.bdat = socket.create_connection(("127.0.0.1", self.B_PORT + 1))
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
        m = re.search(r"BITRATE \(\d+\)\s+(\d+) bps", line)
        if m:
            self.modes.append(int(m.group(1)))
        s = re.search(r"\bSN ([0-9.]+)", line)
        if s:
            self.snrs.append(float(s.group(1)))

    def teardown_stations(self):
        # Graceful link teardown, but leave VARA.exe running (it persists across cells).
        try:
            if self.b is not None:
                self._snd(self.b, "DISCONNECT"); time.sleep(2)
        except OSError:
            pass
        super().teardown_stations()   # SIGTERMs self._stations -- empty here (no-op)
        for pat in ["arecord -D plughw", "aplay -D plughw", "noise_pipe"]:
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
                    if not t or t in ("PTT ON", "PTT OFF", "IAMALIVE"):
                        continue
                    print(f"  <- {self.nm[s]}: {t}", flush=True)
                    if stop and stop(t):
                        return True
        return False


if __name__ == "__main__":
    import sys
    sys.exit(run_adapter(VaraAdapter))
