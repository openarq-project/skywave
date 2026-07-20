# Literature-basis references

This folder collects the measurement and standards literature behind
skywave's channel-realism models. Each page is a sourced survey: it keeps
the measured numbers, rig data, and citations in one place, and maps them
to the simulator knobs they inform. The implemented behavior lives in the
top-level docs (CHANNEL-MODEL.md, QRM-MODEL.md, and so on); these pages are
the "why these values" backing for it.

- [HF-NOISE.md](HF-NOISE.md): atmospheric and impulsive noise (ITU-R P.372),
  the man-made noise floor, and co-channel interference. Backs the noise
  model and the QRM model.
- [TRANSCEIVER-CHAIN.md](TRANSCEIVER-CHAIN.md): measured receiver-AGC,
  transmitter-ALC, and PA-nonlinearity behavior across a range of HF rigs.
  Backs the transmit- and receive-chain stages.
- [RIG-REALISM.md](RIG-REALISM.md): a sixteen-axis gap analysis comparing a
  real amateur HF station against a naive flat-AWGN channel, with the
  literature basis for the realism knobs (T/R timing, path delay, frequency
  and clock offset, rig passband, and the noise and transceiver-chain
  layers above).
- [NVIS-DELAY-SPREAD.md](NVIS-DELAY-SPREAD.md): measured NVIS delay spread
  versus the standards' worst-case tiers, as background for sizing a
  waveform's guard interval or cyclic prefix.
