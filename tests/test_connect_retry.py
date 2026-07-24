"""Connect-retry robustness: a dying station must fail the cell, not the sweep.

Observed on the Mac sock rig (2026-07-24): the sim hit its virtual budget
(VIRTUAL-TIMEOUT) mid-connect, the cable EOF took both stations down, and the
adapter then (a) pumped the dead control socket silently until the wall
deadline (`if not d: continue` treats recv EOF as no-data) and (b) crashed the
whole sweep with an uncaught BrokenPipeError on the retry path's ABORT send
(`_snd` is a bare sendall). A station death mid-connect must instead fail the
cell cleanly and quickly: `link_connect` returns False, no exception, no
full-deadline zombie pump.

The fake TNC peer here OKs MYCALL/LISTEN and closes its socket on CONNECT --
the same shape the adapter saw (station gone after the connect attempt
started), no real armstrong binary or Mac needed. Ephemeral ports only: this
must never touch the bench's real 8300/8310.

Run:  cd skywave && python3 -m pytest tests/test_connect_retry.py -q
"""
import os
import socket
import threading
import time

from skywave.modem_adapter import AdapterConfig
from skywave.adapters.armstrong import ArmstrongAdapter


class _FakeTnc(threading.Thread):
    """Minimal VARA-ish TNC control peer: OKs commands; optionally drops the
    connection the moment CONNECT arrives (a station dying mid-attempt)."""

    def __init__(self, close_on_connect):
        super().__init__(daemon=True)
        self.close_on_connect = close_on_connect
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(2)
        self.srv.settimeout(20)
        self.port = self.srv.getsockname()[1]

    def run(self):
        try:
            c, _ = self.srv.accept()
        except socket.timeout:
            return
        buf = b""
        c.settimeout(20)
        try:
            while True:
                d = c.recv(4096)
                if not d:
                    return
                buf += d
                while b"\r" in buf:
                    line, buf = buf.split(b"\r", 1)
                    if line.startswith(b"CONNECT") and self.close_on_connect:
                        c.close()
                        return
                    if line:
                        c.sendall(b"OK\r")
        except OSError:
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass


def test_link_connect_fails_cleanly_when_station_dies(monkeypatch):
    monkeypatch.delenv("SIM_TRANSPORT", raising=False)
    cfg = AdapterConfig.from_env(argv=["512", "60"], env=dict(os.environ))
    ad = ArmstrongAdapter(cfg)
    a_peer = _FakeTnc(close_on_connect=True)    # station A dies on CONNECT
    b_peer = _FakeTnc(close_on_connect=False)   # station B stays up
    a_peer.start()
    b_peer.start()
    ad.A_PORT, ad.B_PORT = a_peer.port, b_peer.port   # NEVER the bench's 8300/8310

    t0 = time.time()
    ok = ad.link_connect(deadline=time.time() + 2)    # short pump; retries are the point
    elapsed = time.time() - t0

    assert ok is False, "a dead station is a failed cell, not a connected one"
    # The old behavior pumped the corpse to the full wall deadline (60 s+)
    # before crashing; the fix must fail fast (EOF ends the pump, a broken
    # retry send is caught). Generous bound: three retry sleeps ~= 10.5 s.
    assert elapsed < 25, f"took {elapsed:.1f}s — zombie pump on a dead socket"
