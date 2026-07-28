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


def _realized_spread(dop, low_fs, dur_s, rng, filter_mode="codec2"):
    """Autocorrelation-derived 2-sigma spread of the generated gain process
    (R(tau) = exp(-2*pi^2*sigma_f^2*tau^2) for a Gaussian PSD; small-lag fit,
    tail-insensitive — a second-moment PSD estimate is inflated by design-grid
    leakage, which is a separate defect from the width convention)."""
    g = watterson._doppler_gain_lowrate(dop, low_fs, int(dur_s * low_fs), rng,
                                        filter_mode)
    lags = np.arange(1, 6)
    R = np.array([np.abs(np.vdot(g[:-k], g[k:]) / np.vdot(g, g)) for k in lags])
    tau = lags / low_fs
    sigma_f = np.sqrt(-np.log(R) / (2 * np.pi ** 2 * tau ** 2)).mean()
    return 2.0 * sigma_f


def test_realized_doppler_spread():
    """codec2 (default) filter: realized 2-sigma spread is KNOWN-NARROW,
    ~0.8x nominal (missing sqrt(PSD) + design-grid leakage; adjudicated
    2026-07-28 against F.1487 Eq.2 / MIL-STD-188-110C App E / NTIA-Johnson).
    Pinned AT its measured value — not at nominal — so a silent change in
    either direction fails: byte-stability of the default is the contract."""
    rng = np.random.default_rng(3)
    realized = _realized_spread(1.5, 50.0, 240, rng, "codec2")
    assert abs(realized / 1.5 - 0.80) < 0.05, f"codec2 realized {realized:.3f} Hz"


def test_milstd_filter_realizes_nominal_spread():
    """milstd filter (MIL-STD-188-110C App E time-domain Gaussian taps):
    realized 2-sigma spread == nominal within 5% — the standards-faithful
    mode's defining gate."""
    rng = np.random.default_rng(3)
    for dop in (0.5, 1.5, 10.0):
        low_fs = max(50.0, np.ceil(32.0 * dop))
        realized = _realized_spread(dop, low_fs, 240, rng, "milstd")
        assert abs(realized / dop - 1.0) < 0.05, \
            f"milstd realized {realized:.3f} Hz for nominal {dop}"


def test_milstd_psd_shape_within_appe_tolerance():
    """App E sec E.7.4-style gate: the realized Doppler power spectrum vs the
    ideal Gaussian, within +-1.5 dB at the -20 dB point and +-2.0 dB at the
    -30 dB point (single path; long realization stands in for the 3 h tone
    FFT)."""
    dop, low_fs = 1.0, 50.0
    rng = np.random.default_rng(5)
    g = watterson._doppler_gain_lowrate(dop, low_fs, 600_000, rng, "milstd")
    seg = 8192
    nseg = len(g) // seg
    psd = np.zeros(seg)
    for k in range(nseg):
        psd += np.abs(np.fft.fft(g[k * seg:(k + 1) * seg] * np.kaiser(seg, 9.0))) ** 2
    f = np.fft.fftfreq(seg, 1.0 / low_fs)
    idx = np.argsort(f)
    f, psd = f[idx], psd[idx]

    def smooth_db(i):
        # average in LINEAR power over +-2 bins (dB-domain averaging biases
        # low on a curved slope), then convert
        return 10.0 * np.log10(psd[max(0, i - 2):i + 3].mean())

    i0 = int(np.argmin(np.abs(f)))
    ref_db = smooth_db(i0)                          # smoothed 0 Hz reference
    sigma = dop / 2.0
    ideal_db = -10.0 * (f ** 2) / (2 * sigma ** 2) * np.log10(np.e)
    for target_db, tol_db in ((-20.0, 1.5), (-30.0, 2.0)):
        f_pt = sigma * np.sqrt(-2.0 * target_db / (10.0 * np.log10(np.e)))
        for sgn in (-1.0, 1.0):
            i = int(np.argmin(np.abs(f - sgn * f_pt)))
            meas = smooth_db(i) - ref_db
            assert abs(meas - ideal_db[i]) < tol_db, \
                f"at {f[i]:+.2f} Hz: measured {meas:.2f} dB vs ideal {ideal_db[i]:.2f}"


def test_interpolation_images_suppressed():
    """The low-rate -> audio-rate linear interpolation must not leave spectral
    images at the update-rate offsets (Furman: zero-order hold puts them at
    multiples of low_fs; ours measured -77 dBc — pin at -70 with margin).
    Cross-instrument context: PathSim's polyphase interpolator measures
    -62 dBc on the same probe."""
    for mode in ("codec2", "milstd"):
        ch = watterson.WattersonChannel(float(FS), 2.0, 1.0, dur_s=100, seed=9,
                                        filter_mode=mode)
        blk = 8000.0 * np.sin(2 * np.pi * 1500.0 * np.arange(1024) / FS)
        y = np.concatenate([ch.process(blk).copy()
                            for _ in range(int(66 * FS / 1024))])[FS * 5:]
        seg = FS * 60
        X = np.abs(np.fft.rfft(y[:seg] * np.hanning(seg))) ** 2
        fr = np.fft.rfftfreq(seg, 1.0 / FS)
        base = X[(fr > 1495) & (fr < 1505)].max()
        for off in (50.0, 100.0):                 # low_fs and 2*low_fs at d=1
            for sgn in (-1, 1):
                band = (fr > 1500 + sgn * off - 2) & (fr < 1500 + sgn * off + 2)
                img = 10.0 * np.log10(X[band].max() / base)
                assert img < -70.0, f"{mode}: image at {sgn*off:+.0f} Hz = {img:.1f} dBc"


def test_default_filter_mode_is_byte_identical_codec2():
    """The knob must not move the default: explicit codec2 == legacy default."""
    a = watterson.WattersonChannel(FS, 2.0, 1.0, dur_s=10, seed=7)
    b = watterson.WattersonChannel(FS, 2.0, 1.0, dur_s=10, seed=7,
                                   filter_mode="codec2")
    assert np.array_equal(a.p1, b.p1) and np.array_equal(a.p2, b.p2)
    # and milstd is genuinely different
    c = watterson.WattersonChannel(FS, 2.0, 1.0, dur_s=10, seed=7,
                                   filter_mode="milstd")
    assert not np.array_equal(a.p1, c.p1)


def test_unknown_filter_mode_rejected():
    import pytest
    with pytest.raises(ValueError):
        watterson.WattersonChannel(FS, 2.0, 1.0, dur_s=5, seed=1,
                                   filter_mode="nonsense")


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
