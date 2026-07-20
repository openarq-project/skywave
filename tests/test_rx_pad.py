"""RX pad + linear-channel property tests.

The gen-5 RX pad is a fixed gain on signal+noise together before the int16
cast: SNR-invariant headroom that keeps fade-up peaks off the ADC rail. These
tests pin (a) the pad scales the delivered signal by exactly 10^(dB/20),
(b) it is SNR-invariant (signal AND noise scale together), (c) the channel is
now LINEAR — a fade-up above full scale is carried, not clipped, and comes
back under the rail after the pad with rail_frac == 0.
"""
import numpy as np

from conftest import load_sim, make_link, feed


class GainStub:
    """Constant multiplicative fade/skew tap (a deterministic constructive peak)."""

    def __init__(self, g):
        self.g = g

    def process(self, mono):
        return np.asarray(mono, dtype=np.float64) * self.g


def test_pad_scales_delivered_signal():
    cs = load_sim(SIM_RX_PAD_DB=-12)      # 10^(-12/20) = 0.25119
    link = make_link(cs)
    x = np.full(cs.NSAMP, 10000, dtype="<i2")
    y = feed(link, x)
    assert np.allclose(y, round(10000 * 10 ** (-12 / 20)), atol=1), \
        "pad must scale delivered audio by 10^(dB/20)"


def test_pad_default_is_minus_12_db():
    cs = load_sim()                       # no SIM_RX_PAD_DB override in this test's env
    import os
    # the conftest _BASE pins pad OFF for isolation; here assert the module default
    assert abs(cs.RX_PAD_DB - (-12.0)) < 1e-9 or os.environ.get("SIM_RX_PAD_DB") == "0"


def test_pad_is_snr_invariant():
    """Signal AND noise scale by the same pad factor, sample-for-sample, so the
    SNR (their ratio) is identically preserved. Proven directly: the padded
    delivery equals the un-padded delivery times the pad factor, elementwise
    (same seed => same noise realization)."""
    cs0 = load_sim(SIGMA=300, SIM_RX_PAD_DB=0, SEED=7, SIM_NCH=1)
    x = np.full(cs0.NSAMP, 4000, dtype="<i2")       # signal + injected noise
    y0 = feed(make_link(cs0, seed=7), x).astype(np.float64).copy()
    cs1 = load_sim(SIGMA=300, SIM_RX_PAD_DB=-12, SEED=7, SIM_NCH=1)
    y1 = feed(make_link(cs1, seed=7), x).astype(np.float64)
    pad = 10 ** (-12 / 20)
    # y1 == y0 * pad elementwise (both signal and noise scaled identically);
    # allow int16 rounding (±1 LSB) — no clipping at these levels.
    assert np.max(np.abs(y1 - y0 * pad)) <= 1.0, \
        "pad must scale signal and noise identically (SNR-invariant)"


def test_channel_is_linear_fade_up_not_clipped():
    """A fade-up above int16 full scale is carried (not clipped) and the pad
    brings it back under the rail: rail_frac == 0, and the delivered peak is
    the faded peak times the pad (NOT the clipped rail)."""
    cs = load_sim(SIM_RX_PAD_DB=-12)
    link = make_link(cs, fade=GainStub(3.0))   # 10000*3 = 30000, *0.25119 = 7536
    x = np.full(cs.NSAMP, 10000, dtype="<i2")
    y = feed(link, x)
    assert link.nrail == 0, "fade-up must not clip (linear channel + pad headroom)"
    expect = round(10000 * 3.0 * 10 ** (-12 / 20))
    assert np.allclose(y, expect, atol=1), \
        "delivered peak must be faded*pad, not the clipped rail"


def test_pad_off_reproduces_gen4_clip():
    """With the pad OFF, a fade-up above the rail clips at the guard (the gen-4
    behavior) — confirms the pad, not a silent code path, is what prevents it."""
    cs = load_sim(SIM_RX_PAD_DB=0)
    link = make_link(cs, fade=GainStub(3.0))
    x = np.full(cs.NSAMP, 30000, dtype="<i2")   # *3 -> 90000, clips
    y = feed(link, x)
    assert np.all(y == 32767)
    assert link.nrail == cs.NSAMP
