# Cross-calibrating the channel against a reference

skywave's fading model has its own internal statistical self-verification:
Doppler spectrum width, Rayleigh envelope statistics, delay-spread, unit
average power. That is necessary but not sufficient. A simulator can pass
every internal check and still disagree with the rest of the world about
what "2 ms delay, 1 Hz Doppler" actually sounds like. The only way to know
is to run the same fading cell through skywave and through one or more
independently trusted implementations and compare the resulting bit error
rate.

This document describes that method: how to set up a cross-calibration
against a reference channel simulator, what to control so the comparison
is actually measuring channel physics and not noise-convention or harness
artifacts, and what agreement to expect depending on how closely related
the two implementations are. It is a general recipe, not tied to any one
modem or reference tool, though the examples below use codec2's `ch` and
PathSim because they are the two most widely used free HF channel
simulators and both were used to validate skywave itself.

Run this once the channel model's fade physics are frozen for release. A
moving target invalidates any agreement number you produce: comparing a
1.5 Hz cell to a reference's 1.0 Hz cell under the same nominal label is
exactly the kind of quiet mismatch this method exists to catch, so it is
worth double-checking your own cell parameters before ever touching the
reference.

## The core idea: decouple the noise

The single biggest trap in any cross-simulator BER comparison is that every
tool has its own noise convention. codec2's `ch` takes a noise-density
parameter (`--No`); skywave (like many bench harnesses) pins mean-signal
power over mean-noise power in a fixed bandwidth; PathSim has its own SNR
knob. If you feed "the same nominal SNR" into two tools with different
conventions, you are comparing noise-injection bugs, not fading physics,
and you will not know it.

The fix is to remove noise from the comparison entirely. Each channel arm
under test produces a fade-only file (multipath and Doppler applied, no
noise added), and a single shared AWGN injector adds calibrated noise to
all of the fade-only outputs afterward, at a measured target signal-to-
noise ratio. The only thing that differs between arms is then the fade
realization; the noise machinery is identical by construction, so the SNR
axis is identical by construction too.

```
  reference waveform (e.g. a standard OFDM test mode, 8 kHz)
        |
        +--> reference simulator's fade filter   (fade only, noise off)
        +--> skywave's fade filter                (fade only, noise off)
        +--> a second independent simulator       (fade only, noise off)
                    |  each arm's fade-only file
                    v
        one shared AWGN injector, seeded, targeting a measured SNR
                    |
                    v
        a neutral demodulator  -->  packet/bit error rate
                    |
                    v
        a shared SNR-measurement script  -->  plot error rate vs measured SNR
```

The shared measurement script verifies the realized in-band SNR on every
produced file; it should equal the injector's target by construction, and
a mismatch flags a bug in the injector rather than in either channel model.

One convention that has worked well: define SNR as mean-signal power over
mean-noise power, both measured in a fixed 3 kHz band (matching a typical
HF/VHF voice-channel bandwidth), computed as

```
sigma = sqrt(P_sig / (snr_linear * 3000 / (fs / 2)))
```

for a signal at sample rate `fs`. Whatever convention you choose, define it
once, implement it in one place, and use it for every arm.

An AWGN-only cell, with fading disabled entirely, is the harness's own
self-test: with no fading physics in play, all arms plus the injector plus
the demodulator should agree closely (0.5 dB is a reasonable bar). If they
do not agree there, the bug is in the comparison tooling, not in either
channel simulator, and no fading result should be trusted until it is
fixed.

## A neutral modem, plus an optional second leg

Run the comparison through a modem that is not the one you are trying to
validate, ideally something widely available and reproducible by an
outside reader (codec2's own `ofdm_mod`/`ofdm_demod` with a standard test
mode is a reasonable choice; it ships with test-frame and LDPC options
that make BER measurement straightforward). That way, any offset you
measure is attributable to the channel, not to quirks of your own modem's
demodulator, and someone else can reproduce the whole leg with stock,
off-the-shelf tools.

