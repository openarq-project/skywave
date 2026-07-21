#!/usr/bin/env python3
"""channel_config -- the typed configuration surface for the channel simulator.

channel_sim.py reads its entire configuration from ~65 SIM_* environment variables at
import time. That is perfect for the subprocess harness but opaque to a Python consumer
that wants to construct and run the channel IN-PROCESS from a typed object. ChannelConfig
is that object: one dataclass holding the channel-PHYSICS knobs, with

  * from_env(env)  -- parse a SIM_* environment into a ChannelConfig, reproducing
                      channel_sim's exact parsing (same defaults, same `.strip() or
                      default` fallbacks, same lowercasing, same cross-defaults).
  * to_env()       -- serialize back to a SIM_* dict, so a ChannelConfig composes with
                      everything env-driven (channel profiles, sweep_runner, channel_sim).

This is the FOUNDATION of the in-process library API. `from_env` is drift-guarded against
channel_sim's live globals by test_channel_config.py, so it can safely become the
module's single parse point (a following step) and back an importable `Channel` object.

SCOPE (v1): the HF channel-physics + station/asymmetry + HD-mode knobs -- the surface an
external consumer configures. Deliberately EXCLUDED for now (still parsed by channel_sim
directly): the FM port (SIM_FM_*), pure instrumentation (NP_STATS/SIM_TXDUMP/SIM_KEYLOG/
SIM_VERBOSE), the transport/clock layer (SIM_TRANSPORT/SIM_SOCK_*/SIM_CLOCK -- that is the
transport_profile module's domain), and the fine second-order sub-knobs (AGC attack/release,
QRM spread/sweep, ALC settle, FOFF ramp, clock slack, fade xfade). ChannelConfig covers a
superset of every knob the channel_profile module maps.
"""
import os
from dataclasses import dataclass, field, fields


