"""The pin gate generalised to every affected modem.

`test_modem_provenance.py` covers the mechanism and `test_mercury_provenance_gate.py`
the mercury wiring. This covers the generalisation: that the env contract is
per-modem, that a source CHECKOUT (FreeDATA) and a CLOSED-SOURCE binary (VARA,
which has no commit to pin -- only an md5) are both expressible, and that every
adapter that resolves its own executable actually gates.
"""
import json
import os
import subprocess as sp

import pytest

from skywave.modem_adapter import AdapterConfig
from skywave.modem_provenance import PinViolation, capture, gate, gate_from_env

GIT_ID = ["-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false"]


def _git(repo, *a):
    return sp.run(["git", *GIT_ID, *a], cwd=repo, check=True,
                  stdout=sp.PIPE, stderr=sp.STDOUT, text=True).stdout.strip()


@pytest.fixture
def tree(tmp_path):
    r = tmp_path / "checkout"
    r.mkdir()
    (r / "server.py").write_text("print('hi')\n")
    b = r / "thebin"
    b.write_bytes(b"\x7fELF pretend")
    b.chmod(0o755)
    _git(r, "init", "-q", "-b", "develop")
    _git(r, "add", "server.py")
    _git(r, "commit", "-q", "-m", "init")
    return r


def _cfg():
    return AdapterConfig.from_env(argv=["4096", "60"], env={})


def _pin(tmp_path, name="PIN.json", **fields):
    p = tmp_path / name
    p.write_text(json.dumps(fields))
    return str(p)


# ---------- a source checkout has provenance too ----------

def test_a_directory_target_is_captured_as_a_tree(tree):
    """FreeDATA is a checkout launched by an interpreter -- no built binary, but
    every bit as sticky as one (a box keeps whatever was last pulled)."""
    rec = capture(str(tree), modem="freedata")
    assert rec["kind"] == "tree"
    assert rec["commit"] == _git(tree, "rev-parse", "HEAD")
    assert rec["bin_md5"] is None          # a directory has no single file to hash


def test_a_checkout_can_be_pinned_by_commit(tree, tmp_path):
    head = _git(tree, "rev-parse", "HEAD")
    ok = gate(str(tree), modem="freedata",
              pin_file=_pin(tmp_path, commit=head, rationale="the patched fix branch"))
    assert ok["problems"] == []
    with pytest.raises(PinViolation):
        gate(str(tree), modem="freedata",
             pin_file=_pin(tmp_path, "b.json", commit="d" * 40, rationale="other"))


# ---------- closed source: md5 is the only identity ----------

def test_closed_source_modem_pins_by_md5(tree, tmp_path):
    """VARA ships no source, so a commit pin is impossible. Pinning its binary's
    md5 is the only way 'pin the modem' can mean the same thing for every modem."""
    binary = tree / "thebin"
    rec = capture(str(binary), modem="vara")
    ok = gate(str(binary), modem="vara",
              pin_file=_pin(tmp_path, bin_md5=rec["bin_md5"],
                            rationale="VARA 4.8.6, the licensed reference ceiling"))
    assert ok["problems"] == []


def test_md5_pin_catches_a_swapped_binary(tree, tmp_path):
    binary = tree / "thebin"
    p = _pin(tmp_path, bin_md5="f" * 32, rationale="pinned VARA build")
    with pytest.raises(PinViolation) as e:
        gate(str(binary), modem="vara", pin_file=p)
    assert "md5" in str(e.value)


def test_a_pin_with_neither_commit_nor_md5_is_refused(tree, tmp_path):
    with pytest.raises(PinViolation) as e:
        gate(str(tree / "thebin"), modem="vara",
             pin_file=_pin(tmp_path, rationale="I forgot to say which build"))
    assert "neither" in str(e.value)


def test_md5_pin_on_a_directory_is_reported_not_silently_ignored(tree, tmp_path):
    with pytest.raises(PinViolation) as e:
        gate(str(tree), modem="freedata",
             pin_file=_pin(tmp_path, bin_md5="a" * 32, rationale="whatever"))
    assert "directory" in str(e.value)


# ---------- the env contract is per-modem ----------

@pytest.mark.parametrize("modem", ["mercury", "armstrong", "ardop", "modem73",
                                   "freedata", "vara"])
