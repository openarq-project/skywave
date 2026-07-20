#!/usr/bin/env python3
"""channel_profile — declarative HF channel profiles for the simulator + hfchan.

A profile is a small TOML (or JSON) file that names a channel once — fade, noise, TX/RX
chain, QRM, link timing, and optional per-direction asymmetry — instead of a sprawl of
`SIM_*` env vars (channel_sim) or `hfchan` flags (filter). One shareable file, two consumers:

  channel_sim.py : `SIM_PROFILE=poor.toml python3 channel_sim.py ...`
  hfchan.py      : `hfchan --profile poor.toml < tx.s16 > rx.s16`

PRECEDENCE — the profile is the BASELINE; explicit env/CLI OVERRIDES it (the caller/operator
wins, per the standing directive). `channel_sim` loads the profile then leaves any env
var already set untouched (`os.environ.setdefault`); `hfchan` loads it as argparse
defaults that a passed flag overrides.

The profile expresses CHANNEL PHYSICS only (fade/noise/rig-chain/qrm/link) — NOT harness
config (transport, clock mode, half-duplex/PTT, FM port). That split keeps the profile
portable and matches the channel-sim separability directive.

Schema (all keys optional; unset -> the consumer's own default):

    [meta]     name, description (informational)  · seed
    [fade]     preset (watterson.PRESETS name)  OR  delay_ms + doppler_hz  ·  schedule
    [noise]    sigma (per-sample int16 noise std; SNR depends on the consumer's Fs)
               env (quiet|rural|residential|city, P.372)  ·  band_mhz  ·  impulsive_vd_db
    [offset]   freq_hz  ·  clock_ppm
    [tx]       drive (== TXGAIN)  ·  pa_rapp_p  ·  alc_db
    [rx]       rig_bpf (off|data|voice|...)  ·  pad_db  ·  agc (data|voice)
    [qrm]      occupancy  ·  inr_db
    [link]     delay_ms  ·  tr_key_ms  ·  tr_unkey_ms
    [reverse]  sigma  (-> the B->A / ACK-path noise floor)  ·  drive (-> station B drive)

`[reverse]` is the asymmetry hook: the top-level [noise].sigma / [tx].drive set BOTH
directions; [reverse] overrides the reverse direction only (a weak ACK path, a QRP peer).
Most profiles omit it (symmetric). hfchan (a one-way filter) ignores harness-only fields
(link/tr/alc/clock_ppm/reverse) with no error.
"""
import json
import os

# canonical (section, key) -> (SIM_* env name, caster). Special cases (agc, impulsive,
# reverse) are handled in to_sim_env; everything else is a straight 1:1 mapping.
_MAP = {
    ("meta", "seed"): ("SEED", int),
    ("fade", "preset"): ("SIM_WATTERSON", str),
    ("fade", "delay_ms"): ("SIM_FADE_DELAY_MS", float),
    ("fade", "doppler_hz"): ("SIM_FADE_DOPPLER_HZ", float),
    ("fade", "schedule"): ("SIM_FADE_SCHEDULE", str),
    ("noise", "sigma"): ("SIGMA", float),
    ("noise", "env"): ("SIM_NOISE_ENV", str),
    ("noise", "band_mhz"): ("SIM_BAND_MHZ", float),
    ("offset", "freq_hz"): ("SIM_FOFF_HZ", float),
    ("offset", "clock_ppm"): ("SIM_CLOCK_PPM", float),
    ("tx", "drive"): ("TXGAIN", float),
    ("tx", "pa_rapp_p"): ("SIM_PA_P", float),
    ("tx", "alc_db"): ("SIM_ALC_OVERSHOOT_DB", float),
    ("rx", "rig_bpf"): ("SIM_RIG_BPF", str),
    ("rx", "pad_db"): ("SIM_RX_PAD_DB", float),
    ("qrm", "occupancy"): ("SIM_QRM_OCC", float),
    ("qrm", "inr_db"): ("SIM_QRM_INR_DB", float),
    ("link", "delay_ms"): ("SIM_LINK_DELAY_MS", float),
    ("link", "tr_key_ms"): ("SIM_TR_KEY_MS", float),
    ("link", "tr_unkey_ms"): ("SIM_TR_UNKEY_MS", float),
    ("reverse", "sigma"): ("SIM_SIGMA_BA", float),
    ("reverse", "drive"): ("SIM_TXGAIN_B", float),
}
# keys handled specially (validated as known, mapped by hand in to_sim_env)
_SPECIAL = {("noise", "impulsive_vd_db"), ("rx", "agc"), ("meta", "name"),
            ("meta", "description")}
