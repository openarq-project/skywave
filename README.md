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
run. It ships with an in-process reference adapter and a Mercury adapter. More are welcome;
the ModemAdapter contract is documented in docs/MODEM-ADAPTER-CONTRACT.md.

## Quick start

A one-way channel filter, compatible with the codec2 `ch` tool:

```
python3 hfchan.py --No -20 --fade poor < tx.s16 > rx.s16
```

In-process, from a typed config:

```python
from channel_config import ChannelConfig
from channel import Channel

ch = Channel(ChannelConfig(sigma=200, watterson="poor"))
rx_block = ch.process(tx_block)
```

Compare a modem across a set of cells:

```
python3 sweep_runner.py mymodem cells.json out.csv
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
  a modem.
- [docs/TRANSPORT.md](docs/TRANSPORT.md): running with or without an ALSA loopback rig.
- [docs/TRANSPORT-DESIGN.md](docs/TRANSPORT-DESIGN.md): the socket and virtual-clock
  transport design.
- [docs/EQUAL-PEP.md](docs/EQUAL-PEP.md): equalizing transmit drive (PEP) across modems
  for a fair comparison, and the `--calibrate-pep` command.

## License

Apache-2.0.

## Status

Newly extracted from the OpenARQ bench. Interfaces may still change before 1.0.
