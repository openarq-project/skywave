"""MIL-STD-188-110C Appendix E-adapted validation gates (E.7.1/E.7.2/E.7.3).

Adaptations from the instrument-metrology originals, on purpose:
- E.7.2/E.7.3 SNR: instead of tone-in/tone-out with 20-min/2-h wall
  averaging, the signal and noise arms are measured in SEPARATE runs of the
  same seeded, linear chain (valid because the chain is linear at these
  levels and the noise stream is deterministic per seed). Averaging windows
  are statistically scaled down; the fixed seeds make each measured value
  deterministic, so the gates are stable, not flaky.
- E.7.3 runs the fading arm in `milstd` filter mode (the compliance mode) —
  the realized-spread and shape gates for that mode live in
  test_watterson_physics.py; this file gates END-TO-END SNR preservation
  through the full Link chain (fade -> hf_gain -> delay -> noise -> pad ->
  rail), the failure mode that once shipped as the silent rail-clip that
  collapsed every fading cell.
- E.7.1 flatness: the neutral digital path is flat by construction; the only
  ripple sources are the Hilbert-FIR stages inside the fade and foff
  applicators, so those are what get pinned (0.5 dB is the App E bound; the
  pins should sit far inside it).
- Latency: App E budgets 25 ms for the simulator's OWN processing latency, so
  it does not distort the protocol timing of whatever is under test. Nothing
  guarded it, and the shipped chain sits ~1 ms inside the ceiling — a block
  size or Hilbert tap count changed without noticing would blow it silently.
"""
import numpy as np

from conftest import feed, interleave, load_sim, make_link
from skywave import rig_effects as fxm
from skywave import watterson

FS = 48000


def _run_rms(cs, link, mono_block, seconds):
    """Feed the same mono block for `seconds`, return output rms (skips the
    first 0.5 s of filter/fade warmup)."""
    x = interleave(cs, mono_block)
    skip = int(0.5 * cs.FS / cs.BLOCK)
    sq = ns = 0.0
    for k in range(int(seconds * cs.FS / cs.BLOCK)):
        y = feed(link, x)[0::cs.NCH].astype(np.float64)
        if k >= skip:
            sq += float(np.sum(y * y))
            ns += len(y)
    return np.sqrt(sq / ns)


def test_e72_snr_non_fading_within_quarter_db():
    """E.7.2 (adapted): realized full-band SNR through the non-fading chain
    within 0.25 dB of nominal. Signal arm (sigma=0) and noise arm (silence)
    measured separately over the deterministic seeded chain."""
    tone = 8000.0 * np.sin(2 * np.pi * 1500.0 * np.arange(1024) / FS)
    cs = load_sim(SIGMA="0", SIM_RIG_BPF="off")
    s_rms = _run_rms(cs, make_link(cs), tone, 5.0)
    cs = load_sim(SIGMA="1000", SIM_RIG_BPF="off")
    n_rms = _run_rms(cs, make_link(cs), np.zeros(1024), 5.0)
    realized_db = 20.0 * np.log10(s_rms / n_rms)
    nominal_db = 10.0 * np.log10((8000.0 ** 2 / 2.0) / 1000.0 ** 2)
    assert abs(realized_db - nominal_db) < 0.25, \
        f"realized {realized_db:.2f} dB vs nominal {nominal_db:.2f}"


def test_e73_snr_fading_within_half_db():
    """E.7.3 (adapted): realized SNR through the FULL fading chain within
    0.5 dB of nominal — dual path, 2 ms separation, at 1.0 and 10.0 Hz
    Doppler (the two spreads E.7.3 prescribes), milstd filter. The signal
    arm averages over enough fade units that the fixed realization's mean
    power sits well inside the tolerance."""
    tone = 8000.0 * np.sin(2 * np.pi * 1500.0 * np.arange(1024) / FS)
    nominal_db = 10.0 * np.log10((8000.0 ** 2 / 2.0) / 1000.0 ** 2)
    for dop, seconds in ((1.0, 120.0), (10.0, 60.0)):
        cs = load_sim(SIGMA="0", SIM_RIG_BPF="off")
        fade = watterson.WattersonChannel(cs.FS, 2.0, dop, dur_s=seconds + 10,
                                          seed=11, filter_mode="milstd")
        s_rms = _run_rms(cs, make_link(cs, fade=fade), tone, seconds)
        cs = load_sim(SIGMA="1000", SIM_RIG_BPF="off")
        n_rms = _run_rms(cs, make_link(cs), np.zeros(1024), 5.0)
        realized_db = 20.0 * np.log10(s_rms / n_rms)
        assert abs(realized_db - nominal_db) < 0.5, \
            f"D={dop} Hz: realized {realized_db:.2f} dB vs nominal {nominal_db:.2f}"


def _tone_profile_db(y, freqs, fs):
    n = len(y)
    X = np.abs(np.fft.rfft(y * np.hanning(n)))
    fr = np.fft.rfftfreq(n, 1.0 / fs)
    out = []
    for f in freqs:
        band = (fr > f - 4) & (fr < f + 4)
        out.append(20.0 * np.log10(X[band].max()))
    return np.array(out)


