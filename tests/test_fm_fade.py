"""skywave's FM port: Tier A flat fade + shadowing + IONOS noise shaping
behind the harness (the FM port design; fm_channel.py). Verifies
the fade slot through Link.process() plus knob resolution/guard discipline."""
import numpy as np

from conftest import feed, interleave, load_sim, make_link, tone_block


def test_fm_fade_off_by_default():
    cs = load_sim(SIM_FM_PORT="data9600")
    assert cs.FM_FADE == "off" and cs.FM_SHADOW == "off"
    assert cs.FM_NOISE_BW == "off"


def test_ionos_fade_traces_envelope_through_link():
    cs = load_sim(SIM_FM_PORT="data9600",
                  SIM_FM_FADE="ionos:20:3")
    from skywave import fm_channel
    fade = fm_channel.FmFade(cs.FS, "ionos", 60.0, 1,
                             ionos_depth_db=20.0, ionos_rate_hz=3.0)
    link = make_link(cs, fade=fade)
    x = interleave(cs, np.full(cs.BLOCK, 8000.0))
    rms = []
    for _ in range(cs.FS // cs.BLOCK):        # ~1 s = 3 fade periods
        y = feed(link, x)
        rms.append(float(np.sqrt(np.mean(y[0::cs.NCH].astype(float) ** 2))))
    swing = 20.0 * np.log10(max(rms) / max(min(rms), 1e-9))
    assert 18.0 < swing < 22.0                # 20 dB depth traced +-2 dB


def test_rayleigh_fade_preserves_mean_power_through_link():
    cs = load_sim(SIM_FM_PORT="data9600", SIM_FM_FADE="mobile-urban",
                  SIM_FM_BAND="2m")
    from skywave import fm_channel
    spec = fm_channel.resolve_fade_spec("mobile-urban", "2m")
    fade = fm_channel.FmFade(cs.FS, spec[0], 120.0, cs.FADE_SEED + 11,
                             fd_hz=spec[1])
    link = make_link(cs, fade=fade)
    x = interleave(cs, np.full(cs.BLOCK, 8000.0))
    sq = n = 0.0
    for _ in range(40 * cs.FS // cs.BLOCK):   # ~40 s >> 1/fD at 6.4 Hz
        y = feed(link, x)
        m = y[0::cs.NCH].astype(float)
        sq += float(np.sum(m * m)); n += len(m)
    mean_db = 10.0 * np.log10(sq / n / 8000.0 ** 2)
    assert abs(mean_db) < 1.0                 # E[env^2]=1 preserved on air


def test_seeded_fade_determinism_through_link():
    outs = []
    for _ in range(2):
        cs = load_sim(SIM_FM_PORT="data9600", SIM_FM_FADE="rayleigh:20")
        from skywave import fm_channel
        fade = fm_channel.FmFade(cs.FS, "rayleigh", 30.0, cs.FADE_SEED + 11,
                                 fd_hz=20.0)
        link = make_link(cs, fade=fade)
        x = tone_block(cs)
        outs.append(np.concatenate([feed(link, x) for _ in range(8)]))
    assert np.array_equal(outs[0], outs[1])


def test_noise_shaper_bandlimits_link_noise_floor():
    # undelivered path (no TX signal): receiver hears the shaped noise floor
    cs = load_sim(SIM_FM_PORT="data9600", SIM_FM_NOISE_BW="6000",
                  SIGMA="1000")
    from skywave import fm_channel
    link = make_link(cs)
    link.noise_lpf = fm_channel.NoiseShaper(cs.FS, 6000.0, cs.NCH)
    zero = interleave(cs, np.zeros(cs.BLOCK))
    blocks = [feed(link, zero)[0::cs.NCH].astype(float) for _ in range(64)]
    y = np.concatenate(blocks[4:])            # drop the FIR warmup
    w = np.abs(np.fft.rfft(y * np.hanning(len(y)))) ** 2
    fr = np.fft.rfftfreq(len(y), 1.0 / cs.FS)
    pb = float(w[(fr > 500) & (fr < 6300)].mean())
    sb = float(w[fr > 7300].mean())
    assert 10.0 * np.log10(pb / sb) > 40.0    # decisively band-limited


def _main_rc(**env):
    """Run channel_sim.main() in a subprocess with a controlled env; the
    config guards all fire before any transport is opened."""
    import os
    import subprocess as sp
    import sys as _sys
    import skywave
    e = {k: v for k, v in os.environ.items()
         if not (k.startswith("SIM_") or k in ("SIGMA", "TXGAIN", "SEED"))}
    e.update({"SIGMA": "0", "TXGAIN": "1.0", "SEED": "1"})
    e.update({k: str(v) for k, v in env.items()})
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = sp.run([_sys.executable, "-m", "skywave.channel_sim"], env=skywave.child_env(e),
               cwd=repo_root, capture_output=True, text=True, timeout=30)
    return r.returncode, r.stderr


def test_fade_knobs_require_fm_port():
    rc, err = _main_rc(SIM_FM_FADE="mobile-urban")
    assert rc == 2 and "SIM_FM_PORT" in err


def test_unknown_fade_spec_rejected():
    rc, err = _main_rc(SIM_FM_PORT="data9600", SIM_FM_FADE="not-a-preset")
    assert rc == 2 and "SIM_FM_FADE" in err


def test_watterson_knobs_conflict_with_fm_port():
    rc, err = _main_rc(SIM_FM_PORT="micspk", SIM_WATTERSON="poor")
    assert rc == 2 and "SIM_FM_FADE" in err   # points at the FM axis
