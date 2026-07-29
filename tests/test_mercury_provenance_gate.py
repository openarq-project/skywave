"""The MercuryAdapter's provenance/pin gate, at the adapter boundary.

test_modem_provenance.py covers the mechanism; this covers the WIRING: that the
gate runs before anything is launched, that a pin violation refuses the run, and
that an unpinned ad-hoc run is still allowed (adapter unit tests and smokes have
no real Mercury binary).
"""
import json
import os
import subprocess as sp

import pytest

from skywave.modem_adapter import AdapterConfig
from skywave.adapters.mercury import MercuryAdapter
from skywave.modem_provenance import PinViolation

GIT_ID = ["-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false"]


def _git(repo, *a):
    return sp.run(["git", *GIT_ID, *a], cwd=repo, check=True,
                  stdout=sp.PIPE, stderr=sp.STDOUT, text=True).stdout.strip()


@pytest.fixture
def merc_repo(tmp_path):
    r = tmp_path / "mercury"
    r.mkdir()
    (r / "main.c").write_text("int main(void){return 0;}\n")
    b = r / "mercury"
    b.write_bytes(b"\x7fELF pretend mercury")
    b.chmod(0o755)
    _git(r, "init", "-q", "-b", "mercuryv2")
    _git(r, "add", "main.c")
    _git(r, "commit", "-q", "-m", "upstream")
    return r


def _cfg():
    return AdapterConfig.from_env(argv=["4096", "60"], env={})


def _pinfile(tmp_path, commit, rationale="upstream baseline for the bakeoff"):
    p = tmp_path / "MERCURY_PIN.json"
    p.write_text(json.dumps({"commit": commit, "rationale": rationale,
                             "pinned_on": "2026-07-29"}))
    return str(p)


def test_matching_pin_lets_the_adapter_construct(monkeypatch, merc_repo, tmp_path):
    head = _git(merc_repo, "rev-parse", "HEAD")
    monkeypatch.setenv("MERCURY_BIN", str(merc_repo / "mercury"))
    monkeypatch.setenv("MERCURY_PIN_FILE", _pinfile(tmp_path, head))
    ad = MercuryAdapter(_cfg())
    assert ad.provenance["commit"] == head
    assert ad.provenance["pinned"] is True and ad.provenance["problems"] == []


def test_wrong_commit_refuses_before_any_station_starts(monkeypatch, merc_repo,
                                                        tmp_path):
    """The whole point: a bench carrying the wrong Mercury must not produce a
    corpus row at all. Construction is where that has to be caught -- it costs no
    airtime and leaves nothing to tear down."""
    monkeypatch.setenv("MERCURY_BIN", str(merc_repo / "mercury"))
    monkeypatch.setenv("MERCURY_PIN_FILE", _pinfile(tmp_path, "b" * 40))
    with pytest.raises(PinViolation) as e:
        MercuryAdapter(_cfg())
    assert "does not match" in str(e.value)


def test_local_experiment_branch_is_caught(monkeypatch, merc_repo, tmp_path):
    """The 2026-07-29 incident, replayed: a box built from a local branch that has
    diverged from the pinned upstream commit."""
    upstream = _git(merc_repo, "rev-parse", "HEAD")
    _git(merc_repo, "checkout", "-q", "-b", "patternack-expectation")
    (merc_repo / "arq.c").write_text("our own fast-ACK work\n")
    _git(merc_repo, "add", "arq.c")
    _git(merc_repo, "commit", "-q", "-m", "local experiment")
    monkeypatch.setenv("MERCURY_BIN", str(merc_repo / "mercury"))
    monkeypatch.setenv("MERCURY_PIN_FILE", _pinfile(tmp_path, upstream))
    with pytest.raises(PinViolation):
        MercuryAdapter(_cfg())


def test_override_runs_but_records_the_violation(monkeypatch, merc_repo, tmp_path):
    monkeypatch.setenv("MERCURY_BIN", str(merc_repo / "mercury"))
    monkeypatch.setenv("MERCURY_PIN_FILE", _pinfile(tmp_path, "b" * 40))
    monkeypatch.setenv("MERCURY_PIN_OVERRIDE", "1")
    ad = MercuryAdapter(_cfg())
    assert ad.provenance["override"] is True and ad.provenance["problems"]


