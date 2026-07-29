"""Provenance capture + pin gate for binary-shaped modems (mercury first).

Why this exists: nothing in skywave or the campaign driver ever clones, pulls or
builds Mercury -- the adapter just execs $MERCURY_BIN. So a box's binary is
STICKY: whatever was last built there is what every future campaign silently
measures, with no commit recorded anywhere. On 2026-07-29 that let a bench get
provisioned from a LOCAL experiment branch (42 commits ahead of upstream, 30
behind) and it would have been scored as "Mercury".

The gate makes the binary's provenance explicit, and makes the pin a decision
someone takes at campaign start -- with a stated reason -- rather than whatever
the box happens to be carrying.
"""
import json
import os
import subprocess as sp

import pytest

from skywave.modem_provenance import (
    PinViolation,
    ProvenanceError,
    capture,
    gate,
    load_pin,
)

GIT_ID = ["-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false"]


def _git(repo, *args):
    return sp.run(["git", *GIT_ID, *args], cwd=repo, check=True,
                  stdout=sp.PIPE, stderr=sp.STDOUT, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A tiny git repo with a fake 'modem binary' committed inside it."""
    r = tmp_path / "modem"
    (r / "src").mkdir(parents=True)
    (r / "src" / "main.c").write_text("int main(void){return 0;}\n")
    binary = r / "fakemodem"
    binary.write_bytes(b"\x7fELF fake binary v1")
    binary.chmod(0o755)
    _git(r, "init", "-q", "-b", "mainline")
    _git(r, "add", "src/main.c")
    _git(r, "commit", "-q", "-m", "initial")
    return r


def commit_of(repo):
    return _git(repo, "rev-parse", "HEAD")


# ---------- capture ----------

def test_capture_records_commit_and_binary_identity(repo):
    p = capture(str(repo / "fakemodem"), modem="fakemodem")
    assert p["modem"] == "fakemodem"
    assert p["commit"] == commit_of(repo)
    assert p["repo"] == os.path.realpath(str(repo))
    # the binary itself must be identified, not just the tree it came from --
    # a stale binary beside fresh sources is the exact 2026-07-20 failure.
    import hashlib
    want = hashlib.md5((repo / "fakemodem").read_bytes()).hexdigest()
    assert p["bin_md5"] == want
    assert p["bin"].endswith("fakemodem")


def test_capture_reports_branch_and_detached_state(repo):
    p = capture(str(repo / "fakemodem"), modem="m")
    assert p["branch"] == "mainline" and p["detached"] is False
    _git(repo, "checkout", "-q", "--detach", "HEAD")
    p2 = capture(str(repo / "fakemodem"), modem="m")
    assert p2["detached"] is True


def test_untracked_build_artifacts_do_not_count_as_dirty(repo):
    """An in-tree `make` leaves .o/.d files everywhere -- bench4's mercury had 112
    untracked artifacts and ZERO source drift. Counting those as dirty would make
    the gate cry wolf on every normally-built box."""
    (repo / "src" / "main.o").write_bytes(b"obj")
    (repo / "src" / "main.d").write_text("dep")
    assert capture(str(repo / "fakemodem"), modem="m")["dirty_tracked"] is False


def test_modified_tracked_source_does_count_as_dirty(repo):
    (repo / "src" / "main.c").write_text("int main(void){return 1;}\n")
    assert capture(str(repo / "fakemodem"), modem="m")["dirty_tracked"] is True


def test_capture_outside_a_repo_yields_no_commit(tmp_path):
    """bench3's ~/tools/mercury is not a git repo at all -- capture must say so
    rather than inventing provenance."""
    loose = tmp_path / "loose"
    loose.mkdir()
    b = loose / "modem"
    b.write_bytes(b"bin")
    b.chmod(0o755)
    p = capture(str(b), modem="m")
    assert p["repo"] is None and p["commit"] is None


def test_capture_missing_binary_raises(tmp_path):
    with pytest.raises(ProvenanceError):
        capture(str(tmp_path / "nope"), modem="m")


# ---------- pin file ----------

def _pin(tmp_path, **over):
    d = {"commit": "0" * 40, "rationale": "baseline upstream for the X campaign",
         "pinned_on": "2026-07-29"}
    d.update(over)
    p = tmp_path / "MERCURY_PIN.json"
    p.write_text(json.dumps(d))
    return str(p)


def test_load_pin_reads_the_record(tmp_path):
    assert load_pin(_pin(tmp_path))["commit"] == "0" * 40


def test_load_pin_rejects_malformed(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(ProvenanceError):
        load_pin(str(bad))


def test_load_pin_missing_file_raises(tmp_path):
    with pytest.raises(ProvenanceError):
        load_pin(str(tmp_path / "absent.json"))


# ---------- the gate ----------

def test_gate_passes_when_commit_matches(repo, tmp_path):
    rec = gate(str(repo / "fakemodem"), modem="m",
               pin_file=_pin(tmp_path, commit=commit_of(repo)))
    assert rec["pinned"] is True and rec["problems"] == []


def test_gate_refuses_on_commit_mismatch(repo, tmp_path):
    with pytest.raises(PinViolation) as e:
        gate(str(repo / "fakemodem"), modem="m", pin_file=_pin(tmp_path))
    assert "does not match" in str(e.value)


def test_gate_refuses_a_dirty_tracked_tree(repo, tmp_path):
    (repo / "src" / "main.c").write_text("drift\n")
    with pytest.raises(PinViolation) as e:
        gate(str(repo / "fakemodem"), modem="m",
             pin_file=_pin(tmp_path, commit=commit_of(repo)))
    assert "uncommitted" in str(e.value).lower()


def test_gate_refuses_when_there_is_no_repo(tmp_path):
    b = tmp_path / "modem"
    b.write_bytes(b"bin")
    b.chmod(0o755)
    with pytest.raises(PinViolation) as e:
        gate(str(b), modem="m", pin_file=_pin(tmp_path))
    assert "no git repository" in str(e.value).lower()


@pytest.mark.parametrize("bad", ["", "   ", "TODO", "tbd", "n/a", "-"])
def test_gate_refuses_a_pin_with_no_stated_reason(repo, tmp_path, bad):
    """The whole point of the pin is that a human decided what is being tested and
    WHY, at campaign start. A pin with a placeholder rationale is not a decision."""
    with pytest.raises(PinViolation) as e:
        gate(str(repo / "fakemodem"), modem="m",
             pin_file=_pin(tmp_path, commit=commit_of(repo), rationale=bad))
    assert "rationale" in str(e.value).lower()


def test_override_is_allowed_but_recorded(repo, tmp_path):
    """An override must never be silent -- it has to land in the record so a corpus
    can be audited after the fact."""
    rec = gate(str(repo / "fakemodem"), modem="m", pin_file=_pin(tmp_path),
               override=True)
    assert rec["override"] is True
    assert rec["problems"], "the violation must still be recorded, not erased"


def test_unpinned_run_is_allowed_but_flagged(repo):
    """An ad-hoc smoke should not need a pin; it must still say it was unpinned so
    nobody mistakes its numbers for campaign-grade ones."""
    rec = gate(str(repo / "fakemodem"), modem="m")
    assert rec["pinned"] is False and rec["unpinned"] is True
    assert rec["commit"] == commit_of(repo)


def test_gate_accepts_an_inline_pin_commit(repo):
    rec = gate(str(repo / "fakemodem"), modem="m", pin_commit=commit_of(repo),
               rationale="inline pin for a one-off A/B")
    assert rec["pinned"] is True and rec["problems"] == []


def test_record_is_json_serialisable(repo, tmp_path):
    """It gets embedded in campaign_manifest.json / emitted on stdout."""
    rec = gate(str(repo / "fakemodem"), modem="m",
               pin_file=_pin(tmp_path, commit=commit_of(repo)))
    json.loads(json.dumps(rec))
