"""PTT isolation regression tests.

On 2026-07-23 a device-free ("virtual") armstrong sock smoke keyed a real HF
rig over the air: the adapter launched armstrong-hf without --config,
armstrong fell through to the box's PLATFORM operator config
(~/.config/armstrong / ~/Library/Application Support/...), whose active
profile had ptt.method="rigctld" wired to a live rigctld. Sock audio replaces
only the AUDIO transport -- armstrong resolves PTT from the config regardless
of the audio backend.

These tests pin the fix in two strengths:

- argv tests (no binary needed): every constructed launch argv carries an
  explicit config flag pointing at a skywave-owned path, never the platform
  default. Mercury gets the same treatment: it resolves a RELATIVE
  "mercury.ini" against the invocation CWD, where a stray file could point
  hamlib at a real rig.
- the strong form (needs ARMSTRONG_BIN): with a rigctld-configured operator
  config PRESENT at the (redirected) platform path and a dummy listener on
  its rigctld port, one sock-rig station launch must NEVER contact the
  listener. This is the incident, replayed harmlessly; it also proves the
  bench config armstrong generates really is PTT-inert, not just that a flag
  was passed.

Run:  cd skywave && python3 -m pytest tests/test_ptt_isolation.py -q
"""
import os
import socket
import sys
import threading
import time

import pytest

from skywave.modem_adapter import AdapterConfig
from skywave.adapters.armstrong import ArmstrongAdapter
from skywave.adapters.mercury import MercuryAdapter
from skywave.adapters.mercury_sock import MercurySockAdapter

from conftest import sock_dir  # noqa: F401  (fixture)

# Substrings that must never appear in a bench station's config path: the
# platform operator-config dirs armstrong/mercury would fall through to.
PLATFORM_CONFIG_MARKERS = (".config/armstrong", "Application Support")


class _FakeProc:
    """Popen stand-in for argv-capture tests: never runs anything."""
    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def _capture_popen(argvs):
    def fake_popen(argv, **kw):
        argvs.append(list(argv))
        return _FakeProc()
    return fake_popen


def _adapter(monkeypatch, cls, tmp_dir, transport):
    if transport:
        monkeypatch.setenv("SIM_TRANSPORT", transport)
    else:
        monkeypatch.delenv("SIM_TRANSPORT", raising=False)
    monkeypatch.setenv("SIM_SOCK_DIR", tmp_dir)
    cfg = AdapterConfig.from_env(argv=["512", "60"], env=dict(os.environ))
    return cls(cfg)


def _flag_value(argv, flag):
    assert flag in argv, f"launch argv missing {flag}: {argv}"
    return argv[argv.index(flag) + 1]


# ---- argv tests: every launch carries a skywave-owned config ----

@pytest.mark.parametrize("transport", ["sock", ""])
def test_armstrong_argv_carries_bench_config(monkeypatch, sock_dir, transport):
    ad = _adapter(monkeypatch, ArmstrongAdapter, sock_dir, transport)
    ad._no_web = ["--no-web"]        # skip the --help probes (no binary here;
    ad._host_sock = False            # the probe flags have their own tests)
    argvs = []
    monkeypatch.setattr("skywave.adapters.armstrong.sp.Popen", _capture_popen(argvs))
    ad.start_stations()
    assert len(argvs) == 2
    paths = [_flag_value(a, "--config") for a in argvs]
    for p in paths:
        assert not any(m in p for m in PLATFORM_CONFIG_MARKERS), p
        assert os.path.isabs(p) and os.path.isdir(os.path.dirname(p)), p
    assert paths[0] != paths[1], "stations must not share a config file"