def test_e71_fade_path_flatness_half_db():
    """E.7.1 (adapted): the fade applicator's Hilbert/analytic machinery must
    be flat across the voice band. Zero-delay, near-static fade so the
    channel itself contributes no comb/notch physics — what remains is the
    instrument's own ripple."""
    freqs = np.arange(400.0, 2801.0, 200.0)
    n = FS * 2
    t = np.arange(n)
    x = sum(1000.0 * np.sin(2 * np.pi * f * t / FS + 0.7 * i)
            for i, f in enumerate(freqs))
    ch = watterson.WattersonChannel(float(FS), 0.0, 0.05, dur_s=30, seed=3,
                                    filter_mode="milstd")
    y = np.concatenate([ch.process(x[k:k + 1024]) for k in range(0, n, 1024)])
    ripple = _tone_profile_db(y[FS // 2:], freqs, FS) - \
        _tone_profile_db(x[FS // 2:], freqs, FS)
    ripple -= np.median(ripple)          # common (fade) gain cancels
    assert np.abs(ripple).max() < 0.5, f"fade-path ripple {ripple.round(3)}"


def test_e71_foff_path_flatness_half_db():
    """E.7.1 (adapted): FreqShift's Hilbert stage, same bound. Tones measured
    at their shifted positions."""
    freqs = np.arange(400.0, 2801.0, 200.0)
    n = FS * 2
    t = np.arange(n)
    x = sum(1000.0 * np.sin(2 * np.pi * f * t / FS + 0.7 * i)
            for i, f in enumerate(freqs))
    sh = fxm.FreqShift(FS, 10.0)
    y = np.concatenate([sh.process(x[k:k + 1024]) for k in range(0, n, 1024)])
    ripple = _tone_profile_db(y[FS // 2:], freqs + 10.0, FS) - \
        _tone_profile_db(x[FS // 2:], freqs, FS)
    ripple -= np.median(ripple)
    assert np.abs(ripple).max() < 0.5, f"foff-path ripple {ripple.round(3)}"


# ---- latency budget (App E 25 ms) -------------------------------------------------

def _impulse_group_delay(stage, n=8192, at=1000, block=1024):
    """Group delay in SAMPLES of a block-processing stage, from where it puts an
    impulse. Measured rather than read off a constant so a changed tap count fails
    here instead of silently spending the budget."""
    x = np.zeros(n)
    x[at] = 10000.0
    y = np.concatenate([stage(x[k:k + block]) for k in range(0, n, block)])
    return int(np.argmax(np.abs(y))) - at


def test_appe_latency_budget_under_25ms():
    """App E: the simulator's own processing latency must stay under 25 ms.

    Counted: the block quantum (nothing leaves before its block is full) and the fade
    applicator's Hilbert group delay. That is the whole COMPLIANCE chain -- SIM_COMPLIANCE
    strips the rig filters per E.4.3, and SIM_LINK_DELAY_MS is a MODELLED propagation
    delay (part of the channel the operator asked for), not instrument overhead, so
    neither belongs in this budget.

    The margin is about 1 ms at the shipped 1024-frame/48 kHz configuration, which is
    the reason this gate exists: doubling SIM_BLOCK for throughput would put the
    instrument out of compliance with nothing to say so.
    """
    cs = load_sim(SIGMA="0", SIM_RIG_BPF="off")
    block_ms = 1000.0 * cs.BLOCK / cs.FS
    ch = watterson.WattersonChannel(float(cs.FS), 0.0, 0.01, dur_s=30, seed=3,
                                    filter_mode="milstd")
    fade_ms = 1000.0 * _impulse_group_delay(ch.process, block=cs.BLOCK) / cs.FS
    total_ms = block_ms + fade_ms
    assert total_ms < 25.0, (
        f"instrument latency {total_ms:.2f} ms exceeds the App E 25 ms ceiling "
        f"(block {block_ms:.2f} + fade Hilbert {fade_ms:.2f} @ {cs.BLOCK}f/{cs.FS}Hz)")


def test_appe_latency_stage_pins():
    """Per-stage pins, so a regression names the stage that spent the budget rather
    than only the total. The foff stage carries its own Hilbert of the same length: it
    is off by default and excluded from the compliance chain, but fade+foff together
    would be ~26.6 ms, i.e. PAST the ceiling -- which is why it is pinned here rather
    than left for someone to compose by surprise."""
    cs = load_sim(SIGMA="0", SIM_RIG_BPF="off")
    ch = watterson.WattersonChannel(float(cs.FS), 0.0, 0.01, dur_s=30, seed=3,
                                    filter_mode="milstd")
    fade_d = _impulse_group_delay(ch.process, block=cs.BLOCK)
    foff_d = _impulse_group_delay(fxm.FreqShift(cs.FS, 10.0).process, block=cs.BLOCK)
    # 255-tap type-III Hilbert -> (255-1)/2 = 127 samples; +-1 is impulse-peak resolution
    assert abs(fade_d - 127) <= 1, fade_d
    assert abs(foff_d - 127) <= 1, foff_d
    assert cs.BLOCK == 1024 and cs.FS == 48000, (
        f"latency budget is pinned at 1024f/48kHz; got {cs.BLOCK}f/{cs.FS}Hz")
