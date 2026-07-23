"""Half-duplex gate honesty + PTT/T/R timing invariants.

Pins the 2026-07-09 bug class at the sim layer: PTT edges must take effect on the
very next block (relay timeliness), a keyed station must hear NOTHING (zero
crosstalk), and the SIM_TR_* T/R-latency knobs must cost exactly the configured
number of blocks (the decrement-before-gate off-by-one made a 1-block setting a
no-op; fixed 2026-07-10).
"""
import os

import numpy as np

from conftest import load_sim, make_link, feed, tone_block


def hd_pair(cs):
    """Two directions sharing keys+ptt, wired like channel_sim.main()."""
    keys = cs.Keys()
    ptt = cs.PttState()
    ab = make_link(cs, "a", "b", keys=keys, ptt=ptt)
    ba = make_link(cs, "b", "a", keys=keys, ptt=ptt)
    return ab, ba, keys, ptt


def test_ptt_edge_applies_next_block():
    cs = load_sim(SIM_HALF_DUPLEX=1, SIM_PTT=1)
    ab, ba, keys, ptt = hd_pair(cs)
    silence = np.zeros(cs.NSAMP, dtype="<i2")
    tone = tone_block(cs)
    feed(ba, silence)                       # publish b idle (rx_ready)
    # not keyed: nothing delivered
    assert not feed(ab, tone).any()
    # key ON -> the immediately following block is delivered in full (SIGMA=0, gain 1)
    ptt.a = True
    assert np.array_equal(feed(ab, tone), tone)
    # key OFF -> hangtime holds rf_up, but 'active' drops immediately (peer not deaf);
    # after the hangtime tail expires the channel goes silent again. NOTE the tail is
    # HANG_BLOCKS+1 blocks by construction (hang decrements to 0, keyed clears on the
    # NEXT block) — pinned as-is; ±1 block on an ~80 ms tuning knob is immaterial.
    ptt.a = False
    for _ in range(cs.HANG_BLOCKS + 1):
        feed(ab, silence)
    assert not ab.keyed
    assert not feed(ab, tone).any()


def test_receiver_deaf_only_while_emitting():
    """Zero crosstalk while keyed — and deafness ends with 'active', not hangtime."""
    cs = load_sim(SIM_HALF_DUPLEX=1, SIM_PTT=1)
    ab, ba, keys, ptt = hd_pair(cs)
    silence = np.zeros(cs.NSAMP, dtype="<i2")
    tone = tone_block(cs)
    ptt.a = True
    ptt.b = True
    feed(ba, tone)                          # b emitting -> publishes b not rx_ready
    y = feed(ab, tone)                      # a's signal must NOT reach the busy b
    assert not y.any(), "crosstalk while receiver keyed"
    # collision is symmetric: a is also emitting, so b->a is squelched too
    assert not feed(ba, tone).any()
    # b unkeys -> next block b is rx_ready again (TR_UNKEY=0) and delivery resumes
    ptt.b = False
    feed(ba, silence)
    assert np.array_equal(feed(ab, tone), tone)


def test_vox_keying_and_hangtime_bridge():
    cs = load_sim(SIM_HALF_DUPLEX=1, SIM_HANG_MS=80)
    ab, ba, keys, ptt = hd_pair(cs)
    ab.ptt = None                            # VOX mode reads RMS, not PttState
    silence = np.zeros(cs.NSAMP, dtype="<i2")
    tone = tone_block(cs)                    # amp 5000 -> RMS ~3535 > KEY_THRESH 800
    feed(ba, silence)
    assert np.array_equal(feed(ab, tone), tone), "VOX must key on hot signal"
    assert ab.active and ab.keyed
    # intra-burst gap: silence keeps 'keyed' (hangtime) but drops 'active'
    feed(ab, silence)
    assert ab.keyed and not ab.active
    for _ in range(cs.HANG_BLOCKS):
        feed(ab, silence)
    assert not ab.keyed, "hangtime must expire after HANG_BLOCKS+1 silent blocks"


def test_tr_key_settle_costs_exact_blocks():
    """SIM_TR_KEY_MS: the burst head is squelched for exactly ceil(ms/block) blocks."""
    blocks = 2
    cs = load_sim(SIM_HALF_DUPLEX=1, SIM_PTT=1,
                  SIM_TR_KEY_MS=blocks * (1000.0 * 1024 / 48000))
    assert cs.TR_KEY_BLOCKS == blocks
    ab, ba, keys, ptt = hd_pair(cs)
    silence = np.zeros(cs.NSAMP, dtype="<i2")
    tone = tone_block(cs)
    feed(ba, silence)
    ptt.a = True
    for i in range(blocks):
        assert not feed(ab, tone).any(), f"block {i} must still be in key settle"
    assert np.array_equal(feed(ab, tone), tone), "RF must be up after the settle"


