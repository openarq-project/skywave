# skywave

An HF/VHF radio channel simulator and comparative modem test harness.

skywave comes out of the OpenARQ project (openarq.org), and is packaged so that any
modem project can use the same channel and the same measurements.

## What it does

skywave is two things that share one channel model.

The channel simulator reproduces HF and VHF propagation and radio-chain effects. You can
run it as a one-way filter over a file, as a two-station half-duplex link, or as an
in-process Python object. It models:

- Watterson/CCIR ionospheric fading, as named presets or a custom delay/Doppler pair, with
  optional scheduled fade over the course of a run
- ITU-R P.372 background noise environments and impulsive noise
- a transmit chain: ALC overshoot, Rapp soft-PA compression, drive level
- a receive chain: SSB rig passband, AGC, level pad
- co-channel interference (QRM), carrier frequency offset, sample-clock skew, and
  transmit/receive turnaround timing
- per-direction asymmetry, so the forward and reverse paths can differ (for example a weak
  ACK path)
- an FM port profile with mic/speaker and 9600-baud paths, CTCSS, squelch, and FM fade

The test harness drives modems through that channel and scores them against each other. A
modem is added by writing one adapter against a documented contract; it then gets the whole
channel, transport, and scoring stack. The harness provides:

- a ModemAdapter contract, with an in-process reference adapter to copy from
- an ALSA-loopback transport for hardware-faithful runs, and a portable unix-socket
  transport that needs no loopback hardware
- channel and transport profiles as small TOML files, with environment variables that
  override them
- a versioned results schema for the output corpus

## Adapters

One goal of skywave is a growing collection of adapters covering the modems people actually
run. A modem is added by writing one adapter against the ModemAdapter contract (documented
in docs/MODEM-ADAPTER-CONTRACT.md); it then gets the whole harness for free. The adapters
that ship today:

| Adapter | Modem | Notes |
|---|---|---|
| `loopback` | in-process reference | no external modem — copy this to start a new adapter |
| `mercury` | Mercury HF | TCP TNC |
| `armstrong` | Armstrong (OpenARQ reference) | ALSA loopback; native unix-socket transport optional |
| `ardop` | ARDOP (`ardopcf`) | includes chunked, buffer-throttled bulk TX |
| `vara` | VARA HF | proprietary, typically under Wine; two instances brought up by an external up/down lifecycle |
| `freedata` | FreeDATA | REST + websocket; runs under FreeDATA's own venv (set `ADAPTER_PY`) |
| `modem73` | modem73 | datagram KISS TNC with no native ARQ — the adapter runs its own selective-repeat transfer over the air (set `MODEM73_BIN`) |

More are welcome.

### Vector adapters (PHY mode characterization)

`ModemAdapter` measures a modem as a **system** — ARQ, turnaround, CSMA, goodput over a
real link. That is the wrong instrument for characterizing individual **modes**: an ARQ
layer's ACKs ride the data mode, retransmission integrates over channel draws, CSMA airtime
is charged to the mode, and a transfer deadline turns "slow" into "failed".

So there is a second, parallel family: `VectorAdapter`, a **frame codec** contract with no
stations, no link, no PTT and no wall-clock timing. It measures one-way FER vs SNR, one
frame per trial, zero retries; goodput and floors derive from that curve analytically. The
two families share only the channel model. Contract:
[docs/VECTOR-ADAPTER-CONTRACT.md](docs/VECTOR-ADAPTER-CONTRACT.md).

The contract is **files, not an API** — a float32 vector plus a JSON sidecar — so adapters
are language-agnostic and need no FFI:

| Adapter | Implementation | Rate |
|---|---|---|
| `vector_modem73` | wraps a C++ binary (`modevector`) | 48 kHz |
| `vector_armstrong` | wraps `cargo run -p phy --example vector` (Rust) | 8 kHz |

Sample rate is declared per vector, not assumed — the two shipped adapters differ by 6×,
and both run through identical channel code. Every adapter inherits a required selftest
gate (a clean round trip must decode every frame; a deep-noise one must decode none).

```sh
python3 -m skywave.vector_sweep  --adapter armstrong --out out/sweep.csv \
    --presets off,good,moderate,poor --frames 150 --snr-lo -12 --snr-hi 30
python3 -m skywave.vector_report --sweep out/sweep.csv     # gates, exit 0/1/2
python3 -m skywave.vector_analyze --sweep out/sweep.csv --outdir out
```

Rows from different adapters can share one CSV, so modes from different modems land on a
single Pareto frontier measured with one channel generation and one SNR convention.

## Install

skywave is a `src/`-layout Python package (`skywave`), needs Python 3.11+, and
depends on numpy and scipy. From a checkout:

