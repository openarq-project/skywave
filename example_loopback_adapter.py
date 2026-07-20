#!/usr/bin/env python3
"""Reference ModemAdapter -- a runnable, hardware-free template.

This is the "copy this to start" artifact for wiring a NEW modem into the channel_sim
harness. It implements every ModemAdapter hook against an in-process FAKE modem (no
ALSA, no subprocess, no TCP), so it runs anywhere and doubles as the contract's test
fixture. To adapt a REAL modem, replace each hook body with the real thing:

  start_stations   -> launch two modem processes wired to the channel transport
                      (ALSA plughw cards, or SIM_TRANSPORT=sock), append handles to
                      self._stations. See mercury_adapter.py for the shape.
  wait_ready       -> poll each modem's control endpoint (TCP TNC / REST / KISS) until up.
  link_connect     -> drive the native connect handshake; call self.on_line(st, line)
                      for every control line (relays PTT + scans telemetry).
  transfer         -> send the payload on the data channel, read until len(payload) or
                      the deadline, pumping control lines through self.on_line(...).

Run it:  python3 example_loopback_adapter.py 4096 10
         SIGMA=40000 python3 example_loopback_adapter.py 4096 10   # forces a partial
"""
import sys

from modem_adapter import ModemAdapter, run_adapter


class LoopbackAdapter(ModemAdapter):
    name = "loopback"

    def launch_channel(self):
        # In-process fake: no real channel sim / transport. A real adapter keeps the
        # base default (bench_pipes.launch_channel_sim) instead of overriding this.
        self._sim = None

    def start_stations(self):
        self._fake_up = True            # a real adapter would Popen two modem processes

    def wait_ready(self, deadline):
        return getattr(self, "_fake_up", False)

    def link_connect(self, deadline):
        # Exercise the control-line hook (PTT relay is a no-op with no sim, but the path
        # is covered). A real adapter loops on its control socket until "CONNECTED".
        self.on_line("A", "PTT OFF")
        self.on_line("B", "CONNECTED")
        return True

    def transfer(self, payload, deadline):
        # Fake channel: clean link delivers the payload byte-exact; an absurd noise level
        # (SIGMA) drops half the bytes so the partial/fail path is exercised in tests.
        self.scan_telemetry("B", "BITRATE (7) 600 BPS")
        self.scan_telemetry("B", "SN 12.0")
        try:
            sigma = float(self.cfg.sigma or 0)
        except ValueError:
            sigma = 0.0
        if sigma > 20000:
            return payload[: len(payload) // 2]      # partial delivery
        return payload

    def scan_telemetry(self, station, line):
        import re
        m = re.search(r"BITRATE \(\d+\) (\d+) BPS", line)
        if m:
            self.modes.append(int(m.group(1)))
        s = re.search(r"\bSN ([0-9.]+)", line)
        if s:
            self.snrs.append(float(s.group(1)))


if __name__ == "__main__":
    sys.exit(run_adapter(LoopbackAdapter))