It is also useful to run a second, optional leg through your own modem's
actual decode chain on the same faded and noised files, report-only. This
does not drive the pass/fail call, but it connects the external anchor
back to the thing you actually ship. The armstrong modem project, for
example, used skywave this way: codec2's neutral modem carried the
pass/fail verdict, while an armstrong-specific replay leg on the same
files gave a report-only check that the anchor result actually says
something about armstrong's own decode chain.

## Test cells

A small set of standard delay/Doppler cells, matched against whatever flag
names the reference tool uses, is enough to cover the interesting range:

| cell | delay / Doppler | reference flag (codec2 `ch`) |
|---|---|---|
| AWGN anchor | no fade | none |
| good/mild | 0.5 ms / 0.1 Hz | `--mpg` |
| poor/moderate | 2.0 ms / 1.0 Hz | `--mpp` |
| disturbed/stress | 4.0 ms / 2.0 Hz | `--mpd` |

The moderate cell (2 ms / 1 Hz, "CCIR Poor") is the one worth prioritizing:
it is the cell most reference tools support directly, and it sits in a
regime where the fade is discriminating enough to produce a clean error
curve without demanding an enormous amount of exposure time. The mild cell
is the most expensive to run properly (its slow 0.1 Hz fade needs far more
audio to reach the same statistical exposure) and is a reasonable one to
treat as optional or run last.

Sweep 3-5 SNR points per cell, bracketing the mode's error-rate knee (a
coarse pilot pass first, then a finer bracket around the 10% packet-error
point), and always plot error rate against measured SNR, never against a
nominal setting.

## Exposure and seeds

ITU-R Recommendation F.1487, Section 6, specifies a minimum faded-audio
exposure per test point of roughly 3000 divided by the Doppler frequency,
in seconds, to get a statistically meaningful sample of the fading
process. For a 1 Hz Doppler cell that is about 50 minutes of audio; for a
0.1 Hz cell it is over 8 hours. Offline file processing runs far faster
than real time, so there is little reason not to just meet the exposure
rule outright rather than argue around it. Record the realized exposure
per point.

Use a fixed, recorded seed policy, and run at least 3 independent fade
realizations per point on any fading cell (more if you can afford it). A
single fade realization is not a reliable measurement on its own; treat
the whole cell's worth of realizations as the sample.

One practical pitfall worth flagging explicitly: if the demodulator you are
using does one-shot acquisition rather than continuously re-synchronizing,
a single unlucky combination of fade realization and noise seed can cause
it to miss initial acquisition entirely, independent of the underlying
SNR. Scoring that as a 100% data error rate will make a perfectly good
channel model look catastrophically broken. Separate acquisition failures
from data errors explicitly, and average error rate over enough noise
seeds and fade realizations (order 10 noise seeds by 3 or more fade
realizations per SNR point is a reasonable target) that a handful of
acquisition misses cannot dominate the result.

## Two tiers of agreement, not one

Not all reference implementations deserve the same bar. If your fading
model and the reference implementation are, in effect, two renderings of
the same underlying recipe (for instance, if your Watterson channel is a
direct numeric port of the same fading-generation algorithm the reference
tool uses), then a comparison against that reference mainly proves the
port is faithful: it is a check on your own code, not independent
validation of the fading model itself. That comparison deserves a tight
bar, because any gap there is most likely a porting, streaming, or
normalization bug rather than a real difference in channel physics.

A comparison against a genuinely independently implemented channel
simulator, one that was not derived from your code or your reference's
code, is the one that actually earns the label "external validation." It
should still agree, but the acceptable band is wider, because two honestly
different implementations of the same nominal Watterson model can
legitimately disagree by a couple of dB (Furman and Nieto document
disagreement of more than 2.3 dB between two implementations that were
each individually considered conformant to the standard). That is the peer
agreement band this method targets for an independent reference.

In practice this suggests a tiered check rather than a single gate:

