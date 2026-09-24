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

## Stations on other machines — `--listen` (`SIM_LISTEN`)

`SIM_LISTEN` serves the same frames over TCP on one port instead of the two unix
sockets, so the stations can run on other computers:

```console
$ skywave-channel --listen 0.0.0.0 --sigma 300          # the sim host, port 8340

$ armstrong run --callsign W1CAL --relay simhost        # computer 1
$ armstrong run --callsign W1ANS --relay simhost        # computer 2
```

This is armstrong-relay's role and wire with the channel added, so an armstrong station
uses its ordinary `--relay` flag. The first station to connect is A, the second B. A
third is turned away while a pair runs. When either station leaves, the pair ends and the
sim waits for the next two; each pair gets a fresh rig and a virtual clock from zero.
Stations must send PTT in-band (there is no stdin PTT relay in this mode).

- **Address:** `HOST:PORT`, `[IPv6]:PORT`, a bare `HOST` (port 8340, armstrong-relay's),
  or a bare `PORT`, which binds loopback only. Serving another machine takes an explicit
  address such as `0.0.0.0`. There is **no authentication**: keep it on a LAN or behind
  an SSH tunnel.
- **Defaults:** it sets `SIM_TRANSPORT=sock SIM_CLOCK=virt_time SIM_HALF_DUPLEX=1
  SIM_PTT=1 SIM_VIRT_MAX_RATIO=1` (lockstep, half-duplex, wall-clock pace for people and
  host applications). They are setdefaults: channel/transport profiles and explicit env
  still win. `SIM_TRANSPORT=alsa` or `SIM_SOCK_SHIM=1` with it is an error.
- **Network speed:** every block is a round trip to both stations, so a path slower
  than one block (21 ms at 48 kHz / 1024 frames) runs slower than real time. What the
  modems see is unchanged, because their timers follow the sim's clock.
- **`SIM_LISTEN_STALL_S`** (default 60) ends a pair whose station stops answering, so a
  station that vanishes without closing cannot hold the sim. `0` waits forever.
- **`SIM_LISTEN_ONCE=1`** exits after the first pair ends.
- No unix sockets are involved, so this mode does not need `AF_UNIX`.

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
