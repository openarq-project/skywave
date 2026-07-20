# Skywave channel model

Skywave is an HF/VHF radio channel simulator for developing and comparing
ARQ data modems (for example VARA, ARDOP, Mercury, FreeDATA, and armstrong)
under controlled, repeatable channel conditions. This document describes
the channel model as it stands: the signal chain,
the fading model, the noise model, and the transmit/receive realism stages,
together with the standards and measurements each stage is anchored to.

## 1. Overview and design principles

Skywave produces reproducible, cross-comparable measurements of amateur-HF
ARQ data modems (2.8 kHz SSB channel) under controlled, realistic channel
conditions: goodput, robustness, acquisition, and turnaround behavior. Four
principles shape the design:

1. No silent artifacts. Every nonlinearity, clip, gate, or level change in
   the chain either models a documented real-world effect, or does not
   exist. Every stage that could clip or gate carries a telltale counter so
   an unintended artifact shows up as data rather than as a silent bias.
2. Fairness across modems under test. Equal peak-envelope-power (PEP)
   calibration, identical channel realizations (seeded random-number
   generators), and identical protocol-level operating conditions
   (half-duplex, push-to-talk, by default) so that differences between
   modems reflect the modems, not the test setup.
3. Reproducibility. Every fading and noise realization is seeded;
   deterministic runs and paired-seed comparisons are first-class.
4. Realism scaled to purpose. The model includes what measurably changes
   modem rankings or absolute performance numbers, and explicitly registers
   what it deliberately excludes rather than leaving gaps undocumented
   (Section 12).

Internal signal processing is float64 throughout. int16 quantization
appears only at the two modem-facing boundaries (transmit input, receive
output), because that is the real interface an HF modem actually faces: a
sound card.

## 2. Signal chain

```
modem TX (int16, 48 kHz)
  -> TX gain
  -> ALC model (optional)
  -> PA model (hard clip or Rapp, referenced to VSAT)
  -> TX statistics (peak / RMS / PAPR / clip fraction)
  -> Watterson fade (float64, linear, no clip)
  -> frequency offset / clock skew (optional)
  -> link delay
  -> half-duplex delivery gate
  -> + noise: AWGN, or impulsive/atmospheric (optional)
  -> + co-channel interference (optional)
  -> RX passband filter (optional)
  -> RX AGC (optional)
  -> RX level pad (-12 dB default)
  -> rail guard (telltale, expected 0)
  -> int16 cast
  -> modem RX
```

Stages marked optional are opt-in knobs with a stated default; everything
else always runs. The following sections walk the chain in a slightly
reordered sequence: the fade model, the noise model, the transmit chain,
and the receive chain, followed by two properties that cut across the whole
chain (linearity, and scheduled fading).

## 3. The fade model

### 3.1 Model and conformance basis

The fading engine is a Watterson-style Gaussian-scatter model per ITU-R
F.1487: two equal-power Rayleigh-fading taps, each a complex-Gaussian
process with a specified Doppler spread, summed to form the channel
impulse response, normalized to unit average power. The Doppler-spread
generation follows the codec2 `ch` recipe. This is conformant with F.1487
Annex 1 and MIL-STD-188-110 Appendix E.

Stated validity limits: the model is valid to about 12 kHz of channel
bandwidth (skywave operates at 3 kHz, well inside that bound), its
parameters are drawn from mid-latitude measurement campaigns, and it
assumes short-window stationarity (the tap statistics are constant over the
duration being modeled, not evolving path geometry). DSTO and CCIR critiques
in the literature note that Gaussian-scatter is "almost certainly not valid
for all HF channels," and that cross-implementation variance between
different Watterson simulator implementations is a documented problem.
Section 3.3 describes the mitigation.

### 3.2 Tap-update rate

Tap gains are regenerated at a rate

```
low_fs = max(50, ceil(32 * d2sigma))
```

