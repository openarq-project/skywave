# Rig-realism gap analysis (literature basis)

This document is a point-in-time gap analysis from early in skywave's
development. It compares what a real amateur HF station's radio,
propagation path, and operating practice actually do against a naive
channel model that adds only flat AWGN to a Watterson-faded signal,
across sixteen axes of realism, each anchored to measurement literature.
The values recommended in the final section are now implemented as
skywave configuration knobs, documented in full in CHANNEL-MODEL.md;
this document exists to record their literature basis, not to describe
current build status.

## 1. Headline calibration result

The field's own comparative-test convention is Watterson two-path
fading plus plain band-limited AWGN. ITU-R F.1487 Annex 2 Section 2
states the official modem-comparison methodology outright: "The S/N is
set by adding band limited Gaussian noise." FreeDV/codec2 test tooling
and published VARA/ARDOP comparisons follow the same convention.
NTIA's HF Simulator report (E. Johnson) adds the known bias direction:
real HF noise is mainly impulsive, and pure-AWGN testing at matched
average SNR often yields higher modem error rates than measured HF
noise. So a naive AWGN-only channel model sits at parity with the
industry baseline, and the gaps below are differentiation and rigor,
not remediation of a broken baseline. What such a model typically
lacks: keying and turnaround time, path travel time, frequency and
clock offset, and TX/RX chain dynamics. All four are typical conditions
on a real HF circuit, not corner cases.

## 2. Gap matrix

