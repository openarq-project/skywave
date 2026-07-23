#!/usr/bin/env python3
"""ArmstrongAdapter -- an Armstrong adapter on the ModemAdapter base class.

Drives the Armstrong OpenARQ modem through skywave via its VARA-compatible TNC
control protocol (MYCALL / LISTEN / CONNECT, with the data stream on the TNC
port + 1), the connect retries, and teardown, all as the base class's hooks.

By default it runs on the 4-card ALSA loopback rig, the same real-time transport
mercury and ardop use, so all three are measured on one rig. Armstrong presents
48 kHz to ALSA via its built-in resampler (ARM_FORCE_48K), so the plughw cable
carries clean audio without an ALSA resample. Armstrong also has a native socket
audio backend: set SIM_TRANSPORT=sock to run it device-free over skywave's sock
transport instead (deterministic, block-lockstep virtual clock; no aloop rig,
but goodput is then wall-referenced rather than on-air seconds).

Set ARMSTRONG_BIN to a flat-CLI `armstrong-hf` build, then run it as `armstrong`:

  skywave-sweep armstrong spec.json out.csv
"""
import os
import re
import select
import socket
import subprocess as sp
import time

from skywave import bench_pipes
from skywave.modem_adapter import ModemAdapter, run_adapter


class ArmstrongAdapter(ModemAdapter):
    name = "armstrong"
    A_PORT, B_PORT = 8300, 8310          # A = caller/sender, B = answerer/receiver
    # cpal named-PCM endpoints (see armstrong_aloop.conf) on the same 4-card aloop
    # points mercury/ardop use: ARM_TXA=card2/1 ARM_RXA=card3/1 ARM_TXB=card4/0 ARM_RXB=card5/0
    A_TX, A_RX = "ARM_TXA", "ARM_RXA"
    B_TX, B_RX = "ARM_TXB", "ARM_RXB"
    ready_timeout_s = 25.0
    connect_timeout_s = 200.0

    def __init__(self, cfg):
        super().__init__(cfg)
        self.arm = os.environ.get("ARMSTRONG_BIN", "").strip() or "armstrong-hf"
        self.sock = os.environ.get("SIM_TRANSPORT", "").strip() == "sock"   # opt-in device-free path
        # Post-CONNECT settle before data. In virt_time the modem's FSM clock races wall
        # time on a fast host, so a long WALL-clock idle here can burn past the ARQ
        # keepalive-loss budget (arq KEEPALIVE 30s x3) and drop the link before any data
        # flows -- a 2 s settle disconnected reproducibly on an M5 Mac (~9x faster virt
        # stepping than the Linux benches, where 2 s was safe). Keep it brief in virt_time;
        # the real-time/ALSA rig keeps the full 2 s so the rate controller settles before
        # the first burst. Tunable via ARM_SETTLE_S for an unusually slow virt_time host.
        self._virt = (self.sock and
                      os.environ.get("SIM_CLOCK", "virt_time").strip() == "virt_time")
        self.settle_s = float(os.environ.get("ARM_SETTLE_S", "0.3" if self._virt else "2.0"))
        self.sockdir = (os.environ.get("SIM_SOCK_DIR", "").strip()
                        or f"/tmp/skywave-armsock-{os.getpid()}")
        if self.sock:
            os.makedirs(self.sockdir, exist_ok=True)
        self.a = self.b = self.adat = self.bdat = None
        self.nm = {}
        self.buf = {}
        self._no_web = None

    def bench_time(self):
        """SIGNAL time from the sim's status file (<sockdir>/virt_now_ms) on the
        sock/virt_time rig -- the same contract as mercury_sock, so reported
        seconds/goodput are real-time equivalent at any virtual pace. Without
        this override the virtval-2026-07-23 campaign reported wall clock,
        inflating armstrong's virtual goodput +52/+61%. The sim writes the file
        every ~500 ms of signal time from block 0; wall clock is only a startup
        fallback. The ALSA rig (and sock under SIM_CLOCK=real_time) stays wall."""
        if not self._virt:
            return time.time()
        try:
            with open(os.path.join(self.sockdir, "virt_now_ms")) as f:
                return int(f.read()) / 1000.0
        except (OSError, ValueError):
            return time.time()

    # ---- channel: default ALSA aloop rig; sock transport is opt-in ----
    def launch_channel(self):
        if self.sock:
            # Armstrong's sock audio backend runs on a block-lockstep virtual clock, so the
            # sim must be the matching virtual-time master; a real_time-paced sim stalls the
            # handshake. The run is deterministic; goodput here is wall-referenced.
            self._sim = bench_pipes.launch_channel_sim(extra_env={
                "SIM_TRANSPORT": "sock",
                "SIM_CLOCK": os.environ.get("SIM_CLOCK", "virt_time"),
                "SIM_SOCK_DIR": self.sockdir,
            })
        else:
            self._sim = bench_pipes.launch_channel_sim()      # the 4-card ALSA aloop rig

    # ---- hooks ----
    def preclean_patterns(self):
        # Scope kills to the flag so they can never match this Python adapter's own cmdline.
        if self.sock:
            return ["armstrong-hf .*--audio sock"]
        return ["armstrong-hf .*--audio cpal", "arecord -D plughw", "aplay -D plughw"]

    def _no_web_flag(self):
        # Newer armstrong builds start an operator web API on a fixed port by default, so
        # the second station of a pair loses the bind race and dies. Suppress it where
        # supported; probe via --help since older builds reject unknown flags.
        if self._no_web is None:
            try:
                h = sp.run([self.arm, "--help"], capture_output=True, timeout=15)
                self._no_web = ["--no-web"] if b"--no-web" in h.stdout + h.stderr else []
            except Exception:
                self._no_web = []
        return self._no_web

    def start_stations(self):
        if self.sock:
            self._launch_sock("W1CAL", self.A_PORT, "a", "/tmp/armA.log")   # A caller/sender
            self._launch_sock("W1ANS", self.B_PORT, "b", "/tmp/armB.log")   # B answerer/receiver
        else:
            self._launch_alsa("W1CAL", self.A_PORT, self.A_TX, self.A_RX, "/tmp/armA.log")
            self._launch_alsa("W1ANS", self.B_PORT, self.B_TX, self.B_RX, "/tmp/armB.log")

    def _launch_alsa(self, call, port, tx, rx, log):
        conf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "armstrong_aloop.conf")
        env = dict(os.environ, RUST_LOG="info", ARM_FORCE_48K="1", ALSA_CONFIG_PATH=conf)
        p = sp.Popen([self.arm, "--audio", "cpal", "--tx-device", tx, "--rx-device", rx,
                      "--callsign", call, "--tnc-port", str(port),
                      "--host-sock", f"/tmp/armp_{port}.sock"] + self._no_web_flag(),
                     env=env, stdout=open(log, "wb"), stderr=sp.STDOUT)
        self._stations.append(p)

    def _launch_sock(self, call, port, station, log):
        env = dict(os.environ, RUST_LOG="info",
                   ARM_AUDIO_SOCK=os.path.join(self.sockdir, f"{station}.sock"))
        p = sp.Popen([self.arm, "--audio", "sock", "--callsign", call,
                      "--tnc-port", str(port),
                      "--host-sock", f"/tmp/armp_{port}.sock"] + self._no_web_flag(),
                     env=env, stdout=open(log, "wb"), stderr=sp.STDOUT)
        self._stations.append(p)

    def wait_ready(self, deadline):
        return (self._wait_listen(self.A_PORT, deadline)
                and self._wait_listen(self.B_PORT, deadline))

    def _wait_listen(self, port, deadline):
        while time.time() < deadline:
            if any(p.poll() is not None for p in self._stations):
                return False          # a station died at startup (port collision): fail loudly
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                return True
            except OSError:
                time.sleep(0.3)
        return False

    def link_connect(self, deadline):
        self.a = socket.create_connection(("127.0.0.1", self.A_PORT)); self.a.setblocking(False)
        self.b = socket.create_connection(("127.0.0.1", self.B_PORT)); self.b.setblocking(False)
        self.nm = {self.a: "A", self.b: "B"}
        self.buf = {self.a: b"", self.b: b""}
        self._snd(self.a, "MYCALL W1CAL"); self._snd(self.b, "MYCALL W1ANS")
        self._pump(time.time() + 1)
        self._snd(self.b, "LISTEN ON"); time.sleep(0.5)
        for attempt in range(1, 4):
            self._snd(self.a, "CONNECT W1CAL W1ANS")
            if self._pump(min(deadline, time.time() + 60),
                          stop=lambda t: t.startswith("CONNECTED")):
                # let the rate controller settle before data, but keep pumping so PTT
                # and telemetry keep flowing (a hard sleep squelches the first burst).
                # settle_s is short in virt_time so it can't race past keepalive-loss.
                self._pump(time.time() + self.settle_s)
                self.adat = socket.create_connection(("127.0.0.1", self.A_PORT + 1))       # A sender
                self.bdat = socket.create_connection(("127.0.0.1", self.B_PORT + 1)); self.bdat.setblocking(False)
                return True
            print(f"  (connect {attempt}/3 failed; retry)", flush=True)
            self._snd(self.a, "ABORT"); time.sleep(3)
            self._snd(self.b, "LISTEN ON"); time.sleep(0.5)
        return False

    def transfer(self, payload, deadline):
        recv = bytearray()
        self.adat.sendall(payload)
        print(f"sent {len(payload)} B; reading B.data ...", flush=True)
        while len(recv) < len(payload) and time.time() < deadline:
            r, _, _ = select.select([self.bdat, self.a, self.b], [], [], 0.5)
            for s in r:
                if s is self.bdat:
                    try:
                        d = self.bdat.recv(8192)
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
            if self.a is not None:
                self._snd(self.a, "DISCONNECT"); time.sleep(2)
        except OSError:
            pass
        super().teardown_stations()      # SIGTERM the armstrong processes
        if not self.sock:
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
    sys.exit(run_adapter(ArmstrongAdapter))