in Hz, where d2sigma is the preset's Doppler-spread parameter. That is, the
tap process is updated at least 32 times the Doppler spread's characteristic
rate, subject to a 50 Hz floor, following MIL-STD-188-110C Appendix E
guidance on avoiding aliasing in the fading-tap update process. Statistical
self-verification (Section 3.3) already shows no practical aliasing at a
lower, 20x factor; the 32x figure is a conformance margin, not a measured
necessity.

The 50 Hz floor dominates for any Doppler spread at or below about 1.5 Hz,
which covers the good, moderate, and NVIS-family presets: their fading
realizations are governed entirely by the floor, independent of the exact
multiplier. The multiplier only becomes the binding constraint for the
higher-Doppler presets (low-lat-moderate and above).

### 3.3 Self-verification

Because cross-implementation variance between Watterson simulators is a
known problem in the literature, and F.1487 itself specifies no
implementation-verification procedure, skywave carries a statistical
self-verification suite that turns "F.1487-conformant" into a tested claim
rather than an assertion:

- Empirical Doppler power spectral density of the tap-gain process (Welch's
  method) compared against the theoretical bi-Gaussian F.1487 spectral
  shape, with a goodness-of-fit gate. This follows the validation method
  used in MathWorks' HF channel simulation reference.
- Rayleigh envelope distribution check on the tap-gain magnitude.
- Tap independence check (cross-correlation between the two taps close to
  zero).
- Tap-delay verification.

## 4. Presets

| Preset | Delay spread | Doppler spread | Standards anchor |
|---|---|---|---|
| good | 0.5 ms | 0.1 Hz | F.1487 mid-latitude quiet |
| moderate | 1.0 ms | 0.5 Hz | F.1487 mid-latitude moderate |
| poor | 2.0 ms | 1.0 Hz | CCIR 520-2 / MIL-STD-188-110C Poor; matches codec2 `ch --mpp`, PathSim, and DRM Channel 4 |
| low-lat-moderate | 2.0 ms | 1.5 Hz | F.1487 low-latitude moderate |
| flutter | 0.5 ms | 10 Hz | CCIR 520-2 standard flutter cell |
| NVIS | 3 ms | 1 Hz | measured NVIS field data |
| nvis-max | 4 ms | 1 Hz | measured NVIS field data, upper bound |
| disturbed / nvis-disturbed | F.1487 tail conditions | F.1487 tail conditions | F.1487 Annex 3 Section 5 disturbed tails, roughly 5% occurrence |
| auroral-max (high-latitude) | within 1-11 ms | within 2-55 Hz | DAMSON auroral-path 5%-exceedance measurements |

The `poor` preset is aligned to the canonical CCIR 520-2 / MIL-STD-188-110C
definition (2.0 ms / 1.0 Hz). The value it previously used, 2.0 ms / 1.5 Hz,
is F.1487's low-latitude moderate condition rather than mid-latitude poor,
and remains available under its correct name (`low-lat-moderate`) so it can
still be run deliberately; any number measured under the old `poor`
definition should be cited as "poor (2.0 ms / 1.5 Hz)" to avoid ambiguity.
`flutter` (0.5 ms / 10 Hz) is the one standard CCIR 520-2 cell that was
previously missing by name.

auroral-max uses DAMSON auroral-path measurements as its basis: 5%-exceedance
Doppler spread ranging from 2 to 55 Hz and delay spread ranging from 1 to
11 ms, roughly an order of magnitude beyond the mid-latitude poor preset. It
selects one operating point within that measured envelope, exercised
through the same two-tap fading engine as every other preset.

Measured field campaigns broadly validate this preset envelope for
amateur mid-latitude regional and DX use: NVIS measurements in Catalonia
show a delay spread averaging about 0.3 ms with a maximum near 2.9 ms;
WHISPER measurements show 4-10 ms at some frequencies; trans-equatorial DX
paths show delay/Doppler spreads on the order of +-3.5 ms / +-2.5 Hz. These
sit inside or near the modeled envelope.

