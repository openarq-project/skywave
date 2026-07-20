#!/usr/bin/env python3
"""transport_profile -- declarative harness-transport profiles for channel_sim.

The channel_sim can move samples between the two stations two ways (see the TRANSPORT
section of channel_sim.py):

  alsa (native default)  four snd-aloop cards -- the real-hardware-faithful path a
                         real rig runs. Needs a configured 4-card aloop rig.
  sock                   framed unix-domain sockets (sock_frames.py) -- byte-identical
                         delivery with NO ALSA devices, so the harness comes up on a
                         plain host (CI, a laptop, another project) that has no aloop rig.

A transport profile is a small TOML (or JSON) file that names a transport once -- kind,
clock, socket dir/buffer/accept, shim -- instead of a scatter of SIM_* env vars, so a
portable run is one shareable file:

  channel_sim.py : `SIM_TRANSPORT_PROFILE=transports/sock-real_time.toml python3 channel_sim.py`

This is the SEPARABILITY COMPLEMENT to the channel_profile module: that one carries channel
PHYSICS (fade/noise/rig-chain), this one carries HARNESS TRANSPORT. They are ORTHOGONAL
(any physics runs over any transport) and use separate env vars, so you mix them freely:

  SIM_PROFILE=profiles/poor.toml SIM_TRANSPORT_PROFILE=transports/sock-virt_time.toml ...

PRECEDENCE -- the profile is the BASELINE; explicit env OVERRIDES it (the launcher/operator
wins, per the standing directive). apply_to_environ() uses os.environ.setdefault, so any
SIM_TRANSPORT/SIM_SOCK_*/SIM_CLOCK already set on the command line wins.

Schema (all keys optional; unset -> channel_sim's own default):

    [meta]       name, description (informational)
    [transport]  kind (alsa|sock)  ·  clock (real_time|virt_time)  ·  sock_dir  ·  sock_buf
                 accept_s  ·  shim (bool)  ·  max_virtual_s

Notes: clock=virt_time is the block-lockstep virtual clock and REQUIRES kind=sock (a
profile that sets clock=virt_time with an explicit kind!=sock is rejected). `shim` spawns
the sock<->ALSA bridge for real binaries that only speak ALSA -- that path still needs an
aloop rig, so it is NOT the portable, aloop-free option (sock-capable stations or the
in-process reference adapter are).
"""
import json
import os

# canonical (section, key) -> (SIM_* env name, caster). `shim` is special (bool -> 1/0),
# handled in to_sim_env; everything else is a straight 1:1 mapping.
_MAP = {
    ("transport", "kind"): ("SIM_TRANSPORT", str),
    ("transport", "clock"): ("SIM_CLOCK", str),
    ("transport", "sock_dir"): ("SIM_SOCK_DIR", str),
    ("transport", "sock_buf"): ("SIM_SOCK_BUF", int),
    ("transport", "accept_s"): ("SIM_SOCK_ACCEPT_S", float),
    ("transport", "max_virtual_s"): ("SIM_MAX_VIRTUAL_S", float),
}
_SPECIAL = {("transport", "shim"), ("meta", "name"), ("meta", "description")}
_VALID_SECTIONS = {"meta", "transport"}
_VALID_KINDS = {"alsa", "sock"}
_VALID_CLOCKS = {"real_time", "virt_time"}


def load_profile(path):
    """Parse + validate a .toml or .json transport profile into its canonical nested
    dict. Rejects unknown sections/keys (typo protection) and bad enum values."""
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
        raise SystemExit(f"transport_profile: {path}: top level must be a table")
    for section, body in prof.items():
        if section not in _VALID_SECTIONS:
            raise SystemExit(f"transport_profile: {path}: unknown section [{section}] "
                             f"(valid: {', '.join(sorted(_VALID_SECTIONS))})")
        if not isinstance(body, dict):
            raise SystemExit(f"transport_profile: {path}: [{section}] must be a table")
        for key in body:
            if (section, key) not in _MAP and (section, key) not in _SPECIAL:
                raise SystemExit(f"transport_profile: {path}: unknown key {section}.{key}")
    tr = prof.get("transport", {})
    kind, clock = tr.get("kind"), tr.get("clock")
    if kind is not None and kind not in _VALID_KINDS:
        raise SystemExit(f"transport_profile: {path}: transport.kind={kind!r} "
                         f"(valid: {', '.join(sorted(_VALID_KINDS))})")
    if clock is not None and clock not in _VALID_CLOCKS:
        raise SystemExit(f"transport_profile: {path}: transport.clock={clock!r} "
                         f"(valid: {', '.join(sorted(_VALID_CLOCKS))})")
    # the one real cross-field constraint: the lockstep virtual clock only exists on the
    # sock transport. Catch a profile that pins clock=virt_time with an explicit alsa kind
    # up front (env may still override kind, so only reject when the profile itself says so).
    if clock == "virt_time" and kind is not None and kind != "sock":
        raise SystemExit(f"transport_profile: {path}: clock=virt_time requires kind=sock "
                         f"(got kind={kind!r})")


def _fmt(v):
    """int-valued floats render without a trailing .0 so SIM_* strings stay clean."""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def to_sim_env(prof):
    """Map a canonical transport profile to a {SIM_*: str} env dict for channel_sim.py."""
    env = {}
    for (section, key), (name, cast) in _MAP.items():
        if section in prof and key in prof[section]:
            env[name] = _fmt(cast(prof[section][key]))
    tr = prof.get("transport", {})
    if "shim" in tr:
        env["SIM_SOCK_SHIM"] = "1" if tr["shim"] else "0"
    return env


def apply_to_environ(env=None):
    """channel_sim entry point: if SIM_TRANSPORT_PROFILE is set, load it and inject its
    SIM_* into the environment as DEFAULTS (setdefault -> any already-set env var wins).
    No-op if SIM_TRANSPORT_PROFILE is unset. Returns the profile's name (or None)."""
    env = os.environ if env is None else env
    path = env.get("SIM_TRANSPORT_PROFILE", "").strip()
    if not path:
        return None
    prof = load_profile(path)
    for k, v in to_sim_env(prof).items():
        env.setdefault(k, v)          # env already present -> profile does NOT override
    return prof.get("meta", {}).get("name", os.path.basename(path))