def test_env_contract_is_namespaced_per_modem(tree, tmp_path, monkeypatch, modem):
    """<MODEM>_PIN_FILE must gate only that modem -- one modem's pin must never
    leak into another's, or a campaign could 'verify' the wrong thing."""
    binary = str(tree / "thebin")
    monkeypatch.setenv(f"{modem.upper()}_PIN_FILE",
                       _pin(tmp_path, commit="e" * 40, rationale="deliberately wrong"))
    with pytest.raises(PinViolation):
        gate_from_env(modem, binary, emit=False)
    # a DIFFERENT modem reading the same environment is unaffected
    other = "ardop" if modem != "ardop" else "mercury"
    assert gate_from_env(other, binary, emit=False)["unpinned"] is True


def test_override_env_is_also_per_modem(tree, tmp_path, monkeypatch):
    binary = str(tree / "thebin")
    monkeypatch.setenv("ARDOP_PIN_FILE",
                       _pin(tmp_path, commit="e" * 40, rationale="wrong on purpose"))
    monkeypatch.setenv("MERCURY_PIN_OVERRIDE", "1")     # the WRONG modem's override
    with pytest.raises(PinViolation):
        gate_from_env("ardop", binary, emit=False)
    monkeypatch.setenv("ARDOP_PIN_OVERRIDE", "1")
    assert gate_from_env("ardop", binary, emit=False)["override"] is True


def test_provenance_file_env_is_per_modem(tree, tmp_path, monkeypatch):
    out = tmp_path / "ardop_prov.json"
    monkeypatch.setenv("ARDOP_PROVENANCE_FILE", str(out))
    gate_from_env("ardop", str(tree / "thebin"), emit=False)
    assert json.loads(out.read_text())["modem"] == "ardop"


# ---------- every adapter that resolves its own executable gates ----------

ADAPTERS = [
    ("armstrong", "skywave.adapters.armstrong", "ArmstrongAdapter", "ARMSTRONG_BIN"),
    ("ardop", "skywave.adapters.ardop", "ArdopAdapter", "ARDOP_BIN"),
    ("modem73", "skywave.adapters.modem73", "Modem73Adapter", "MODEM73_BIN"),
    ("mercury", "skywave.adapters.mercury", "MercuryAdapter", "MERCURY_BIN"),
]


@pytest.mark.parametrize("modem,mod,cls_name,binenv", ADAPTERS)
def test_adapter_refuses_a_wrong_pin_at_construction(tree, tmp_path, monkeypatch,
                                                     modem, mod, cls_name, binenv):
    """Construction is where this has to be caught: it costs no airtime and leaves
    no half-started stations to tear down."""
    import importlib
    cls = getattr(importlib.import_module(mod), cls_name)
    monkeypatch.setenv(binenv, str(tree / "thebin"))
    monkeypatch.setenv(f"{modem.upper()}_PIN_FILE",
                       _pin(tmp_path, commit="e" * 40, rationale="a real reason"))
    with pytest.raises(PinViolation):
        cls(_cfg())


@pytest.mark.parametrize("modem,mod,cls_name,binenv", ADAPTERS)
def test_adapter_records_provenance_when_the_pin_matches(tree, tmp_path, monkeypatch,
                                                         modem, mod, cls_name, binenv):
    import importlib
    cls = getattr(importlib.import_module(mod), cls_name)
    head = _git(tree, "rev-parse", "HEAD")
    monkeypatch.setenv(binenv, str(tree / "thebin"))
    monkeypatch.setenv(f"{modem.upper()}_PIN_FILE",
                       _pin(tmp_path, commit=head, rationale="the build under test"))
    ad = cls(_cfg())
    assert ad.provenance["commit"] == head and ad.provenance["problems"] == []


def test_freedata_adapter_gates_on_its_checkout(tree, tmp_path, monkeypatch):
    """FreeDATA's target is $FREEDATA_DIR, not a binary -- it is resolved at import
    time, so reload the module under the patched env."""
    import importlib
    monkeypatch.setenv("FREEDATA_DIR", str(tree))
    monkeypatch.setenv("FREEDATA_PIN_FILE",
                       _pin(tmp_path, commit="e" * 40, rationale="a real reason"))
    mod = importlib.reload(importlib.import_module("skywave.adapters.freedata"))
    try:
        with pytest.raises(PinViolation):
            mod.FreedataAdapter(_cfg())
    finally:
        monkeypatch.undo()
        importlib.reload(mod)