Two gaps are worth naming explicitly. High-latitude/auroral propagation was
a real gap before auroral-max was added (DAMSON measurements put it an
order of magnitude beyond mid-latitude poor, which the earlier preset set
did not reach). Sporadic-E propagation remains unmodeled: no measured
dataset was available from which to derive delay/Doppler parameters
responsibly, and fabricating parameters would break the discipline of
keeping every model number measured and cited.

## 5. The noise model

SNR values throughout are quoted as mean-signal-power to mean-noise-power
ratio in a 3 kHz reference bandwidth, the MIL-STD-188-110 / F.1487
convention. This should be stated explicitly on any number quoted outside
skywave, since not every channel simulator uses the same reference
bandwidth.

### 5.1 AWGN: the comparability baseline

The default noise model is pure Gaussian: a fixed sigma injected after the
fade stage (noise is physically at the receiver, independent of fade
state, so this ordering is the correct physics, and it keeps sigma's SNR
meaning unchanged by any level change elsewhere in the chain). Every formal
conformance methodology surveyed is Gaussian-only at its core: ITU-R
F.1487 Annex 2 ("S/N is set by adding band limited Gaussian noise"),
MIL-STD-188-110B conformance procedures (AWGN plus ITU-R Poor), STANAG 4539
practice, and the Winlink community's own IONOS-SIM modem comparison.
Keeping the AWGN ladders pure Gaussian, uncontaminated by any other noise
layer, is what keeps skywave's numbers comparable to published modem
figures elsewhere in the field.

### 5.2 P.372 man-made noise environments

For cells meant to represent a realistic operating environment rather than
a pure comparability ladder, ITU-R P.372 supplies a closed-form man-made
noise floor:

```
Fam(dB) = c - d * log10(f_MHz)
```

with environment-specific coefficients (c / d): City 76.8 / 27.7,
Residential 72.5 / 27.7, Rural 67.2 / 27.7, Quiet-rural 53.6 / 28.6. At
7 MHz, for example, Residential comes out around 49 dB above kT0b and
Quiet-rural around 29 dB. The City-to-Quiet-rural spread is on the order of
24 dB, which exceeds most of the fading margins the rest of the model
measures: environment choice is a bigger real-world variable than most
other knobs. P.372's man-made noise dataset is geographically and
temporally dated (a documented gap in current literature), so these
categories should be read as relative (city is noisier than quiet rural)
rather than as absolute figures.

### 5.3 Impulsive and atmospheric noise

An opt-in impulsive/atmospheric noise layer models a Middleton Class-A
mixture, optionally Markov-burst-layered to produce impulse clustering,
calibrated against the ITU-R Study Group 3 reference software
(github.com/ITU-R-Study-Group-3/ITU-R-HF) rather than hand-digitized P.372
figures. The motivation is that coding/interleaver literature shows
impulsive error floors scale with the product of impulse index and block
size: a modem that looks fine under Gaussian noise can hide a much worse
impulsive floor, and no formal conformance standard tests for it. No
external golden reference curve exists for this layer, so it is calibrated
internally and self-consistently. It runs against both clean and faded
channel bases, and is kept as a separate cell family, never mixed into the
Gaussian comparability ladders of Section 5.1.

### 5.4 Co-channel interference