| tier | reference relationship | proves | suggested gate |
|---|---|---|---|
| statistics | any simulator, no modem | matching Doppler width, Rayleigh envelope, delay, unit power | pass/fail against each simulator's own self-verify bounds; cheap, run first |
| port fidelity | shared-lineage reference (same underlying recipe) | your implementation matches its own reference faithfully | tight, roughly 1 dB or better |
| independence | independently implemented reference | your model matches the physics, not just one code path | roughly 2 dB or better (peer band), 2-3 dB worth documenting and publishing with the discrepancy stated, more than 3 dB means stop and root-cause before citing the number externally |

A codec2 `ch` comparison, if your fading code descends from the same
Octave fading-generation recipe `ch` itself uses, is naturally a
port-fidelity check rather than an independence check, however useful it
is. PathSim, or another simulator you did not build from the same source,
is closer to a true independence check. Mercury's own clean-room Watterson
implementation is a reasonable independence-tier substitute if a
standalone reference build proves troublesome.

Whatever gates you choose, the AWGN anchor (0.5 dB) always comes first:
it is a test of your harness and injector, not of either channel model,
and it should pass before any fading-cell number is trusted.

## Confounds worth pinning off deliberately

- Disable anything downstream of the fading physics on the reference side
  that has no counterpart on your side (clipping, SSB filtering, frequency
  offset) so the comparison is fade-only end to end.
- Verify fade power preservation (output average power over input average
  power, close to 1) for every arm before trusting any error-rate number
  from it; a broken fade filter often shows up as a power anomaly before
  it shows up as a BER anomaly.
- Keep fades deterministic and seeds recorded on every arm; if a reference
  tool needs a sample-rate conversion to line up with your pipeline, do it
  once, with one documented method, and never let it drift between points.

## What to publish

A citable cross-calibration verdict should include the error-rate-vs-
measured-SNR curves per cell per arm, the SNR-at-10%-error-rate delta per
tier against the gates above, full configuration stamps (reference tool
commit and build flags, your own simulator's version, tap-update rate and
other fading parameters, exposure per point, the seed list, and the exact
SNR-measurement method), and an appendix that lets an external reader
reproduce the anchor cells with stock, publicly available tools alone.
Treat this as a prerequisite for citing simulator-based bench numbers
publicly, not as a gate on day-to-day development.

## Example result

Running this method on skywave's own Watterson channel against codec2
`ch` (port-fidelity tier) and against PathSim (independence tier), both at
the 2 ms / 1 Hz moderate cell, gave a signal-to-noise delta at 10% packet
error rate of about -0.11 dB against codec2 `ch` (well inside the roughly
1 dB port-fidelity band) and about +0.14 dB against PathSim (well inside
the roughly 2 dB independence band). Both results indicate the fading
model is indistinguishable from the reference implementations at the bit
error rate level, not just at the level of summary statistics.

## A contribution along the way

Running the independence-tier comparison against PathSim surfaced a real
bug in PathSim itself: its Hilbert filter built both the real and
imaginary halves of the analytic signal from the same in-phase filter
coefficients, so the quadrature branch was never actually applied. The
practical effect was that PathSim's faded envelope was measurably
non-Rayleigh (an easy thing to miss if you only look at summary
statistics computed the same way the simulator itself computes them,
harder to miss once you cross-check envelope statistics against a
reference distribution). The fix was small, two lines swapping which
accumulator the quadrature filter taps feed, and it was contributed
upstream as a pull request against the reference implementation. This is
the kind of finding a cross-calibration exercise is meant to surface:
apparent disagreement between two simulators is sometimes a real bug in
one of them, and tracking it down is worth the detour.

## Non-goals

This method targets the channel model in isolation. It does not attempt
to validate a station's full RF signal chain, does not compare ARQ or
retry behavior between systems, and it is one-way by construction (most
reference tools like `ch` and PathSim have no notion of a two-way link).
Treat it as a gate on citing channel-simulator bench numbers externally,
not as a substitute for end-to-end, full-duplex, or over-the-air testing.

For a broader survey of how different channel simulators compare in
practice, see the companion channel-simulator comparison document.
