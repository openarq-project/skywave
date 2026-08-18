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


def test_per_run_launches_both_prefixes_and_records_the_cost(monkeypatch):
    v = _adapter(monkeypatch, "per-run")
    a = v.VaraAdapter.__new__(v.VaraAdapter)
    launched = []
    monkeypatch.setattr(v.VaraAdapter, "_kill_vara", lambda self: None)
    monkeypatch.setattr(v.VaraAdapter, "_launch_one",
                        lambda self, prefix, port: launched.append(port))
    monkeypatch.setattr(v.VaraAdapter, "_ports_up", lambda self, dl: True)
    a.start_stations()
    assert launched == [8300, 8310]
    # the startup cost is the entire reason the legacy mode existed; it must be
    # measured and printed, not folklore
    assert a._start_s and a._start_s[0] >= 0


def test_a_failed_launch_retries_then_gives_up_without_raising(monkeypatch):
    """A launch failure must surface as an ordinary fail_connect row, not an
    exception that loses the cell -- and it must not retry forever."""
    v = _adapter(monkeypatch, "per-run")
    a = v.VaraAdapter.__new__(v.VaraAdapter)
    attempts = []
    monkeypatch.setattr(v.VaraAdapter, "_kill_vara",
                        lambda self: attempts.append("kill"))
    monkeypatch.setattr(v.VaraAdapter, "_launch_one", lambda self, p, port: None)
    monkeypatch.setattr(v.VaraAdapter, "_ports_up", lambda self, dl: False)
    a.start_stations()                      # must not raise
    assert attempts.count("kill") == v.VARA_START_ATTEMPTS
    assert a._start_s == []


def test_wine_loader_stays_pinned_to_ubuntu_wine_9(monkeypatch):
    """VARA HF B wedges under winehq-staging (measured 2026-07-19 2/2): process
    up, command port never opens. Bare `wine` now resolves to staging, so the
    absolute 9.0 loader path is load-bearing."""
    v = _adapter(monkeypatch, "per-run")
    assert v.WINE_LOADER == "/usr/lib/wine/wine"
