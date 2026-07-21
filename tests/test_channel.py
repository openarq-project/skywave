"""Tests for the in-process Channel object.

The two load-bearing tests: (1) BYTE-IDENTITY -- a Channel with no fade/rig/fx delivers
samples bit-for-bit identical to the proven bare Link (make_link) the rest of the suite
trusts, so the in-process API is the real channel, not an approximation; (2) EFFECTS ARE
WIRED -- a config that asks for a fade/ALC/rig actually builds those objects (guards
against the reduced-fidelity trap this API explicitly rejects). Plus direction wiring,
config-error raising, and block-size validation.

Channel mutates process-global state (reloads channel_sim), so the autouse fixture
restores os.environ and reloads a clean module afterward.

Run:  cd skywave && python3 -m pytest tests/test_channel.py -q
"""
import importlib
import os

import numpy as np
import pytest

from conftest import make_link, feed, tone_block
from skywave import channel_sim
from skywave.channel import Channel, ChannelConfigError
from skywave.channel_config import ChannelConfig


@pytest.fixture(autouse=True)
def _restore():
    snap = dict(os.environ)
    yield
    for k in list(os.environ):
        if k not in snap:
            del os.environ[k]
    os.environ.update(snap)
    importlib.reload(channel_sim)          # leave a module consistent with the ambient env


# --- basic: runs, right shape, deterministic ------------------------------

def test_channel_runs_shape_and_deterministic():
    cfg = ChannelConfig(sigma=200, seed=5)
    ch = Channel(cfg)
    blk = tone_block(ch._cs)
    assert len(blk) == ch.nsamp
    out = ch.process(blk)
    assert out.dtype == np.dtype("<i2") and len(out) == ch.nsamp
    assert not np.array_equal(out, blk)              # the channel changed the signal
    # same config -> identical output (even across the reload churn)
    ch2 = Channel(ChannelConfig(sigma=200, seed=5))
    assert np.array_equal(ch2.process(tone_block(ch2._cs)), out)


# --- (1) BYTE-IDENTITY to the proven bare Link ----------------------------

def test_channel_byte_identical_to_bare_link():
    # No fade/rig/fx and neutral timing -> Channel's A->B Link must equal a bare make_link
    # Link (what 268 other tests trust) built on the same reloaded module, same seed.
    cfg = ChannelConfig(sigma=200, gain=1.0, seed=5, rig_bpf="off",
                        link_delay_ms=0.0, tr_key_ms=0.0, tr_unkey_ms=0.0)
    ch = Channel(cfg)
    cs = ch._cs
    ref = make_link(cs, seed=cs.SEED + 11)          # bare: gain=GAIN, sigma=SIGMA, no fx
    blk = tone_block(cs)
    assert np.array_equal(ch.process(blk), feed(ref, blk))


# --- (2) EFFECTS ARE WIRED (the anti-trap) --------------------------------

def test_channel_effects_are_actually_built():
    # a fade config yields a real fade object on the Link (not silently dropped)
    assert Channel(ChannelConfig(watterson="poor"))._link.fade is not None
    # an ALC config yields a real fx.alc
    assert Channel(ChannelConfig(alc_db=3.0, sigma=100))._link.fx.alc is not None
    # a rig-BPF config yields real rig filters
    ch = Channel(ChannelConfig(rig_bpf="data"))
    assert ch._link.rig_tx is not None and ch._link.rig_rx is not None
    # off -> genuinely absent
    off = Channel(ChannelConfig(watterson="off", rig_bpf="off"))
    assert off._link.fade is None and off._link.rig_rx is None


# --- direction wiring: A->B uses the A-side levels -------------------------

def test_channel_uses_A_side_levels():
    ch = Channel(ChannelConfig(sigma=200, gain=1.0, gain_a=1.2, gain_b=0.8,
                               sigma_ab=200.0, sigma_ba=9000.0))
    assert ch._link.gain == 1.2               # GAIN_A, not GAIN_B
    assert ch._link.sigma == 200.0            # SIGMA_AB, not SIGMA_BA


# --- config errors surface as ChannelConfigError --------------------------

def test_channel_bad_preset_raises():
    with pytest.raises(ChannelConfigError):
        Channel(ChannelConfig(watterson="not-a-real-preset"))
    # QRM without SIGMA>0 is a config error too (the builder returns 2)
    with pytest.raises(ChannelConfigError):
        Channel(ChannelConfig(qrm_occ=0.3, sigma=0.0))


def test_channel_wrong_block_size_raises():
    ch = Channel(ChannelConfig(sigma=100))
    with pytest.raises(ValueError):
        ch.process(np.zeros(ch.nsamp - 2, dtype="<i2"))
