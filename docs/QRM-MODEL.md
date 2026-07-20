# Co-channel interference (QRM) model

skywave models co-channel interference from other on-air HF traffic (QRM):
Morse-keyed carriers that come and go inside the receive passband, plus an
optional swept carrier standing in for an over-the-horizon radar (OTHR)
source. See the channel-model doc for where QRM sits in the overall
simulation chain; it is applied as an additive interferer stream before the
final pad and rail stage.

## 1. Overview

The model has two parts:

1. An in-channel CW interferer process. Interferers arrive and hold the
   channel one at a time (Section 2), each drawing an interference-to-noise
   ratio (INR) once at spawn (Section 3), keyed with a raised-cosine PARIS
   Morse envelope (Section 4).
2. An optional swept carrier that models a chirping OTHR source crossing the
   passband periodically (Section 5).

All interferer levels are channel-referenced: INR is the interferer's
carrier power over the in-channel noise power sigma^2, not an absolute field
strength. This keeps the model meaningful on a channel that is defined by
sigma (the noise standard deviation) rather than by antenna, location, and
date. Section 6 covers the rail budget constraint that keeps the combined
signal, noise, and interference under the pre-pad clip threshold.

Timing and keying follow the interference-simulator literature (Section 8)
closely. Density and level are re-keyed to an occupancy fraction and an INR
distribution so the model stays meaningful across arbitrary signal levels
and passband widths, rather than reproducing the source paper's
whole-HF-band, absolute-dBm parameterization directly.

## 2. Occupancy: arrivals and hold time

Occupancy is the fraction of time an in-channel CW interferer is present. It
is modeled as an Erlang-loss queue (M/G/1/1): Poisson arrivals, exponentially
distributed hold times, and a concurrency cap of one active interferer at a
time (M=1); arrivals that occur while the channel is already occupied are
lost rather than queued.

The knob is SIM_QRM_OCC, in [0, 1). For an M/G/1/1 queue the busy fraction
is a/(1+a) with a = lambda * mu_d, so the spawn rate needed to reach a target
occupancy is

    lambda_spawn = occ / (mu_d * (1 - occ))

with hold time mu_d = 10 s, matching Table I of the interference-simulator
paper cited in Section 8.

### Why one carrier at a time

Realistic occupancy rarely produces two simultaneous in-channel
interferers. At a contest-peak occupancy of about 0.04 (derived below) the
uncapped process gives P(at least 2 in-channel) of about 0.08%; even at a
deliberately crowded occupancy of 0.25 it is only about 4%. Multiple
simultaneous in-passband CW carriers is not a realistic HF condition, so
capping concurrency at one keeps the rail budget (Section 6) exact and
makes the interference cell equivalent to classic single-tone interference
testing (MIL-STD-style conformance, ITU-R F.339 protection ratios).

### Reference occupancy points

The source paper's whole-band Poisson process (lambda = 6.68 QSO onsets per
second at ARRL Field Day 2011 peak, spread across roughly 3.8 MHz of amateur
HF allocations, exponential hold time mu_d = 10 s) implies an expected
number of simultaneous interferers inside a single 2.4 kHz passband of

    6.68 * 10 s * (2400 / 3.8e6) ~ 0.04

which anchors the reference points used for SIM_QRM_OCC:

