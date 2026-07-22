"""Noise-injection calibration anchors for channel_sim.

The whole SNR axis of every campaign rests on: (1) SIGMA really being the noise std
in int16 LSBs over the full 24 kHz Nyquist band, (2) the gain path being unity and
linear below the clip, and (3) the AWGN being white so the documented
SNR3k = 20*log10(8198/sigma) + 9 dB conversion (RESULTS.md:57; the +9 dB is
10*log10(24000/3000)) holds. The BER anchor closes the loop against textbook theory
through the FULL Link.process() chain.
"""
import math

import numpy as np

from conftest import load_sim, make_link, feed, interleave


def test_noise_variance_matches_sigma():
    cs = load_sim(SIGMA=2000)
    link = make_link(cs)
    zeros = np.zeros(cs.NSAMP, dtype="<i2")
    buf = []
    for _ in range(400):
        buf.append(feed(link, zeros).astype(np.float64))
    y = np.concatenate(buf)
    assert abs(y.mean()) < 20.0
    assert abs(y.std() / 2000.0 - 1.0) < 0.02, f"noise std {y.std():.1f} vs SIGMA 2000"


def test_noise_is_white_3k_band_fraction():
    """3 kHz-band noise power must be sigma^2 * (3000/24000) — the +9 dB SNR3k term."""
    cs = load_sim(SIGMA=2000)
    link = make_link(cs)
    zeros = np.zeros(cs.NSAMP, dtype="<i2")
    buf = []
    for _ in range(600):
        buf.append(feed(link, zeros)[0::cs.NCH].astype(np.float64))   # one channel
    y = np.concatenate(buf)
    Y = np.abs(np.fft.rfft(y)) ** 2
    f = np.fft.rfftfreq(len(y), 1.0 / cs.FS)
    frac = Y[f <= 3000.0].sum() / Y.sum()
    assert abs(frac - 3000.0 / 24000.0) < 0.01, f"3k band fraction {frac:.4f}"


def test_sigma_ref_rate_identity_at_48k():
    """At the reference rate the scale is exactly 1: every existing 48 kHz cell
    is byte-identical under the reference-rate semantics."""
    cs = load_sim(SIGMA=1200)
    assert cs.SIGMA_SCALE == 1.0
    assert cs.SIGMA_AB == 1200.0 and cs.SIGMA_BA == 1200.0


def test_sigma_ref_rate_scales_at_8k():
    """SIGMA is specified at 48 kHz; the injected per-sample std at 8 kHz is
    sigma*sqrt(8/48), keeping in-band noise density (cell SNR) rate-invariant."""
    cs = load_sim(SIGMA=1000, SIM_FS=8000, SIM_NCH=1)
    assert abs(cs.SIGMA_SCALE - math.sqrt(8000 / 48000)) < 1e-12
    assert abs(cs.SIGMA_AB - 1000 * math.sqrt(8000 / 48000)) < 1e-9
    # measured through the full Link chain
    link = make_link(cs)
    zeros = np.zeros(cs.NSAMP, dtype="<i2")
    buf = [feed(link, zeros).astype(np.float64) for _ in range(400)]
    y = np.concatenate(buf)
    assert abs(y.std() / cs.SIGMA_AB - 1.0) < 0.02, f"noise std {y.std():.1f}"


def test_sigma_ref_rate_in_band_density_invariant():
    """The actual guarantee: the same SIGMA yields the same noise power per Hz
    in the modem band (500-2500 Hz) at 48 kHz and at 8 kHz."""
    def band_density(cs):
        link = make_link(cs)
        zeros = np.zeros(cs.NSAMP, dtype="<i2")
        buf = [feed(link, zeros)[0::cs.NCH].astype(np.float64) for _ in range(400)]
        y = np.concatenate(buf)
        Y = np.abs(np.fft.rfft(y)) ** 2
        f = np.fft.rfftfreq(len(y), 1.0 / cs.FS)
        m = (f >= 500.0) & (f <= 2500.0)
        return Y[m].sum() / len(y) / 2000.0          # power per Hz in band
    d48 = band_density(load_sim(SIGMA=2000))
    d8 = band_density(load_sim(SIGMA=2000, SIM_FS=8000, SIM_NCH=1))
    ratio_db = 10 * math.log10(d8 / d48)
    assert abs(ratio_db) < 0.3, f"in-band density differs by {ratio_db:.2f} dB"