def test_tr_unkey_recovery_costs_exact_blocks():
    """SIM_TR_UNKEY_MS: the station is deaf for exactly N blocks after ITS key-up ends."""
    blocks = 2
    cs = load_sim(SIM_HALF_DUPLEX=1, SIM_PTT=1,
                  SIM_TR_UNKEY_MS=blocks * (1000.0 * 1024 / 48000))
    assert cs.TR_UNKEY_BLOCKS == blocks
    ab, ba, keys, ptt = hd_pair(cs)
    silence = np.zeros(cs.NSAMP, dtype="<i2")
    tone = tone_block(cs)
    # a transmits one burst, then unkeys; b starts transmitting immediately
    ptt.a = True
    feed(ab, tone)
    ptt.a = False
    ptt.b = True
    for i in range(blocks):
        feed(ab, silence)                    # advances a's recovery countdown
        assert not feed(ba, tone).any(), f"a must still be deaf in recovery block {i}"
    feed(ab, silence)
    assert np.array_equal(feed(ba, tone), tone), "a must hear again after recovery"


def test_keylog_written_under_sim_ptt(tmp_path):
    """SIM_KEYLOG must record wall-anchored key edges in PTT mode too."""
    import time
    klog = str(tmp_path / "keylog")
    cs = load_sim(SIM_HALF_DUPLEX=1, SIM_PTT=1, SIM_KEYLOG=klog)
    ab, ba, keys, ptt = hd_pair(cs)
    tone = tone_block(cs)
    silence = np.zeros(cs.NSAMP, dtype="<i2")
    t0 = time.time()
    ptt.a = True
    feed(ab, tone)
    ptt.a = False
    for _ in range(cs.HANG_BLOCKS + 1):
        feed(ab, silence)
    lines = open(klog + ".a").read().splitlines()
    assert len(lines) >= 2, f"expected on+off edges, got {lines}"
    for ln in lines:
        ts = float(ln.split()[0])
        assert abs(ts - t0) < 60.0, "keylog timestamps must be wall-anchored epoch"
    assert lines[0].split()[1] == "1" and lines[-1].split()[1] == "0"


def test_key_bursts_counts_bursts_not_blocks():
    """key_bursts counts keyed RISING edges (TX bursts / T-R switches): a gap
    shorter than the hangtime is bridged (same burst); a gap past the tail
    starts a new one. Motivated by the 2026-07-23 qrm-cw forensics: npstats
    carried key_duty (aggregate airtime) but no burst count, so ARQ-cycle
    excursions had to be inferred from keyed-seconds quantization."""
    cs = load_sim(SIM_HALF_DUPLEX=1, SIM_PTT=1)
    ab, ba, keys, ptt = hd_pair(cs)
    silence = np.zeros(cs.NSAMP, dtype="<i2")
    tone = tone_block(cs)
    assert ab.key_bursts == 0
    ptt.a = True
    for _ in range(3):
        feed(ab, tone)                        # burst 1: three keyed blocks, ONE burst
    ptt.a = False
    feed(ab, silence)                         # intra-burst gap << hangtime: bridged
    ptt.a = True
    for _ in range(2):
        feed(ab, tone)                        # still burst 1
    assert ab.key_bursts == 1
    ptt.a = False
    for _ in range(cs.HANG_BLOCKS + 1):
        feed(ab, silence)                     # tail expires -> unkeyed
    assert not ab.keyed
    ptt.a = True
    feed(ab, tone)                            # burst 2
    assert ab.key_bursts == 2
    assert ba.key_bursts == 0                 # per-direction, not shared


def test_key_bursts_in_npstats(tmp_path):
    """write_stats emits key_bursts alongside key_duty."""
    import json
    cs = load_sim(SIM_HALF_DUPLEX=1, SIM_PTT=1)
    keys, ptt = cs.Keys(), cs.PttState()
    ab = make_link(cs, "a", "b", keys=keys, ptt=ptt,
                   stats_path=str(tmp_path / "np.11"))
    ba = make_link(cs, "b", "a", keys=keys, ptt=ptt)
    silence = np.zeros(cs.NSAMP, dtype="<i2")
    feed(ba, silence)                         # publish b idle (rx_ready)
    ptt.a = True
    feed(ab, tone_block(cs))
    ab.nblocks += 1                           # feed() drives process() directly; the
                                              # run loop owns this counter (key_duty denom)
    ab.write_stats()
    stats = json.load(open(str(tmp_path / "np.11")))
    assert stats["key_bursts"] == 1
    assert stats["key_duty"] > 0
