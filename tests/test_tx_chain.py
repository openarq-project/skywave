"""TX-chain (gain / PA / clip) and stats pins for channel_sim."""
import json

import numpy as np

from conftest import load_sim, make_link, feed, interleave


def test_rapp_pa_formula_golden():
    cs = load_sim(SIM_PA_P=2, SIM_PA_VSAT=16000)
    link = make_link(cs)
    x = np.linspace(-30000, 30000, cs.NSAMP).astype("<i2")
    y = feed(link, x).astype(np.float64)
    xf = x.astype(np.float64)
    expect = xf / (1.0 + (np.abs(xf) / 16000.0) ** 4.0) ** 0.25
    assert np.max(np.abs(y - expect)) <= 1.0, "Rapp AM/AM curve drifted"


def test_hard_clip_and_nclip_counter():
    """PEP hard clip must count clipped samples (clip_frac was a dead 0)."""
    cs = load_sim(TXGAIN=2.0)
    link = make_link(cs, stats_path="")
    x = np.full(cs.NSAMP, 20000, dtype="<i2")     # *2.0 -> 40000, clips to 32767
    y = feed(link, x)
    assert np.all(y == 32767)
    assert link.nclip == cs.NSAMP
    # a clean block adds nothing
    x2 = np.full(cs.NSAMP, 1000, dtype="<i2")
    feed(link, x2)
    assert link.nclip == cs.NSAMP


def test_rapp_nclip_counts_past_vsat():
    cs = load_sim(SIM_PA_P=2, SIM_PA_VSAT=16000)
    link = make_link(cs)
    x = np.zeros(cs.NSAMP, dtype="<i2")
    x[:100] = 20000                                # 100 samples beyond Vsat
    feed(link, x)
    assert link.nclip == 100


def test_clip_frac_reported_in_stats(tmp_path):
    stats = str(tmp_path / "np_stats.json")
    cs = load_sim(TXGAIN=2.0)
    link = make_link(cs, stats_path=stats)
    x = np.full(cs.NSAMP, 20000, dtype="<i2")
    feed(link, x)
    link.write_stats()
    d = json.load(open(stats))
    assert d["clip_frac"] == 1.0
    assert d["gain"] == 2.0


def test_robust_peak_skips_cold_start_transient():
    cs = load_sim(SIM_STATS_SKIP_BLOCKS=8)
    link = make_link(cs)
    glitch = np.zeros(cs.NSAMP, dtype="<i2")
    glitch[0] = 30000                              # the snd-aloop cold-start spike
    feed(link, glitch)
    body = np.full(cs.NSAMP, 8000, dtype="<i2")
    for _ in range(10):
        feed(link, body)
    assert link.peak == 30000.0
    assert link.robust_peak == 8000.0, "robust peak must exclude the startup transient"


def test_act_rms_and_papr_math(tmp_path):
    """act_rms gates on ACT_THRESH and the PAPR figures follow from peak/act_rms."""
    stats = str(tmp_path / "s.json")
    cs = load_sim()
    link = make_link(cs, stats_path=stats)
    x = np.zeros(cs.NSAMP, dtype="<i2")
    x[: cs.NSAMP // 2] = 1000                      # active half; silence below thresh
    for _ in range(3):
        feed(link, x)
    link.write_stats()
    d = json.load(open(stats))
    assert abs(d["act_rms"] - 1000.0) < 1e-6
    assert abs(d["duty"] - 0.5) < 1e-9
    assert abs(d["papr_db"] - 0.0) < 1e-6          # constant-amplitude active signal
    assert d["robust_peak"] == 1000                # SKIP window smaller than 3 blocks? no:
    # robust_peak falls back to peak when the skip window wasn't exceeded — the
    # write_stats fallback (rpeak = peak when robust_peak == 0) is itself pinned here.