_VALID_SECTIONS = {"meta", "fade", "noise", "offset", "tx", "rx", "qrm", "link", "reverse"}


def load_profile(path):
    """Parse + validate a .toml or .json profile into its canonical nested dict. Rejects
    unknown sections/keys (typo protection)."""
    with open(path, "rb") as f:
        raw = f.read()
    if str(path).endswith(".json"):
        prof = json.loads(raw)
    else:
        import tomllib
        prof = tomllib.loads(raw.decode())
    _validate(prof, path)
    return prof


def _validate(prof, path):
    if not isinstance(prof, dict):
        raise SystemExit(f"channel_profile: {path}: top level must be a table")
    for section, body in prof.items():
        if section not in _VALID_SECTIONS:
            raise SystemExit(f"channel_profile: {path}: unknown section [{section}] "
                             f"(valid: {', '.join(sorted(_VALID_SECTIONS))})")
        if not isinstance(body, dict):
            raise SystemExit(f"channel_profile: {path}: [{section}] must be a table")
        for key in body:
            if (section, key) not in _MAP and (section, key) not in _SPECIAL:
                raise SystemExit(f"channel_profile: {path}: unknown key "
                                 f"{section}.{key}")


def _fmt(v):
    """int-valued floats render without a trailing .0 so SIM_* strings stay clean."""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def to_sim_env(prof):
    """Map a canonical profile to a {SIM_*: str} env dict for channel_sim.py."""
    env = {}
    for (section, key), (name, cast) in _MAP.items():
        if section in prof and key in prof[section]:
            env[name] = _fmt(cast(prof[section][key]))
    noise = prof.get("noise", {})
    if "impulsive_vd_db" in noise:
        env["SIM_NOISE_VD"] = _fmt(float(noise["impulsive_vd_db"]))
    rx = prof.get("rx", {})
    if rx.get("agc"):
        env["SIM_RX_AGC"] = "1"
        env["SIM_RX_AGC_MODE"] = str(rx["agc"])
    return env


def to_hfchan_defaults(prof):
    """Map a canonical profile to hfchan argparse defaults (dest -> value). Harness-only
    fields (link/tr/alc/clock_ppm/reverse/rig_bpf) have no hfchan knob and are skipped."""
    d = {}
    meta, fade, noise = prof.get("meta", {}), prof.get("fade", {}), prof.get("noise", {})
    offset, tx, rx, qrm = (prof.get(s, {}) for s in ("offset", "tx", "rx", "qrm"))
    if "seed" in meta:
        d["seed"] = int(meta["seed"])
    if "preset" in fade:
        d["fade"] = str(fade["preset"])
    if "delay_ms" in fade:
        d["delay"] = float(fade["delay_ms"])
    if "doppler_hz" in fade:
        d["doppler"] = float(fade["doppler_hz"])
    if "sigma" in noise:
        d["sigma"] = float(noise["sigma"])
    if "env" in noise:
        d["noise_env"] = str(noise["env"])
    if "band_mhz" in noise:
        d["band_mhz"] = float(noise["band_mhz"])
    if "impulsive_vd_db" in noise:
        d["impulsive_vd"] = str(noise["impulsive_vd_db"])
    if "freq_hz" in offset:
        d["freq"] = float(offset["freq_hz"])
    if "drive" in tx:
        d["gain"] = float(tx["drive"])
    if "pa_rapp_p" in tx:
        d["pa_rapp"] = str(tx["pa_rapp_p"])
    if rx.get("agc"):
        d["agc"] = str(rx["agc"])
    if qrm.get("occupancy"):
        inr = qrm.get("inr_db")
        d["qrm_occ"] = f"{qrm['occupancy']},{inr}" if inr is not None else str(qrm["occupancy"])
    return d


def apply_to_environ(env=None):
    """channel_sim entry point: if SIM_PROFILE is set, load it and inject its SIM_* into
    the environment as DEFAULTS (setdefault -> any already-set env var wins). No-op if
    SIM_PROFILE is unset. Returns the profile's name (or None)."""
    env = os.environ if env is None else env
    path = env.get("SIM_PROFILE", "").strip()
    if not path:
        return None
    prof = load_profile(path)
    for k, v in to_sim_env(prof).items():
        env.setdefault(k, v)          # env already present -> profile does NOT override
    return prof.get("meta", {}).get("name", os.path.basename(path))
