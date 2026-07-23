#!/usr/bin/env python3
"""MercurySockAdapter -- Mercury over its virtual-clock `-x sock` backend.

Runs the Mercury HF modem device-free AND wall-clock-free: each station connects
its framed-socket audio backend (`mercury -x sock`, env MERCURY_AUDIO_SOCK) to
skywave's sock transport, and channel_sim is the lockstep virtual-time master
(SIM_TRANSPORT=sock + SIM_CLOCK=virt_time). The transport carries the clock --
Mercury's ARQ timers advance with `virtual_now_ms` on each sim frame -- so there
is no real-time pacing anywhere: runs are deterministic, identical on every OS,
and faster than real time. This is the transport that replaces the fifo bridge,
whose synthesized 8 kHz wall clock raced Mercury's real-time fifo backend and
stalled on fast hosts (the M5 Mac).

The cable runs at Mercury's native rate: SIM_FS=8000, SIM_NCH=1 (mono), so no
resampler on either side. Mercury reports PTT in-band on every station frame
(block-exact key edges); the stdin PTT relay stays as a harmless second source.

CELL CALIBRATION: SIGMA is specified at the 48 kHz reference rate and
channel_sim auto-scales the injected per-sample std by sqrt(FS/48000)
(SIM_SIGMA_REF_FS, default 48000), so the in-band noise density -- the cell's
SNR -- is rate-invariant: reuse 48 kHz cell specs verbatim. Set
SIM_SIGMA_REF_FS=0 only if you deliberately want raw per-sample sigma at
8 kHz (each unit is then ~2.45x the 48 kHz in-band effect).
Determinism scope: the transport and
all ARQ timing are virtual (no real-time pacing, no starvation flake), but
Mercury's internal thread interleavings still vary run-to-run, so results are
stable, not bit-identical.

The control plane (TCP TNC, MYCALL/LISTEN/CONNECT, transfer, telemetry) is
inherited unchanged from MercuryAdapter; only the channel launch and process
launch differ.

Needs a Mercury build with the `-x sock` backend (branch audio-sock-virtual-clock
or later). Set MERCURY_BIN, then run as the `mercury_sock` modem:

  skywave-sweep mercury_sock spec.json out.csv
"""
import os
import socket
import subprocess as sp
import sys
import time

from skywave import bench_pipes
from skywave.adapters.mercury import MercuryAdapter
from skywave.modem_adapter import ModemAdapter, run_adapter

FS = 8000       # Mercury's native modem rate; the sock cable runs at it directly


