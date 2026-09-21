"""QRM replay mode (rig_effects.QrmReplay + channel_sim SIM_QRM_REPLAY) — pre-reg §7, built 2026-09-21.

The contract: a KiwiSDR IQ capture replaces the white noise, scaled so ITS floor equals sigma's density, so the
cell's SNR axis is unchanged and every interferer rides at its true INR. Pinned here on a synthetic capture with a
known floor and a known tone, through the class and through the full Link chain.
"""
import json
import math
import os
import wave

import numpy as np
import pytest

from conftest import load_sim, make_link, make_fx, feed

FS_REC = 20251
SEED = 7


def write_capture(path, seconds=8.0, n0=100.0, tones=(), seed=1, sidecar=True):
    """Synthetic Kiwi IQ wav: complex white noise with one-sided density n0 (units^2/Hz over the full
    fs_rec complex band: total power n0*fs_rec) + tones at (f_hz, power_over_noise_in_3k_dB)."""
    rng = np.random.default_rng(seed)
    n = int(seconds * FS_REC)
    t = np.arange(n) / FS_REC
    z = (rng.normal(0, 1, n) + 1j * rng.normal(0, 1, n)) * math.sqrt(n0 * FS_REC / 2.0)
    for f_hz, inr_db in tones:
        p_noise_3k = n0 * 3000.0
        amp = math.sqrt(p_noise_3k * 10 ** (inr_db / 10))
        z += amp * np.exp(2j * np.pi * f_hz * t)
    x = np.empty((n, 2))
    x[:, 0], x[:, 1] = z.real, z.imag
    x = np.clip(np.round(x), -32768, 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(FS_REC); w.writeframes(x.tobytes())
    if sidecar:
        json.dump({"station": "synth", "centre_khz": 14097.0, "rssi_min_dbm": -93.0, "fs": FS_REC},
                  open(path[:-4] + ".json", "w"))
    return path


def band_power(y, fs, lo, hi):
    Y = np.abs(np.fft.rfft(y)) ** 2 / len(y)
    f = np.fft.rfftfreq(len(y), 1.0 / fs)
    return float(Y[(f >= lo) & (f < hi)].sum() * 2 / len(y))


def run_fill(rep, seconds, block=1024):
    out = []
    for _ in range(int(seconds * rep.fs / block)):
        b = np.zeros(block)
        rep.fill(b)
        out.append(b)
    return np.concatenate(out)


@pytest.fixture
def corpus(tmp_path):
    a = write_capture(str(tmp_path / "a.wav"), n0=100.0, tones=[(1000.0, 12.0)], seed=1)
    b = write_capture(str(tmp_path / "b.wav"), n0=400.0, tones=[(-1000.0, 12.0)], seed=2)   # 6 dB hotter floor, tone BELOW dial
    return [a, b]


def test_floor_is_scaled_to_sigma_density(corpus):
    """The replayed floor's in-band power equals sigma^2 * bw/(fs/2): the AWGN 3k-band contract, per file."""
    from skywave.rig_effects import QrmReplay
    fs, sigma = 48000, 2000.0
    for f in corpus:
        rep = QrmReplay(fs, np.random.default_rng(SEED), sigma, [f], dial_hz=0.0, bw_hz=3000.0)
        y = run_fill(rep, 6.0)
        # measure the floor away from the tone (1.5–2.9 kHz is tone-free in both files)
        p = band_power(y, fs, 1500, 2900)
        expect = sigma ** 2 * 1400.0 / (fs / 2.0)
        assert abs(10 * math.log10(p / expect)) < 0.5, f"{f}: floor {10*math.log10(p/expect):+.2f} dB off"


def test_tone_keeps_its_inr_and_usb_sign(corpus):
    """A tone +1 kHz above the dial lands at 1 kHz audio at its recorded INR (12 dB over the 3 kHz noise);
    a tone 1 kHz BELOW the dial is not in the USB slice."""
    from skywave.rig_effects import QrmReplay
    fs, sigma = 48000, 2000.0
    rep = QrmReplay(fs, np.random.default_rng(SEED), sigma, [corpus[0]])
    y = run_fill(rep, 6.0)
    tone = band_power(y, fs, 950, 1050) - band_power(y, fs, 1100, 1200)
    noise3k = sigma ** 2 * 3000.0 / (fs / 2.0)
    assert abs(10 * math.log10(tone / noise3k) - 12.0) < 0.7
    rep_b = QrmReplay(fs, np.random.default_rng(SEED), sigma, [corpus[1]])
    yb = run_fill(rep_b, 6.0)
    assert band_power(yb, fs, 950, 1050) < 1.5 * band_power(yb, fs, 1100, 1200), "LSB tone leaked into the USB slice"


def test_dial_offset_moves_the_slice(corpus):
    """With the dial at -1500 Hz the +1 kHz tone appears at 2.5 kHz audio."""
    from skywave.rig_effects import QrmReplay
    fs, sigma = 48000, 2000.0
    rep = QrmReplay(fs, np.random.default_rng(SEED), sigma, [corpus[0]], dial_hz=-1500.0)
    y = run_fill(rep, 6.0)
    assert band_power(y, fs, 2450, 2550) > 8 * band_power(y, fs, 2100, 2200)


def test_playlist_loops_without_a_gap_and_is_deterministic(corpus):
    """Two 8 s files, 20 s of playback: power is continuous across every file boundary (no gap, no step >2 dB
    on 100 ms), and the same seed reproduces the samples exactly."""
    from skywave.rig_effects import QrmReplay
    fs, sigma = 48000, 1000.0
    # noise-only files with DIFFERENT recorded floors: after per-file normalisation their levels must match
    d = os.path.dirname(corpus[0])
    corpus = [write_capture(os.path.join(d, "n1.wav"), n0=50.0, seed=11), write_capture(os.path.join(d, "n2.wav"), n0=800.0, seed=12)]
    y1 = run_fill(QrmReplay(fs, np.random.default_rng(SEED), sigma, corpus), 20.0)
    y2 = run_fill(QrmReplay(fs, np.random.default_rng(SEED), sigma, corpus), 20.0)
    assert np.array_equal(y1, y2)
    win = int(0.1 * fs)
    p = np.array([np.mean(y1[i:i + win] ** 2) for i in range(0, len(y1) - win, win)])
    assert p.min() > 0.3 * np.median(p), "a gap at a file boundary"
    assert np.max(np.abs(np.diff(10 * np.log10(p)))) < 2.5, "a level step at a file boundary"
    y3 = run_fill(QrmReplay(fs, np.random.default_rng(SEED + 1), sigma, corpus), 20.0)
    assert not np.array_equal(y1, y3), "the playlist order is not seeded"


def test_describe_and_peak(corpus):
    from skywave.rig_effects import QrmReplay
    rep = QrmReplay(48000, np.random.default_rng(SEED), 2000.0, corpus)
    d = rep.describe()
    assert "2 files" in d and "dial +0 Hz" in d and "anchor -127.8 dBm/Hz" in d
    # peak_amp is a BOUND: sqrt2 * scaled complex peak * 1.5 — above anything streamed, but not absurd
    y = run_fill(rep, 40.0)                       # crosses several boundaries (8 s files, crossfades)
    assert rep.peak_seen <= rep.peak_amp <= 12.0 * 2000.0
    assert np.max(np.abs(y)) <= rep.peak_amp
    assert rep.replaces_noise is True


def test_link_replaces_awgn_and_keeps_the_snr_axis(corpus):
    """Through the full Link chain: with replay active, NO white noise is added on top (the 3 kHz-band noise power
    equals the AWGN contract sigma^2*3000/24000 within 0.5 dB, not 3 dB high), on both the deliver and the
    idle branches."""
    cs = load_sim(SIGMA=2000, SIM_RIG_BPF="off")
    from skywave.rig_effects import QrmReplay
    fx = make_fx()
    fx.qrm = QrmReplay(cs.FS, np.random.default_rng(SEED), cs.SIGMA_AB, [corpus[0]])
    link = make_link(cs, fx=fx)
    zeros = np.zeros(cs.NSAMP, dtype="<i2")
    buf = [feed(link, zeros)[0::cs.NCH].astype(np.float64) for _ in range(300)]
    y = np.concatenate(buf)
    p = band_power(y, cs.FS, 1500, 2900)
    expect = cs.SIGMA_AB ** 2 * 1400.0 / (cs.FS / 2.0)
    assert abs(10 * math.log10(p / expect)) < 0.6, f"link floor {10*math.log10(p/expect):+.2f} dB off (AWGN double-added?)"
    # and nothing outside the slice: the band 4–20 kHz is empty (white AWGN would fill it)
    assert band_power(y, cs.FS, 5000, 20000) < 0.05 * p


def test_env_wiring_and_conflicts(corpus, monkeypatch):
    """SIM_QRM_REPLAY expands globs; replay + generative or + noise_vd is a config conflict."""
    d = os.path.dirname(corpus[0])
    cs = load_sim(SIGMA=2000, SIM_QRM_REPLAY=os.path.join(d, "*.wav"))
    assert cs.qrm_replay_files(cs.QRM_REPLAY) == sorted(corpus)
    with pytest.raises(SystemExit):
        load_sim(SIGMA=2000, SIM_QRM_REPLAY=corpus[0], SIM_QRM_OCC=0.1)


def test_builder_constructs_replay_from_env_and_gates_the_rail(corpus):
    """The real construction path (build_channel_effects, the builder main() uses): SIM_QRM_REPLAY builds a
    QrmReplay per direction with different seeds, the banner names it, SIM_NOISE_VD alongside is a config
    error (int 2), and an exhausted rail budget (no pad, huge sigma) is a config error, never a clamp."""
    from skywave.rig_effects import QrmReplay
    d = os.path.dirname(corpus[0])
    cs = load_sim(SIGMA=2000, SIM_QRM_REPLAY=os.path.join(d, "*.wav"), SIM_WATTERSON="off", SIM_RX_PAD_DB=-12)
    eff = cs.build_channel_effects()
    assert not isinstance(eff, int)
    assert isinstance(eff.fx_ab.qrm, QrmReplay) and isinstance(eff.fx_ba.qrm, QrmReplay)
    assert "qrm=replay(2 files" in " ".join(str(x) for x in vars(eff).values())
    cs = load_sim(SIGMA=2000, SIM_QRM_REPLAY=corpus[0], SIM_NOISE_VD=5, SIM_RX_PAD_DB=-12)
    assert cs.build_channel_effects() == 2
    cs = load_sim(SIGMA=6000, SIM_QRM_REPLAY=corpus[0], SIM_RX_PAD_DB=0)
    assert cs.build_channel_effects() == 2


def test_negative_dial_straddling_zero_hz(tmp_path):
    """Review 2026-09-21 finding 1: a tone at -1000 Hz IQ with the dial at -1500 Hz must land at +500 Hz audio
    (bins on both sides of the FFT's 0 Hz wrap map by frequency, not by array index)."""
    from skywave.rig_effects import QrmReplay
    f = write_capture(str(tmp_path / "neg.wav"), n0=100.0, tones=[(-1000.0, 12.0)], seed=3)
    fs, sigma = 48000, 2000.0
    y = run_fill(QrmReplay(fs, np.random.default_rng(SEED), sigma, [f], dial_hz=-1500.0), 6.0)
    tone = band_power(y, fs, 450, 550) - band_power(y, fs, 600, 700)
    noise3k = sigma ** 2 * 3000.0 / (fs / 2.0)
    assert abs(10 * math.log10(tone / noise3k) - 12.0) < 0.7, "tone lost or moved (0 Hz wrap)"
    assert band_power(y, fs, 4000, 23000) < 0.02 * band_power(y, fs, 0, 3000), "energy dumped out of band"


def test_fill_never_renders_and_prefetch_is_ready(corpus):
    """Review finding 2: a file boundary inside fill() is a buffer swap, not a re-render — the next file was
    rendered on a background thread; every render after construction runs off the calling thread."""
    import threading, time
    from skywave.rig_effects import QrmReplay
    fs, sigma = 48000, 1000.0
    rep = QrmReplay(fs, np.random.default_rng(SEED), sigma, corpus)
    threads = []
    orig = rep._render

    def spy(path):
        threads.append(threading.current_thread().name)
        return orig(path)
    rep._render = spy
    time.sleep(1.0)                                   # let the first prefetch (started in __init__) finish
    block = 1024
    worst = 0.0
    for _ in range(int(20.0 * fs / block)):           # 20 s over 8 s files: 2 boundaries
        b = np.zeros(block); t0 = time.perf_counter(); rep.fill(b); worst = max(worst, time.perf_counter() - t0)
        if rep.pos < block:                           # just crossed a boundary: the test runs ~40x faster than real
            time.sleep(0.3)                           # time, so give the 8 s file's render the slack a real 8 s has
    assert rep.renders_in_fill == 0, "a boundary waited on an unfinished render"
    assert threads and all(t == "qrm-replay-prefetch" for t in threads), threads
    assert worst < 0.05, f"a fill() call took {worst*1000:.0f} ms"


def test_asymmetric_zero_sigma_and_onset_fail_loud(corpus):
    """Review findings 4 + 5: one direction at sigma 0, or a sigma onset schedule, with replay set is a config
    error (2 from the builder), never a ValueError and never a silent full-level pre-onset."""
    d = os.path.dirname(corpus[0])
    cs = load_sim(SIGMA=2000, SIM_SIGMA_BA=0, SIM_QRM_REPLAY=corpus[0], SIM_RX_PAD_DB=-12)
    assert cs.build_channel_effects() == 2
    cs = load_sim(SIGMA=2000, SIM_SIGMA_BA=2000, SIM_QRM_REPLAY=corpus[0], SIM_RX_PAD_DB=-12, SIM_SIGMA_BA_ONSET_S=30)
    assert cs.build_channel_effects() == 2