- contest peak: about 0.04-0.05 (derived above)
- typical evening (source paper's whole-band lambda of 0.1-0.5/s): about
  0.0006-0.003
- 0.25: a deliberately crowded band, still physically plausible, used as a
  stress point

## 3. Interferer level: the INR distribution

Each spawned interferer draws its level once, at spawn time:

    INR_dB ~ Normal(SIM_QRM_INR_DB, SIM_QRM_INR_SPREAD_DB), truncated to
             min(SIM_QRM_INR_MAX_DB, runtime rail budget)
    amp = sigma * 10^(INR_dB/20) * sqrt(2)
        (carrier power = sigma^2 * 10^(INR_dB/10))

Truncation clips to the bounds rather than redrawing.

Defaults: median 10 dB, spread 6 dB, cap 16 dB.

The normal-in-dB (lognormal) spread stands in for the source paper's
Hall-pdf-plus-congestion-model level distribution (its power pdf is
symmetric in dBm). The spread value is an implementation calibration
choice, open to future refinement against Laycock-Gott congestion-vs-
threshold statistics, whose congestion curve Q(threshold) implies a level
distribution for occupied channels. Truncation at the cap has little effect
on modem impact in practice: above about 15 dB INR a CW carrier's damage to
a data signal saturates.

Levels are channel-referenced (sigma-relative) rather than absolute dBm
because the model's cells are defined by sigma and occupancy is naturally a
threshold-over-noise quantity; the source paper's absolute field-strength
chain (location, date, antenna) has no equivalent here.

## 4. Keying: PARIS envelope

Interferers are keyed with the source paper's actual CW modulation model
(its Section V.B-C): the word PARIS repeated for the interferer's duration,
with raised-cosine rise and fall.

- duration: tld ~ Exp(mu_d = 10 s), floored at 0.1 s, capped at 120 s
  (P(> 120 s) is about 6e-6)
- dot length: dot = tld / 331 (source paper eq. 18), clamped to a 10-60 WPM
  range (dot 120-20 ms); the clamp is an implementation addition, since
  eq. 18 unclamped gives implausible WPM for short duration draws
- raised-cosine rise/fall time: dot / 10 (source paper eq. 19)
- PARIS unit pattern: 50 units per word, 22 on, giving 44% duty
- carrier frequency: uniform in (300, 2700) Hz; phase uniform

The envelope is precomputed once at spawn time (a rare event, at most 120 s
of floats at 8 kHz) and played back in the per-block loop as a slice plus a
sine multiply, so the steady-state allocation cost stays negligible.

## 5. Swept carrier (optional OTHR model)

The optional swept carrier (SIM_QRM_SWEEP) models an over-the-horizon radar
(OTHR) source. Real swept-carrier systems, per IARUMS OTHR reports, chirp
across tens of kHz to MHz, so a fixed 2.4 kHz passband only ever sees the
crossing: a short burst each time the sweep passes through, not a
continuous in-band tone.

The sweeper chirps across a virtual span, SIM_QRM_SWEEP_BAND_HZ (default
24 kHz), and only the in-passband slice of each sweep is rendered:

- in-channel duty = 2400 / sweep_band = 10% at defaults (burst length =
  duty / rate = 10 ms; in-band slope 240 kHz/s at defaults)
- SIM_QRM_SWEEP_INR_DB (default 10 dB) is the peak, while-crossing level.
  The long-run average in-channel INR is peak + 10*log10(duty), which is
  0 dB at defaults, giving a long-run noise-floor rise of about 3.0 dB.
  Measured standalone at sigma = 7000 over 30 s, this puts effective SINR
  at about +2.3 dB: degraded and sync-stressing, but not a dead channel.
- SIM_QRM_SWEEP_RATE (default 10/s) is the burst repetition rate.
- a requested span below 2400 Hz is rejected at init, since a chirp cannot
  be narrower than the passband it crosses; span = 2400 Hz reproduces a
  continuous in-band jammer and is only used if requested explicitly.

The rail budget check for the sweeper uses its peak amplitude, with the
same peak formula as the CW interferer (Section 6); the burst nature of the
sweep does not relax the budget.

## 6. Rail budget

The channel path is linear in floating point up to a single int16
conversion boundary: signal, noise, and interference are summed, a receive
pad (SIM_RX_PAD_DB) is applied, and a rail guard checks the result against
the int16 range before quantization. The QRM allowance has to fit under the
pre-pad clip threshold together with the worst-case signal and noise:

    thr_pre    = 32768 / RX_PAD
    sig_peak   = 32767 * (10^(10/20) if Watterson fading else 1)
    noise_peak = 4.9 * sigma                    (per-sample tail ~1e-6)
    amp_budget = thr_pre - sig_peak - noise_peak     (must be > 0)
    INR_budget = 20 * log10(amp_budget / (sigma * sqrt(2)))

For example, at sigma = 7000 with a -12 dB RX pad and no fading: thr_pre is
130,447; amp_budget = 130,447 - 32,767 - 34,300 = 63,380, giving
INR_budget of about 16.1 dB, which is why the default cap is 16 dB. Because
M=1 (Section 2), one carrier consumes the whole allowance, so a CW draw and
an active sweep have to share the budget, checked at init time.

### Fail-loud rules

Checked at init; on violation the process exits with an error rather than
degrading silently:

- occupancy greater than 0 with sigma == 0 is a configuration error: the
  sigma-relative model is undefined without a noise reference.
- a requested median INR above INR_budget is a configuration error; the
  operator is told to deepen SIM_RX_PAD_DB (for example, combining
  occupancy with fading at the same signal level typically needs a deeper
  pad, on the order of -22 dB).
- the effective cap actually used is min(user cap, INR_budget); the runtime
  banner echoes the actual values in effect.

## 7. Configuration knobs

| environment variable   | meaning                                   | default |
|-------------------------|---------------------------------------------|---------|
| SIM_QRM_OCC             | in-channel occupancy fraction                | 0 (off) |
| SIM_QRM_INR_DB          | median per-carrier INR                       | 10      |
| SIM_QRM_INR_SPREAD_DB   | level spread (dB, normal-in-dB)              | 6       |
| SIM_QRM_INR_MAX_DB      | level cap (runtime-clamped to budget)        | 16      |
| SIM_QRM_SWEEP           | swept carrier on                             | 0       |
| SIM_QRM_SWEEP_INR_DB    | sweeper peak (while-crossing) INR            | 10      |
| SIM_QRM_SWEEP_RATE      | sweeps per second (burst repetition rate)    | 10      |
| SIM_QRM_SWEEP_BAND_HZ   | virtual sweep span (duty = 2400 / span)      | 24000   |

At occupancy 0 with the sweep off, the interferer process is not
constructed at all, so output is bit-exact with QRM fully disabled. Given
the same RNG seed, the interferer stream (arrivals, durations, levels,
keying) is deterministic and reproduces bit-identically across runs.

## 8. References

- E. Mendieta-Otero, I. A. Perez-Alvarez, B. Perez-Diaz, "Interference
  Simulator for the Whole HF Band: Application to CW-Morse," IEEE Trans.
  EMC, DOI 10.1109/TEMC.2014.2313064; arXiv:2402.04742.
- P. J. Laycock, G. F. Gott et al., "HF channel occupancy and band
  congestion: the other-user interference problem," Radio Science 1991,
  DOI 10.1029/91RS00558 (UMIST 1.6-30 MHz occupancy program).
- "Models of HF Interference over Cyprus," Appl. Sci. 12(22):11808, 2022
  (recent occupancy re-measurements).
- ITU-R F.339 (protection ratios, SIR-specified discrete interferers);
  MIL-STD-188-110B (the body has no CW-interferer test; tone tests live in
  conformance procedures, SIR-specified).
- ITU-R P.372 (HF noise floor; used elsewhere in the simulator's
  environment presets).

## 9. Known deviations from the source model

- sigma-relative INR is used instead of an absolute dBm field-strength
  chain, since the model's cells are defined by sigma (Section 3).
- the lognormal (normal-in-dB) level spread is a stand-in for the source
  paper's Hall/congestion-model level distribution; the spread value is a
  calibration choice open to refinement (Section 3).
- concurrency is capped at one active interferer (Erlang loss, M=1); this
  is negligible at realistic occupancy (Section 2).
- keying WPM is clamped to 10-60; interferer duration is capped at 120 s
  (Section 4).
- the swept carrier is retained from IARUMS OTHR reports rather than the
  source paper itself; its span and rate are IARUMS-plausible round
  numbers rather than a calibrated fit to a specific OTHR system
  (Section 5).