def test_pin_without_a_reason_is_refused(monkeypatch, merc_repo, tmp_path):
    head = _git(merc_repo, "rev-parse", "HEAD")
    monkeypatch.setenv("MERCURY_BIN", str(merc_repo / "mercury"))
    monkeypatch.setenv("MERCURY_PIN_FILE", _pinfile(tmp_path, head, rationale="TODO"))
    with pytest.raises(PinViolation) as e:
        MercuryAdapter(_cfg())
    assert "rationale" in str(e.value).lower()


def test_untracked_build_artifacts_do_not_trip_the_gate(monkeypatch, merc_repo,
                                                        tmp_path):
    """A normally-built box is covered in .o/.d files; the gate must not cry wolf."""
    head = _git(merc_repo, "rev-parse", "HEAD")
    (merc_repo / "main.o").write_bytes(b"obj")
    (merc_repo / "main.d").write_text("dep")
    monkeypatch.setenv("MERCURY_BIN", str(merc_repo / "mercury"))
    monkeypatch.setenv("MERCURY_PIN_FILE", _pinfile(tmp_path, head))
    assert MercuryAdapter(_cfg()).provenance["problems"] == []


def test_unpinned_missing_binary_still_constructs(monkeypatch):
    """Adapter unit tests and dry smokes have no Mercury on PATH. Unpinned, that
    must stay non-fatal -- the launch reports it better than we can."""
    monkeypatch.delenv("MERCURY_BIN", raising=False)
    monkeypatch.delenv("MERCURY_PIN_FILE", raising=False)
    monkeypatch.delenv("MERCURY_PIN", raising=False)
    ad = MercuryAdapter(_cfg())
    assert ad.provenance["unresolved"] is True
    assert ad.provenance["unpinned"] is True


def test_pinned_missing_binary_is_fatal(monkeypatch, tmp_path):
    """...but once a pin is declared, an unidentifiable binary is exactly what the
    pin exists to prevent, so it must NOT be waved through."""
    monkeypatch.setenv("MERCURY_BIN", str(tmp_path / "definitely-absent"))
    monkeypatch.setenv("MERCURY_PIN_FILE", _pinfile(tmp_path, "c" * 40))
    with pytest.raises(Exception):
        MercuryAdapter(_cfg())


def test_provenance_line_is_emitted_for_the_run_log(monkeypatch, merc_repo,
                                                    tmp_path, capsys):
    head = _git(merc_repo, "rev-parse", "HEAD")
    monkeypatch.setenv("MERCURY_BIN", str(merc_repo / "mercury"))
    monkeypatch.setenv("MERCURY_PIN_FILE", _pinfile(tmp_path, head))
    MercuryAdapter(_cfg())
    out = capsys.readouterr().out
    assert "MERCURY_PROVENANCE " in out
    payload = [l for l in out.splitlines() if l.startswith("MERCURY_PROVENANCE ")][0]
    assert json.loads(payload[len("MERCURY_PROVENANCE "):])["commit"] == head


def test_provenance_file_is_written_when_requested(monkeypatch, merc_repo, tmp_path):
    head = _git(merc_repo, "rev-parse", "HEAD")
    out = tmp_path / "prov.json"
    monkeypatch.setenv("MERCURY_BIN", str(merc_repo / "mercury"))
    monkeypatch.setenv("MERCURY_PIN_FILE", _pinfile(tmp_path, head))
    monkeypatch.setenv("MERCURY_PROVENANCE_FILE", str(out))
    MercuryAdapter(_cfg())
    assert json.loads(out.read_text())["commit"] == head


def test_sock_variant_inherits_the_gate(monkeypatch, merc_repo, tmp_path):
    from skywave.adapters.mercury_sock import MercurySockAdapter
    monkeypatch.setenv("MERCURY_BIN", str(merc_repo / "mercury"))
    monkeypatch.setenv("MERCURY_PIN_FILE", _pinfile(tmp_path, "b" * 40))
    with pytest.raises(PinViolation):
        MercurySockAdapter(_cfg())
