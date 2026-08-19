"""VARA station lifecycle: per-run by default, persistent only on request.

Filed 2026-08-18 (owner-directed). VARA used to be launched ONCE per campaign
and persist across every cell, because it is slow to start; every other modem
gets a fresh process per run. That asymmetry gave VARA a warm process and
whatever adaptation state survives a DISCONNECT, in a round whose whole point
is a fair comparison -- and two resident VARA.exe plus wineserver measurably
loaded the box (mercury 17.6 B/s on a VARA-resident bench5 vs 60.7 B/s on a
VARA-free bench3, same binary, same skywave pin).

These tests pin the CONTRACT, not the wine mechanics: no VARA is launched here.

Run:  cd skywave && python3 -m pytest tests/test_vara_lifecycle.py -q
"""
import importlib

import pytest


def _adapter(monkeypatch, mode=None):
    """Re-import the adapter module under a chosen SKYW_VARA_LIFECYCLE."""
    if mode is None:
        monkeypatch.delenv("SKYW_VARA_LIFECYCLE", raising=False)
    else:
        monkeypatch.setenv("SKYW_VARA_LIFECYCLE", mode)
    import skywave.adapters.vara as v
    return importlib.reload(v)


def test_per_run_is_the_default(monkeypatch):
    """The fairness default. If this ever flips back, it must be a deliberate,
    reviewed change -- not an env var someone forgot to set on the run that
    matters."""
    assert _adapter(monkeypatch).VARA_LIFECYCLE == "per-run"


def test_persistent_is_still_reachable(monkeypatch):
    """The E5 control arm and interactive debugging both need it."""
    assert _adapter(monkeypatch, "persistent").VARA_LIFECYCLE == "persistent"


def test_a_typo_is_a_loud_error_not_a_silent_default(monkeypatch):
    """A misspelled mode must not quietly select a lifecycle -- that is exactly
    how an arm ends up not being the arm you think it is."""
    monkeypatch.setenv("SKYW_VARA_LIFECYCLE", "persistant")   # sic
    import skywave.adapters.vara as v
    with pytest.raises(SystemExit):
        importlib.reload(v)


def test_per_run_kills_vara_in_preclean_and_teardown(monkeypatch):
    v = _adapter(monkeypatch, "per-run")
    a = v.VaraAdapter.__new__(v.VaraAdapter)
    assert "VARA.exe" in a.preclean_patterns()
    killed = []
    monkeypatch.setattr(v.sp, "run", lambda argv, **kw: killed.append(argv[-1]))
    monkeypatch.setattr(v.ModemAdapter, "teardown_stations", lambda self: None)
    a.b = None
    a.teardown_stations()
    assert "VARA.exe" in killed


def test_persistent_never_kills_vara(monkeypatch):
    """The legacy mode's whole contract: the instances must survive to the next
    cell. Killing them here would break the mode it belongs to."""
    v = _adapter(monkeypatch, "persistent")
    a = v.VaraAdapter.__new__(v.VaraAdapter)
    assert "VARA.exe" not in a.preclean_patterns()
    killed = []
    monkeypatch.setattr(v.sp, "run", lambda argv, **kw: killed.append(argv[-1]))
    monkeypatch.setattr(v.ModemAdapter, "teardown_stations", lambda self: None)
    a.b = None
    a.teardown_stations()
    assert "VARA.exe" not in killed


def test_persistent_start_stations_launches_nothing(monkeypatch):
    v = _adapter(monkeypatch, "persistent")
    a = v.VaraAdapter.__new__(v.VaraAdapter)
    monkeypatch.setattr(v.VaraAdapter, "_launch_one",
                        lambda *args: pytest.fail("persistent mode must not launch"))
    monkeypatch.setattr(v.VaraAdapter, "_kill_vara",
                        lambda *args: pytest.fail("persistent mode must not kill"))
    a.start_stations()


