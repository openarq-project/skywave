"""Physics pins for the Watterson fading applicator (watterson.py).

These assert the model realizes what F.1487 says it should: preset table golden,
power normalization, the realized differential delay, the realized Doppler spread,
and the Rayleigh envelope statistics of each path.
"""
import numpy as np
import pytest
from scipy.signal import hilbert

from conftest import REPO_ROOT  # noqa: F401  (ensures sys.path)
from skywave import watterson

FS = 48000


def test_presets_match_f1487_goldens():
    # "poor" is the
    # CANONICAL CCIR 520-2 / MIL-STD-188-110C Poor (2 ms / 1.0 Hz), matching
    # codec2 `ch --mpp`, PathSim, and DRM Ch.4. The
    # hotter 1.5 Hz cell (F.1487 low-lat moderate, the original "poor" preset) keeps
    # its own name; "flutter" is the CCIR 520-2 flutter cell. Plus the project
    # NVIS ladder and F.1487
    # extremes.
    assert watterson.PRESETS["good"] == (0.5, 0.1)
    assert watterson.PRESETS["moderate"] == (1.0, 0.5)
    assert watterson.PRESETS["poor"] == (2.0, 1.0)
    assert watterson.PRESETS["low-lat-moderate"] == (2.0, 1.5)
    assert watterson.PRESETS["flutter"] == (0.5, 10.0)
    assert watterson.PRESETS["nvis"] == (3.0, 1.0)
    assert watterson.PRESETS["nvis-max"] == (4.0, 1.0)
    assert watterson.PRESETS["nvis-disturbed"] == (7.0, 1.0)   # F.1487 Annex 3 §3.4
    assert watterson.PRESETS["disturbed"] == (6.0, 10.0)       # low-lat disturbed
    assert watterson.PRESETS["high-lat"] == (7.0, 30.0)        # high-lat disturbed


def test_power_normalization():
    """Average faded power must equal input power (the AWGN SNR axis depends on it)."""
    ch = watterson.WattersonChannel(FS, 2.0, 1.5, dur_s=120, seed=99)
    rng = np.random.default_rng(1)
    in_sq = out_sq = 0.0
    n = 2048
    for _ in range(1000):   # ~42 s of audio, ~60 fade cycles at 1.5 Hz
        x = rng.standard_normal(n) * 4000.0
        y = ch.process(x)
        in_sq += float(np.dot(x, x))
        out_sq += float(np.dot(y, y))
    ratio = (out_sq / in_sq) ** 0.5
    assert abs(ratio - 1.0) < 0.06, f"out/in RMS = {ratio:.3f}"


def test_realized_differential_delay():
    """The second path must sit at exactly delay_ms behind the first.

    Doppler is set to the minimum so the tap gains are near-constant; correlating the
    output against the ANALYTIC input makes the peak magnitudes phase-independent.
    """
    delay_ms = 2.0
    tau = int(round(delay_ms * 1e-3 * FS))            # 96 samples
    ch = watterson.WattersonChannel(FS, delay_ms, 0.05, dur_s=30, seed=7)
    rng = np.random.default_rng(2)
    x = rng.standard_normal(FS * 2)                    # 2 s of white noise
    y = np.empty_like(x)
    n = 2048
    for k in range(0, len(x), n):
        y[k:k + n] = ch.process(x[k:k + n])
    xa = hilbert(x)
    # the applicator delays its output by the Hilbert group delay (gdelay samples),
    # so path 1 sits at lag gdelay and path 2 at gdelay + tau
    g = ch.gdelay
    lags = np.arange(0, g + 2 * tau)
    c = np.array([np.abs(np.vdot(xa[: len(x) - lags[-1]],
                                 y[l: l + len(x) - lags[-1]])) for l in lags])
    floor = np.median(np.delete(c, [g, g + tau]))
    assert c[g] > 4 * floor, f"path-1 peak {c[g]:.3g} vs floor {floor:.3g}"
    assert c[g + tau] > 4 * floor, f"path-2 peak {c[g+tau]:.3g} vs floor {floor:.3g}"
    # the two DOMINANT peaks must be the two paths (Hilbert-kernel sidelobes hug the
    # peaks, so suppress a +-4-lag neighborhood around the first before the second)
    l1 = int(np.argmax(c))
    c2 = c.copy()
    c2[max(0, l1 - 4): l1 + 5] = 0.0
    l2 = int(np.argmax(c2))
    assert {l1, l2} == {g, g + tau}, f"dominant lags {{{l1},{l2}}} != {{{g},{g+tau}}}"


def test_realized_doppler_spread():
    """The generated tap-gain process must have the requested 2-sigma spectral width."""
    dop = 1.5
    low_fs = 50.0
    rng = np.random.default_rng(3)
    g = watterson._doppler_gain_lowrate(dop, low_fs, int(240 * low_fs), rng)
    # Welch-ish: average periodograms over 8 segments
    nseg = 8
    seg = len(g) // nseg
    psd = np.zeros(seg)
    for k in range(nseg):
        s = g[k * seg:(k + 1) * seg] * np.hanning(seg)
        psd += np.abs(np.fft.fft(s)) ** 2
    f = np.fft.fftfreq(seg, 1.0 / low_fs)
    sigma_f = np.sqrt(np.sum(psd * f ** 2) / np.sum(psd))
    realized = 2.0 * sigma_f                           # 2-sigma width convention
    assert abs(realized - dop) / dop < 0.3, f"realized 2-sigma spread {realized:.2f} Hz"


def test_rayleigh_envelope():
    """Each path's |gain| must be Rayleigh (median/mean = sqrt(ln4)/sqrt(pi/2) = 0.939)."""
    rng = np.random.default_rng(4)
    g = watterson._doppler_gain_lowrate(1.5, 50.0, 12000, rng)   # 240 s => ~700 indep samples
    env = np.abs(g)
    ratio = np.median(env) / np.mean(env)
    assert abs(ratio - 0.9394) < 0.05, f"median/mean = {ratio:.3f}"


def test_tap_update_rate_meets_milstd_32x():
    """MIL-STD-188-110C Appendix E: tap gains computed at >= 32x the Doppler
    spread (this was 20x, which passed self-verification but sat below the
    written guideline). The 50 Hz floor covers slow fades."""
    for dop in (0.1, 0.5, 1.0, 1.5, 10.0, 30.0, 55.0):
        ch = watterson.WattersonChannel(FS, 1.0, dop, dur_s=5, seed=1)
        assert ch.low_fs >= max(50.0, 32.0 * dop)
