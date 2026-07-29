#!/usr/bin/env python3
"""Provenance capture + pin gate for binary-shaped modems.

skywave never clones, pulls or builds the competitor modems -- an adapter just
execs `$<MODEM>_BIN`. That makes a box's binary **sticky**: whatever was last
built there is what every later campaign silently measures, and until now no
commit was recorded anywhere in the corpus.

On 2026-07-29 a freshly provisioned bench was built from a LOCAL experiment
branch (42 commits ahead of upstream, 30 behind) carrying our own ARQ
optimisations, and it would have been scored as stock "Mercury". Three of four
boxes turned out to disagree on which Mercury they were running, and one had no
git repository at all -- so no cross-box comparison was valid.

This module makes two things true:

  * **Provenance is always recorded.** Which repo, which commit, whether the
    tracked tree was dirty, and the md5/mtime of the actual binary.
  * **A pin is a decision, not a default.** When a pin is in force the gate
    refuses to run unless the tree matches it AND the pin states a reason. The
    person starting a campaign chooses what is being tested and why; a pin whose
    rationale is blank or `TODO` is rejected as not-a-decision.

Ad-hoc runs (a smoke, a one-off A/B) need no pin -- but the record then carries
`unpinned: True` so nobody mistakes those numbers for campaign-grade ones.
"""
import hashlib
import json
import os
import subprocess as sp
from datetime import datetime

__all__ = ["ProvenanceError", "PinViolation", "capture", "load_pin", "gate",
           "format_record"]

# Rationales that are technically non-empty but state nothing. The pin exists so a
# human records intent; these are the ways people avoid doing that.
_PLACEHOLDER_RATIONALES = {"", "-", "--", "todo", "tbd", "n/a", "na", "none",
                           "xxx", "fixme", "?", "."}


class ProvenanceError(Exception):
    """Provenance could not be established (missing binary, unreadable pin file)."""


class PinViolation(Exception):
    """The declared pin is not satisfied. Carries the record for logging."""

    def __init__(self, message, record=None):
        super().__init__(message)
        self.record = record or {}


def _git(repo, *args):
    """Run git in `repo`; return stripped stdout, or None if git fails."""
    try:
        r = sp.run(["git", *args], cwd=repo, stdout=sp.PIPE, stderr=sp.DEVNULL,
                   text=True, timeout=30)
    except (OSError, sp.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _find_repo(start):
    """Walk up from a path looking for the work tree that contains it.

    Uses `git rev-parse --show-toplevel` rather than hunting for a `.git`
    directory, so worktrees and submodules (where `.git` is a FILE) resolve
    correctly instead of silently reporting 'no repository'.
    """
    top = _git(start, "rev-parse", "--show-toplevel")
    return os.path.realpath(top) if top else None


def capture(binary, *, modem):
    """Identify `binary` and the git state of whatever tree it was built in.

    Returns a JSON-serialisable dict. `commit`/`repo` are None when the binary
    does not live in a git work tree -- that is reported, never invented.
    """
    path = os.path.realpath(os.path.expanduser(binary))
    if not os.path.isfile(path):
        raise ProvenanceError(f"{modem}: binary not found: {binary}")

    with open(path, "rb") as f:
        digest = hashlib.md5()
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)

    rec = {
        "modem": modem,
        "bin": path,
        "bin_md5": digest.hexdigest(),
        "bin_mtime": datetime.fromtimestamp(os.path.getmtime(path)).isoformat(
            timespec="seconds"),
        "repo": None, "commit": None, "describe": None,
        "branch": None, "detached": None, "dirty_tracked": None,
        "captured": datetime.now().isoformat(timespec="seconds"),
    }

    repo = _find_repo(os.path.dirname(path))
    if repo is None:
        return rec

    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    # `--untracked-files=no` is load-bearing: an in-tree `make` scatters .o/.d
    # files across the source dirs (bench4's mercury had 112 untracked artifacts
    # and zero source drift). Counting those as dirty would fail the gate on
    # every normally-built box and train people to pass --override.
    status = _git(repo, "status", "--porcelain", "--untracked-files=no")
    rec.update({
        "repo": repo,
        "commit": _git(repo, "rev-parse", "HEAD"),
        "describe": _git(repo, "describe", "--tags", "--always", "--dirty"),
        "branch": branch,
        "detached": (branch == "HEAD") if branch is not None else None,
        "dirty_tracked": bool(status) if status is not None else None,
    })
    return rec