def test_per_run_launches_both_prefixes(monkeypatch):
    v = _adapter(monkeypatch, "per-run")
    a = v.VaraAdapter.__new__(v.VaraAdapter)
    launched = []
    monkeypatch.setattr(v.VaraAdapter, "_kill_vara", lambda self: None)
    monkeypatch.setattr(v.VaraAdapter, "_launch_one",
                        lambda self, prefix, port: launched.append(port))
    a.start_stations()
    assert launched == [8300, 8310]


def test_launch_path_NEVER_probes_the_command_ports(monkeypatch):
    """REGRESSION (2026-08-18, mine). VARA permits ONE client per command port
    and treats a client disconnect as a session event, so a connect-then-close
    readiness probe followed by wait_ready's real open trips a reset right
    after MYCALL -- station B never registers, on every cell, deterministically.

    I added exactly that probe to start_stations when the per-run lifecycle
    landed, with the warning sitting in the function directly below it. This
    test pins the contract the comment states: the command port is opened
    EXACTLY ONCE, by wait_ready, and the launch path opens nothing at all.
    """
    v = _adapter(monkeypatch, "per-run")
    a = v.VaraAdapter.__new__(v.VaraAdapter)
    monkeypatch.setattr(v.VaraAdapter, "_kill_vara", lambda self: None)
    monkeypatch.setattr(v.VaraAdapter, "_launch_one", lambda self, p, port: None)
    monkeypatch.setattr(
        v.socket, "create_connection",
        lambda *args, **kw: pytest.fail(
            "start_stations opened a command port -- that probe is the "
            "regression this test exists to prevent"))
    a.start_stations()


def test_readiness_retry_is_a_full_relaunch_not_a_reopen(monkeypatch):
    """A half-open pair is the state that wedges VARA, so a retry must kill and
    relaunch rather than re-open against the same process."""
    v = _adapter(monkeypatch, "per-run")
    a = v.VaraAdapter.__new__(v.VaraAdapter)
    a._start_s, a._launch_t0 = [], None
    relaunches = []
    monkeypatch.setattr(v.VaraAdapter, "_relaunch",
                        lambda self: relaunches.append(1))
    monkeypatch.setattr(v.VaraAdapter, "_connect", lambda self, port, dl: None)
    assert a.wait_ready(0) is False
    assert len(relaunches) == v.VARA_START_ATTEMPTS - 1


def test_readiness_records_the_startup_cost_on_success(monkeypatch):
    """The startup cost was the whole justification for the old persistent
    mode; it should be data, not folklore."""
    v = _adapter(monkeypatch, "per-run")
    a = v.VaraAdapter.__new__(v.VaraAdapter)
    a._start_s = []
    a._launch_t0 = 100.0
    monkeypatch.setattr(v.time, "time", lambda: 142.0)
    fake = type("S", (), {"setblocking": lambda self, b: None})()
    monkeypatch.setattr(v.VaraAdapter, "_connect", lambda self, port, dl: fake)
    assert a.wait_ready(1e9) is True
    assert a._start_s == [42.0]


def test_cold_start_deadline_covers_a_real_launch(monkeypatch):
    """20 s was fine when an external lifecycle had already started VARA; in
    per-run mode wait_ready IS the readiness gate and must outlast a cold
    start (vara_up.py polls to 75 s)."""
    assert _adapter(monkeypatch, "per-run").VaraAdapter.ready_timeout_s >= 75.0
    assert _adapter(monkeypatch, "persistent").VaraAdapter.ready_timeout_s == 20.0


def test_wine_loader_stays_pinned_to_ubuntu_wine_9(monkeypatch):
    """VARA HF B wedges under winehq-staging (measured 2026-07-19 2/2): process
    up, command port never opens. Bare `wine` now resolves to staging, so the
    absolute 9.0 loader path is load-bearing."""
    v = _adapter(monkeypatch, "per-run")
    assert v.WINE_LOADER == "/usr/lib/wine/wine"