@dataclass
class ChannelConfig:
    """The channel simulator's physics configuration as a typed object. Field defaults
    match channel_sim's UNSET behaviour exactly. The four *inheriting* fields default to
    None and resolve in __post_init__ to their base (matching channel_sim's `... or
    SIGMA`/`... or GAIN`/`str(SEED)` fallbacks): sigma_ab/sigma_ba <- sigma, gain_a/gain_b
    <- gain, fade_seed <- seed."""
    # ---- levels / basics ----
    nch: int = 2                    # SIM_NCH        cable channel count
    gain: float = 1.0               # TXGAIN         TX audio drive (both stations)
    sigma: float = 0.0              # SIGMA          noise std, int16 LSBs (both dirs)
    seed: int = 1234                # SEED
    block: int = 1024               # SIM_BLOCK      frames per channel per block
    # ---- TX nonlinearity ----
    pa_p: float = 0.0               # SIM_PA_P       Rapp soft-PA sharpness (0 = off)
    pa_vsat: float = 32767.0        # SIM_PA_VSAT
    alc_db: float = 0.0             # SIM_ALC_OVERSHOOT_DB
    alc_preset: str = "off"         # SIM_ALC_PRESET
    # ---- RX chain ----
    rx_pad_db: float = -12.0        # SIM_RX_PAD_DB
    rx_agc_mode: str = "0"          # SIM_RX_AGC     0=off | data | voice
    rig_bpf: str = "data"           # SIM_RIG_BPF    off | data | voice | ...
    rig_order: int = 6              # SIM_RIG_ORDER
    # ---- fade (Watterson/CCIR) ----
    watterson: str = "off"          # SIM_WATTERSON  preset name or off
    fade_doppler_hz: str = ""       # SIM_FADE_DOPPLER_HZ  (custom fade; "" = unset)
    fade_delay_ms: str = ""         # SIM_FADE_DELAY_MS    (custom fade; "" = unset)
    fade_dur_s: float = 1200.0      # SIM_FADE_DUR_S
    fade_schedule: str = ""         # SIM_FADE_SCHEDULE
    fade_seed: int = None           # SIM_FADE_SEED  (inherits seed)
    # ---- link timing / turnaround ----
    link_delay_ms: float = 3.0      # SIM_LINK_DELAY_MS   (empty env -> 0, not 3)
    tr_key_ms: float = 15.0         # SIM_TR_KEY_MS       (empty env -> 0)
    tr_unkey_ms: float = 25.0       # SIM_TR_UNKEY_MS     (empty env -> 0)
    tr_jitter_ms: float = 0.0       # SIM_TR_JITTER_MS
    # ---- frequency / clock offset ----
    foff_hz: float = 0.0            # SIM_FOFF_HZ
    clock_ppm: float = 0.0          # SIM_CLOCK_PPM
    # ---- noise environment ----
    noise_vd: float = 0.0           # SIM_NOISE_VD   impulsive Vd (dB)
    noise_env: str = "off"          # SIM_NOISE_ENV  quiet|rural|residential|city|off
    band_mhz: float = 7.0           # SIM_BAND_MHZ
    # ---- QRM ----
    qrm_occ: float = 0.0            # SIM_QRM_OCC
    qrm_inr_db: float = 10.0        # SIM_QRM_INR_DB
    # ---- per-direction / per-station asymmetry ----
    sigma_ab: float = None          # SIM_SIGMA_AB   A->B noise floor (inherits sigma)
    sigma_ba: float = None          # SIM_SIGMA_BA   B->A / ACK path  (inherits sigma)
    gain_a: float = None            # SIM_TXGAIN_A   station A drive   (inherits gain)
    gain_b: float = None            # SIM_TXGAIN_B   station B drive   (inherits gain)
    # ---- channel mode ----
    half_duplex: bool = False       # SIM_HALF_DUPLEX
    ptt: bool = False               # SIM_PTT        in-band/explicit PTT vs VOX keying

    def __post_init__(self):
        if self.fade_seed is None:
            self.fade_seed = self.seed
        if self.sigma_ab is None:
            self.sigma_ab = self.sigma
        if self.sigma_ba is None:
            self.sigma_ba = self.sigma
        if self.gain_a is None:
            self.gain_a = self.gain
        if self.gain_b is None:
            self.gain_b = self.gain

    # field -> the SIM_* env var it maps to (used by to_env and the drift-guard test).
    ENV = {
        "nch": "SIM_NCH", "gain": "TXGAIN", "sigma": "SIGMA", "seed": "SEED",
        "block": "SIM_BLOCK", "pa_p": "SIM_PA_P", "pa_vsat": "SIM_PA_VSAT",
        "alc_db": "SIM_ALC_OVERSHOOT_DB", "alc_preset": "SIM_ALC_PRESET",
        "rx_pad_db": "SIM_RX_PAD_DB", "rx_agc_mode": "SIM_RX_AGC",
        "rig_bpf": "SIM_RIG_BPF", "rig_order": "SIM_RIG_ORDER",
        "watterson": "SIM_WATTERSON", "fade_doppler_hz": "SIM_FADE_DOPPLER_HZ",
        "fade_delay_ms": "SIM_FADE_DELAY_MS", "fade_dur_s": "SIM_FADE_DUR_S",
        "fade_schedule": "SIM_FADE_SCHEDULE", "fade_seed": "SIM_FADE_SEED",
        "link_delay_ms": "SIM_LINK_DELAY_MS", "tr_key_ms": "SIM_TR_KEY_MS",
        "tr_unkey_ms": "SIM_TR_UNKEY_MS", "tr_jitter_ms": "SIM_TR_JITTER_MS",
        "foff_hz": "SIM_FOFF_HZ", "clock_ppm": "SIM_CLOCK_PPM",
        "noise_vd": "SIM_NOISE_VD", "noise_env": "SIM_NOISE_ENV",
        "band_mhz": "SIM_BAND_MHZ", "qrm_occ": "SIM_QRM_OCC",
        "qrm_inr_db": "SIM_QRM_INR_DB", "sigma_ab": "SIM_SIGMA_AB",
        "sigma_ba": "SIM_SIGMA_BA", "gain_a": "SIM_TXGAIN_A", "gain_b": "SIM_TXGAIN_B",
        "half_duplex": "SIM_HALF_DUPLEX", "ptt": "SIM_PTT",
    }

    @classmethod
    def from_env(cls, env=None):
        """Parse a SIM_* environment into a ChannelConfig, reproducing channel_sim's
        exact parse (see test_channel_config.py for the drift guard vs the live module)."""
        e = os.environ if env is None else env

        def s(k, d=""):
            return e.get(k, d).strip()

        def f(k, d):                                   # float, empty -> d ("... or d")
            return float(s(k, str(d)) or str(d))

        def i(k, d):                                   # int, empty -> d
            return int(s(k, str(d)) or str(d))

        def lo(k, d, fb):                              # lowercased enum, empty -> fb
            return (e.get(k, d).strip().lower() or fb)

        seed = i("SEED", 1234)
        sigma = f("SIGMA", 0.0)
        gain = f("TXGAIN", 1.0)
        return cls(
            nch=i("SIM_NCH", 2), gain=gain, sigma=sigma, seed=seed,
            block=i("SIM_BLOCK", 1024),
            pa_p=f("SIM_PA_P", 0), pa_vsat=f("SIM_PA_VSAT", 32767),
            alc_db=f("SIM_ALC_OVERSHOOT_DB", 0), alc_preset=lo("SIM_ALC_PRESET", "off", "off"),
            rx_pad_db=f("SIM_RX_PAD_DB", -12), rx_agc_mode=lo("SIM_RX_AGC", "0", "0"),
            rig_bpf=lo("SIM_RIG_BPF", "data", "off"), rig_order=i("SIM_RIG_ORDER", 6),
            watterson=lo("SIM_WATTERSON", "off", "off"),
            fade_doppler_hz=s("SIM_FADE_DOPPLER_HZ"), fade_delay_ms=s("SIM_FADE_DELAY_MS"),
            fade_dur_s=f("SIM_FADE_DUR_S", 1200), fade_schedule=s("SIM_FADE_SCHEDULE"),
            fade_seed=i("SIM_FADE_SEED", seed),
            # NB the get-default and the `or` fallback DIFFER for these (unset -> the
            # non-zero default; explicitly empty -> 0), so they are spelled out.
            link_delay_ms=float(s("SIM_LINK_DELAY_MS", "3") or "0"),
            tr_key_ms=float(s("SIM_TR_KEY_MS", "15") or "0"),
            tr_unkey_ms=float(s("SIM_TR_UNKEY_MS", "25") or "0"),
            tr_jitter_ms=f("SIM_TR_JITTER_MS", 0),
            foff_hz=f("SIM_FOFF_HZ", 0), clock_ppm=f("SIM_CLOCK_PPM", 0),
            noise_vd=f("SIM_NOISE_VD", 0), noise_env=lo("SIM_NOISE_ENV", "off", "off"),
            band_mhz=f("SIM_BAND_MHZ", 7),
            qrm_occ=f("SIM_QRM_OCC", 0), qrm_inr_db=f("SIM_QRM_INR_DB", 10),
            sigma_ab=float(s("SIM_SIGMA_AB") or sigma), sigma_ba=float(s("SIM_SIGMA_BA") or sigma),
            gain_a=float(s("SIM_TXGAIN_A") or gain), gain_b=float(s("SIM_TXGAIN_B") or gain),
            half_duplex=s("SIM_HALF_DUPLEX", "0") == "1", ptt=s("SIM_PTT", "0") == "1",
        )

    def to_env(self):
        """Serialize to a {SIM_*: str} dict fully specifying this channel. Booleans map to
        "1"/"0"; int-valued floats render without a trailing .0 so the strings stay clean."""
        out = {}
        for fld in fields(self):
            name = fld.name
            env_name = self.ENV.get(name)
            if env_name is None:
                continue
            v = getattr(self, name)
            if isinstance(v, bool):
                out[env_name] = "1" if v else "0"
            elif isinstance(v, float) and v.is_integer():
                out[env_name] = str(int(v))
            else:
                out[env_name] = str(v)
        return out
