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

CELL CALIBRATION CAVEAT: SIGMA is a per-sample noise std, so its in-band
density depends on the cable rate -- at 8 kHz all the noise power lands in
4 kHz instead of 24 kHz. For the same in-band noise density as a 48 kHz cell,
scale sigma by sqrt(8/48) ~ 0.408 (e.g. 48 kHz sigma=1000 -> 8 kHz sigma=408).
Do NOT reuse 48 kHz cell specs verbatim. Determinism scope: the transport and
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
        })

    # ---- stations: mercury -x sock, audio path via env ----
    def start_stations(self):
        self._launch("a", self.A_PORT, 8100, "/tmp/msA.log")   # A answerer
        self._launch("b", self.B_PORT, 8110, "/tmp/msB.log")   # B caller/sender

    def _launch(self, station, port, bcast, log):
        env = dict(os.environ,
                   MERCURY_AUDIO_SOCK=os.path.join(self.sockdir, f"{station}.sock"))
        p = sp.Popen([self.merc, "-x", "sock",
                      "-p", str(port), "-b", str(bcast), "-m", "1"],
                     env=env, stdout=open(log, "wb"), stderr=sp.STDOUT)
        self._stations.append(p)

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
