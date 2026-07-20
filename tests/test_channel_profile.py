"""Tests for the declarative channel-profile schema.

Covers the loader (TOML + JSON, validation), the two mappers (channel_sim SIM_* env and
hfchan defaults), the env-override precedence (profile is a baseline, explicit env wins),
the asymmetry [reverse] hook, the shipped example profiles, and hfchan --profile end to end.

Run:  cd skywave && python3 -m pytest tests/test_channel_profile.py -q
"""
import json
import os

import numpy as np
import pytest

import channel_profile as cp

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILES = os.path.join(HERE, "profiles")


def _write(tmp_path, text, name="p.toml"):
    p = tmp_path / name
    p.write_text(text)
    return str(p)


def test_load_toml_and_map_to_sim_env(tmp_path):
    path = _write(tmp_path, """
[meta]
name = "t"
seed = 77
[fade]
preset = "poor"
[noise]
sigma = 4000
env = "city"
[tx]
drive = 1.4
[link]
delay_ms = 3
""")
    prof = cp.load_profile(path)
    env = cp.to_sim_env(prof)
    assert env["SEED"] == "77"
    assert env["SIM_WATTERSON"] == "poor"
    assert env["SIGMA"] == "4000"          # int-valued float renders clean
    assert env["SIM_NOISE_ENV"] == "city"
    assert env["TXGAIN"] == "1.4"
    assert env["SIM_LINK_DELAY_MS"] == "3"


def test_load_json_equivalent(tmp_path):
    path = _write(tmp_path, json.dumps({"noise": {"sigma": 2000}, "fade": {"preset": "moderate"}}),
                  name="p.json")
    env = cp.to_sim_env(cp.load_profile(path))
    assert env["SIGMA"] == "2000" and env["SIM_WATTERSON"] == "moderate"


def test_reverse_asymmetry_hook(tmp_path):
    path = _write(tmp_path, """
[noise]
sigma = 1500
[tx]
drive = 1.0
[reverse]
sigma = 9000
drive = 0.6
""")
    env = cp.to_sim_env(cp.load_profile(path))
    assert env["SIGMA"] == "1500"          # forward / symmetric baseline
    assert env["SIM_SIGMA_BA"] == "9000"   # reverse (ACK path) override
    assert env["SIM_TXGAIN_B"] == "0.6"    # station B drive


def test_agc_and_impulsive_specials(tmp_path):
    path = _write(tmp_path, """
[noise]
impulsive_vd_db = 6
[rx]
agc = "data"
""")
    env = cp.to_sim_env(cp.load_profile(path))
    assert env["SIM_NOISE_VD"] == "6"
    assert env["SIM_RX_AGC"] == "1" and env["SIM_RX_AGC_MODE"] == "data"


def test_unknown_section_and_key_rejected(tmp_path):
    with pytest.raises(SystemExit, match="unknown section"):
        cp.load_profile(_write(tmp_path, "[bogus]\nx = 1\n"))
    with pytest.raises(SystemExit, match="unknown key"):
        cp.load_profile(_write(tmp_path, "[noise]\nsgima = 1\n"))   # typo


def test_apply_to_environ_setdefault_env_wins(tmp_path):
    path = _write(tmp_path, "[noise]\nsigma = 4000\n[fade]\npreset = \"poor\"\n")
    env = {"SIM_PROFILE": path, "SIGMA": "9999"}       # SIGMA already set -> must win
    name = cp.apply_to_environ(env)
    assert env["SIGMA"] == "9999"                      # profile did NOT override
    assert env["SIM_WATTERSON"] == "poor"              # unset key filled from profile
    assert name is not None


def test_apply_to_environ_noop_without_profile():
    env = {"SIGMA": "1"}
    assert cp.apply_to_environ(env) is None and env == {"SIGMA": "1"}


def test_shipped_profiles_load_and_validate():
    for name in ("clean.toml", "poor.toml", "poor-weak-ack.toml"):
        prof = cp.load_profile(os.path.join(PROFILES, name))
        cp.to_sim_env(prof)                            # must map without error
    wa = cp.to_sim_env(cp.load_profile(os.path.join(PROFILES, "poor-weak-ack.toml")))
    assert wa["SIGMA"] == "1500" and wa["SIM_SIGMA_BA"] == "9000"   # the asymmetry cell


def test_hfchan_profile_applies_noise_and_fade(tmp_path):
    """hfchan --profile loads the channel; the profile's sigma sets the added-noise std."""
    import hfchan
    inp, outp = tmp_path / "in.raw", tmp_path / "out.raw"
    n = 8000 * 3
    (5000.0 * np.sin(2 * np.pi * 1000 * np.arange(n) / 8000)).astype("<i2").tofile(inp)
    prof = _write(tmp_path, "[noise]\nsigma = 800\n")
    hfchan.main([str(inp), str(outp), "--profile", prof, "--ssbfilt", "0", "--quiet"])
    x = np.fromfile(inp, dtype="<i2").astype(np.float64)
    y = np.fromfile(outp, dtype="<i2").astype(np.float64)
    m = min(len(x), len(y))
    assert np.std(y[:m] - x[:m]) == pytest.approx(800, rel=0.1)   # profile sigma applied


def test_hfchan_cli_flag_overrides_profile(tmp_path):
    """An explicit flag beats the profile default (precedence)."""
    import hfchan
    inp, outp = tmp_path / "in.raw", tmp_path / "out.raw"
    (5000.0 * np.sin(2 * np.pi * 1000 * np.arange(8000 * 3) / 8000)).astype("<i2").tofile(inp)
    prof = _write(tmp_path, "[noise]\nsigma = 800\n")
    # profile says 800, CLI --sigma 3000 must win
    hfchan.main([str(inp), str(outp), "--profile", prof, "--sigma", "3000",
                 "--ssbfilt", "0", "--quiet"])
    x = np.fromfile(inp, dtype="<i2").astype(np.float64)
    y = np.fromfile(outp, dtype="<i2").astype(np.float64)
    m = min(len(x), len(y))
    assert np.std(y[:m] - x[:m]) == pytest.approx(3000, rel=0.1)
