"""Per-direction noise (SNR) + per-station audio drive asymmetry (2026-07-19).

The channel_sim architecture is per-direction (two Link chains); these tests cover the
new knobs that let the two directions carry DIFFERENT noise floors and drives:
  SIM_SIGMA_AB / SIM_SIGMA_BA   per-direction noise std (receiver floor)
  SIM_TXGAIN_A / SIM_TXGAIN_B   per-station TX audio drive (transmitter)
Defaults inherit the symmetric globals, so an unset knob is byte-identical to before.

Run:  cd skywave && python3 -m pytest tests/test_asymmetric_channel.py -q
"""
import os
import threading

import numpy as np
import pytest

from conftest import load_sim


@pytest.fixture(autouse=True)
def _restore_env():
    """load_sim mutates os.environ (clears SIM_* + sets conftest _BASE) and does not
    restore it. Snapshot/restore around each test so this file — which sorts before
    test_cbab.py — cannot leak SIM_TR_KEY_MS into an order-dependent downstream test."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


class _FakeProc:
    stdout = None


def _link(cs, sigma=None, gain=None, fx=None, seed=1):
    """A bare Link (no transport) for exercising per-direction methods directly."""
    return cs.Link("a->b", _FakeProc(), 0, seed, "", threading.Event(),
                   "a", "b", cs.Keys(), None, None, 0, None, None, fx, None,
                   gain=gain, sigma=sigma)


def test_env_defaults_symmetric():
    cs = load_sim(SIGMA=2000, TXGAIN=1.0)
    assert cs.SIGMA_AB == cs.SIGMA == cs.SIGMA_BA == 2000
    assert cs.GAIN_A == cs.GAIN == cs.GAIN_B == 1.0
    assert cs.ASYM is False


def test_env_per_direction_override():
    cs = load_sim(SIGMA=2000, TXGAIN=1.0, SIM_SIGMA_AB=5000, SIM_SIGMA_BA=1000,
                  SIM_TXGAIN_A=1.5, SIM_TXGAIN_B=0.5)
    assert cs.SIGMA_AB == 5000 and cs.SIGMA_BA == 1000
    assert cs.GAIN_A == 1.5 and cs.GAIN_B == 0.5
    assert cs.ASYM is True


def test_env_unset_direction_inherits_global():
    cs = load_sim(SIGMA=2000, SIM_SIGMA_AB=5000)      # only AB overridden
    assert cs.SIGMA_AB == 5000 and cs.SIGMA_BA == 2000   # BA falls back to SIGMA
    assert cs.ASYM is True


def test_per_direction_noise_level():
    """Each Link's _fill_noise uses its OWN sigma, so the two directions can differ."""
    cs = load_sim()
    hi = _link(cs, sigma=4000.0)
    lo = _link(cs, sigma=1000.0)
    b_hi = np.empty(cs.NSAMP)
    b_lo = np.empty(cs.NSAMP)
    hi._fill_noise(b_hi)
    lo._fill_noise(b_lo)
    assert np.std(b_hi) == pytest.approx(4000, rel=0.1)
    assert np.std(b_lo) == pytest.approx(1000, rel=0.1)


def test_per_station_drive():
    """Each Link's tx_shape scales by its OWN gain (per-station audio drive)."""
    cs = load_sim()                                   # PA off, HD off by default
    hot = _link(cs, gain=2.0)
    hot.xin[:] = 5000
    w_hot = hot.tx_shape()
    assert np.mean(np.abs(w_hot)) == pytest.approx(10000, rel=0.02)
    soft = _link(cs, gain=0.5)
    soft.xin[:] = 5000
    w_soft = soft.tx_shape()
    assert np.mean(np.abs(w_soft)) == pytest.approx(2500, rel=0.02)


def test_default_link_inherits_globals():
    """A Link built without gain/sigma inherits the module globals (regression guard)."""
    cs = load_sim(SIGMA=3000, TXGAIN=1.2)
    link = _link(cs)
    assert link.sigma == 3000 and link.gain == 1.2