| # | Axis | Real world (cited) | Gap identified | Gap severity for modem comparisons |
|---|------|--------------------|-----------------|-------------------------------------|
| 1 | PTT to RF attack | Measured IC-7300: 7.7 ms (TX-delay off) to 30.6 ms (30 ms menu setting) [dh1tw.de scope measurement]. Mechanical antenna relays pull in over 20-30 ms; PIN-diode rigs (K3/K4) are near-zero. Community empirical floor: ARDOP leader 160 ms default, VARA TX-delay 100-500 ms, JS8Call 200 ms, AX.25 250-500 ms; the software padding encodes real-world attack, VOX, and interface slop. | A naive model applies PTT instantly: keying delay and relay pull-in do not exist, so every turnaround is free. | HIGH for absolute half-duplex goodput: every turnaround is free in a naive model. A modem with tight windowed ARQ profits most from free turnarounds, so cross-modem half-duplex gaps are understated for stop-and-wait peers. |
| 2 | TX to RX recovery (unkey) | Relay release about 10-25 ms (G90 sequencer margin 25 ms; IC-7300 about 4 ms RF tail, community-reported); RX AGC recovery 4.5-12 ms after a strong signal (AGC literature); K3 default TX delay 8 ms; hang-AGC tails up to about 1 s exist. | A naive model releases the channel instantly on unkey: relay release delay and AGC recovery after a strong signal do not exist. | HIGH, same mechanism as row 1, other side. Any turnaround-timing constant tuned against a zero-cost assumption inherits this gap. |
| 3 | VOX keying | Attack fast but poorly documented; hang 30-3000 ms across rigs (FT-991A 30-3000 ms, default 500; TS-590SG voice default 1500 ms; SignaLink 15 ms-3 s knob). | VOX hang time is a distinct mechanism from PTT keying delay (rows 1-2); a naive model either omits it or, where present, tunes it far below any real rig default. | LOW for most comparisons, since most stations key via PTT rather than VOX. MED for a VOX-keyed target profile: a real 500 ms hang would serialize turnarounds brutally. |
| 4 | Half-duplex semantics | A keyed station hears nothing off-air (own sidetone only, via the radio, not the channel); when both stations key at once, capture effect can let the stronger signal survive at the peer rather than both being lost. | A naive model enforces deaf-while-transmitting behavior with perfect squelch and mutual loss on any collision. | LOW-MED. Perfect squelch and mutual-loss collisions are conservative assumptions; a real capture effect would occasionally rescue collisions a naive model always destroys. |
| 5 | Rig passband and group delay | SSB TX filters run 300-2700 Hz standard, 100-2900 Hz wide (IC-7300 bandwidth presets); RX DSP latency measured at 7.4 ms (3.6 kHz) to 17 ms (250 Hz) [Farson, IC-7610]; the worst group-delay ripple sits between the -3 dB and -10 dB skirt points, exactly where wide-mode edge carriers live. | A naive model runs an effectively flat passband from DC to Nyquist, with none of the roll-off or group-delay ripple a real SSB transmit/receive filter chain imposes. | HIGH for wide modes: a flat passband overstates any waveform whose edge subcarriers would sit inside a real rig's filter skirt. A modem's wide-mode advantage may partly be a rig-passband-off artifact. |
| 6 | Noise floor shape | P.372-9: Fam = c - d.log10(f); residential at 7 MHz is about 49 dB, rural about 44 dB, above kT0b; within a 3 kHz slice the spectrum is effectively flat. What differs from Gaussian is amplitude statistics: Vd (RMS-to-average envelope ratio, 200 Hz reference bandwidth) is about 1.05 for Gaussian noise, typically 2-8 at temperate-latitude HF, higher in storm season. | A naive model uses flat white Gaussian noise; the axis it omits is amplitude statistics, not spectral shape, since within a 3 kHz slice real HF noise is spectrally close to flat already. | MED. Parity with the field standard (Section 1). Closing the gap means an opt-in Vd-calibrated impulsive layer (a Hall-model fit to a target Vd from P.372 Figs. 39-40), which makes the model more realistic than the field baseline, in the direction the NTIA finding predicts: AWGN is pessimistic at matched SNR. |
| 7 | Impulsive QRN (static crashes) | Lightning stepped leader 2-30 ms, flash 40-200 ms, impulses 60 dB or more above the Gaussian background; the P.372 Vd/APD curves are the ITU-sanctioned quantification. No authoritative HF-specific Middleton (A, Gamma) pair exists in the open literature (a genuine open gap). | A naive model has no impulsive-noise layer at all. | MED, a prerequisite for the QRM item below; recommended shape in Section 3. |
| 8 | QRM (interferers) | Mendieta-Otero et al. (IEEE TEMC 2014): Poisson arrivals, lambda = 6.68/s at a contest peak, exponential duration with a 10 s mean, about 44% key-down duty, Hall-amplitude, raised-cosine keying; a complete, citable CW-QRM generator. Over-the-horizon radar: about 10 sweeps/s, 160-360 kHz wide [IARUMS]. | A naive model has no co-channel interference at all. | MED. |
| 9 | Path travel time | About 3.33 microseconds/km slant range. NVIS one-hop about 1.7-2.0 ms; a 1000 km one-hop F2 path about 3.9 ms; 3000 km about 10.2 ms. A continental-scale contact sees 0-10 ms one-way, so 0-20 ms of extra round-trip time per turnaround; typical, not corner-case. | A naive model has zero propagation delay: every turnaround pays 0 ms where reality pays 2-20 ms round trip. | MED-HIGH for turnaround-heavy profiles (any control-plane exchange with frequent turnarounds). Cheap to close, since realistic one-hop delay is well under typical PTT hang time. |
| 10 | Frequency offset | Two-station LO mismatch: a modern TCXO gives about +/-0.5 ppm, about +/-7 Hz at 14 MHz per side; non-TCXO rigs run up to about 10 ppm, about 140 Hz. Ionospheric Doppler shift is sub-Hz in quiet conditions, up to about 2 Hz under disturbance. Typical pair offset: +/-7-15 Hz; worst case about +/-100-150 Hz. | A naive model runs both stations perfectly frequency-locked to each other. | HIGH for acquisition realism (sync and mode-id detection tuned at zero offset); the typical real-world case is nonzero. |
| 11 | Sample-clock ppm | Consumer sound cards run up to about +/-100 ppm, about 1-5 ppm once warmed up; a realistic two-station differential is 10-50 ppm, worst case 150-200 ppm. The mechanism differs from RF frequency offset: it is accumulating symbol-timing skew proportional to ppm times burst length, not a static tone shift. | A naive model runs both stations' sample clocks perfectly locked, so it has no accumulating symbol-timing skew across a burst. | MED-HIGH for long bursts (a windowed-ARQ data burst is among the longest on-air objects a modem transmits): clock skew stresses a tracking loop differently than a static frequency offset does. |
| 12 | TX ALC dynamics | ALC is a closed-loop gain control: burst-onset overshoot measured at 0.2 dB (IC-9700) to about 6-8 dB (IC-706MKII, 130-145 W on the first dit regardless of the power setting, under 2 ms pulse); ALC action on a data signal grew FT8 splatter from about 400 Hz to 9 kHz at -60 dB [G4DBN]. Time constants: modern DSP ALC is near-instant; legacy analog loops attack in milliseconds and release over 100 ms to several seconds. | A naive model applies a static, memoryless clip or saturation curve, which cannot produce burst-onset gain overshoot or gain pumping at all. | MED-HIGH. Drive is often modeled statically, but a real rig's first symbols of every burst are hotter than steady state; directly relevant to short acquisition and control bursts, where overshoot covers proportionally more of the burst. |
| 13 | RX AGC | Presets: IC-7300 SSB fast/mid/slow are 0.1/2/6 s; attack 1-5 ms (classic) to about 20 ms (audio-derived); gain is wrong for the first symbols after a quiet gap until the loop settles; data-mode practice favors fast AGC, and direct-sampling rigs advise against AGC off. | A naive model uses fixed receive gain with no AGC, so it has no burst-head gain error after a quiet gap. | MED. Burst-head SNR penalty after quiet gaps is unmodeled; the same burst-head sensitivity class as rows 1 and 12. |
| 14 | TX IMD / PA model | Amateur PA IMD3 at rated PEP: -24 to -39 dBc measured (IC-7610, by band; FT-991 -37 dBc); ITU-R SM.326 guideline is -25 dBc. Literature Rapp exponent p for a class-AB SSPA: about 1-5 (p about 1.6 is a practical fit; p above 5 approaches a hard limiter). | Where a PA model is present at all, a Rapp curve with a literature-typical exponent already spans the measured range. | LOW; the Rapp knob spans the measured range once enabled. The gap is mainly whether drive-sensitivity comparisons remember to turn it on rather than defaulting to a hard clip. |
| 15 | Regulatory bounds | 47 CFR 97.3(a)(8): occupied bandwidth uses the -26 dB mean-power definition; the HF data ceiling is 2.8 kHz; spurious emissions must be 43 dB or more down (post-2003, under 30 MHz); the PEP cap is 1.5 kW, with typical data operation at 25-100 W. | Not a gap; already the model's gate (see the bandwidth doc), occupied bandwidth at or under 2.8 kHz. | None; context that bounds which scenarios matter. |
| 16 | Doppler flutter (completeness) | Polar cap about 3 Hz (summer, 90th percentile) to 6-8 Hz (winter); trans-auroral paths measured up to about 9.5 Hz worst case. | Covered by the high-latitude preset (7 ms / 30 Hz), which already envelopes these values. | None; covered. |

