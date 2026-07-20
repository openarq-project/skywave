"""Shared helpers for the channel-sim test suite.

channel_sim.py reads its entire configuration from the environment at import time,
so every test variant (re)loads the module through load_sim() with a controlled env.
No ALSA devices are needed: tests drive Link.process() directly, the same technique
as the pre-existing test_channel_sim_fade.py standalone check.

Run:  cd skywave && python3 -m pytest tests/ -q
"""
import importlib
import os
import sys
import threading

import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# every env key channel_sim reads (keep in sync with its header block)
_SIM_ENV = ("SIGMA", "TXGAIN", "SEED", "NP_STATS", "SIM_TXDUMP", "SIM_KEYLOG")
# physics/stage tests default to the RX pad OFF so each stage's output
# is verified in isolation (the pad is a uniform final scaling with its own
# dedicated tests in test_rx_pad.py). A test wanting the pad in-loop sets
# SIM_RX_PAD_DB explicitly.
# The four realistic defaults (BPF data, 3 ms link delay, TR 15/25)
# are ON in channel_sim; stage tests pin them NEUTRAL here for the same
# isolation reason as the RX pad. test_default_profile.py guards the shipped
# defaults; a test wanting a knob in-loop sets it explicitly.
_BASE = {"SIGMA": "0", "TXGAIN": "1.0", "SIM_NCH": "2", "SIM_BLOCK": "1024",
         "SEED": "1234", "SIM_RX_PAD_DB": "0", "SIM_RIG_BPF": "off",
         "SIM_LINK_DELAY_MS": "0", "SIM_TR_KEY_MS": "0", "SIM_TR_UNKEY_MS": "0"}


def load_sim(**env):
    """(Re)load channel_sim with base env + overrides; returns the module."""
    e = dict(_BASE)
    e.update({k: str(v) for k, v in env.items()})
    for k in list(os.environ):
        if k.startswith("SIM_") or k in _SIM_ENV:
            del os.environ[k]
    os.environ.update(e)
    import channel_sim
    return importlib.reload(channel_sim)


class FakeProc:
    stdout = None


def make_link(cs, src="a", sink="b", keys=None, ptt=None, fade=None,
              link_delay_samp=None, rig_tx=None, rig_rx=None, seed=1,
              stats_path="", fx=None, squelch=None):
    keys = keys if keys is not None else cs.Keys()
    dls = cs.LINK_DELAY_SAMP if link_delay_samp is None else link_delay_samp
    return cs.Link(f"{src}->{sink}", FakeProc(), 0, seed, stats_path,
                   threading.Event(), src, sink, keys, ptt, fade, dls,
                   rig_tx, rig_rx, fx, squelch)


def make_fx(**kw):
    """Effects bundle for direct Link construction (mirrors channel_sim.main)."""
    import types
    ns = types.SimpleNamespace(alc=None, foff=None, skew=None, agc=None,
                               imp=None, qrm=None)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def feed(link, x_int16):
    """Run one block through the link; returns the int16 output copy."""
    link.xin[:] = x_int16
    link.process()
    return link.xout.copy()


def interleave(cs, mono):
    """Broadcast a mono float/int block to the cable's NCH interleaved int16."""
    out = np.empty(len(mono) * cs.NCH, dtype="<i2")
    m = np.asarray(mono)
    for c in range(cs.NCH):
        out[c::cs.NCH] = m.astype("<i2")
    return out


def mono_blocks(cs, mono, pad=True):
    """Split a long mono signal into per-block interleaved int16 arrays."""
    n = cs.BLOCK
    if pad and len(mono) % n:
        mono = np.concatenate([mono, np.zeros(n - len(mono) % n)])
    for k in range(0, len(mono), n):
        yield interleave(cs, mono[k:k + n])


def tone_block(cs, amp=5000.0, freq=1500.0, block_index=0):
    """One interleaved block of a phase-continuous sine (continuity across blocks)."""
    n = np.arange(cs.BLOCK) + block_index * cs.BLOCK
    return interleave(cs, amp * np.sin(2 * np.pi * freq * n / cs.FS))
