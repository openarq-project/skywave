#!/usr/bin/env python3
"""ModemAdapter -- the documented Device-Under-Test contract for the channel_sim
harness.

WHY. The harness already drives several modems (vara, mercury, freedata, ardop, and
others) through the shared half-duplex channel sim, one hand-written `*_arq_pipe.py`
per modem. Those adapters share a lifecycle but were copy-pasted, and their only output
contract is a `RESULT`-line format that sweep_runner scrapes with regexes. This module
turns that IMPLICIT, copy-pasted contract into an EXPLICIT one: a base class that owns the
shared lifecycle (channel launch, PTT relay, payload, timing, result emission, teardown)
and a small set of modem-specific hooks a new project fills in. Write ONE adapter -> get
the whole channel + transport + campaign + scoring stack.

TIGHT INTEGRATION PRESERVED. The base emits the EXACT `RESULT: ...` line the existing
sweep_runner already parses (RES_BYTES/RES_IN/RES_INTACT/RES_GP/RES_PEAK/RES_SN), so an
adapter written on this base is a drop-in for the current framework -- AND it additionally
emits a structured `RESULT_JSON {...}` line as the forward contract. The existing
hand-written adapters are unchanged; this is an additive, opt-in base they can migrate
onto incrementally.

THE CONTRACT (see MODEM-ADAPTER-CONTRACT.md for the full spec):
  INPUT   argv: [payload_bytes] [timeout_s];  env: SIGMA TXGAIN SEED NP_STATS
          SIM_HALF_DUPLEX SIM_PTT SIM_WATTERSON (+ any SIM_* the channel reads) and the
          adapter's own <MODEM>_BIN. All parsed once into AdapterConfig.
  HOOKS   start_stations / wait_ready / link_connect / transfer  (+ optional
          preclean_patterns / on_line / teardown_stations / telemetry).
  OUTPUT  AdapterResult -> a `RESULT:` line (framework-compatible) + `RESULT_JSON` line.
          A connect failure calls fail_connect() which prints the NOCONN token
          sweep_runner classifies as `fail_connect`.

A runnable reference adapter (in-process fake modem, no ALSA/subprocess) lives in
example_loopback_adapter.py -- copy it as a starting point.
"""
import abc
import json
import os
import signal
import statistics
import subprocess as sp
import sys
import time
from dataclasses import dataclass, field, asdict

import numpy as np

import bench_pipes

RESULT_SCHEMA = "modem-adapter-result/1"


@dataclass
class AdapterConfig:
    """Everything the framework hands an adapter, parsed once from argv + env. This
    dataclass IS the documented input surface -- add a field here (not an ad-hoc
    os.environ.get deep in adapter code) when the contract grows a knob."""
    payload_bytes: int = 4096
    timeout_s: float = 120.0
    sigma: str = "0"                 # noise std (int16 LSBs); passed through to channel_sim
    txgain: str = "1.0"              # equal-PEP drive (results/<modem>_txgain.txt)
    seed: int = 1234
    np_stats: str = ""               # signal-stats sidecar prefix (channel_sim writes it)
    half_duplex: bool = False
    ptt: bool = False
    watterson: str = "off"
    env: dict = field(default_factory=dict)   # full env, for passthrough to channel_sim

    @classmethod
    def from_env(cls, argv=None, env=None):
        argv = list(sys.argv[1:] if argv is None else argv)
        e = dict(os.environ if env is None else env)

        def g(k, d):
            return (e.get(k, d) or d).strip()
        return cls(
            payload_bytes=int(argv[0]) if len(argv) > 0 else 4096,
            timeout_s=float(argv[1]) if len(argv) > 1 else 120.0,
            sigma=g("SIGMA", "0"),
            txgain=g("TXGAIN", "1.0"),
            seed=int(g("SEED", "1234") or "1234"),
            np_stats=g("NP_STATS", ""),
            half_duplex=g("SIM_HALF_DUPLEX", "0") == "1",
            ptt=g("SIM_PTT", "0") == "1",
            watterson=g("SIM_WATTERSON", "off"),
            env=e,
        )


@dataclass
class AdapterResult:
    """The measured outcome of one transfer. `result_line()` is the framework-compatible
    stdout contract sweep_runner parses; `as_dict()` is the structured forward contract."""
    got: int
    total: int
    seconds: float
    intact: bool
    goodput: float
    peak_bitrate: int = 0
    sn_med: float = -99.0

    def result_line(self) -> str:
        # EXACT tokens sweep_runner's RES_* regexes match; do not reorder/rename casually.
        return (f"RESULT: {self.got}/{self.total} B in {self.seconds:.1f}s "
                f"intact={self.intact} goodput={self.goodput:.1f} B/s "
                f"| peak_bitrate={self.peak_bitrate}bps | SN_med={self.sn_med:.1f}")

    def as_dict(self) -> dict:
        d = {"schema": RESULT_SCHEMA}
        d.update(asdict(self))
        return d


