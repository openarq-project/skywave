"""Tests for the cell `rep_base` field (GEN2 T5 floor-pin rep escalation).

A floor claim needs >= 8 reps at the rungs bracketing the 0.5 crossing
(accepting-the-null discipline), but the T5 walk only knows WHICH rungs those
are after the base grid ran at 3 reps. `rep_base` lets the escalation
invocation ADD reps 3..7 to an existing cell: absolute rep indices keep
SEED = 1234 + rep*7 unique per rep, keep log/npstats basenames distinct from
the existing rows (r3.. vs r0-r2), and never supersede collected data.

Run:  cd skywave && python3 -m pytest tests/test_rep_base.py -q
"""
import pytest

from skywave import sweep_runner
from skywave.sweep_runner import rep_range


def test_default_reps_start_at_zero():
    assert list(rep_range({"reps": 3})) == [0, 1, 2]
    assert list(rep_range({})) == [0]


def test_rep_base_offsets_the_range():
    assert list(rep_range({"rep_base": 3, "reps": 5})) == [3, 4, 5, 6, 7]


def test_escalation_cells_do_not_collide_with_their_base_cells():
    """The basename fail-fast must accept a spec holding a cell's base reps and
    its escalation reps side by side: the ranges are disjoint by construction."""
    base = {"sigma": 12000, "watterson": "poor", "payload": 256,
            "timeout": 1300, "reps": 3, "atten_db": 16, "label": "d16"}
    esc = dict(base, rep_base=3, reps=5)
    names = set()
    for c in (base, esc):
        for rep in rep_range(c):
            key = sweep_runner._cell_basename_declared("t", "vara", c, rep)
            assert key not in names, f"collision at {key}"
            names.add(key)
    assert len(names) == 8


def test_negative_rep_base_is_rejected():
    with pytest.raises(SystemExit):
        sweep_runner.validate_rep_base({"rep_base": -1, "reps": 3}, 0, "spec")


def test_non_integer_rep_base_is_rejected():
    with pytest.raises(SystemExit):
        sweep_runner.validate_rep_base({"rep_base": "three"}, 0, "spec")


def test_valid_rep_base_passes():
    sweep_runner.validate_rep_base({"rep_base": 3, "reps": 5}, 0, "spec")
    sweep_runner.validate_rep_base({}, 0, "spec")