## 3. Recommended values, now implemented

**T/R timing profile** (rows 1-3), implemented as `SIM_TR_KEY_MS` and
`SIM_TR_UNKEY_MS`, with per-edge randomness available via
`SIM_TR_JITTER_MS`:

- `typical` (a modern relay rig, for example an IC-7300-class
  transceiver): `SIM_TR_KEY_MS=15`, `SIM_TR_UNKEY_MS=25` [dh1tw measured
  7.7-10.7 ms plus relay margin; G90 sequencer 25 ms release margin].
- `conservative` (an older rig or a padded interface chain):
  `SIM_TR_KEY_MS=30`, `SIM_TR_UNKEY_MS=50` [relay pull-in 20-30 ms; AGC
  recovery tail].
- The community's 100-500 ms TX-delay numbers are what a modem pads on
  top of this, not what the rig itself needs: the model represents the
  rig (15-50 ms) and lets a modem's own lead-in policy pay its own
  padding, which is exactly the goodput-relevant difference between
  modems. See the channel-model doc for exact preset wiring.

**Path delay** (row 9): `SIM_LINK_DELAY_MS`, default 3 ms (a
mid-latitude one-hop path), sweepable across 0-10 ms [3.33
microseconds/km; NVIS 1.7-2 ms; a 3000 km hop about 10.2 ms].