class ModemAdapter(abc.ABC):
    """Base class encoding the shared adapter lifecycle. Subclass and implement
    the four abstract hooks; override the optional ones as needed. Then:

        if __name__ == "__main__":
            sys.exit(run_adapter(MyAdapter))
    """

    name = "modem"
    ready_timeout_s = 15.0           # how long to wait for both stations to accept control
    connect_timeout_s = 140.0        # overall budget for link_connect (incl. its retries)

    def __init__(self, cfg: AdapterConfig):
        self.cfg = cfg
        self._sim = None             # channel_sim Popen (a session leader); torn down in run()
        self._stations = []          # station process handles the base SIGTERMs on teardown
        self.modes = []              # telemetry: observed bitrates (peak_bitrate = max)
        self.snrs = []               # telemetry: observed SN samples (sn_med = median)

    # ---------------- modem-specific hooks ----------------
    def preclean_patterns(self):
        """pkill -9 -f patterns to clear stale processes before launch. MUST NOT match
        this adapter's own cmdline (the self-kill trap; see sweep_runner KILL_PATS)."""
        return []

    @abc.abstractmethod
    def start_stations(self):
        """Launch the two modem instances wired to the channel transport. Append each
        process handle to self._stations so the base tears them down."""

    @abc.abstractmethod
    def wait_ready(self, deadline: float) -> bool:
        """Return True once both stations accept control connections (poll until deadline)."""

    @abc.abstractmethod
    def link_connect(self, deadline: float) -> bool:
        """Bring up the ARQ link A<->B (handshake, retries as the protocol needs). Return
        True when connected. Call self.on_line(station, line) for every control line so
        PTT is relayed to the channel and telemetry is scanned."""

    @abc.abstractmethod
    def transfer(self, payload: bytes, deadline: float) -> bytes:
        """Send `payload` A->B and return the bytes received at B, stopping at len(payload)
        or `deadline`. Keep calling self.on_line(...) for control traffic during the pump."""

    def teardown_stations(self):
        """Default: SIGTERM every launched station. Override for modem-specific cleanup."""
        for p in self._stations:
            try:
                p.send_signal(signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass

    def scan_telemetry(self, station: str, line: str):
        """Optional: pull bitrate/SN out of a control line into self.modes/self.snrs."""

    # ---------------- shared machinery ----------------
    def launch_channel(self):
        """Start the shared half-duplex channel sim (owns the transport). Override with a
        no-op for modems whose channel persists across cells (e.g. VARA)."""
        self._sim = bench_pipes.launch_channel_sim()

    def teardown_channel(self):
        if self._sim is not None:
            try:
                os.killpg(os.getpgid(self._sim.pid), 9)
            except (OSError, ProcessLookupError):
                pass

    def on_line(self, station: str, line: str):
        """Handle one control line from a station: relay PTT to the channel (real-PTT HD)
        and scan telemetry. Adapters call this for every line they read."""
        bench_pipes.fwd_ptt(self._sim, station, line)
        self.scan_telemetry(station, line)

    def make_payload(self) -> bytes:
        """Incompressible, seed-deterministic payload (so paired-seed A/B sees identical
        bytes). Real adapters may override (e.g. os.urandom for a non-reproducible run)."""
        return np.random.default_rng(self.cfg.seed).bytes(self.cfg.payload_bytes)

    def fail_connect(self, msg: str = ""):
        """Emit the token sweep_runner classifies as `fail_connect` (transient, one retry)."""
        print(f"NOCONN {msg}".strip(), flush=True)

    def peak_bitrate(self) -> int:
        return max(self.modes) if self.modes else 0

    def sn_med(self) -> float:
        return round(statistics.median(self.snrs), 1) if self.snrs else -99.0

    def run(self) -> int:
        """Template method: the full adapter lifecycle. Returns a process exit code
        (0 = intact delivery, 2 = partial/failed transfer, 1 = connect failure)."""
        cfg = self.cfg
        for pat in self.preclean_patterns():
            sp.run(["pkill", "-9", "-f", pat], stdout=sp.DEVNULL, stderr=sp.DEVNULL)
        try:
            self.launch_channel()
            if self._sim is not None:
                time.sleep(1.0)          # let channel_sim open its devices before stations
            self.start_stations()
            if not self.wait_ready(time.time() + self.ready_timeout_s):
                self.fail_connect("stations not listening")
                return 1
            if not self.link_connect(time.time() + self.connect_timeout_s):
                self.fail_connect(f"no CONNECT sigma={cfg.sigma}")
                return 1
            payload = self.make_payload()
            t0 = time.time()
            recv = bytes(self.transfer(payload, t0 + cfg.timeout_s))
            dt = time.time() - t0
            got = len(recv)
            intact = recv[:cfg.payload_bytes] == payload
            goodput = got / dt if dt > 0 else 0.0
            res = AdapterResult(got, cfg.payload_bytes, dt, intact, goodput,
                                self.peak_bitrate(), self.sn_med())
            print(res.result_line(), flush=True)
            print("RESULT_JSON " + json.dumps(res.as_dict()), flush=True)
            return 0 if (got >= cfg.payload_bytes and intact) else 2
        finally:
            self.teardown_stations()
            self.teardown_channel()


def run_adapter(adapter_cls, argv=None, env=None) -> int:
    """Convenience entry point: parse config, construct, run. An adapter's __main__ is
    just `sys.exit(run_adapter(MyAdapter))`."""
    return adapter_cls(AdapterConfig.from_env(argv=argv, env=env)).run()
