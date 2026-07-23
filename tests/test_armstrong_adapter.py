"""ArmstrongAdapter unit tests (no armstrong binary needed).

Pins the virtval-2026-07-23 finding: on the sock/virt_time rig the adapter must
report SIGNAL time (the sim's virt_now_ms status file), not compressed wall
clock -- the campaign shipped +52/+61% inflated goodput because bench_time()
had no override. On the ALSA rig (no SIM_TRANSPORT=sock) wall clock is correct
and the status file must be ignored.

Run:  cd skywave && python3 -m pytest tests/test_armstrong_adapter.py -q
"""
import os
import time

from skywave.modem_adapter import AdapterConfig
from skywave.adapters.armstrong import ArmstrongAdapter

from conftest import sock_dir  # noqa: F401  (fixture)


def _mk_adapter(monkeypatch, tmp_sock_dir, transport="sock"):
    if transport:
        monkeypatch.setenv("SIM_TRANSPORT", transport)
    else:
        monkeypatch.delenv("SIM_TRANSPORT", raising=False)
    monkeypatch.setenv("SIM_SOCK_DIR", tmp_sock_dir)
    cfg = AdapterConfig.from_env(argv=["512", "60"], env=dict(os.environ))
    return ArmstrongAdapter(cfg)


def test_bench_time_reads_signal_time_on_sock_rig(monkeypatch, sock_dir):
    """Same contract as mercury_sock: signal seconds from virt_now_ms, wall
    clock only as a startup fallback before the sim writes the file."""
    ad = _mk_adapter(monkeypatch, sock_dir)
    w0 = time.time()
    assert abs(ad.bench_time() - w0) < 2.0          # no file yet: wall fallback
    with open(os.path.join(sock_dir, "virt_now_ms"), "w") as f:
        f.write("73500")
    assert ad.bench_time() == 73.5                  # signal seconds, not wall


def test_bench_time_stays_wall_clock_on_alsa_rig(monkeypatch, sock_dir):
    """The real-time rig must never read a (stale) status file."""
    with open(os.path.join(sock_dir, "virt_now_ms"), "w") as f:
        f.write("73500")
    ad = _mk_adapter(monkeypatch, sock_dir, transport="")
    w0 = time.time()
    assert abs(ad.bench_time() - w0) < 2.0
