# Equal-PEP drive calibration

Different modems emit different waveforms, so at the same nominal audio level they
hit the channel at different peak envelope powers (PEP). PEP is the amateur legal
transmit limit, so the fair way to compare modems is at *equal PEP* -- otherwise a
modem that simply drives harder looks better for a reason that has nothing to do
with its DSP. The channel-model doc (Section 6.1) covers why PEP, not RMS, is the
fairness axis.

skywave equalizes drive with a per-modem `TXGAIN`: a scalar the channel applies to
that modem's transmit audio before the PEP clip. Calibration writes one number per
modem to `results/<modem>_txgain.txt`, and `sweep_runner` applies it automatically
to every cell, so all modems in a campaign transmit at the same PEP. Without a cal
file a modem runs at `TXGAIN=1.0` (uncalibrated) -- fine for a single-modem smoke,
but **not** a fair cross-modem comparison.

## Measuring it: `--calibrate-pep`

    python3 sweep_runner.py --calibrate-pep <modem> [target_dbfs] [payload] [timeout]

This runs the modem once on a clean channel (`SIGMA=0`) at `TXGAIN=1.0` with signal
stats on, reads its robust TX peak, and writes the `TXGAIN` that puts that peak at
the target:

    TXGAIN = 10^(target_dbfs / 20) * 32767 / robust_peak

Defaults: `target_dbfs=-1` (1 dB below the int16 full-scale rail, leaving headroom
for the constructive fade-up and noise before the rail), `payload=1500`, `timeout=70`.

    $ python3 sweep_runner.py --calibrate-pep armstrong
    measuring armstrong TX peak (clean run, payload=1500 B) ...
      .11: robust_peak=17232 (-5.6 dBFS)  rms=4600  papr=11.4 dB
      .22: robust_peak=17232 (-5.6 dBFS)  rms=4600  papr=11.4 dB
    armstrong: robust_peak=17232 (-5.6 dBFS), PAPR=11.4 dB  ->  TXGAIN=1.6948  (target -1 dBFS)
    wrote .../results/armstrong_txgain.txt

Run it once per modem, on the transport you will bench on (the ALSA loopback rig for
the ALSA modems; `SIM_TRANSPORT=sock` for a device-free armstrong run). It normalizes
off the `robust_peak`, which excludes the loopback cold-start transient -- setting the
gain off the raw peak would key it to that glitch and under-drive the modem by several
dB.

## Notes

- Re-run after any change that moves a modem's TX peak (a mode or geometry change, a
  clipping-policy change).
- `EQUAL_GAIN=1` forces `TXGAIN=1.0` for every modem regardless of the cal files --
  the uncalibrated baseline.
- The target is referenced to the int16 full-scale rail. Do not retarget it casually:
  it rescales the SNR meaning of every noise level already in use (channel-model doc,
  Section 6.1).
- The in-process reference adapter (`loopback`) has no real transmit chain, so there
  is nothing to calibrate -- `--calibrate-pep` only applies to modems that put audio
  through the channel.
