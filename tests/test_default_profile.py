"""Shipped-defaults regression guard (four-defaults flip, 2026-07-13).

channel_sim ships with the realistic profile ON by default. conftest.load_sim
pins these four knobs NEUTRAL for stage-isolation tests, so this file does NOT
use load_sim: it clears the SIM_* namespace itself and reloads channel_sim to
observe the true shipped defaults. Anyone re-flipping a default (or a future
release that forgets one) fails here, not silently in a campaign.
"""
import importlib
import os

from conftest import _SIM_ENV


def test_gen7_shipped_defaults_are_realistic_profile():
    for k in list(os.environ):
        if k.startswith("SIM_") or k in _SIM_ENV:
            del os.environ[k]
    import channel_sim
    cs = importlib.reload(channel_sim)
    assert cs.RIG_BPF == "data"
    assert cs.RIG_BPF_PRESETS["data"] == (150.0, 2900.0)
    assert cs.LINK_DELAY_MS == 3.0
    assert cs.TR_KEY_MS == 15.0
    assert cs.TR_UNKEY_MS == 25.0


def test_rig_gen_is_7():
    from rig_version import RIG_GEN
    assert RIG_GEN == 7