@pytest.mark.parametrize("cls,module", [
    (MercuryAdapter, "skywave.adapters.mercury"),
    (MercurySockAdapter, "skywave.adapters.mercury_sock"),
])
def test_mercury_argv_carries_bench_ini(monkeypatch, sock_dir, cls, module):
    ad = _adapter(monkeypatch, cls, sock_dir,
                  "sock" if cls is MercurySockAdapter else "")
    argvs = []
    monkeypatch.setattr(f"{module}.sp.Popen", _capture_popen(argvs))
    ad.start_stations()
    assert len(argvs) == 2
    for a in argvs:
        ini = _flag_value(a, "-C")
        assert os.path.isabs(ini), ini
        with open(ini) as f:
            body = f.read()
        assert "radio_model = -1" in body, body


# ---- the strong form: the incident, replayed harmlessly ----

@pytest.mark.skipif(sys.platform != "linux",
                    reason="XDG_CONFIG_HOME redirect of the platform config "
                           "dir is Linux-only")
@pytest.mark.skipif(not os.environ.get("ARMSTRONG_BIN"),
                    reason="needs a real armstrong binary (set ARMSTRONG_BIN)")
def test_sock_station_never_contacts_operator_rigctld(monkeypatch, sock_dir):
    """A rigctld-configured operator config sits at the platform path; a dummy
    listener plays its rigctld. Launching one sock-rig station pair must
    produce ZERO connections to the listener."""
    # trap operator config at the redirected platform path
    xdg = os.path.join(sock_dir, "xdg")
    os.makedirs(os.path.join(xdg, "armstrong"))
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    ptt_port = probe.getsockname()[1]
    probe.close()
    with open(os.path.join(xdg, "armstrong", "armstrong.toml"), "w") as f:
        f.write('config_version = 1\n'
                'active_profile = "trap"\n'
                '[station]\ncallsign = "W1TRP"\n'
                '[profiles.trap]\nname = "trap"\n'
                '[profiles.trap.ptt]\nmethod = "rigctld"\n'
                f'rigctld_addr = "127.0.0.1:{ptt_port}"\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", xdg)

    accepts = []

    def rigctld_trap():
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", ptt_port))
        s.listen(4)
        s.settimeout(10)
        try:
            while True:
                c, addr = s.accept()
                accepts.append(addr)
        except socket.timeout:
            pass
        finally:
            s.close()

    held = []      # keep accepted audio conns alive: a dropped conn closes the
                   # cable and the station exits, tripping the liveness guard

    def audio_stub(path):
        # accept-and-hold: enough for armstrong's sock transport to open and
        # startup to proceed past PTT resolution (no sim needed)
        s = socket.socket(socket.AF_UNIX)
        s.bind(path)
        s.listen(2)
        s.settimeout(10)
        try:
            while True:
                c, _ = s.accept()
                held.append(c)
        except socket.timeout:
            pass
        finally:
            s.close()

    threads = [threading.Thread(target=rigctld_trap, daemon=True)]
    threads += [threading.Thread(target=audio_stub,
                                 args=(os.path.join(sock_dir, f"{st}.sock"),),
                                 daemon=True) for st in ("a", "b")]
    for t in threads:
        t.start()
    time.sleep(0.3)

    ad = _adapter(monkeypatch, ArmstrongAdapter, sock_dir, "sock")
    ad._no_web = ["--no-web"]
    ad.start_stations()
    try:
        time.sleep(6)        # rigctld connect happens within seconds of startup
        # Liveness guard: a station that died at startup (bad flag, bad binary)
        # never resolves PTT, which would pass the no-contact assertion
        # VACUOUSLY. The first version of this test green-washed exactly that
        # way against a binary without --host-sock.
        dead = [p for p in ad._stations if p.poll() is not None]
        if dead:
            logs = ""
            for lg in ("/tmp/armA.log", "/tmp/armB.log"):
                try:
                    with open(lg, errors="replace") as f:
                        logs += f"\n--- {lg} ---\n" + f.read()[-800:]
                except OSError:
                    pass
            pytest.fail("station died at startup — run is vacuous, proves "
                        f"nothing about PTT isolation:{logs}")
    finally:
        ad.teardown_stations()
    assert accepts == [], (
        f"sock/'virtual' station contacted the operator config's rigctld: "
        f"{accepts} — PTT isolation is broken (2026-07-23 incident shape)")
