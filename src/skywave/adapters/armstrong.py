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
import shlex
import shutil
import re
import select
import socket
import subprocess as sp
import time

from skywave import bench_pipes
from skywave.modem_adapter import ModemAdapter, run_adapter
from skywave.modem_provenance import gate_from_env


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
        # Identify (and, if pinned, enforce) WHICH build this is, before anything is
        # launched -- see skywave.modem_provenance. A bare name is resolved on PATH
        # so the record names the file that will actually be exec'd.
        _t = self.arm
        if os.sep not in _t:
            _t = shutil.which(_t) or _t
        self.provenance = gate_from_env("armstrong", _t)
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
        # PTT isolation (2026-07-23 incident): every launch passes --config
        # into this skywave-owned dir so armstrong can NEVER fall back to the
        # box's platform operator config (~/.config/armstrong, ~/Library/
        # Application Support/...). A "virtual" sock smoke inherited exactly
        # that config once -- active profile ptt.method="rigctld" wired to a
        # live rigctld -- and keyed a real HF rig over the air: sock audio
        # replaces only the AUDIO transport, PTT still comes from the config.
        # A fresh file here is inert by construction (armstrong writes
        # commented defaults with ptt method "none" on first run); see
        # tests/test_ptt_isolation.py for the regression pins.
        self.cfgdir = self.sockdir if self.sock else f"/tmp/skywave-armcfg-{os.getpid()}"
        os.makedirs(self.cfgdir, exist_ok=True)
        # Data direction: "ab" (default) A calls AND A sends; "ba" A still calls
        # (the CONNECT handshake is unchanged) but B (the answerer) sends the
        # payload and A receives it. See link_connect() for where this is applied.
        self.direction = os.environ.get("SKYW_DIRECTION", "ab").strip() or "ab"
        if self.direction not in ("ab", "ba"):
            raise RuntimeError(f"SKYW_DIRECTION must be ab or ba (got {self.direction!r})")
        # Connect-then-fade cells: hold the data phase off until SIM_ATTEN_SCHEDULE's
        # first step has landed (+ this many extra seconds), so the whole transfer
        # runs at the held (post-step) attenuation instead of starting at the flat
        # pre-step value. Unset/empty = off; see _data_hold().
        _hold = os.environ.get("SKYW_DATA_HOLD_AFTER_STEP_S", "").strip()
        if _hold:
            try:
                self.data_hold_s = float(_hold)
            except ValueError:
                raise RuntimeError(
                    f"SKYW_DATA_HOLD_AFTER_STEP_S must be a float (got {_hold!r})")
            if self.data_hold_s < 0:
                raise RuntimeError(
                    f"SKYW_DATA_HOLD_AFTER_STEP_S must be >= 0 (got {self.data_hold_s})")
        else:
            self.data_hold_s = None
        self._chan_t0 = None    # wall-clock anchor for the hold on a real-time rig; see launch_channel()
        self.a = self.b = self.adat = self.bdat = None
        self.nm = {}
        self.buf = {}
        self._no_web = None
        self._host_sock = None

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
        # Anchor for SKYW_DATA_HOLD_AFTER_STEP_S on a real-time (non-virt) rig, where
        # bench_time() is plain wall clock and so can't be diffed against 0 the way the
        # sim's own signal clock can -- see _data_hold(). An approximation of the sim's
        # audio clock (channel launch != first sample), good enough for a hold bound.
        self._chan_t0 = time.time()
        if self.sock:
            # Armstrong's sock audio backend runs on a block-lockstep virtual clock, so the
            # sim must be the matching virtual-time master; a real_time-paced sim stalls the
            # handshake. The run is deterministic; goodput here is wall-referenced.
            self._sim = bench_pipes.launch_channel_sim(extra_env={
                "SIM_TRANSPORT": "sock",
                "SIM_CLOCK": os.environ.get("SIM_CLOCK", "virt_time"),
                "SIM_SOCK_DIR": self.sockdir,
                # Armstrong's protocol timers run on WALL clock under sock
                # audio (armstrong TODO P3), so an uncapped virtual clock on a
                # fast host races ahead of the wall-paced handshake and the
                # SIM_MAX_VIRTUAL_S budget expires before the first burst is
                # keyed (observed on an M5: act_rms=0 for 60 virtual s, every
                # connect dead). Cap the pace like mercury_sock does; 3 is
                # confirmed sufficient on the fastest box. Export overrides.
                "SIM_VIRT_MAX_RATIO": os.environ.get("SIM_VIRT_MAX_RATIO", "3"),
            })
        else:
            self._sim = bench_pipes.launch_channel_sim()      # the 4-card ALSA aloop rig

    # ---- hooks ----
    def preclean_patterns(self):
        # Scope kills to the flag so they can never match this Python adapter's own cmdline.
        if self.sock:
            return ["armstrong-(hf|fm) .*--audio sock"]
        return ["armstrong-(hf|fm) .*--audio cpal", "arecord -D plughw", "aplay -D plughw"]

    def _extra_args(self, station):
        """Extra CLI args for a station: ARMSTRONG_ARGS (both) then
        ARMSTRONG_ARGS_A / ARMSTRONG_ARGS_B (per station), shell-split. The
        `--fm-*` profile flags of `armstrong-fm` reach the DUT this way (FM
        cell B2.1). A session-wide flag that MUST agree on both ends
        (`--fm-airtime-ms`) is rejected here when the per-station lists
        disagree: a mismatched pair cannot connect and the row would record
        a silent failed connect instead of a launch error."""
        common = shlex.split(os.environ.get("ARMSTRONG_ARGS", ""))
        per = shlex.split(os.environ.get(f"ARMSTRONG_ARGS_{station.upper()}", ""))
        return common + per

    @staticmethod
    def _flag_value(args, flag):
        # The LAST occurrence: a per-station list follows the common one.
        idx = [i for i, a in enumerate(args) if a == flag]
        return args[idx[-1] + 1] if idx and idx[-1] + 1 < len(args) else None

    def _check_session_flags(self):
        a, b = self._extra_args("a"), self._extra_args("b")
        for flag in ("--fm-airtime-ms",):
            va, vb = self._flag_value(a, flag), self._flag_value(b, flag)
            if va != vb:
                raise RuntimeError(f"{flag} must match on both stations "
                                   f"(A={va!r}, B={vb!r}) — a session-wide value")

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

    @staticmethod
    def station_log(station, env=None):
        """Where a station's own process log (stdout+stderr, RUST_LOG=info)
        goes for THIS cell. Under sweep_runner every cell exports NP_STATS
        as `<logs dir>/<cell base>.npstats`, so the station logs land beside
        the cell's other artifacts as `<cell base>.armA.log` / `.armB.log`
        and survive the harvest (armstrong's always-on `session summary`
        line lives there). Before 2026-09-02 they went to fixed /tmp files
        that every cell overwrote — a campaign kept only its LAST cell's
        story. Outside a sweep (no NP_STATS) the /tmp fallback stands."""
        env = os.environ if env is None else env
        stats = (env.get("NP_STATS") or "").strip()
        if not stats:
            return f"/tmp/arm{station}.log"
        base = stats[:-len(".npstats")] if stats.endswith(".npstats") else stats
        return f"{base}.arm{station}.log"

    def start_stations(self):
        self._check_session_flags()
        log_a, log_b = self.station_log("A"), self.station_log("B")
        if self.sock:
            self._launch_sock("W1CAL", self.A_PORT, "a", log_a)   # A caller/sender
            self._launch_sock("W1ANS", self.B_PORT, "b", log_b)   # B answerer/receiver
        else:
            self._launch_alsa("W1CAL", self.A_PORT, self.A_TX, self.A_RX, log_a)
            self._launch_alsa("W1ANS", self.B_PORT, self.B_TX, self.B_RX, log_b)

    def _host_sock_flags(self, port):
        # Older armstrong builds (pre-e4e158d) bind a host-API unix socket at a
        # FIXED default path (/tmp/armstrong.sock), so the pair's second station
        # dies on the bind race without a per-station path. Newer builds retired
        # the flag (the host plane rides the web API, which --no-web already
        # disables) and reject unknown argv at parse. Probe via --help, same as
        # --no-web, so one skywave drives both generations.
        if self._host_sock is None:
            try:
                h = sp.run([self.arm, "--help"], capture_output=True, timeout=15)
                self._host_sock = b"--host-sock" in h.stdout + h.stderr
            except Exception:
                self._host_sock = False
        return ["--host-sock", f"/tmp/armp_{port}.sock"] if self._host_sock else []

    def _bench_config(self, port):
        """Per-station --config path in skywave's own dir, never the platform
        default (the PTT-isolation invariant -- see __init__)."""
        return os.path.join(self.cfgdir, f"armstrong_bench_{port}.toml")

    def _launch_alsa(self, call, port, tx, rx, log):
        conf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "armstrong_aloop.conf")
        env = dict(os.environ, RUST_LOG="info", ARM_FORCE_48K="1", ALSA_CONFIG_PATH=conf)
        station = "a" if port == self.A_PORT else "b"
        p = sp.Popen([self.arm, "--config", self._bench_config(port),
                      "--audio", "cpal", "--tx-device", tx, "--rx-device", rx,
                      "--callsign", call, "--tnc-port", str(port)]
                     + self._host_sock_flags(port) + self._no_web_flag()
                     + self._extra_args(station),
                     env=env, stdout=open(log, "wb"), stderr=sp.STDOUT)
        self._stations.append(p)

    def _launch_sock(self, call, port, station, log):
        env = dict(os.environ, RUST_LOG="info",
                   ARM_AUDIO_SOCK=os.path.join(self.sockdir, f"{station}.sock"))
        p = sp.Popen([self.arm, "--config", self._bench_config(port),
                      "--audio", "sock", "--callsign", call,
                      "--tnc-port", str(port)]
                     + self._host_sock_flags(port) + self._no_web_flag()
                     + self._extra_args(station),
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
            # A station can die mid-attempt (observed 2026-07-24 on the Mac
            # sock rig: the sim's VIRTUAL-TIMEOUT closed the cable and the
            # stations exited, leaving this control socket broken). Retrying
            # on a dead socket cannot recover -- fail the CELL cleanly instead
            # of killing the whole sweep with an uncaught BrokenPipeError.
            try:
                self._snd(self.a, "CONNECT W1CAL W1ANS")
                if self._pump(min(deadline, time.time() + 60),
                              stop=lambda t: t.startswith("CONNECTED")):
                    # let the rate controller settle before data, but keep pumping so PTT
                    # and telemetry keep flowing (a hard sleep squelches the first burst).
                    # settle_s is short in virt_time so it can't race past keepalive-loss.
                    self._pump(time.time() + self.settle_s)
                    self._data_hold()
                    if self.direction == "ba":
                        self.adat = socket.create_connection(("127.0.0.1", self.B_PORT + 1))     # B (answerer) sender
                        self.bdat = socket.create_connection(("127.0.0.1", self.A_PORT + 1)); self.bdat.setblocking(False)
                        print("DIRECTION ba (answerer sends)", flush=True)
                    else:
                        self.adat = socket.create_connection(("127.0.0.1", self.A_PORT + 1))     # A (caller) sender
                        self.bdat = socket.create_connection(("127.0.0.1", self.B_PORT + 1)); self.bdat.setblocking(False)
                        print("DIRECTION ab (caller sends)", flush=True)
                    return True
                print(f"  (connect {attempt}/3 failed; retry)", flush=True)
                self._snd(self.a, "ABORT"); time.sleep(3)
                self._snd(self.b, "LISTEN ON"); time.sleep(0.5)
            except OSError as e:
                print(f"  (connect {attempt}/3: control socket died: {e})", flush=True)
                return False
        return False

    def transfer(self, payload, deadline):
        recv = bytearray()
        self.adat.sendall(payload)
        print(f"sent {len(payload)} B; reading B.data ...", flush=True)
        while len(recv) < len(payload) and time.time() < deadline:
            self.progress(len(recv))
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
        # armstrong's wire format is `BITRATE {tx} {rx}` -- two bare integers
        # (applied TX rate, armed RX rate; link_telemetry.rs:123/138), NOT the
        # VARA/mercury `BITRATE (n) NNN BPS` shape. The old regex demanded parens
        # + a BPS suffix and never matched, so self.modes stayed empty and
        # peak_bitrate() read 0 for EVERY armstrong row (2026-08-22). Take group 1
        # = the applied TX rate (the mode armstrong is transmitting at).
        m = re.search(r"BITRATE (\d+) (\d+)", line)
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
    @staticmethod
    def _atten_schedule_first_step_secs():
        """The duration of SIM_ATTEN_SCHEDULE's FIRST segment ("<db>:<secs>,..."),
        parsed locally so this module never imports skywave.channel_sim (it reads
        the environment at import time and can sys.exit on a malformed value --
        channel_sim itself, run as the channel-sim subprocess, is the real
        validator). Returns None when there's no schedule (or it can't be parsed
        here); a segment with no ":" or an empty seconds part is secs=0 (matches
        parse_atten_schedule's own default)."""
        text = os.environ.get("SIM_ATTEN_SCHEDULE", "").strip()
        if not text:
            return None
        tok = text.split(",", 1)[0]
        db, _, secs = tok.strip().partition(":")
        if not db.strip():
            return None
        try:
            return float(secs) if secs.strip() else 0.0
        except ValueError:
            return None

    def _data_hold(self):
        """Connect-then-fade support (SKYW_DATA_HOLD_AFTER_STEP_S): if a
        SIM_ATTEN_SCHEDULE is in force and its first segment steps at secs0 > 0,
        keep pumping the control sockets (never a hard sleep -- PTT/telemetry must
        keep flowing) until bench time reaches secs0 + self.data_hold_s, so the
        whole data phase runs at the held (post-step) attenuation. No-op when the
        knob is unset, there's no schedule, or the schedule holds from t=0."""
        if self.data_hold_s is None:
            return
        secs0 = self._atten_schedule_first_step_secs()
        if not secs0:
            return
        target = secs0 + self.data_hold_s
        print(f"DATA_HOLD until bench t={target:.1f} "
              f"(step at {secs0:.1f} + {self.data_hold_s:.1f})", flush=True)
        wall_deadline = time.time() + 4 * target
        while True:
            # virt sock rig: bench_time() IS the sim's signal clock the schedule steps
            # on. Real-time rig (ALSA, or sock+SIM_CLOCK=real_time): bench_time() is
            # plain wall clock, so diff it against the channel-launch anchor instead --
            # an approximation of the sim's audio clock (see launch_channel()).
            t = self.bench_time() if self._virt else (time.time() - (self._chan_t0 or time.time()))
            if t >= target:
                print(f"DATA_HOLD released at bench t={t:.1f}", flush=True)
                return
            if time.time() >= wall_deadline:
                print("DATA_HOLD timeout", flush=True)
                return
            slice_end = min(time.time() + 1.0, wall_deadline)
            alive = self._pump(slice_end)
            if not alive and time.time() < slice_end - 0.05:
                # _pump returned early (not just at its deadline): the dead-socket
                # path (control-socket EOF) already logged its own line.
                print("DATA_HOLD timeout", flush=True)
                return

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
                    # recv EOF: the station closed its control socket (it
                    # exited or crashed). Pumping on would spin silently until
                    # the wall deadline -- observed masking a station death
                    # for a full 60 s. End the pump; the caller treats it as
                    # a failed wait.
                    print(f"  ({self.nm[s]}: control socket EOF)", flush=True)
                    return False
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
