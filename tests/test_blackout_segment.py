"""Tests for the `blackout` fade-schedule segment (GEN2 stall-wrongness cell).

The GEN2 design's §3.3 wrongness cell drives a total signal loss of
STALL_S + 60 s mid-transfer, then restores a workable channel: the modem is
silent through no fault of its own -- the exact condition the no-progress
early-out must NOT punish. The Watterson presets can't express that (their
worst case still passes signal), so `blackout` is a schedule-segment name:
zero output for its duration, entered and left through the standard
crossfade (a real channel doesn't step to silence instantaneously).
Pass-through physics are untouched when the name is never used.

Run:  cd skywave && python3 -m pytest tests/test_blackout_segment.py -q
"""
import numpy as np
import pytest

from skywave.watterson import ScheduledFade

FS = 8000


def _run(sched, seconds, block=160):
    outs = []
    blk = np.ones(block)
    for _ in range(int(seconds * FS / block)):
        outs.append(sched.process(blk.copy()))
    return np.concatenate(outs)


def test_blackout_segment_outputs_silence_between_live_segments():
    log = []
    s = ScheduledFade(FS, [("off", 2), ("blackout", 2), ("off", 0)], 10.0,
                      seed=7, xfade_s=0.5,
                      on_transition=lambda t, a, b: log.append((round(t), a, b)))
    y = _run(s, 6.0)
    mid = y[int(3.0 * FS):int(3.5 * FS)]          # deep inside the blackout
    assert np.max(np.abs(mid)) == 0.0
    before = y[int(1.0 * FS):int(1.4 * FS)]       # off segment: pass-through
    after = y[int(5.2 * FS):int(5.8 * FS)]
    assert np.allclose(before, 1.0) and np.allclose(after, 1.0)
    assert (2, "off", "blackout") in log and (4, "blackout", "off") in log


def test_blackout_edges_crossfade_not_step():
    s = ScheduledFade(FS, [("off", 1), ("blackout", 0)], 10.0, seed=7,
                      xfade_s=0.5)
    y = _run(s, 2.0)
    ramp = y[int(1.1 * FS)]                       # mid-crossfade sample
    assert 0.0 < ramp < 1.0, f"expected a blend, got {ramp}"


def test_blackout_recovery_supports_a_completing_transfer_shape():
    """The wrongness cell's whole point: silence for the window, then a
    channel the transfer can finish on -- unity pass-through after."""
    s = ScheduledFade(FS, [("off", 1), ("blackout", 1), ("off", 0)], 10.0,
                      seed=7, xfade_s=0.2)
    y = _run(s, 3.0)
    assert np.allclose(y[int(2.5 * FS):], 1.0)


def test_unknown_segment_names_still_rejected_by_channel_sim_rule():
    """channel_sim validates names against PRESETS + 'blackout'; the fade
    engine itself treats any unknown-name segment as pass-through (preset
    None), so the validation rule is the guard -- assert the contract the
    validator relies on: 'blackout' is NOT silently a Watterson preset."""
    from skywave import watterson
    assert "blackout" not in watterson.PRESETS
