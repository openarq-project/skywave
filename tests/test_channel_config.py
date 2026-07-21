"""Tests for the ChannelConfig typed config surface.

The load-bearing test is the DRIFT GUARD: ChannelConfig.from_env(env) must reproduce
channel_sim's live module globals field-for-field, for the same env. This proves
from_env is a faithful mirror of the module's own parse -- the prerequisite for making
it the module's single parse point and backing an importable Channel object without any
byte-drift. The rest cover unset defaults, the empty-string fallbacks, __post_init__
inheritance, and the to_env round-trip.

Run:  cd skywave && python3 -m pytest tests/test_channel_config.py -q
"""
import os

import pytest

from conftest import load_sim, _BASE
from skywave.channel_config import ChannelConfig

# ChannelConfig field -> the channel_sim module global it must equal.
FIELD_TO_GLOBAL = {
    "nch": "NCH", "gain": "GAIN", "sigma": "SIGMA", "seed": "SEED", "block": "BLOCK",
    "pa_p": "PA_P", "pa_vsat": "PA_VSAT", "alc_db": "ALC_DB", "alc_preset": "ALC_PRESET",
    "rx_pad_db": "RX_PAD_DB", "rx_agc_mode": "RX_AGC_MODE", "rig_bpf": "RIG_BPF",
    "rig_order": "RIG_ORDER", "watterson": "WATTERSON", "fade_doppler_hz": "FADE_DOPPLER",
    "fade_delay_ms": "FADE_DELAY", "fade_dur_s": "FADE_DUR_S",
    "fade_schedule": "FADE_SCHEDULE", "fade_seed": "FADE_SEED",
    "link_delay_ms": "LINK_DELAY_MS", "tr_key_ms": "TR_KEY_MS", "tr_unkey_ms": "TR_UNKEY_MS",
    "tr_jitter_ms": "TR_JITTER_MS", "foff_hz": "FOFF_HZ", "clock_ppm": "CLOCK_PPM",
    "noise_vd": "NOISE_VD", "noise_env": "NOISE_ENV", "band_mhz": "BAND_MHZ",
    "qrm_occ": "QRM_OCC", "qrm_inr_db": "QRM_INR_DB", "sigma_ab": "SIGMA_AB",
    "sigma_ba": "SIGMA_BA", "gain_a": "GAIN_A", "gain_b": "GAIN_B",
    "half_duplex": "HALF_DUPLEX", "ptt": "SIM_PTT",
}

RICH = {
    "SIM_NCH": "1", "TXGAIN": "1.3", "SIGMA": "1500", "SEED": "99", "SIM_BLOCK": "512",
    # NB: alc_preset stays "off" here on purpose — a non-off preset OVERRIDES ALC_DB in
    # channel_sim (runtime resolution), which ChannelConfig keeps raw; that path is
    # checked separately in test_alc_preset_is_raw_parse.
    "SIM_PA_P": "2.5", "SIM_PA_VSAT": "30000", "SIM_ALC_OVERSHOOT_DB": "3",
    "SIM_RX_PAD_DB": "-6", "SIM_RX_AGC": "data",
    "SIM_RIG_BPF": "voice", "SIM_RIG_ORDER": "4", "SIM_WATTERSON": "POOR",
    "SIM_FADE_DOPPLER_HZ": "1.0", "SIM_FADE_DELAY_MS": "2.0", "SIM_FADE_DUR_S": "600",
    "SIM_FADE_SCHEDULE": "good,poor", "SIM_FADE_SEED": "7", "SIM_LINK_DELAY_MS": "5",
    "SIM_TR_KEY_MS": "20", "SIM_TR_UNKEY_MS": "30", "SIM_TR_JITTER_MS": "4",
    # NB: noise_env stays "off" here on purpose — a non-off P.372 env DERIVES SIGMA
    # (runtime resolution), which ChannelConfig keeps raw; parsed standalone below.
    "SIM_FOFF_HZ": "1.5", "SIM_CLOCK_PPM": "0.5", "SIM_NOISE_VD": "26",
    "SIM_BAND_MHZ": "14", "SIM_QRM_OCC": "0.3",
    "SIM_QRM_INR_DB": "12", "SIM_SIGMA_AB": "1200", "SIM_SIGMA_BA": "9000",
    "SIM_TXGAIN_A": "1.1", "SIM_TXGAIN_B": "0.9", "SIM_HALF_DUPLEX": "1", "SIM_PTT": "1",
}
# empty-string overrides exercise the "... or 0"/"... or SIGMA" fallback branch, which
# differs from the unset default (channel_sim: SIM_LINK_DELAY_MS unset -> 3, ="" -> 0).
EMPTY_FALLBACK = {"SIM_LINK_DELAY_MS": "", "SIM_TR_KEY_MS": "", "SIM_TR_UNKEY_MS": "",
                  "SIM_SIGMA_AB": "", "SIM_SIGMA_BA": "", "SIM_TXGAIN_A": "",
                  "SIM_TXGAIN_B": "", "SIGMA": "800", "TXGAIN": "1.2"}


