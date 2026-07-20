"""SIM_TR_JITTER_MS — seeded per-keydown T/R-latency jitter. Guards:

- OFF (default): no jitter stream is constructed; the edge logic uses the
  fixed TR_*_BLOCKS constants — the baseline stays bit-exact.
- ON: draws are per-edge, clamped >= 0, block-quantized, and
  SEED-DETERMINISTIC per direction (paired-seed A/Bs see identical jitter).
- The jitter stream is separate from the noise stream (`self.rng` draw
  sequence untouched).
"""
import threading
import types

import numpy as np
from conftest import load_sim


def make_link(cs, seed=1234):
    return cs.Link("t", types.SimpleNamespace(stdout=None), -1, seed,
                   None, threading.Event(), "a", "b", cs.Keys())


def drive_edges(cs, link, n_keydowns):
    """Feed alternating key-on / key-off blocks through _update_key (VOX
    path: a loud block keys, silence + expired hangtime unkeys) and collect
    the per-edge (rf_settle, rx_recover) draws."""
    loud = np.full(cs.NSAMP, 20000.0)
    quiet = np.zeros(cs.NSAMP)
    settles, recovers = [], []
    for _ in range(n_keydowns):
        link._update_key(loud)
        # rf_settle is read POST-decrement (the edge block itself counts:
        # gates evaluate before the countdown — the documented one-block
        # ordering in _update_key), so captured values are draw - 1.
        settles.append(link.rf_settle)
        # silence until the hangtime tail expires and the falling edge fires
        for _ in range(cs.HANG_BLOCKS + 2):
            link._update_key(quiet)
        recovers.append(link.rx_recover if link.rx_recover else 0)
    return settles, recovers


def test_off_is_bitexact_constants():
    cs = load_sim(SIM_TR_KEY_MS=60, SIM_TR_UNKEY_MS=90)
    link = make_link(cs)
    assert link.trj is None, "OFF: no jitter stream constructed"
    settles, _ = drive_edges(cs, link, 4)
    assert settles == [max(0, cs.TR_KEY_BLOCKS - 1)] * 4, \
        "OFF: fixed constant every edge (post-decrement view)"


def test_on_draws_vary_and_are_seed_deterministic():
    mk = lambda: load_sim(SIM_TR_KEY_MS=60, SIM_TR_UNKEY_MS=90,
                          SIM_TR_JITTER_MS=40)
    cs = mk()
    a = drive_edges(cs, make_link(cs, seed=1234), 12)
    assert len(set(a[0])) > 1, "ON: per-edge draws must vary"
    cs2 = mk()
    b = drive_edges(cs2, make_link(cs2, seed=1234), 12)
    assert a == b, "same seed => identical jitter sequence (paired A/B)"
    cs3 = mk()
    c = drive_edges(cs3, make_link(cs3, seed=5678), 12)
    assert a != c, "different seed => different jitter sequence"


def test_clamped_nonnegative_and_quantized():
    # nominal 5 ms with ±40 ms jitter swings well below zero: every draw
    # must clamp at 0 blocks, never negative.
    cs = load_sim(SIM_TR_KEY_MS=5, SIM_TR_UNKEY_MS=5, SIM_TR_JITTER_MS=40)
    link = make_link(cs)
    vals = [link._tr_blocks(cs.TR_KEY_MS) for _ in range(200)]
    assert min(vals) >= 0
    assert all(isinstance(v, int) for v in vals)
    # block quantization: values bounded by (5+40)ms / BLOCK_MS
    assert max(vals) <= int(round(45 / cs.BLOCK_MS))


def test_noise_stream_untouched_by_jitter():
    # The jitter stream must not perturb the noise RNG sequence: with the
    # same seed, the first noise block is identical with jitter on and off.
    outs = []
    for j in (0, 40):
        cs = load_sim(SIGMA=100, SIM_TR_KEY_MS=15, SIM_TR_UNKEY_MS=25,
                      SIM_TR_JITTER_MS=j)
        link = make_link(cs)
        out = np.empty(cs.NSAMP)
        link._fill_noise(out)
        outs.append(out.copy())
    assert np.array_equal(outs[0], outs[1]), \
        "noise draw sequence must be independent of the jitter stream"