def test_sigma_ref_rate_disable_and_per_direction_override():
    """SIM_SIGMA_REF_FS=0 restores raw per-sample sigma; SIM_SIGMA_AB/BA
    overrides carry the same reference-rate semantics as SIGMA."""
    cs = load_sim(SIGMA=1000, SIM_FS=8000, SIM_NCH=1, SIM_SIGMA_REF_FS=0)
    assert cs.SIGMA_SCALE == 1.0 and cs.SIGMA_AB == 1000.0
    cs = load_sim(SIGMA=1000, SIM_FS=8000, SIM_NCH=1, SIM_SIGMA_AB=600)
    assert abs(cs.SIGMA_AB - 600 * math.sqrt(8000 / 48000)) < 1e-9
    assert abs(cs.SIGMA_BA - 1000 * math.sqrt(8000 / 48000)) < 1e-9


def test_deterministic_given_seed():
    """Same SEED => byte-identical channel output (paired-arm reproducibility)."""
    outs = []
    for _ in range(2):
        cs = load_sim(SIGMA=1500)
        link = make_link(cs, seed=777)
        rng = np.random.default_rng(5)
        x = rng.integers(-6000, 6000, cs.NSAMP).astype("<i2")
        outs.append(b"".join(bytes(feed(link, x).tobytes()) for _ in range(20)))
    assert outs[0] == outs[1]


def test_directions_use_independent_noise():
    cs = load_sim(SIGMA=2000)
    a = make_link(cs, seed=1234 + 11)
    b = make_link(cs, src="b", sink="a", seed=1234 + 22)
    zeros = np.zeros(cs.NSAMP, dtype="<i2")
    ya = np.concatenate([feed(a, zeros).astype(float) for _ in range(50)])
    yb = np.concatenate([feed(b, zeros).astype(float) for _ in range(50)])
    rho = np.corrcoef(ya, yb)[0, 1]
    assert abs(rho) < 0.02, f"cross-direction noise correlation {rho:.4f}"


def test_ber_vs_ebn0_anchor():
    """BPSK through the FULL chain must hit textbook BER = Q(A*sqrt(T/2)/sigma).

    500 baud BPSK on a 1500 Hz carrier (3 cycles/symbol, phase-continuous), matched-
    filter detection. Amplitude chosen for z = 2.33 => theory BER 0.0099. 60k symbols
    => ~600 expected errors, sampling sigma ~4% of the mean; tolerance ±25% catches
    any dB-scale miscalibration of the SIGMA axis while staying flake-free.
    """
    sigma = 2000.0
    T = 96                      # samples/symbol @ 48k = 500 baud
    z = 2.33
    A = z * sigma / math.sqrt(T / 2.0)          # ~672.6 LSB, far below clip
    nsym = 60000
    cs = load_sim(SIGMA=sigma)
    rng = np.random.default_rng(6)
    bits = rng.integers(0, 2, nsym)
    n = np.arange(nsym * T)
    carrier = np.sin(2 * np.pi * 1500.0 * n / cs.FS)
    tx = (A * np.where(np.repeat(bits, T) == 1, 1.0, -1.0) * carrier).astype("<i2")

    link = make_link(cs, seed=42)
    out = []
    blk = cs.BLOCK
    for k in range(0, len(tx), blk):
        chunk = tx[k:k + blk]
        if len(chunk) < blk:
            chunk = np.concatenate([chunk, np.zeros(blk - len(chunk), dtype="<i2")])
        out.append(feed(link, interleave(cs, chunk))[0::cs.NCH].astype(np.float64))
    y = np.concatenate(out)[: nsym * T]

    ref = carrier[:T]
    corr = (y.reshape(nsym, T) @ ref)
    errs = int(np.count_nonzero((corr > 0).astype(int) != bits))
    ber = errs / nsym
    theory = 0.5 * math.erfc(z / math.sqrt(2.0))
    assert theory * 0.75 < ber < theory * 1.25, (
        f"BER {ber:.5f} vs theory {theory:.5f} ({errs} errors) — "
        "SIGMA axis calibration drifted")
