"""Tests for hfchan -- the codec2 `ch`-compatible one-way channel filter.

Correctness: the `--No` bridge (added real-noise level + reported SNR3k), bit-exact
determinism from --seed, ragged-tail handling, near-passthrough baseline, and that
each skywave-extra stage runs. Reference (golden) check: when the codec2 `ch`
binary is present, hfchan's reported SNR3k must match ch's within a tight gate at
matching --No -- ch is the community-standard reference this tool is compatible with.

Run:  cd skywave && python3 -m pytest tests/test_hfchan.py -q
"""
import os
import re
import subprocess

import numpy as np
import pytest

from skywave import hfchan

FS = 8000
CH_BIN = os.path.expanduser("~/tools/mercury/modem/freedv/ch")


def _tone(path, amp=8000.0, freq=1000.0, secs=6.0, fs=FS):
    t = np.arange(int(fs * secs)) / fs
    (amp * np.sin(2 * np.pi * freq * t)).astype("<i2").tofile(path)
    return amp * amp / 2.0                      # real signal power A^2/2


def _run(inp, outp, *args):
    """Call hfchan in-process; return captured stderr stats text."""
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        hfchan.main([str(inp), str(outp), *map(str, args)])
    return buf.getvalue()


def _snr3k(stats_text):
    m = re.search(r"SNR3k\(dB\):\s*([-\d.]+)", stats_text)
    return float(m.group(1)) if m else None


@pytest.mark.parametrize("nodb", [-40.0, -30.0, -20.0])
def test_no_bridge_noise_level(tmp_path, nodb):
    """Added real-noise variance == Fs*No/2, and reported SNR3k == the ch formula
    10log10(2*Psig/(No*3000)). No fade/clip/ssbfilt so out = in + noise exactly."""
    inp, outp = tmp_path / "in.raw", tmp_path / "out.raw"
    p_in = _tone(inp)
    stats = _run(inp, outp, "--No", nodb, "--ssbfilt", 0, "--seed", 7)

    x = np.fromfile(inp, dtype="<i2").astype(np.float64)
    y = np.fromfile(outp, dtype="<i2").astype(np.float64)
    n = min(len(x), len(y))
    noise = y[:n] - x[:n]
    No = 10.0 ** (nodb / 10.0) * 1e6
    assert np.var(noise) == pytest.approx(FS * No / 2.0, rel=0.05)

    expected_snr = 10.0 * np.log10(2.0 * p_in / (No * 3000.0))
    assert _snr3k(stats) == pytest.approx(expected_snr, abs=0.1)


def test_determinism(tmp_path):
    inp = tmp_path / "in.raw"
    _tone(inp)
    a, b, c = (tmp_path / f"{s}.raw" for s in "abc")
    _run(inp, a, "--No", -15, "--mpp", "--seed", 1, "--quiet")
    _run(inp, b, "--No", -15, "--mpp", "--seed", 1, "--quiet")
    _run(inp, c, "--No", -15, "--mpp", "--seed", 2, "--quiet")
    ya = np.fromfile(a, dtype="<i2")
    yb = np.fromfile(b, dtype="<i2")
    yc = np.fromfile(c, dtype="<i2")
    assert np.array_equal(ya, yb)               # same seed -> bit-identical
    assert not np.array_equal(ya, yc)           # different seed -> different realization


def test_ragged_tail_length(tmp_path):
    """Input length not a multiple of --block yields exactly that many samples out."""
    inp, outp = tmp_path / "in.raw", tmp_path / "out.raw"
    n = 8000 * 3 + 137                          # deliberately not a block multiple
    (1000.0 * np.sin(2 * np.pi * 1000 * np.arange(n) / FS)).astype("<i2").tofile(inp)
    _run(inp, outp, "--No", -30, "--block", 1024, "--quiet")
    assert len(np.fromfile(outp, dtype="<i2")) == n


def test_clean_baseline_is_near_passthrough(tmp_path):
    """No=-100, no impairments, ssbfilt off -> output tracks input within LSBs."""
    inp, outp = tmp_path / "in.raw", tmp_path / "out.raw"
    _tone(inp)
    _run(inp, outp, "--No", -100, "--ssbfilt", 0, "--quiet")
    x = np.fromfile(inp, dtype="<i2").astype(np.float64)
    y = np.fromfile(outp, dtype="<i2").astype(np.float64)
    assert np.abs(y - x).max() < 16.0