**Frequency offset** (row 10): `SIM_FOFF_HZ`, applied per direction
with opposite sign on each side; default about +/-10 Hz, with a stress
knob up to +/-100 Hz [TCXO +/-0.5 ppm per side gives about +/-7 Hz at
14 MHz, plus up to 1-2 Hz of ionospheric Doppler; non-TCXO rigs reach
about 140 Hz]. A slow drift on top of the static offset is modeled
separately via `SIM_FOFF_RAMP_HZ` and `SIM_FOFF_RAMP_S` (see the
channel-model doc's timing and frequency realism section).

**Clock ppm** (row 11): `SIM_CLOCK_PPM`, modeled as resampling drift
rather than a static tone shift; default 25 ppm differential, stress
150 ppm [sound-card field data: 10-50 ppm typical differential, about
200 ppm worst case].

**Rig passband** (row 5): `SIM_RIG_BPF`, with edge frequencies and
filter order tunable via `SIM_RIG_LO`, `SIM_RIG_HI`, and
`SIM_RIG_ORDER`. A data-style passband (about 150-2900 Hz) is a
reasonable default rig profile, with a narrower voice-style passband
(about 300-2700 Hz) kept as a conservative sweep point [IC-7300 wide
100-2900 Hz / mid 300-2700 Hz].

**Noise upgrades** (rows 6-8): `SIM_NOISE_VD` adds a Vd-calibrated
impulsive layer on top of the Gaussian core, with target-Vd presets
translated from the 200 Hz P.372 reference bandwidth via Figs. 39-40.
`SIM_NOISE_ENV` selects a P.372 man-made-noise environment (city,
residential, rural, quiet) for the noise-floor shape. Co-channel
interference is covered separately by the `SIM_QRM_*` family
(occupancy, INR distribution, and an optional swept carrier); see the
QRM model doc for the full parameterization.

**TX/RX dynamics** (rows 12-13): `SIM_ALC_PRESET` selects a burst-onset
ALC model ahead of the PA stage, `modern` (about 0.8 dB of overshoot
over about 25 ms) or `legacy` (about 7 dB over about 2 ms, with roughly
a 5 second re-arm time). `SIM_RX_AGC_MODE` selects a receive AGC model
applied after noise injection, `data` (about 10 ms attack / 25 ms
release) or `voice` (about 30 ms attack / around 1000 ms release).
Exact preset numbers and signal-chain placement are documented in the
channel-model doc; the literature basis is Salas (QEX 2018) and
Farson's IC-7610/IC-705 measurement tables for ALC, and IC-7300 manual
data plus the AGC literature for the receive side.

## 4. Sources

Primary standards (ITU-R F.1487, ITU-R P.372-9) were fetched and
text-extracted directly; the sources behind the citations above are:

- ITU-R F.1487 (05/2000): channel table (delay/Doppler per latitude and
  condition), Annex 2 test methodology, Watterson validity (Section 4:
  validated to 3 kHz, arguably to 12 kHz; stationarity about 10
  minutes).
  https://www.itu.int/dms_pubrec/itu-r/rec/f/R-REC-F.1487-0-200005-I!!PDF-E.pdf
- ITU-R P.372-9 (and the current -17): Fam = c - d.log10(f) with (c, d):
  city (76.8, 27.7), residential (72.5, 27.7), rural (67.2, 27.7),
  quiet-rural (53.6, 28.6), galactic (52.0, 23.0); decile spreads;
  Vd/APD Figs. 39-40.
  https://www.itu.int/dms_pubrec/itu-r/rec/p/R-REC-P.372-9-200708-S!!PDF-E.pdf
- dh1tw.de IC-7300/IC-9700 TX-delay scope measurements:
  https://dh1tw.de/2020/01/icom-ic7300-and-ic9700-variable-tx-delay-verified/
- Farson (AB4OJ) IC-7610/IC-705/IC-7100/IC-9700 test reports (ALC
  overshoot, AGC, IMD, DSP latency):
  https://www.qsl.net/ab4oj/icom/ic7610/7610notes.pdf
- Salas, "Amplifier Overshoot-Drive Protection," QEX Sep/Oct 2018
  (IC-706MKII 130-145 W first-dit overshoot):
  https://www.arrl.org/files/file/QEX_Next_Issue/Aug-Sep2018/Salas1.pdf
- G4DBN, FT8/ALC splatter measurement: https://www.g4dbn.uk/?p=783
- Mendieta-Otero, Perez-Alvarez, Perez-Diaz, "Interference Simulator
  for the Whole HF Band," IEEE Trans. EMC 2014:
  https://arxiv.org/pdf/2402.04742
- NTIA "HF Simulator: Channel and Modem Modules" (E. Johnson), the
  impulsive-noise bias statement: https://its.ntia.gov/
- ARRL/IARUMS OTHR interference reporting:
  https://www.arrl.org/news/observations-of-over-the-horizon-radar-interference-in-ham-bands-top-all-others
- VARA/ARDOP/JS8Call/Winlink TX-delay setup guides (the community
  empirical floor): ARDOP TNC spec (leader 160 ms), VARA setup guides
  (100-500 ms), JS8 guide (200 ms), Winlink packet guides (250-500 ms).
- Rig manuals: FT-991A (VOX 30-3000 ms), TS-590SG (VOX 150-3000 ms,
  break-in 50-1000 ms), IC-7300 (AGC 0.1/2/6 s presets, bandwidth
  presets), SignaLink manual (hang 15 ms-3 s).
- Sound-card clock data: qsl.net/dl4yhf frequency-calibration notes
  (a measured 44 ppm card; thermal drift about 1.5 ppm per 30 minutes),
  fldigi WWV calibration docs.
- Path-delay geometry: standard slant-path physics; F2 hop limits
  (Wikipedia F2 propagation; EA4FSI NVIS guide).

Confidence flags: the IC-7300 TX-delay curve, the F.1487/P.372 numbers,
the Farson tables, the FT-991A/TS-590SG manual ranges, and the
ARDOP/VARA/JS8 defaults are primary or manufacturer grade. Anecdotal,
use with care: the IC-7300 4 ms RF tail, the G90's roughly 60 ms
turnaround, DigiRig latency (no numeric spec exists), and K3/K4
PIN-diode switching speed (class-generic numbers only). Open gaps: no
HF-calibrated Middleton (A, Gamma) pair exists in the open literature;
MIL-STD-188-110 impulsive/interferer injection levels are not freely
accessible; no published 40 m/20 m amateur band-occupancy study was
found.