class MercurySockAdapter(MercuryAdapter):
    name = "mercury_sock"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.sockdir = (os.environ.get("SIM_SOCK_DIR", "").strip()
                        or f"/tmp/skywave-mercsock-{os.getpid()}")
        os.makedirs(self.sockdir, exist_ok=True)

    # ---- channel: the shared sim as lockstep virtual-time master ----
    def preclean_patterns(self):
        return ["mercury .*-x sock"]

    def launch_channel(self):
        # SIM_FS/SIM_NCH are forced, not defaulted: mercury's sock backend is
        # 8 kHz mono with no resampler, so any other cable rate is silently
        # wrong, never a knob. SIM_BLOCK=160 (20 ms) keeps half-duplex key-edge
        # quantization at the validated 48 kHz rig's granularity (1024/48k =
        # 21.3 ms) -- the 48 kHz default of 1024 frames is 128 ms at 8 kHz,
        # coarse enough to clip a burst head under PTT keying (observed: HD
        # handshake never completing). 160 is also mercury's native modem
        # chunk. SIM_CLOCK stays overridable for debugging.
        self._sim = bench_pipes.launch_channel_sim(extra_env={
            "SIM_TRANSPORT": "sock",
            "SIM_CLOCK": os.environ.get("SIM_CLOCK", "virt_time"),
            "SIM_SOCK_DIR": self.sockdir,
            "SIM_FS": str(FS),
            "SIM_NCH": "1",
            "SIM_BLOCK": "160",
            # Mercury's protocol timers are virtual but its thread handoffs
            # are wall-scheduled; unbounded pace (60-175x observed on an M5)
            # turns milliseconds of scheduling into virtual seconds and blows
            # its channel guards. 10x keeps handoffs ~two orders below the
            # ~700 ms guards while still far faster than real time.
            "SIM_VIRT_MAX_RATIO": os.environ.get("SIM_VIRT_MAX_RATIO", "10"),
        })

    # ---- stations: mercury -x sock, audio path via env ----
    def start_stations(self):
        self._launch("a", self.A_PORT, 8100, "/tmp/msA.log")   # A answerer
        self._launch("b", self.B_PORT, 8110, "/tmp/msB.log")   # B caller/sender

    def _launch(self, station, port, bcast, log):
        env = dict(os.environ,
                   MERCURY_AUDIO_SOCK=os.path.join(self.sockdir, f"{station}.sock"))
        p = sp.Popen([self.merc, "-C", self._bench_ini(),
                      "-x", "sock",
                      "-p", str(port), "-b", str(bcast), "-m", "1"],
                     env=env, stdout=open(log, "wb"), stderr=sp.STDOUT)
        self._stations.append(p)

    def bench_time(self):
        """SIGNAL time from the sim's status file (<sockdir>/virt_now_ms), so
        `seconds`/`goodput` are real-time equivalent regardless of the virtual
        pace: a 10x-capped run and a 1x run report the same goodput. The file
        exists long before the transfer starts (the sim writes it every 500 ms
        of signal time from block 0); wall clock is only a startup fallback."""
        try:
            with open(os.path.join(self.sockdir, "virt_now_ms")) as f:
                return int(f.read()) / 1000.0
        except (OSError, ValueError):
            return time.time()

    def link_connect(self, deadline):
        """The base's connect choreography, re-paced for virtual time.

        Mercury's whole CALL/ACCEPT cycle runs in signal time, which on a fast
        host steps 10-60x wall -- a failed attempt is over in well under a
        second of wall time, and the base's wall-clock sleeps (1 s settle, 45 s
        pump, 3 s between attempts) let tens of virtual seconds of ACCEPT
        window expire unobserved (observed on the M5: caller exhausted all CALL
        slots in 0.3 s wall). So: brief sleeps, brief per-attempt pumps, and
        MORE attempts -- the caller/callee retry interleave is what needs the
        tries, and tries are nearly free in wall time."""
        time.sleep(0.3)
        self.a = socket.create_connection(("127.0.0.1", self.A_PORT)); self.a.setblocking(False)
        self.b = socket.create_connection(("127.0.0.1", self.B_PORT)); self.b.setblocking(False)
        self.adat = socket.create_connection(("127.0.0.1", self.A_PORT + 1)); self.adat.setblocking(False)
        self.bdat = socket.create_connection(("127.0.0.1", self.B_PORT + 1))
        self.nm = {self.a: "A", self.b: "B"}
        self.buf = {self.a: b"", self.b: b""}
        self._snd(self.a, "MYCALL W1ABC"); self._snd(self.b, "MYCALL W2XYZ")
        self._pump(time.time() + 0.3)
        self._snd(self.a, "LISTEN ON"); time.sleep(0.2)
        for attempt in range(1, 13):
            self._snd(self.b, "CONNECT W2XYZ W1ABC")
            if self._pump(min(deadline, time.time() + 12),
                          stop=lambda t: t.startswith("CONNECTED")):
                return True
            if time.time() >= deadline:
                break
            print(f"  (connect {attempt}/12 failed; retry)", flush=True)
            self._snd(self.b, "ABORT"); time.sleep(0.5)
            self._snd(self.a, "LISTEN ON"); time.sleep(0.2)
        return False

    def teardown_stations(self):
        try:
            if self.b is not None:
                self._snd(self.b, "DISCONNECT"); time.sleep(2)
        except OSError:
            pass
        ModemAdapter.teardown_stations(self)     # SIGTERM stations; no ALSA pkill needed
        sp.run(["pkill", "-9", "-f", "mercury .*-x sock"],
               stdout=sp.DEVNULL, stderr=sp.DEVNULL)


if __name__ == "__main__":
    sys.exit(run_adapter(MercurySockAdapter))