@pytest.fixture(autouse=True)
def _restore_env():
    snap = dict(os.environ)
    yield
    for k in list(os.environ):
        if k not in snap:
            del os.environ[k]
    os.environ.update(snap)


@pytest.mark.parametrize("overrides", [{}, RICH, EMPTY_FALLBACK], ids=["base", "rich", "empty"])
def test_from_env_matches_live_module(overrides):
    """from_env(env) == channel_sim's own parse, field for field, for the SAME env
    load_sim fed the module (_BASE + overrides)."""
    cs = load_sim(**overrides)
    env = {**_BASE, **overrides}
    cfg = ChannelConfig.from_env(env)
    for f, g in FIELD_TO_GLOBAL.items():
        assert getattr(cfg, f) == getattr(cs, g), f"{f} != channel_sim.{g}"


def test_unset_defaults_match_source():
    """from_env({}) reproduces channel_sim's UNSET defaults, including the non-zero ones
    (link_delay 3 ms, T/R 15/25 ms) that _BASE masks in the drift guard above."""
    cfg = ChannelConfig.from_env({})
    assert cfg.nch == 2 and cfg.gain == 1.0 and cfg.sigma == 0.0 and cfg.seed == 1234
    assert cfg.block == 1024 and cfg.rig_bpf == "data" and cfg.rx_pad_db == -12.0
    assert cfg.link_delay_ms == 3.0 and cfg.tr_key_ms == 15.0 and cfg.tr_unkey_ms == 25.0
    assert cfg.watterson == "off" and cfg.band_mhz == 7.0 and cfg.pa_vsat == 32767.0


def test_post_init_inheritance():
    cfg = ChannelConfig(sigma=1500.0, gain=1.3, seed=42)
    assert cfg.sigma_ab == 1500.0 and cfg.sigma_ba == 1500.0   # inherit sigma
    assert cfg.gain_a == 1.3 and cfg.gain_b == 1.3             # inherit gain
    assert cfg.fade_seed == 42                                 # inherit seed
    # an explicit asymmetry override is kept, not overwritten
    assert ChannelConfig(sigma=1500.0, sigma_ba=9000.0).sigma_ba == 9000.0


def test_alc_preset_is_raw_parse():
    # ChannelConfig stores the RAW knobs: the preset NAME (lowercased) and the raw
    # alc_db, independently. channel_sim's "preset overrides alc_db" is a runtime
    # resolution step downstream of the parse, not modelled here.
    assert ChannelConfig.from_env({"SIM_ALC_PRESET": "MODERN"}).alc_preset == "modern"
    assert ChannelConfig.from_env({}).alc_preset == "off"
    cfg = ChannelConfig.from_env({"SIM_ALC_PRESET": "legacy", "SIM_ALC_OVERSHOOT_DB": "3"})
    assert cfg.alc_preset == "legacy" and cfg.alc_db == 3.0   # raw kept, not the 7.0 preset


def test_noise_env_is_raw_parse():
    # Like alc_preset: ChannelConfig stores the raw noise_env NAME and raw sigma; the
    # "noise_env derives SIGMA" step is channel_sim runtime resolution downstream.
    cfg = ChannelConfig.from_env({"SIM_NOISE_ENV": "CITY", "SIGMA": "1500"})
    assert cfg.noise_env == "city" and cfg.sigma == 1500.0   # raw kept, not the derived floor
    assert ChannelConfig.from_env({}).noise_env == "off"


def test_env_map_covers_every_field():
    from dataclasses import fields
    names = {f.name for f in fields(ChannelConfig)}
    assert set(ChannelConfig.ENV) == names          # 1:1, nothing unmapped


def test_to_env_roundtrips():
    cfg = ChannelConfig.from_env(RICH)
    back = ChannelConfig.from_env(cfg.to_env())
    for f in FIELD_TO_GLOBAL:
        assert getattr(back, f) == getattr(cfg, f), f"{f} did not round-trip through to_env"
    # booleans + int-valued floats render clean
    e = ChannelConfig(sigma=1500.0, half_duplex=True).to_env()
    assert e["SIM_HALF_DUPLEX"] == "1" and e["SIGMA"] == "1500"
