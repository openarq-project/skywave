"""Tests for the PROGRESS byte-vs-time tick contract (2026-07-29).

The ticks are what let the scorer answer "intact at budget B" for any B after the
fact, and what lets a no-progress early-out tell a stalled transfer from a slow
one. Both uses have sharp requirements that are easy to regress:

  - OFF by default, so every pre-existing corpus/spec keeps its exact output.
  - Ticks fire on CADENCE, not on the byte count changing -- a flatline is the
    signal, so silence must mean "the process died", not "nothing moved".
  - No catch-up burst: a long blocking read must not emit one tick per missed
    slot when it finally returns.
  - A terminal tick pins the curve's last point at the true end of transfer.

Run:  cd skywave && python3 -m pytest tests/test_progress_ticks.py -q
"""
import contextlib
import io
import re

from skywave.modem_adapter import AdapterConfig, ModemAdapter, run_adapter
from skywave.adapters.example import LoopbackAdapter

TICK = re.compile(r"^PROGRESS t=([\d.]+)s bytes=(\d+)$", re.M)


class FakeClockAdapter(LoopbackAdapter):
    """LoopbackAdapter with a scripted bench clock and a scripted delivery curve.

    `script` is a list of (bench_time, bytes_delivered) the pump walks through,
    so cadence behaviour is tested deterministically with no sleeping.
    """
    script = []

    def __init__(self, cfg):
        super().__init__(cfg)
        self._now = 0.0

    def bench_time(self):
        return self._now

    def transfer(self, payload, deadline):
        for t, n in self.script:
            self._now = t
            self.progress(n)
        return payload


def _ticks(cls, env, payload=64):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        run_adapter(cls, argv=[str(payload), "120"], env=env)
    return [(float(a), int(b)) for a, b in TICK.findall(buf.getvalue())], buf.getvalue()


def test_off_by_default():
    """No SKYW_PROGRESS_S -> not a single tick, so old corpora reproduce byte-for-byte."""
    cls = type("A", (FakeClockAdapter,), {"script": [(t, t * 10) for t in range(0, 30)]})
    ticks, _ = _ticks(cls, env={})
    assert ticks == []


def test_config_parses_env():
    cfg = AdapterConfig.from_env(argv=["64", "120"], env={"SKYW_PROGRESS_S": "5"})
    assert cfg.progress_s == 5.0
    assert AdapterConfig.from_env(argv=["64", "120"], env={}).progress_s == 0.0


def test_fires_on_cadence_not_on_change():
    """A FLATLINED transfer must still tick -- that is the stall detector's whole input."""
    cls = type("A", (FakeClockAdapter,),
               {"script": [(t, 0) for t in range(0, 31)]})   # 0 bytes for 30 s
    ticks, _ = _ticks(cls, env={"SKYW_PROGRESS_S": "10"})
    times = [t for t, _ in ticks]
    assert times[:4] == [0.0, 10.0, 20.0, 30.0], times
    assert all(n == 0 for _, n in ticks[:4])


def test_no_catchup_burst_after_a_long_blocking_read():
    """One tick on return, not one per missed slot.

    ticks[-1] is the terminal tick run() emits after transfer() returns, so it is
    excluded here; it can legitimately share a timestamp with the last pump tick,
    which is why consumers must read the curve as "where two ticks share a t, the
    later line wins".
    """
    cls = type("A", (FakeClockAdapter,),
               {"script": [(0.0, 0), (95.0, 500), (100.0, 600)]})
    ticks, _ = _ticks(cls, env={"SKYW_PROGRESS_S": "10"})
    pump = [t for t, _ in ticks[:-1]]
    assert pump == [0.0, 95.0, 100.0], ticks


def test_terminal_tick_pins_the_final_point():
    """The last tick must carry the delivered count at the true end of transfer."""
    cls = type("A", (FakeClockAdapter,), {"script": [(0.0, 0)]})
    ticks, _ = _ticks(cls, env={"SKYW_PROGRESS_S": "10"}, payload=64)
    assert ticks[-1][1] == 64, ticks


def test_ticks_never_follow_the_result_line():
    """progress() is disarmed after transfer(); a tick after RESULT would corrupt
    any scorer that reads the curve as the transfer window."""
    cls = type("A", (FakeClockAdapter,), {"script": [(0.0, 0), (5.0, 32)]})
    _, out = _ticks(cls, env={"SKYW_PROGRESS_S": "1"})
    assert out.index("RESULT") > out.rindex("PROGRESS")


def test_result_contract_unchanged_when_enabled():
    """Turning ticks on must not perturb the RESULT line sweep_runner scrapes."""
    cls = type("A", (FakeClockAdapter,), {"script": [(0.0, 0), (5.0, 64)]})
    _, on = _ticks(cls, env={"SKYW_PROGRESS_S": "1"})
    _, off = _ticks(cls, env={})
    grab = lambda s: re.search(r"^RESULT: .*$", s, re.M).group(0)
    assert grab(on) == grab(off)
