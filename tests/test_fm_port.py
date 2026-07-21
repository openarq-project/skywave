"""skywave's FM port: FM port profiles + squelch gate behind the harness
(the FM port design; fm_rig.py). Verifies the FM stages through
Link.process() itself — the same technique as the fade/BPF stage tests — plus
the off-by-default discipline (no SIM_FM_* knob => baseline bit-exact)."""
import numpy as np

from conftest import feed, interleave, load_sim, make_link, tone_block


def test_fm_off_by_default():
    cs = load_sim()
    assert cs.FM_PORT == "off"
    # no FM knob => Link runs the legacy path bit-exactly (passthrough here)
    link = make_link(cs)
    x = tone_block(cs)
    assert np.array_equal(feed(link, x), x)


def test_data9600_flat_port_is_passthrough():
    cs = load_sim(SIM_FM_PORT="data9600")
    from skywave import fm_rig
    link = make_link(cs, rig_tx=fm_rig.FmPortTx(cs.FS, "data9600"),
                     rig_rx=fm_rig.FmPortRx(cs.FS, "data9600"))
    for k in range(4):
        x = tone_block(cs, block_index=k)
        assert np.array_equal(feed(link, x), x)


def test_micspk_chain_shapes_audio():
    cs = load_sim(SIM_FM_PORT="micspk")
    from skywave import fm_rig
    link = make_link(cs, rig_tx=fm_rig.FmPortTx(cs.FS, "micspk"),
                     rig_rx=fm_rig.FmPortRx(cs.FS, "micspk"))
    # steady-state 1 kHz survives the round trip near unity; 100 Hz is crushed
    # by the voice BPF skirts (order 6 both ends)
    out1k = outlo = inp = None
    for k in range(12):
        x = tone_block(cs, amp=5000.0, freq=1000.0, block_index=k)
        out1k = feed(link, x)
        inp = x
    r1k = np.sqrt(np.mean(out1k[0::cs.NCH].astype(float) ** 2)
                  / np.mean(inp[0::cs.NCH].astype(float) ** 2))
    assert 0.5 < r1k < 1.5          # near-unity (round-trip ripple + emphasis)

    link2 = make_link(cs, rig_tx=fm_rig.FmPortTx(cs.FS, "micspk"),
                      rig_rx=fm_rig.FmPortRx(cs.FS, "micspk"))
    for k in range(12):
        x = tone_block(cs, amp=5000.0, freq=100.0, block_index=k)
        outlo = feed(link2, x)
        inp = x
    rlo = np.sqrt(np.mean(outlo[0::cs.NCH].astype(float) ** 2)
                  / np.mean(inp[0::cs.NCH].astype(float) ** 2))
    assert rlo < 0.1                # >= 20 dB down out of band


def test_squelch_clips_burst_head_under_hd_vox():
    """The S1-class head-loss mechanism: under HD the squelch carrier is the
    transmitter's rf_up; audio reaches the peer only after the attack window."""
    cs = load_sim(SIM_FM_PORT="micspk", SIM_HALF_DUPLEX=1)
    from skywave import fm_rig
    blk_ms = 1000.0 * cs.BLOCK / cs.FS
    sq = fm_rig.SquelchGate(cs.FS, cs.BLOCK, open_ms=4 * blk_ms, tone_ms=0.0)
    link = make_link(cs, squelch=sq)
    active = []
    for k in range(10):
        x = tone_block(cs, amp=5000.0, block_index=k)   # loud => VOX keys
        y = feed(link, x)
        active.append(bool(np.any(y != 0)))
    # 4 attack blocks muted (head clipped), then audio flows
    assert active == [False] * 4 + [True] * 6


def test_squelch_energy_fallback_full_duplex():
    """Full-duplex (no keying state): the gate energy-detects the carrier."""
    cs = load_sim(SIM_FM_PORT="micspk")     # HALF_DUPLEX off
    from skywave import fm_rig
    blk_ms = 1000.0 * cs.BLOCK / cs.FS
    sq = fm_rig.SquelchGate(cs.FS, cs.BLOCK, open_ms=2 * blk_ms, tone_ms=0.0,
                            thresh=800.0)
    link = make_link(cs, squelch=sq)
    active = []
    for k in range(8):
        amp = 5000.0 if 2 <= k < 6 else 0.0     # signal blocks 2..5 only
        x = tone_block(cs, amp=amp, block_index=k)
        y = feed(link, x)
        active.append(bool(np.any(y != 0)))
    # idle muted; 2 attack blocks muted; open for the rest of the burst; muted after
    assert active == [False, False, False, False, True, True, False, False]


def test_squelch_mutes_idle_noise_floor():
    """A closed squelch silences the channel noise too (that IS its job)."""
    cs = load_sim(SIM_FM_PORT="micspk", SIGMA="200")
    from skywave import fm_rig
    sq = fm_rig.SquelchGate(cs.FS, cs.BLOCK, open_ms=0.0, tone_ms=0.0,
                            thresh=800.0)
    link = make_link(cs, squelch=sq)
    x = interleave(cs, np.zeros(cs.BLOCK))      # nothing transmitted
    y = feed(link, x)
    assert not np.any(y != 0)

    # same noise floor with squelch absent is audible (control)
    link2 = make_link(cs)
    y2 = feed(link2, x)
    assert np.any(y2 != 0)
