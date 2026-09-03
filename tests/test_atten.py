"""SIM_ATTEN_DB: the FRINGE-campaign path-loss knob (FRINGE-CAMPAIGN-HANDOFF-2026-07-26.md
Sec 3, openarq/reviews/bakeoff). SIGMA alone cannot reach deep-fringe SNRs (a large SIGMA
just clips the int16 sum instead of lowering SNR further), so ATTEN scales the signal
itself, after fade/foff/skew and before the delay line + AWGN.

The correctness trap the handoff calls out explicitly: the TX-stats accumulator (feeding
act_rms, hence snr3k) runs in tx_shape(), BEFORE deliver_block's ATTEN stage -- so a
consumer of act_rms/SIGMA must subtract SIM_ATTEN_DB or its derived SNR reads high by
exactly the attenuation. This pins both halves: the physics (channel_sim actually
attenuates by the requested dB) and the bookkeeping (sweep_runner actually corrects for
it), so a regression in either direction fails loudly instead of quietly re-flattering
every FRINGE-campaign row.

Run:  cd skywave && python3 -m pytest tests/test_atten.py -q
"""
from conftest import load_sim, make_link, feed, tone_block


def _rms(cs, atten_db):
    link = make_link(cs)
    outs = [feed(link, tone_block(cs, block_index=k)) for k in range(4)]
    ss = sum(float((o.astype("float64") ** 2).sum()) for o in outs)
    n = sum(o.size for o in outs)
    return (ss / n) ** 0.5


def test_atten_db_zero_is_exact_passthrough():
    cs = load_sim(SIGMA="0")
    assert cs.ATTEN_DB == 0.0 and cs.ATTEN == 1.0


def test_atten_db_moves_delivered_rms_by_exactly_its_value():
    # Same synthetic signal, same seed, ATTEN_DB=0 vs 12 -- the ratio must be 12.00 dB
    # within float tolerance (linear chain, no clip at these levels).
    import math
    r0 = _rms(load_sim(SIGMA="0", SIM_ATTEN_DB="0"), 0)
    r12 = _rms(load_sim(SIGMA="0", SIM_ATTEN_DB="12"), 12)
    assert abs(20 * math.log10(r0 / r12) - 12.0) < 0.05


def test_atten_leaves_txgain_and_calibration_untouched():
    # ATTEN must not be foldable into TXGAIN's role: it is a channel/path-loss property,
    # not a per-station drive property, so GAIN stays whatever TXGAIN says regardless of
    # SIM_ATTEN_DB (equal-PEP fairness -- every modem takes the same channel dB).
    cs = load_sim(TXGAIN="1.6948", SIM_ATTEN_DB="12")
    assert cs.GAIN == 1.6948 and cs.ATTEN_DB == 12.0


def test_sweep_runner_snr3k_correction_matches_the_attenuation():
    # The bookkeeping half: sweep_runner must subtract SIM_ATTEN_DB from the reported
    # snr3k, because act_rms (the TX-stats accumulator) is measured pre-ATTEN and would
    # otherwise overstate delivered SNR by exactly this amount.
    from skywave.sweep_runner import snr3k_measured
    act_rms, sigma, atten_db = 8198.0, 1000.0, 12.0
    base = snr3k_measured(act_rms, sigma)
    corrected = round(base - atten_db, 1)
    assert round(base - corrected, 1) == atten_db


# ---- SIM_ATTEN_SCHEDULE: the stepped path loss (FM cell B2.1's wrongness arm) ----

def _block_rms(o):
    return float((o.astype("float64") ** 2).mean()) ** 0.5


def test_atten_schedule_steps_at_the_boundary_and_holds(capsys):
    import math
    cs = load_sim(SIGMA="0", SIM_ATTEN_SCHEDULE="0:{:.6f},6:0".format(3 * 1024 / 48000))
    assert cs.ATTEN_SCHEDULE == [(0.0, 3 * 1024 / 48000), (6.0, 0.0)]
    link = make_link(cs)
    outs = []
    for k in range(6):
        link.nblocks = k                 # run()/lockstep advance this after each block
        outs.append(feed(link, tone_block(cs, block_index=k)))
    r = [_block_rms(o) for o in outs]
    # Blocks 0..2 are the clean lead (equal RMS), blocks 3.. are 6.00 dB down and hold.
    assert abs(20 * math.log10(r[0] / r[1])) < 0.05
    assert abs(20 * math.log10(r[0] / r[3]) - 6.0) < 0.05
    assert abs(20 * math.log10(r[3] / r[5])) < 0.05
    err = capsys.readouterr().err
    assert "[atten-schedule" in err and "0 -> 6" in err, err


def test_atten_schedule_first_segment_is_the_static_value():
    cs = load_sim(SIGMA="0", SIM_ATTEN_SCHEDULE="12:0")
    link = make_link(cs)
    r12 = _block_rms(feed(link, tone_block(cs)))
    cs0 = load_sim(SIGMA="0", SIM_ATTEN_DB="12")
    r_static = _block_rms(feed(make_link(cs0), tone_block(cs0)))
    assert abs(r12 - r_static) < 1e-6


def test_atten_schedule_rejects_a_static_atten_alongside_and_bad_tokens():
    import pytest
    with pytest.raises(SystemExit):
        load_sim(SIGMA="0", SIM_ATTEN_SCHEDULE="0:10,6:0", SIM_ATTEN_DB="3")
    with pytest.raises(SystemExit):
        load_sim(SIGMA="0", SIM_ATTEN_SCHEDULE="0:10,:0")
    load_sim(SIGMA="0")  # leave the module static for the other tests


def test_sweep_row_stamps_the_hold_value_of_a_schedule():
    from skywave.channel_sim import parse_atten_schedule
    assert parse_atten_schedule("0:40,6:0")[-1][0] == 6.0
    assert parse_atten_schedule("6:40,0:0")[-1][0] == 0.0
