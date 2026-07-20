"""Physics pins for the R6 realism effects (rig_effects.py) + their Link wiring."""
import math

import numpy as np
import pytest
from scipy.signal import hilbert

from conftest import load_sim, make_link, make_fx, feed, interleave
import rig_effects as fxm

FS = 48000


def dominant_freq(x, fs=FS):
    X = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return float(np.fft.rfftfreq(len(x), 1.0 / fs)[int(np.argmax(X))])


# ---------------------------------------------------------------- FreqShift
def test_freq_shift_moves_tone_and_preserves_power():
    sh = fxm.FreqShift(FS, 50.0)
    n = np.arange(FS * 2)
    x = 5000.0 * np.sin(2 * np.pi * 1000.0 * n / FS)
    y = np.concatenate([sh.process(x[k:k + 1024]) for k in range(0, len(x), 1024)])
    body = y[FS // 2:]                                   # skip the Hilbert warm-up
    assert abs(dominant_freq(body) - 1050.0) < 2.0
    assert abs(np.std(body) / np.std(x) - 1.0) < 0.02
    # negative shift too (the B->A direction)
    sh2 = fxm.FreqShift(FS, -50.0)
    y2 = np.concatenate([sh2.process(x[k:k + 1024]) for k in range(0, len(x), 1024)])
    assert abs(dominant_freq(y2[FS // 2:]) - 950.0) < 2.0


def test_freq_shift_link_wiring():
    cs = load_sim()
    fx = make_fx(foff=fxm.FreqShift(cs.FS, 25.0))
    link = make_link(cs, fx=fx)
    out = []
    for k in range(60):
        n = np.arange(cs.BLOCK) + k * cs.BLOCK
        blk = interleave(cs, 5000 * np.sin(2 * np.pi * 1500.0 * n / cs.FS))
        out.append(feed(link, blk)[0::cs.NCH].astype(float))
    y = np.concatenate(out)[cs.FS // 2:]
    assert abs(dominant_freq(y, cs.FS) - 1525.0) < 2.0


# ---------------------------------------------------------------- ClockSkew
def test_clock_skew_scales_frequency():
    ppm = 10000.0                                        # exaggerated for resolution
    sk = fxm.ClockSkew(FS, ppm)
    n = np.arange(FS * 2)
    x = 5000.0 * np.sin(2 * np.pi * 1000.0 * n / FS)
    y = np.concatenate([sk.process(x[k:k + 1024]) for k in range(0, len(x), 1024)])
    body = y[FS // 2:]
    assert abs(dominant_freq(body) - 1010.0) < 2.0       # f * (1 + ppm*1e-6)
    assert abs(np.std(body) / np.std(x[FS // 2:]) - 1.0) < 0.03


def test_clock_skew_resets_per_burst():
    """Two identical bursts separated by a long gap must come out identical —
    the resampler clock re-acquires at each burst onset (per-burst drift)."""
    sk = fxm.ClockSkew(FS, 150.0)
    n = np.arange(1024 * 8)
    burst = 5000.0 * np.sin(2 * np.pi * 1000.0 * n / FS)
    gap = np.zeros(1024)

    def run_burst():
        return np.concatenate([sk.process(burst[k:k + 1024])
                               for k in range(0, len(burst), 1024)])
    y1 = run_burst()
    for _ in range(6):                                   # > rearm_blocks of idle
        sk.process(gap)
    y2 = run_burst()
    assert np.allclose(y1, y2, atol=1e-9), "burst-onset reset failed"


# ---------------------------------------------------------------- AlcOvershoot
def test_alc_overshoot_envelope():
    alc = fxm.AlcOvershoot(FS, 6.0, settle_ms=10.0, nch=1)
    tone = 8000.0 * np.sin(2 * np.pi * 1500.0 * np.arange(1024) / FS)
    b0 = alc.process(tone.copy())
    peak0 = np.abs(b0[:100]).max()
    assert peak0 > 8000.0 * 1.85, f"onset peak {peak0:.0f} (want ~2x for +6 dB)"
    for _ in range(4):                                   # settle: 21 ms/block >> 3*tau
        last = alc.process(tone.copy())
    assert np.abs(last).max() < 8000.0 * 1.02, "steady state must be unity gain"
    # a short intra-burst gap (< rearm) must NOT retrigger the overshoot
    alc.process(np.zeros(1024))
    b = alc.process(tone.copy())
    assert np.abs(b).max() < 8000.0 * 1.02, "short gap retriggered the overshoot"
    # a long gap (>= rearm) re-arms it
    for _ in range(5):
        alc.process(np.zeros(1024))
    b = alc.process(tone.copy())
    assert np.abs(b[:100]).max() > 8000.0 * 1.85, "long gap must re-arm"


# ---------------------------------------------------------------- RxAgc
def test_rx_agc_levels_and_burst_head():
    agc = fxm.RxAgc(FS, attack_ms=2.0, release_ms=100.0, target=8000.0)
    tone = 2000.0 * np.sin(2 * np.pi * 1500.0 * np.arange(1024) / FS)
    out = None
    for _ in range(30):                                  # ~0.64 s >> release
        out = agc.process(tone.copy())
    assert abs(np.abs(out).max() - 8000.0) / 8000.0 < 0.1, "AGC must level to target"
    # long quiet: gain rides up to max on the floor
    floor = np.random.default_rng(0).standard_normal(1024) * 50.0
    for _ in range(60):
        agc.process(floor.copy())
    # burst head arrives over-amplified until the attack settles
    burst = 8000.0 * np.sin(2 * np.pi * 1500.0 * np.arange(1024) / FS)
    y = agc.process(burst.copy())
    head = np.abs(y[:64]).max()
    tail = np.abs(y[-256:]).max()
    assert head > 4.0 * tail, f"burst head {head:.0f} vs settled tail {tail:.0f}"
    assert tail < 8000.0 * 1.5, "attack must settle within the block"


# ---------------------------------------------------------------- ImpulsiveNoise
def test_impulsive_noise_hits_vd_and_power():
    sigma = 2000.0
    imp = fxm.ImpulsiveNoise(sigma, vd_db=6.0)
    rng = np.random.default_rng(9)
    out = np.empty(200000)
    imp.fill(rng, out)
    assert abs(out.std() / sigma - 1.0) < 0.03, "total power must stay sigma^2"
    env = np.abs(hilbert(out / out.std()))
    vd = 20.0 * math.log10(math.sqrt(float((env ** 2).mean())) / float(env.mean()))
    assert abs(vd - 6.0) < 1.2, f"realized Vd {vd:.2f} dB (target 6)"
    # deterministic per rng seed
    out2 = np.empty(200000)
    imp.fill(np.random.default_rng(9), out2)
    assert np.array_equal(out, out2)


def test_impulsive_noise_link_power_invariant():
    """At the Link level the SNR axis must not move: noise power == SIGMA^2."""
    cs = load_sim(SIGMA=2000)
    fx = make_fx(imp=fxm.ImpulsiveNoise(2000.0, vd_db=6.0))
    link = make_link(cs, fx=fx)
    zeros = np.zeros(cs.NSAMP, dtype="<i2")
    buf = [feed(link, zeros).astype(float) for _ in range(600)]
    y = np.concatenate(buf)
    assert abs(y.std() / 2000.0 - 1.0) < 0.05, f"link noise std {y.std():.0f}"


def test_impulsive_noise_unreachable_vd_raises():
    with pytest.raises(ValueError):
        fxm.ImpulsiveNoise(2000.0, vd_db=25.0, k_db=10.0)


# ---------------------------------------------------------------- QrmGenerator
# Occupancy/INR model.
def test_qrm_off_is_silent():
    q = fxm.QrmGenerator(FS, np.random.default_rng(1), sigma=2000.0)
    out = np.zeros(1024)
    q.fill(out)
    assert not out.any()


def test_qrm_cw_band_limited():
    q = fxm.QrmGenerator(FS, np.random.default_rng(2), sigma=1000.0,
                         occupancy=0.9, inr_db=20.0, inr_spread_db=0.0,
                         inr_max_db=20.0)
    blocks = []
    for _ in range(300):                                 # ~38 s at occ 0.9
        b = np.zeros(1024)
        q.fill(b)
        blocks.append(b)
    y = np.concatenate(blocks)
    assert np.abs(y).max() > 0, "interferer must spawn"
    Y = np.abs(np.fft.rfft(y)) ** 2
    f = np.fft.rfftfreq(len(y), 1.0 / FS)
    inband = Y[(f >= 280.0) & (f <= 2720.0)].sum()
    outband = Y[(f > 3000.0)].sum()
    assert inband > 20.0 * outband, "CW QRM must stay in the audio band"


def test_qrm_occupancy_matches_knob():
    """Gate 1: long-run active fraction tracks the occupancy knob."""
    q = fxm.QrmGenerator(FS, np.random.default_rng(11), sigma=1000.0,
                         occupancy=0.3, mean_dur_s=2.0)
    active = 0
    nblk = 20000                                         # ~427 s of channel
    b = np.zeros(1024)
    for _ in range(nblk):
        b.fill(0.0)
        q.fill(b)
        active += q.active is not None
    frac = active / nblk
    assert 0.2 < frac < 0.4, f"occupancy {frac:.2f} vs knob 0.3"


def test_qrm_paris_keying_duty():
    """Gate 2: ~44% keyed duty (PARIS: 22/50 units) with silent gaps."""
    q = fxm.QrmGenerator(FS, np.random.default_rng(4), sigma=1000.0,
                         occupancy=0.5, inr_db=20.0, inr_spread_db=0.0,
                         inr_max_db=20.0, mean_dur_s=1000.0)
    q.active = q._spawn()                                # dur capped at 120 s
    amp = q.active["amp"]
    y = np.zeros(FS * 8)                                 # 8 s: many PARIS words
    for k in range(0, len(y), 1024):
        b = np.zeros(1024)
        q.fill(b)
        y[k:k + 1024] = b
    on_frac = float(np.mean(np.abs(y) > 0.1 * amp))
    # |sin| exceeds 0.1 ~93.6% of the time while keyed -> ~0.41 at 44% duty
    assert 0.25 < on_frac < 0.6, f"keying duty fraction {on_frac:.2f}"
    silent = float(np.mean(np.abs(y) < 1e-9))
    assert silent > 0.25, "off-key gaps must be truly silent"


def test_qrm_inr_draws_median_and_cap():
    """Gate 3: per-interferer INR ~ Normal(median, spread) truncated at cap."""
    q = fxm.QrmGenerator(FS, np.random.default_rng(9), sigma=1000.0,
                         occupancy=0.9, inr_db=10.0, inr_spread_db=6.0,
                         inr_max_db=16.0, mean_dur_s=0.5)
    draws, last = [], None
    b = np.zeros(1024)
    for _ in range(4000):                                # ~85 s, fast QSO churn
        b.fill(0.0)
        q.fill(b)
        it = q.active
        if it is not None and it is not last:
            draws.append(it["inr_db"])
            last = it
    assert len(draws) > 100, f"only {len(draws)} spawns"
    assert max(draws) <= 16.0 + 1e-9, "INR draw above the cap"
    med = float(np.median(draws))
    assert 8.0 < med < 12.0, f"INR median {med:.1f} vs knob 10"


def test_qrm_rail_budget_worst_case():
    """Gate 4 — the Task C STOP regression: every carrier at the 16 dB cap on
    the qrm cell (sigma7000, pad -12) atop a full-scale signal + noise must
    leave the post-pad rail essentially untouched. The 4.9-sigma noise-peak
    term in the budget admits ~1e-6-tail Gaussian excursions, hence the
    <1e-5 assertion rather than exactly zero (gate itself is 1e-4)."""
    cs = load_sim(SIGMA=7000, SIM_RX_PAD_DB=-12)
    q = fxm.QrmGenerator(cs.FS, np.random.default_rng(1005), sigma=7000.0,
                         occupancy=0.9, inr_db=16.0, inr_spread_db=0.0,
                         inr_max_db=16.0)
    link = make_link(cs, fx=make_fx(qrm=q))
    n = np.arange(cs.BLOCK)
    x = interleave(cs, 32767.0 * np.sin(2 * np.pi * 1500 * n / cs.FS))
    nblk = 1500                                          # ~32 s of channel
    for _ in range(nblk):
        feed(link, x)
    frac = link.nrail / (nblk * cs.NSAMP)
    assert frac < 1e-5, f"rail_frac {frac:.2e} — QRM blew the pad budget"


def test_qrm_deterministic_per_seed():
    """Gate 5: same rng seed => bit-identical interference stream."""
    outs = []
    for _ in range(2):
        q = fxm.QrmGenerator(FS, np.random.default_rng(42), sigma=1000.0,
                             occupancy=0.5, sweep=True)
        y = np.zeros(1024 * 200)
        for k in range(0, len(y), 1024):
            b = np.zeros(1024)
            q.fill(b)
            y[k:k + 1024] = b
        outs.append(y)
    assert np.array_equal(outs[0], outs[1])


def test_qrm_legacy_env_hard_errors():
    """Gate 6: retired lambda/SNR knobs must fail loud, not run mis-scaled."""
    import pytest
    with pytest.raises(SystemExit):
        load_sim(SIM_QRM_CW_LAMBDA="1")
    load_sim()                                           # leave a clean module


def test_qrm_rail_budget_helper_values():
    """Reference numbers: ~16.1 dB INR room at the qrm cell; no
    room at pad 0 or with fading at sigma7000/pad -12."""
    import math
    cs = load_sim()
    room = cs.qrm_rail_room_amp(7000.0, 10.0 ** (-12.0 / 20.0), False)
    inr = 20.0 * math.log10(room / (7000.0 * math.sqrt(2.0)))
    assert abs(inr - 16.1) < 0.2, f"INR budget {inr:.2f} dB"
    assert cs.qrm_rail_room_amp(7000.0, 1.0, False) < 0, "pad 0 must have no room"
    assert cs.qrm_rail_room_amp(7000.0, 10.0 ** (-12.0 / 20.0), True) < 0, \
        "fading at sigma7000/pad-12 must have no room (needs a deeper pad)"


def test_qrm_sweeper_covers_band():
    """Gate 7 (carried over): each passband crossing chirps lo->hi."""
    q = fxm.QrmGenerator(FS, np.random.default_rng(3), sigma=1000.0,
                         sweep=True, sweep_inr_db=20.0, sweep_rate=10.0)
    y = np.zeros(FS)                                     # 10 crossings
    for k in range(0, len(y), 1024):
        b = np.zeros(min(1024, len(y) - k))
        q.fill(b)
        y[k:k + len(b)] = b
    Y = np.abs(np.fft.rfft(y * np.hanning(len(y)))) ** 2
    f = np.fft.rfftfreq(len(y), 1.0 / FS)
    for probe in (500.0, 1500.0, 2500.0):                # energy across the band
        assert Y[(f > probe - 100) & (f < probe + 100)].sum() > 0.01 * Y.sum()


def test_qrm_sweep_duty_and_burst_rate():
    """Gate 9: the sweeper is a burst train, not a continuous jammer —
    in-channel duty = passband/sweep_band (10% at defaults) and one burst
    per sweep (sweep_rate/s). The v4 smoke regression: 100% duty raised
    the noise floor +10.4 dB and killed the sweep grid point outright."""
    q = fxm.QrmGenerator(FS, np.random.default_rng(3), sigma=1000.0,
                         sweep=True, sweep_inr_db=10.0, sweep_rate=10.0)
    secs = 4
    y = np.zeros(FS * secs)
    for k in range(0, len(y), 1024):
        b = np.zeros(min(1024, len(y) - k))
        q.fill(b)
        y[k:k + len(b)] = b
    active = np.abs(y) > 1e-12
    duty = float(np.mean(active))
    assert 0.08 < duty < 0.12, f"sweep duty {duty:.3f} vs 2400/24000 = 0.10"
    bursts = int(np.sum(active[1:] & ~active[:-1]) + active[0])
    assert abs(bursts - 10 * secs) <= 1, f"{bursts} bursts in {secs} s vs rate 10/s"


def test_qrm_sweep_peak_and_average_inr():
    """Gate 10: sweep_inr_db is the while-crossing PEAK level; the average
    in-channel INR is peak + 10*log10(duty). At defaults (10 dB, 10% duty)
    the long-run floor rise is ~3 dB, not the pre-redesign +10.4 dB."""
    sigma, inr = 1000.0, 10.0
    q = fxm.QrmGenerator(FS, np.random.default_rng(5), sigma=sigma,
                         sweep=True, sweep_inr_db=inr, sweep_rate=10.0)
    y = np.zeros(FS * 4)
    for k in range(0, len(y), 1024):
        b = np.zeros(min(1024, len(y) - k))
        q.fill(b)
        y[k:k + len(b)] = b
    active = np.abs(y) > 1e-12
    peak_db = 10.0 * np.log10(np.mean(y[active] ** 2) / sigma ** 2)
    assert abs(peak_db - inr) < 1.0, f"crossing INR {peak_db:.1f} vs knob {inr}"
    avg_db = 10.0 * np.log10(np.mean(y ** 2) / sigma ** 2)
    want = inr + 10.0 * np.log10(2400.0 / 24000.0)
    assert abs(avg_db - want) < 1.0, f"average INR {avg_db:.1f} vs {want:.1f}"


def test_qrm_sweep_band_narrower_than_passband_rejected():
    """The virtual span must cover the channel; span = passband (the retired
    continuous-jammer shape) stays constructible but only by explicit ask."""
    with pytest.raises(ValueError):
        fxm.QrmGenerator(FS, np.random.default_rng(1), sigma=1000.0,
                         sweep=True, sweep_band_hz=1000.0)
    with pytest.raises(SystemExit):
        load_sim(SIM_QRM_SWEEP="1", SIM_QRM_SWEEP_BAND_HZ="1000")
    load_sim()                                           # leave a clean module


# ---------------------------------------------------------------- off = bit-exact
def test_all_knobs_off_is_bitexact_baseline():
    """fx=None and fx=empty-bundle must both produce the identical byte stream."""
    cs = load_sim(SIGMA=1500)
    rng = np.random.default_rng(11)
    x = rng.integers(-6000, 6000, cs.NSAMP).astype("<i2")
    a = make_link(cs, seed=5)
    b = make_link(cs, seed=5, fx=make_fx())
    for _ in range(20):
        ya, yb = feed(a, x), feed(b, x)
        assert np.array_equal(ya, yb)
