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

import pytest

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


class _FakeProc:
    def poll(self):
        return None


def _fake_run(help_bytes):
    def fake_run(argv, **kw):
        class R:
            stdout = help_bytes
            stderr = b""
            returncode = 0
        return R()
    return fake_run


@pytest.mark.parametrize("help_text,expected", [
    (b"... --host-sock <HOST_SOCK> ... --no-web ...", True),   # pre-e4e158d build
    (b"... --no-web ...", False),                              # flag retired upstream
])
def test_host_sock_flag_probed_from_help(monkeypatch, sock_dir, help_text, expected):
    """Older armstrong builds bind a host-API unix socket at a FIXED default
    path, so a two-station pair needs per-station --host-sock paths; newer
    builds retired the flag (the host plane rides the web API, which --no-web
    already disables) and REJECT unknown argv at parse. The adapter must key
    the flag off --help, same as --no-web, so one skywave drives both
    generations."""
    ad = _mk_adapter(monkeypatch, sock_dir)
    argvs = []
    monkeypatch.setattr("skywave.adapters.armstrong.sp.run", _fake_run(help_text))
    monkeypatch.setattr("skywave.adapters.armstrong.sp.Popen",
                        lambda argv, **kw: argvs.append(list(argv)) or _FakeProc())
    ad.start_stations()
    assert len(argvs) == 2
    for argv in argvs:
        assert ("--host-sock" in argv) == expected, argv
    if expected:
        paths = [a[a.index("--host-sock") + 1] for a in argvs]
        assert paths[0] != paths[1], "stations must not share a host socket"
