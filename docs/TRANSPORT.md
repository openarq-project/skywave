# Transports — running skywave with or without an ALSA aloop rig

The channel sim moves samples between the two stations one of two ways. The channel
transform, keying, per-direction threads, and statistics are **identical** across both —
only the block I/O changes, and `test_sock_transport.py` golden-pins that the delivered
samples are byte-for-byte the same.

| Transport | What it is | Needs |
|-----------|------------|-------|
| **alsa** (default) | Four `snd-aloop` cards (`arecord`/`aplay`) — the real-hardware-faithful path. | A configured 4-card aloop rig. |
| **sock** | Framed unix-domain sockets (`sock_frames.py`). Byte-identical channel with **no ALSA devices**. | Nothing — runs on a plain host (CI, a laptop, another project). |

## Selecting a transport — the declarative profile

A **transport profile** names a transport once, in one shareable TOML/JSON file, instead
of a scatter of `SIM_*` env vars:

```console
$ SIM_TRANSPORT_PROFILE=transports/sock-real_time.toml skywave-channel
```

Three are shipped in `transports/`:

- **`alsa-native.toml`** — the native 4-card default (also the behaviour when no profile
  is set); listed so the default topology is nameable.
- **`sock-real_time.toml`** — the **portable, aloop-free** transport: unix sockets, real-time
  pacing. This is the one to use on a host with no aloop rig.
- **`sock-virt_time.toml`** — unix sockets with the block-lockstep virtual-time clock: the
  sim is the clock master, neither station runs ahead of the other, there is no wall
  pacing, so a run goes as fast as the slower station computes and is **reproducible**.

The transport profile is **orthogonal** to the channel profile (`SIM_PROFILE`, which
carries channel *physics* — fade/noise/rig chain). They use separate env vars, so any
physics runs over any transport:

```console
$ SIM_PROFILE=profiles/poor.toml \
  SIM_TRANSPORT_PROFILE=transports/sock-virt_time.toml \
  skywave-channel
```

**Precedence:** the profile is the baseline; an explicit `SIM_TRANSPORT` / `SIM_SOCK_*` /
`SIM_CLOCK` env var **overrides** it (an explicit setting wins). This mirrors the channel-profile precedence rule.

## The honest aloop-free caveat

The **channel** runs device-free over sockets, always. A fully device-free *modem* run
also needs a station that speaks sockets:

- **A sock-capable station** — e.g. a modem's `--audio sock` backend — talks to the sim directly
  over the sockets. Fully aloop-free.
- **The in-process reference adapter** (`skywave/adapters/example.py`) — no subprocess,
  no ALSA at all; the portable starting point for a new modem's `ModemAdapter`.
- **`SIM_SOCK_SHIM=1`** bridges sockets ↔ ALSA so a modem that *only* speaks ALSA can run
  on the sock sim — but that bridge still needs an aloop rig, so it is **not** the
  portable option. Use it for the real-binary-on-the-cable regression topology, not for a
  no-hardware host.

## Transport profile schema

```toml
[meta]
name = "sock-real_time"                 # informational
description = "..."

[transport]
kind = "sock"                      # alsa | sock          (default: alsa)
clock = "real_time"                # real_time | virt_time  (virt_time REQUIRES kind=sock)
sock_dir = "/tmp/simsock"          # socket directory     (default: /tmp/simsock-<pid>)
sock_buf = 65536                   # SO_SNDBUF/SO_RCVBUF bytes
accept_s = 30                      # accept timeout for both stations
max_virtual_s = 0                  # virtual-clock run bound, seconds; 0 = unbounded
shim = false                       # spawn the sock<->ALSA bridge (needs aloop)
```

Unknown sections/keys and bad enum values are rejected at load (typo protection).
