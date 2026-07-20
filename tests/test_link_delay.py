"""Link (propagation) delay pins.

History: the HD deliver gate originally evaluated rf_up at "now" while the signal
left the transmitter LINK_DELAY_MS earlier; the misalignment was masked by the PTT
hangtime tail for delays <= HANG_MS (~80 ms) and clipped the burst tail beyond it
(pinned here as a strict xfail while it stood). FIXED for the virtual-rig stage-3
latency calibration, which inserts the measured ~144 ms real-rig audio-pipeline
latency (> HANG_MS): the gate's TX side is now rf_up FIFO-delayed by the delay's
whole blocks (TX gated at transmit time, RX rx_ready at arrival = now). Both
regimes are pinned below as plain passes, plus the stage-3 delay itself.
"""
import numpy as np

from conftest import load_sim, make_link, feed, tone_block


def test_full_duplex_delay_is_exact_shift():
    cs = load_sim(SIM_LINK_DELAY_MS=30)          # 1440 samples > one block
    d = cs.LINK_DELAY_SAMP
    assert d == int(round(0.030 * cs.FS))
    link = make_link(cs)
    imp = np.zeros(cs.NSAMP, dtype="<i2")
    imp[10 * cs.NCH] = 6000                      # ch-0 impulse at sample 10
    zeros = np.zeros(cs.NSAMP, dtype="<i2")
    out = [feed(link, imp)] + [feed(link, zeros) for _ in range(3)]
    ch0 = np.concatenate([o[0::cs.NCH].astype(int) for o in out])
    pos = int(np.argmax(np.abs(ch0)))
    assert pos == 10 + d
    assert np.count_nonzero(ch0) == 1


def _burst_energy_ratio(delay_ms):
    cs = load_sim(SIM_HALF_DUPLEX=1, SIM_PTT=1, SIM_LINK_DELAY_MS=delay_ms)
    keys, ptt = cs.Keys(), cs.PttState()
    ab = make_link(cs, "a", "b", keys=keys, ptt=ptt)
    ba = make_link(cs, "b", "a", keys=keys, ptt=ptt)
    silence = np.zeros(cs.NSAMP, dtype="<i2")
    feed(ba, silence)
    nblk = 6
    sent = got = 0.0
    ptt.a = True
    for k in range(nblk):
        t = tone_block(cs, block_index=k)
        sent += float(np.dot(t.astype(float), t.astype(float)))
        y = feed(ab, t).astype(float)
        got += float(np.dot(y, y))
    ptt.a = False
    delay_blocks = int(np.ceil(cs.LINK_DELAY_SAMP / cs.BLOCK))
    for _ in range(cs.HANG_BLOCKS + delay_blocks + 4):
        y = feed(ab, silence).astype(float)
        got += float(np.dot(y, y))
    return got / sent


def test_hd_delay_within_hangtime_preserves_burst():
    """Delays <= HANG_MS lose no energy (the pre-fix regime, still exact)."""
    ratio = _burst_energy_ratio(30)
    assert ratio > 0.98, f"energy ratio {ratio:.3f} at 30 ms delay"


def test_hd_delay_beyond_hangtime_preserves_burst():
    """Delays > HANG_MS lose no energy now that the gate's TX side is evaluated
    at transmit time (was a strict xfail before the rf_up FIFO fix)."""
    ratio = _burst_energy_ratio(150)             # > 80 ms hangtime
    assert ratio > 0.98, f"energy ratio {ratio:.3f} at 150 ms delay"


def test_hd_stage3_calibration_delay_preserves_burst():
    """A realistic ~144 ms audio-pipeline latency (a measured soundcard-loopback
    turnaround) must pass through the HD gate without clipping burst energy."""
    ratio = _burst_energy_ratio(144)
    assert ratio > 0.98, f"energy ratio {ratio:.3f} at 144 ms delay"


def test_hd_delay_does_not_leak_pre_key_signal():
    """Alignment sanity in the other direction: with the TX gate delayed, a
    block transmitted while UNKEYED must still never reach the receiver, even
    though the gate FIFO is primed False (gate leads the signal by the
    sub-block residual only, never trails into pre-key leakage)."""
    cs = load_sim(SIM_HALF_DUPLEX=1, SIM_PTT=1, SIM_LINK_DELAY_MS=144)
    keys, ptt = cs.Keys(), cs.PttState()
    ab = make_link(cs, "a", "b", keys=keys, ptt=ptt)
    ba = make_link(cs, "b", "a", keys=keys, ptt=ptt)
    silence = np.zeros(cs.NSAMP, dtype="<i2")
    feed(ba, silence)
    got = 0.0
    # PTT never asserted: nothing may come out, however long we wait.
    for k in range(20):
        y = feed(ab, tone_block(cs, block_index=k)).astype(float)
        got += float(np.dot(y, y))
    assert got == 0.0, f"unkeyed leakage energy {got:.1f}"
