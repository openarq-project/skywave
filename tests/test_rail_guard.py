"""Post-channel rail guard + rail_frac diagnostic.

The fade stage multiplies AFTER the PEP clip; with the equal-PEP calibration
targeting 0 dBFS a constructive Watterson peak exceeds int16 full scale and is
hard-clipped at the rail — the distortion that collapsed sigma=0 fading cells
at the applied calibration gain. These tests pin (a) the rail_frac counter
that makes that exposure visible, (b) clip-not-wrap on the final cast for
stages without their own clip (clock skew), and (c) bit-exactness of the
final guard for in-rail audio.
"""
import json

import numpy as np

from conftest import load_sim, make_link, make_fx, feed


class GainStub:
    """Minimal fade/skew stand-in: constant multiplicative tap (constructive peak)."""

    def __init__(self, g):
        self.g = g

    def process(self, mono):
        return np.asarray(mono, dtype=np.float64) * self.g


def test_fade_rail_hit_counts_and_clips():
    cs = load_sim()
    link = make_link(cs, fade=GainStub(1.5))
    x = np.full(cs.NSAMP, 30000, dtype="<i2")      # *1.5 post-fade -> 45000
    y = feed(link, x)
    assert np.all(y == 32767), "fade overflow must clip at the rail"
    assert link.nrail == cs.NSAMP


def test_skew_overflow_clips_not_wraps():
    """Skew has no per-stage clip; pre-guard the unsafe cast WRAPPED here."""
    cs = load_sim()
    link = make_link(cs, fx=make_fx(skew=GainStub(1.5)))
    x = np.full(cs.NSAMP, 30000, dtype="<i2")
    y = feed(link, x)
    assert np.all(y == 32767), "skew overflow must clip at the rail (wrapped?)"
    assert link.nrail == cs.NSAMP


def test_final_guard_bit_exact_in_rail():
    cs = load_sim()
    link = make_link(cs)
    x = np.linspace(-32000, 32000, cs.NSAMP).astype("<i2")
    y = feed(link, x)
    assert np.array_equal(y, x), "in-rail audio must pass bit-exact"
    assert link.nrail == 0


def test_rail_frac_reported_in_stats(tmp_path):
    stats = str(tmp_path / "np_stats.json")
    cs = load_sim()
    link = make_link(cs, fade=GainStub(2.0), stats_path=stats)
    x = np.full(cs.NSAMP, 20000, dtype="<i2")      # *2.0 -> 40000, all rail hits
    feed(link, x)
    link.write_stats()
    d = json.load(open(stats))
    assert d["rail_frac"] == 1.0


def test_active_clipping_warning_fires_once(capsys):
    """rail_frac over the threshold prints codec2-`ch`-style WARNING to stderr,
    once per direction, independent of NP_STATS."""
    cs = load_sim()
    link = make_link(cs, fade=GainStub(2.0), stats_path="")   # no NP_STATS
    feed(link, np.full(cs.NSAMP, 20000, dtype="<i2"))
    link.write_stats()
    err = capsys.readouterr().err
    assert "WARNING output clipping" in err and "b" in err  # direction name
    link.write_stats()                                        # second call
    assert "WARNING output clipping" not in capsys.readouterr().err  # once only


def test_no_warning_when_in_rail(capsys):
    cs = load_sim()
    link = make_link(cs, stats_path="")
    feed(link, np.linspace(-30000, 30000, cs.NSAMP).astype("<i2"))
    link.write_stats()
    assert "WARNING output clipping" not in capsys.readouterr().err