def test_fade_changes_signal_preserves_power(tmp_path):
    """--mpp fade alters the waveform but WattersonChannel normalizes average power,
    so clean-vs-faded RMS stays close over a long wideband exposure. A WIDEBAND input
    (not a tone) is used so the frequency-SELECTIVE 2-tap fade averages across the band
    rather than parking the single tone in a comb notch; the residual spread is the
    honest finite-window temporal fading variance (the ensemble normalization itself is
    proven in test_watterson_verify.py). Gate 2 dB, deterministic seed."""
    inp, clean, faded = (tmp_path / f"{s}.raw" for s in ("in", "clean", "faded"))
    rng = np.random.default_rng(0)
    (rng.standard_normal(FS * 20) * 4000.0).astype("<i2").tofile(inp)   # wideband, 20 s
    _run(inp, clean, "--No", -100, "--ssbfilt", 0, "--quiet")
    _run(inp, faded, "--No", -100, "--ssbfilt", 0, "--mpp", "--seed", 3, "--quiet")
    yc = np.fromfile(clean, dtype="<i2").astype(np.float64)
    yf = np.fromfile(faded, dtype="<i2").astype(np.float64)
    assert not np.array_equal(yc.astype("<i2"), yf.astype("<i2"))
    rms_c = np.sqrt(np.mean(yc ** 2))
    rms_f = np.sqrt(np.mean(yf ** 2))
    assert abs(20 * np.log10(rms_f / rms_c)) < 2.0


@pytest.mark.parametrize("extra", [
    ["--agc", "data"], ["--impulsive-vd", "6"], ["--qrm-occ", "0.3,12"],
    ["--pa-rapp", "2"], ["--fade", "poor"], ["--freq", "20"],
])
def test_extras_run(tmp_path, extra):
    """Each skywave-extra stage produces well-formed output of the input length."""
    inp, outp = tmp_path / "in.raw", tmp_path / "out.raw"
    _tone(inp)
    n_in = len(np.fromfile(inp, dtype="<i2"))
    _run(inp, outp, "--No", -20, "--quiet", *extra)
    y = np.fromfile(outp, dtype="<i2")
    assert len(y) == n_in
    assert np.all(np.abs(y) <= 32767)


def test_noise_env_scales_floor(tmp_path):
    """--noise-env raises the noise floor by the P.372 man-made delta relative to the
    quiet-rural @7MHz anchor --No sets. City@7MHz should add noise power == the delta
    of the ch table (76.8 vs 53.6 quiet at c, common d) => added-noise std ratio matches
    10^(delta/20); quiet@7MHz is the anchor (~0 dB change)."""
    inp = tmp_path / "in.raw"
    _tone(inp)

    def noise_std_of(env, band=7.0):
        # --No far below the rail so even city (+~24 dB) stays unclipped and the
        # measured (out-in) noise std reflects the injected level, not the int16 rail.
        outp = tmp_path / f"o_{env}_{band}.raw"
        args = ["--No", -55, "--ssbfilt", 0, "--seed", 7]
        if env != "anchor":
            args += ["--noise-env", env, "--band-mhz", band]
        _run(inp, outp, *args)
        x = np.fromfile(inp, dtype="<i2").astype(np.float64)
        y = np.fromfile(outp, dtype="<i2").astype(np.float64)
        n = min(len(x), len(y))
        return np.std(y[:n] - x[:n])

    base = noise_std_of("anchor")
    quiet = noise_std_of("quiet")
    city = noise_std_of("city")
    # quiet @ 7 MHz IS the anchor -> no change
    assert quiet == pytest.approx(base, rel=0.05)
    # city delta at 7 MHz: (76.8 - 27.7*log10(7)) - (53.6 - 28.6*log10(7))
    delta = (76.8 - 27.7 * np.log10(7.0)) - (53.6 - 28.6 * np.log10(7.0))
    assert 20 * np.log10(city / base) == pytest.approx(delta, abs=0.3)
    assert city > base                                   # busier env = higher floor


@pytest.mark.skipif(not os.path.exists(CH_BIN), reason="codec2 ch binary not present")
@pytest.mark.parametrize("nodb", [-40, -30, -20])
def test_cross_check_snr3k_vs_ch_binary(tmp_path, nodb):
    """Golden/reference: reported SNR3k matches codec2 ch within 0.1 dB (ssbfilt off,
    AWGN only -- isolates the --No bridge, the one number that must be drop-in)."""
    inp, outp = tmp_path / "in.raw", tmp_path / "out.raw"
    _tone(inp)
    ch = subprocess.run([CH_BIN, str(inp), str(outp), "--No", str(nodb), "--ssbfilt", "0"],
                        capture_output=True, text=True)
    ch_snr = _snr3k(ch.stderr)
    hf_snr = _snr3k(_run(inp, outp, "--No", nodb, "--ssbfilt", 0, "--seed", 7))
    assert ch_snr is not None and hf_snr is not None
    assert hf_snr == pytest.approx(ch_snr, abs=0.1)
