#!/usr/bin/env python3
"""VaraAdapter -- a VARA HF adapter on the ModemAdapter base class.

Drives two VARA HF instances (VARA.exe, typically under Wine) through skywave's shared
half-duplex channel_sim via VARA's TCP command/data ports. A faithful port of a
hand-written VARA TNC driver onto the ModemAdapter contract, in the same shape as
adapters/mercury.py (a TCP TNC that speaks CR-terminated commands).

STATION LIFECYCLE. As of 2026-08-18 this adapter launches and kills VARA per cell, the
same as mercury/ardop/freedata -- see the VARA_LIFECYCLE block below for why the old
persistent behaviour was retired and how to get it back (`SKYW_VARA_LIFECYCLE=persistent`)
for the E5 control arm or interactive debugging. In per-run mode `start_stations` brings
both instances up and records the startup cost; in persistent mode it is a no-op and the
external vara_up.py/vara_down.py pair owns the lifecycle. Either way each instance must end
up listening on its command port (A=8300, B=8310) with the data port at +1 (8301/8311),
wired to the correct audio devices.

The channel IS launched per cell (the shared `channel_sim`, base default) -- only the
modem stations persist. SIGMA/TXGAIN/NP_STATS/SIM_* pass through to channel_sim untouched
(the base + channel_sim read them from the environment).

Callsigns default to generic test calls; a REGISTERED VARA (full-speed) needs its
licensed callsign -- set VARA_ACALL / VARA_BCALL, or edit ACALL/BCALL below.

Channel width is VARA_BW (500 / 2300 / 2750, default 2300). The three widths are separate
physical layers -- each has its own speed-level table, carrier sets and MFSK combs -- so a
sweep cell is only comparable to another at the same width.

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


# ---------------------------------------------------------------------------
# STATION LIFECYCLE (2026-08-18, owner-directed).
#
# Historically VARA.exe was launched ONCE per campaign and persisted across
# every cell, because it is proprietary and slow to start. Every other modem
# (mercury/ardop/freedata/armstrong) gets a FRESH process per run. That
# asymmetry hands VARA two things nobody else gets in a comparative round: no
# cold start, and whatever adaptation state survives a DISCONNECT -- VARA HF
# tracks speed levels and per-link quality, so if any of it persists then its
# reps are not independent, cell ORDER becomes a hidden variable, and the
# scorer's median-over-reps assumes an exchangeability the data may not have.
#
# It also loads the box: two resident VARA.exe plus wineserver drew ~1.3 cores
# continuously on bench5, and mercury measured 17.6 B/s there against 60.7 B/s
# on a VARA-free bench3 running the SAME binary and the SAME skywave pin
# (E1/E3, 2026-08-18).
#
# So per-run is now the default and persistence must earn its place:
#   per-run    (default) launch before each cell, kill after -- symmetric with
#              every other adapter.
#   persistent (legacy)  the external vara_up.py/vara_down.py lifecycle; the
#              control arm for the E5 state-carryover measurement, and an
#              escape hatch for interactive debugging.
# ---------------------------------------------------------------------------
VARA_LIFECYCLE = (os.environ.get("SKYW_VARA_LIFECYCLE", "").strip().lower()
                  or "per-run")
if VARA_LIFECYCLE not in ("per-run", "persistent"):
    raise SystemExit(f"SKYW_VARA_LIFECYCLE={VARA_LIFECYCLE!r}: expected "
                     f"'per-run' or 'persistent'")

# Launch parameters, kept identical to vara_up.py -- the wine loader is PINNED
# to Ubuntu wine-9.0 on purpose (winehq-staging 11.13 repointed /usr/bin/wine
# and VARA HF B wedges under staging: process up, cmd port never opens,
# MEASURED 2026-07-19 2/2). Do not "simplify" this to bare `wine`.
WINE_LOADER = "/usr/lib/wine/wine"
WINE_DLL_OVERRIDES = "mscoree=d;mshtml=d"      # kills the wine-mono/.NET popups
VARA_PREFIXES = [("/home/spinkham/.wine32", 8300),
                 ("/home/spinkham/.wine32B", 8310)]
VARA_START_TIMEOUT_S = 75.0                    # same ceiling as vara_up.py
VARA_START_ATTEMPTS = 2                        # one retry: a cold wine start
                                               # occasionally loses a port race


class VaraAdapter(ModemAdapter):
    name = "vara"
    A_PORT, B_PORT = 8300, 8310          # A = answerer, B = caller; data ports are +1
    ACALL = os.environ.get("VARA_ACALL", "").strip() or "W1ABC"
    BCALL = os.environ.get("VARA_BCALL", "").strip() or "W2XYZ"
    # VARA HF has three channel widths and they are different physical layers, not just
    # different rates: each has its own speed-level table, its own carrier sets and its own
    # MFSK combs.  A cell measured at one width says nothing about another, so the width
    # has to be selectable rather than fixed at 2300.
    BW = os.environ.get("VARA_BW", "").strip() or "2300"
    # A cold VARA start takes real time (vara_up.py polls to 75 s), and in
    # per-run mode wait_ready IS the readiness gate -- so the deadline has to
    # cover the launch, not just a connect to an already-listening port.
    ready_timeout_s = (VARA_START_TIMEOUT_S + 15.0
                       if VARA_LIFECYCLE == "per-run" else 20.0)
    connect_timeout_s = 250.0

    def __init__(self, cfg):
        super().__init__(cfg)
        self.a = self.b = self.adat = self.bdat = None
        self.nm = {}
        self.buf = {}

    # ---- hooks ----
    def preclean_patterns(self):
        # Channel-side helpers always. VARA.exe is matched ONLY in per-run mode:
        # under the legacy persistent lifecycle it must survive between cells,
        # so killing it here would break the very mode it belongs to.
        pats = ["arecord -D plughw", "aplay -D plughw", "noise_pipe"]
        if VARA_LIFECYCLE == "per-run":
            pats.append("VARA.exe")
        return pats

    def start_stations(self):
        """Launch both VARA instances (per-run), or no-op (persistent legacy).

        Symmetric with mercury/ardop/freedata: one cold process per cell, so a
        cell's numbers carry no state from the cell before it.

        ⚠ This method DELIBERATELY does not probe the command ports. VARA
        permits ONE client per port and treats a client disconnect as a session
        event, so a connect-then-close readiness probe followed by
        wait_ready's real open hits the port twice at machine speed and trips a
        reset right after MYCALL -- see wait_ready's comment, and the
        2026-08-18 regression where exactly that probe made station B fail to
        register on every vara cell. Readiness is wait_ready's job: `_connect`
        already retries to a deadline, and the FIRST successful open is the
        one and only open.
        """
        self._start_s = []
        self._launch_t0 = None
        if VARA_LIFECYCLE == "persistent":
            print("  vara lifecycle=persistent: stations assumed already up",
                  flush=True)
            return
        self._relaunch()

    def _relaunch(self):
        self._kill_vara()
        self._launch_t0 = time.time()
        for prefix, port in VARA_PREFIXES:
            self._launch_one(prefix, port)
        print(f"  VARA launched ({len(VARA_PREFIXES)} instances); readiness is "
              f"wait_ready's single open", flush=True)

    def _launch_one(self, prefix, port):
        vdir = os.path.join(prefix, "drive_c/VARA")
        env = dict(os.environ, WINEPREFIX=prefix, WINEARCH="win32",
                   WINEDLLOVERRIDES=WINE_DLL_OVERRIDES,
                   DISPLAY=os.environ.get("DISPLAY", ":0"))
        sp.Popen([WINE_LOADER, "VARA.exe"], cwd=vdir, env=env,
                 stdout=open(f"/tmp/vara_{port}.log", "wb"), stderr=sp.STDOUT,
                 start_new_session=True)

    def _kill_vara(self):
        sp.run(["pkill", "-9", "-f", "VARA.exe"], stdout=sp.DEVNULL,
               stderr=sp.DEVNULL)
        time.sleep(1.5)

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
        for attempt in range(1, VARA_START_ATTEMPTS + 1):
            self.a = self._connect(self.A_PORT, deadline)
            self.b = self._connect(self.B_PORT, deadline)
            if self.a is not None and self.b is not None:
                if self._launch_t0 is not None:
                    self._start_s = [round(time.time() - self._launch_t0, 1)]
                    print(f"  VARA_START ready in {self._start_s[0]}s "
                          f"(attempt {attempt})", flush=True)
                break
            # Retrying means a FULL relaunch, never a re-open against the same
            # process: a half-open pair is exactly the state that wedges VARA.
            for sk in (self.a, self.b):
                if sk is not None:
                    sk.close()
            self.a = self.b = None
            if VARA_LIFECYCLE != "per-run" or attempt == VARA_START_ATTEMPTS:
                return False
            print(f"  VARA not ready (attempt {attempt}); relaunching",
                  flush=True)
            self._relaunch()
            deadline = time.time() + self.ready_timeout_s
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
            for c in (f"MYCALL {call}", "COMPRESSION OFF", f"BW{self.BW}"):
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
            self.progress(len(recv))
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
        # Graceful link teardown first, either way -- a DISCONNECT is how VARA
        # is meant to end a session, and skipping it would change what the next
        # cell sees in persistent mode.
        try:
            if self.b is not None:
                self._snd(self.b, "DISCONNECT"); time.sleep(2)
        except OSError:
            pass
        super().teardown_stations()   # SIGTERMs self._stations -- empty here (no-op)
        pats = ["arecord -D plughw", "aplay -D plughw", "noise_pipe"]
        if VARA_LIFECYCLE == "per-run":
            # Kill the stations too: leaving them resident is what loaded the
            # box AND what let state cross cell boundaries.
            pats.append("VARA.exe")
        for pat in pats:
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