An opt-in co-channel interference (QRM) layer uses parameters from a
published HF interference simulator (IEEE Transactions on
Electromagnetic Compatibility; arXiv:2402.04742): Poisson interferer
arrivals (rate on the order of 6.7 per second across amateur allocations
on a busy contest weekend, scaled down for quieter-band cells), exponential
hold time per interferer around 10 seconds, and a Hall-model amplitude
distribution. Real HF systems handle interference primarily through
energy-detect-and-defer channel access (ARDOP's BUSYDET threshold,
IONOS-SIM's FFT busy detector, ALE link-quality analysis) rather than
through raw forward-error-correction robustness, so QRM test cells are
designed to exercise the ARQ layer's channel-access behavior, whether
busy-detect and backoff engage sensibly, as much as physical-layer
survival, and are run half-duplex to match real push-to-talk operation.

## 6. The transmit chain

### 6.1 Gain staging and equal-PEP calibration

Equal-PEP calibration is referenced to 0 dBFS at a TX statistics
measurement point (peak, RMS, PAPR, and clip fraction are all recorded
there). The amateur radio legal transmit limit is peak envelope power, so
equal-PEP is the correct fairness axis across modems with different
waveforms; changing the calibration target would silently rescale the SNR
meaning of every noise level already in use.

### 6.2 ALC overshoot

Automatic level control (ALC) overshoot is modeled as a burst-onset-only
transient, absent in steady state, matching measured behavior even with
disciplined drive levels: roughly 0.6-1.1 dB of settling over 20-30 ms on a
modern SDR-based rig (bench-measured on an IC-7610), rising to spikes of
around 7 dB lasting under 2 ms on older rigs. The transient re-arms after
about 5 seconds of silence, a generic first-transmission artifact reported
to generalize across rig brands (QEX-measured). That re-arm-after-silence
signature lines up closely with half-duplex ARQ turnaround structure: every
burst restarts the transient. The ALC model is a burst-onset decaying-gain
envelope with two literature-anchored presets, `alc=modern` (about 0.8 dB
over 25 ms) and `alc=legacy` (about 7 dB over 2 ms, 5 second re-arm). Real
ALC also imparts AM/PM (phase) distortion; this amplitude-only model does
not capture that, which is an acknowledged fidelity gap.

### 6.3 PA nonlinearity

PA saturation is modeled at the transmit side only, and is referenced to
the calibrated PEP level rather than to the int16 numeric rail, so that a
deliberate transmit-gain overdrive experiment (driving above the
calibration point) meets a modeled PA ceiling rather than a wire artifact.
This keeps a modem's PAPR-versus-average-power tradeoff expressed entirely
inside the PA model.

Two PA models are available. The default is a hard clip at the saturation
level (VSAT). The alternative is a Rapp soft-saturation model with a
literature default sharpness parameter p = 2, and a documented sweep range
of 1.5 to 5 (the consensus range for a "typical class-AB solid-state PA");
a plain Rapp model is known to under-model AM/AM behavior at low drive
levels, so this range should be read as an honest bound on model precision
rather than a tight fit. The validation reference is measured amateur-rig
two-tone third-order intermodulation distortion (IMD3), spanning -24 to
-46 dBc below two-tone PEP across several rigs (IC-7300 -31 to -46 dBc,
FT-991 -24 to -33 dBc, K3 -27 dBc, TS-590S -29 dBc; "below PEP" is the ARRL
QST chart convention).

The BER penalty from PA nonlinearity is mild for serial-tone or PSK
waveforms (under 0.5-1 dB near saturation), but the out-of-band splatter
penalty is sharp, and multicarrier waveforms have historically needed
1.5-2 dB more backoff than serial-tone waveforms (the DERA finding that
shaped STANAG 4539's backoff requirement). PA-backoff sensitivity should
therefore be measured separately for OFDM-like waveforms versus
serial-tone-like waveforms, with occupied-bandwidth compliance treated as a
first-class output alongside goodput, not just a secondary check. For
historical context, a 1980 ANDVT study found an optimal deliberate peak
clipping level of 8.0-9.5 dB for multitone DPSK, an early data point on the
same clip-versus-backoff tradeoff.

## 7. The receive chain

### 7.1 Passband filter

An optional receive bandpass filter models the receiver's SSB passband
ahead of demodulation, one of the opt-in receiver-emulation stages
alongside AGC.

### 7.2 AGC

The default receive chain runs without AGC. This is a field-confirmed
choice, not just a modeling preference: Furman and Nieto's HF
channel-simulator requirements paper (Harris/HFIA) states directly that "no
simulated radio filters or AGC should be used" in a channel simulator, and
identifies AGC in the loop as a documented source of inter-simulator
measurement variance. F.1487 itself treats level/gain control as an
external system effect outside the channel model. Practitioner convention
for narrowband ARQ agrees (ARDOP's documentation recommends AGC off, manual
RF gain). The RX level pad (Section 7.3) provides the level-management
function AGC would otherwise serve, without AGC's side effects.

AGC is available as an opt-in receiver-emulation stage with two
literature-anchored presets: `agc=data`, 10 ms attack / 25 ms release (the
MIL-STD-188-141C data-service requirement), and `agc=voice`, 30 ms attack /
800-1200 ms release (141C non-data timing). Notably, MIL-STD-188-110B
Appendix C actually recommends the slower voice-style timing for its own
QAM data waveform, because fast AGC pumps gain mid-burst and corrupts
amplitude-bearing constellations; that standard reserves several preamble
blocks purely for AGC settling. Amateur-rig reference points: IC-7300 SSB
time constants of 0.3/1.6/6.0 s, and PowerSDR's 2 ms attack with
50-2000 ms decay presets.

A dedicated AGC cell family is useful because burst-onset AGC pumping is a
standards-documented mechanism that specifically stresses short
synchronization preambles: after inter-burst silence, AGC gain has risen
toward the noise floor, and the first symbols of the next burst arrive
into compressed, transient gain. Modems with short preambles and modems
with longer legacy preambles are affected differently, so this is a
plausible ranking-changer between modems that no Gaussian, no-AGC cell can
reveal.

### 7.3 RX level pad

A fixed gain, -12 dB by default, is applied to signal and noise together,
immediately before the int16 cast. Because it is applied after noise
injection, it is SNR-invariant: every sigma-based noise cell and
calibration keeps its original meaning regardless of the pad setting. It
models the real practice of setting receive audio output with headroom
below ADC full scale. The default of -12 dB clears the fade stage's
measured constructive-fade-up ceiling (Section 8) with margin; a value of
0 dB restores pre-pad levels for direct comparison against older results.
Some modems' squelch behavior is sensitive to receive level; any modem that
objects to -12 dB of pad is handled with a documented per-modem exemption
(for example, a shallower pad) rather than by changing the default globally.

### 7.4 Rail guard and quantization budget

A rail guard at the final int16 cast is a pure telltale: `rail_frac` is
expected to be approximately zero after the pad, and any nonzero value is
a configuration alert rather than an expected artifact, mirroring
codec2 `ch`'s own greater-than-0.1%-output-clipping warning. Because
nothing upstream of the cast clips (Section 8), wraparound at the cast is
impossible by construction; the guard exists purely to catch a
misconfiguration.

With the pad in place, active-signal RMS sits roughly 75 dB above the
int16 quantization floor, and every noise cell in the model is
noise-dominated by orders of magnitude more headroom than that. The 16-bit
boundary at the modem interface stays both correct and realistic: real
external modems interface over a sound card and cannot reliably negotiate
a 24-bit or float32 path, so skywave does not model one.

## 8. The linear-channel property

The channel itself is linear. F.1487's model contains no nonlinearity
anywhere in the propagation path: a tapped delay line, complex-Gaussian tap
gains, summation, and additive noise. Consistent with that, nothing between
the PA model and the final int16 cast clips: the fade stage, the frequency
offset/skew stage, the noise injection, and the receive filter all operate
in float64 with no rail. The single boundary clip in the entire chain is
the int16 cast at the very end, guarded by the rail-guard telltale
(Section 7.4).

This matters because the fade stage can legitimately produce output that
exceeds 0 dBFS during a constructive fade-up. Measured across good,
moderate, and poor conditions, the worst observed excursion over a 10
minute window is about +10 dB over the mean level (P99.9 is about +8.7 dB).
Clipping the fade stage to some numeric rail, rather than letting it swing
and pulling the whole receive path down afterward, would silently distort
exactly the constructive-interference peaks a fading model exists to
produce. The RX level pad (Section 7.3) is sized to absorb that +10 dB
ceiling with margin, which is why the linear-channel property and the pad
value are a matched pair: the pad is what makes the "never clip before the
final cast" rule affordable without running out of int16 headroom.

The same principle drives where PA saturation is allowed to live (transmit
side only, referenced to calibrated PEP, Section 6.3): a nonlinearity
anywhere else in the chain would be an unmodeled artifact rather than a
deliberate, documented effect, violating the no-silent-artifacts principle
in Section 1.

## 9. Scheduled fading

Static, single-condition presets never exercise a modem's rate-adaptation
or mode-switching logic, even though every modem under test is adaptive.
Skywave supports scripted fading schedules that crossfade between presets
within a single session (for example, good to poor and back to good),
with the transition points reported as stderr ground truth so a scorer can
align modem behavior against the known schedule. This is, as far as the
available literature search turned up, not a published channel-simulator
methodology; static per-cell testing is otherwise the norm.

## 10. Timing and frequency realism

Beyond fading and noise, the model carries several timing and frequency
effects: measured, block-quantized transmit/receive keying delays; a
default link/propagation delay calibrated from measurement (144 ms); and
configurable static frequency offset and clock-rate drift (in ppm).

Slow carrier drift is modeled as an additional, optional effect. Measured
diurnal frequency-offset magnitude is on the order of 0.1 Hz in steady
state, rising to roughly 1-2 Hz through sunrise/sunset transitions over
disturbance periods of 10-80 minutes; two independent peer-reviewed studies
agree on that magnitude, though neither publishes an exact rate in Hz per
minute or a formal test protocol. Skywave models this as a
phase-continuous frequency-offset ramp, with the magnitude taken from the
literature and the ramp duration chosen as an explicit, documented modeling
choice rather than a cited figure. This matters for long ARQ sessions that
cross a sunrise/sunset ("greyline") transition. For commercial precedent,
the RapidM RS10 hardware channel simulator ships a comparable
"time-varying Doppler offset" feature.

## 11. Fairness, seeding, and half-duplex operation

Two operational conventions apply across every cell in the model, not just
the channel physics:

- Every fading and noise realization is seeded. Paired-seed comparisons
  (the same channel realization run against two configurations) are the
  primary tool for isolating the effect of a single variable.
- The chain defaults to half-duplex, push-to-talk operation, matching real
  amateur and military HF practice; full-duplex operation is available but
  should be labeled explicitly wherever it is used, since it structurally
  favors modems that can decode while their own transmitter is keyed.

## 12. Scope and limitations

Deliberate exclusions, and the reasoning behind each:

- 24-bit or float32 modem interconnect. Not modeled; int16 is the realistic
  sound-card interface real external modems actually use, and the
  quantization budget after the RX pad (Section 7.4) is already generous.
- Wideband (over 3 kHz) channel models. Out of scope; the model targets the
  2.8 kHz amateur/legal HF SSB channel, though F.1487's own validity bound
  extends to about 12 kHz should a future need arise.
- Full nonlinear ionospheric effects beyond Gaussian scatter (Doppler
  flutter, traveling ionospheric disturbances). Not modeled; the disturbed
  presets (F.1487 tail conditions, Section 4) cover the tail behavior
  currently tested for, and this is revisited only with measured evidence
  that a target path class needs more.
- Sporadic-E propagation. No preset exists, because no measured dataset was
  available from which to derive delay/Doppler parameters responsibly.
- The full ITU latitude-by-condition preset matrix. Not implemented; the
  preset set in Section 4, plus the auroral high-latitude cell, span the
  useful range for amateur HF use, and adding every published cell would be
  scope without a concrete user.
- Military numeric pass/fail acceptance thresholds (for example,
  probability-of-linking tables or formal certification gates). Not
  imported, since they are calibrated to specific waveforms and acquisition
  contexts different from the modems skywave targets. Where a methodology
  pattern from that literature is reusable, such as probability-of-linking
  as a statistic, or the calling-transmission-to-link-established
  definition of link-setup time, it is adopted; the specific pass/fail
  numbers are not.
- Multi-station, contended-channel operation (multiple simultaneous
  transmitters, collision physics, channel-access arbitration among several
  stations). Out of scope as a channel-model concern: it is a distinct
  shared-medium subsystem that would need its own design, not a channel
  knob. The co-channel interference model (Section 5.4) covers
  single-interferer QRM robustness without the full multi-station
  machinery.