```
pip install -e .          # editable; add [test] for pytest: pip install -e ".[test]"
```

This puts the `skywave` package on the path and installs four console scripts:
`hfchan`, `skywave-sweep`, `skywave-channel`, and `skywave-score-transitions`.
You can also run any entry point without installing, straight from `src/`, with
`PYTHONPATH=src python3 -m skywave.<module>`.

### Platforms

The channel sim, the DSP, and the full test suite run on Linux and macOS. The
device-free `sock` transport runs on both, so a modem with a native socket audio
backend can be benchmarked with no audio hardware. The real snd-aloop **ALSA rig is
Linux-only**; off Linux the harness says so and points at the `sock` transport.
Windows is not supported yet (the small, catalogued gaps are in
[docs/PORTABILITY.md](docs/PORTABILITY.md)). On macOS you need Python 3.11+ (the
system 3.9 is too old); see [docs/PORTABILITY.md](docs/PORTABILITY.md) for a
one-command setup and a device-free end-to-end example.

## Quick start

A one-way channel filter, compatible with the codec2 `ch` tool:

```
hfchan --No -20 --fade poor < tx.s16 > rx.s16
```

In-process, from a typed config:

```python
from skywave.channel_config import ChannelConfig
from skywave.channel import Channel

ch = Channel(ChannelConfig(sigma=200, watterson="poor"))
rx_block = ch.process(tx_block)
```

Compare a modem across a set of cells:

```
skywave-sweep mymodem cells.json out.csv
```

Score what a modem did when the channel changed under it. A cell with a scheduled
fade (`"env": {"SIM_FADE_SCHEDULE": "good:120,poor:180,good:0"}`) is the only one
that exercises adaptive rate control; run it with delivery ticks on, then join each
transition to the modem's own behaviour around it:

```
SKYW_PROGRESS_S=5 skywave-sweep mymodem sched_cells.json out.csv
skywave-score-transitions out.csv -o transitions.csv
```

## Documentation

Channel model and physics:

- [docs/CHANNEL-MODEL.md](docs/CHANNEL-MODEL.md): how the channel model works, stage by
  stage, with the standards and measurements each stage is anchored to.
- [docs/CHANNEL-CONDITIONS.md](docs/CHANNEL-CONDITIONS.md): the HF channel-conditions
  literature survey behind the fading presets.
- [docs/QRM-MODEL.md](docs/QRM-MODEL.md): the co-channel interference (QRM) model.
- [docs/FM-PORT.md](docs/FM-PORT.md): the FM and VHF port profiles.
- [docs/BANDWIDTH.md](docs/BANDWIDTH.md): occupied bandwidth and regulatory limits.

Literature basis (the measurement and standards sources behind the models):

- [docs/references/HF-NOISE.md](docs/references/HF-NOISE.md): atmospheric, man-made,
  and co-channel interference noise (ITU-R P.372 and related).
- [docs/references/TRANSCEIVER-CHAIN.md](docs/references/TRANSCEIVER-CHAIN.md): receiver
  AGC, transmitter ALC, and PA nonlinearity, with measured rig data.
- [docs/references/RIG-REALISM.md](docs/references/RIG-REALISM.md): a gap analysis
  comparing a real HF station against a naive AWGN channel.
- [docs/references/NVIS-DELAY-SPREAD.md](docs/references/NVIS-DELAY-SPREAD.md): NVIS
  delay spread and guard-interval sizing.

Validation and comparison:

- [docs/COMPARISON.md](docs/COMPARISON.md): skywave versus the other open-source HF
  channel simulators.
- [docs/CROSS-CALIBRATION.md](docs/CROSS-CALIBRATION.md): validating the fade against a
  reference implementation.

Harness and transports:

- [docs/MODEM-ADAPTER-CONTRACT.md](docs/MODEM-ADAPTER-CONTRACT.md): the contract for adding
  a modem, including the optional byte-vs-time delivery curve.
- [docs/TRANSPORT.md](docs/TRANSPORT.md): running with or without an ALSA loopback rig.
- [docs/TRANSPORT-DESIGN.md](docs/TRANSPORT-DESIGN.md): the socket and virtual-clock
  transport design.
- [docs/PORTABILITY.md](docs/PORTABILITY.md): platform support (Linux/macOS), the
  device-free path off Linux, and what a Windows port needs.
- [docs/EQUAL-PEP.md](docs/EQUAL-PEP.md): equalizing transmit drive (PEP) across modems
  for a fair comparison, and the `--calibrate-pep` command.

## License

Apache-2.0.

## Status

Newly extracted from the OpenARQ bench. Interfaces may still change before 1.0.