def load_pin(pin_file):
    """Read a pin record: {commit, rationale, [tag], [pinned_on]}."""
    path = os.path.expanduser(pin_file)
    try:
        with open(path) as f:
            pin = json.load(f)
    except OSError as e:
        raise ProvenanceError(f"pin file unreadable: {path} ({e})") from e
    except ValueError as e:
        raise ProvenanceError(f"pin file is not valid JSON: {path} ({e})") from e
    if not isinstance(pin, dict):
        raise ProvenanceError(f"pin file must contain a JSON object: {path}")
    return pin


def _rationale_problem(rationale):
    text = (rationale or "").strip()
    if text.lower() in _PLACEHOLDER_RATIONALES:
        return ("pin states no rationale -- record WHY this commit is the one "
                "under test (what is being compared, and against what)")
    return None


def gate(binary, *, modem, pin_file=None, pin_commit=None, rationale=None,
         override=False):
    """Capture provenance and enforce the pin, if one is declared.

    Declare a pin with `pin_file` (a MERCURY_PIN.json-style record) or inline via
    `pin_commit` + `rationale`. With no pin the run is allowed but the record is
    marked `unpinned`.

    Returns the provenance record (with `pinned`, `pin`, `problems`, `override`).
    Raises PinViolation when the pin is unmet and `override` is falsy.
    """
    rec = capture(binary, modem=modem)

    pin = None
    if pin_file is not None:
        pin = load_pin(pin_file)
        rec["pin_file"] = os.path.expanduser(pin_file)
    elif pin_commit is not None:
        pin = {"commit": pin_commit, "rationale": rationale}

    rec["pinned"] = pin is not None
    rec["unpinned"] = pin is None
    rec["override"] = bool(override)
    rec["pin"] = pin
    rec["problems"] = problems = []

    if pin is None:
        return rec

    want = (pin.get("commit") or "").strip()
    if not want:
        problems.append("pin declares no commit")
    elif rec["commit"] is None:
        problems.append(
            f"{modem}: no git repository behind {rec['bin']} -- its provenance "
            "cannot be established, so it cannot be pinned (re-clone the tree "
            "and rebuild on the box)")
    elif not rec["commit"].startswith(want) and not want.startswith(rec["commit"]):
        problems.append(
            f"{modem}: built commit {rec['commit'][:12]} does not match the "
            f"pinned {want[:12]}"
            + (f" (tag {pin['tag']})" if pin.get("tag") else ""))

    if rec["dirty_tracked"]:
        problems.append(
            f"{modem}: the tree at {rec['repo']} has uncommitted changes to "
            "tracked files, so the pinned commit does not describe what was built")

    rp = _rationale_problem(pin.get("rationale"))
    if rp:
        problems.append(f"{modem}: {rp}")

    if problems and not override:
        raise PinViolation(
            f"{modem} pin gate FAILED:\n  - " + "\n  - ".join(problems)
            + f"\n\nSet {modem.upper()}_PIN_OVERRIDE=1 to run anyway (the "
              "violation is recorded in the results provenance).",
            record=rec)
    return rec


def format_record(rec):
    """One-line human summary for adapter/campaign logs."""
    who = rec.get("describe") or (rec.get("commit") or "no-git")[:12]
    state = "pinned" if rec.get("pinned") else "UNPINNED"
    if rec.get("override") and rec.get("problems"):
        state = "OVERRIDDEN"
    dirty = " +dirty" if rec.get("dirty_tracked") else ""
    return (f"{rec['modem']} provenance: {who}{dirty} [{state}] "
            f"bin={os.path.basename(rec['bin'])} md5={rec['bin_md5'][:12]}")
