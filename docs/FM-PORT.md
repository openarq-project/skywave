# FM and VHF port profiles

This document covers skywave's modeling of narrowband VHF/UHF FM amateur
channels (12.5/25 kHz), including audio-passband data modems (the VARA-FM
class of soundcard modems) reached via the speaker/mic path or a 9600-baud
flat data port. See the channel-model documentation for the HF
(ionospheric) side of the simulator; this document is the VHF/FM
counterpart.

## 1. What existing tools model, and the gap

A survey of prior art found that every publicly documented FM channel
simulator models the channel as a linear audio/baseband effect: flat
Rayleigh/Rician fading (typically a Jakes-type sum-of-sinusoids generator),
additive white Gaussian noise, and a slow frequency/deviation offset
applied directly to the audio. None of the tools surveyed implements
discriminator nonlinearity, capture effect, or FM-threshold/click-noise
behavior.

- Winlink's IONOS-SIM (a Teensy-based hardware simulator by KN6KB and
  KA6IQA) is the most comparable prior tool, and is what produced the
  published Winlink VARA-FM comparative numbers. Its full "channels
  modeled" list is: white Gaussian noise, CCIR multipath presets, flat
  fading from 0 to 40 dB, fixed frequency offset, and slow FM
  deviation/rate variation. Its VHF/FM mode is the same audio-DSP
  architecture widened to 6 kHz; there is no RF modulate/discriminate
  stage anywhere in its block diagrams, and the companion performance
  report states plainly that radios are absent from the setup ("all is
  done with audio").
  <https://winlink.org/sites/default/files/downloads/ionos_simulator_manual_22may20.pdf>
  <https://winlink.org/sites/default/files/downloads/a_winlink_digital_mode_performance_comparison_based_on_the_ionis_sim_hf_vhf_channel_simulator_-_november_2_2020_0.pdf>
- GNU Radio's `channels.fading_model` is a Jakes-type sum-of-sinusoids
  linear fading-coefficient generator with a Rayleigh/Rician switch,
  validated upstream only against power-distribution and
  Bessel-autocorrelation statistics. It has nothing FM-specific in it.
  <https://wiki.gnuradio.org/index.php/Fading_Model>
- GNU Radio's narrowband FM transceiver example runs real NBFM
  modulate/demodulate blocks, but connects TX to RX over a bare ZMQ
  socket with no channel-impairment block in between: a codec loopback,
  not a channel simulation.

The implication is that a modulate to complex-baseband-channel to
limit/discriminate architecture that reproduces threshold, capture, and
click physics is not a replication of an established pattern; it appears
to be new territory. The linear audio fade is the field's de facto
accepted compromise for FM data-modem benchmarking, and it is what
published VARA-FM numbers rest on. Whether some unpublished or
paywalled academic/commercial tool implements the full chain is an open
question (absence of evidence after a reasonably thorough search, not a
confirmed negative).

## 2. FM threshold and capture effect

Two classical, textbook-stable effects (traceable to Rice, 1963) do not
show up in any of the linear simulators above, and are worth calling out
because they are qualitatively different from a lower SNR floor:

FM threshold: the linear improvement of output SNR with carrier-to-noise
ratio (CNR) breaks down at roughly 10 dB CNR (by the conventional 1 dB
departure criterion). Below that point, discriminator output noise
transitions from smooth/Gaussian to impulsive clicks, caused by
noise-phasor cycle slips, which merge into continuous crackle as CNR
drops further. The shape and duration of these clicks, and how the click
rate scales with carrier-to-noise ratio, are characterized in Lindgren
(1984). This is the FM analog of the knee behavior seen in HF channels,
and a linear fading model cannot produce it.

Capture effect: at the limiter, the stronger of two co-channel signals is
demodulated and the weaker one is suppressed almost entirely; when the
two signals are close in level, the receiver output flips unpredictably
between them ("flutter"). This is winner-take-all behavior, structurally
different from HF co-channel interference, where both signals typically
degrade the link gracefully together.

## 3. Doppler and propagation parameter sources

ITU-R M.1225 is out of scope for this model. It is a self-declared
IMT-2000 evaluation methodology for the 1885-2025/2110-2200 MHz bands,
not a source of 2 m/70 cm Doppler or multipath presets, and should not be
cited for VHF/UHF amateur channel work.

No source with concrete, citable Doppler-spread or multipath parameters
for 144/430 MHz amateur land-mobile or fixed point-to-point paths was
found in the pass that produced this document. Candidate sources for a
follow-up targeted literature pass: TIA-603 (which does carry
test-signal and decoder-timing specifications relevant to squelch and
CTCSS, see Section 4), the ITU-R P.1546 land-mobile propagation series,
and academic shadowed-Rayleigh/Suzuki land-mobile studies. In the
meantime, a first-principles anchor is available from the Doppler
relation fD = v*f/c, giving roughly 13 Hz at 146 MHz for a 100 km/h
vehicle and roughly 40 Hz at 440 MHz for the same speed. Published
presets should replace this derivation wherever they can be found.

## 4. Two port profiles: mic/speaker versus 9600-baud data

The mic/speaker (audio) path and the 9600-baud flat data port behave
materially differently, and a channel model needs to represent both
rather than treating "the FM channel" as one profile:

Pre/de-emphasis: the voice path applies a standard 6 dB/octave transfer
function over roughly 300-3000 Hz. A commonly repeated explanation for
why (that it exists to prevent limiter clipping of low frequencies) did
not hold up under review; the transfer function itself should be modeled
without assuming that particular causal story.

CTCSS (PL tone): sub-audible tones below 300 Hz are frequency-division
multiplexed under the 300-3000 Hz voice/data band on the same audio
path, and receiver squelch gates directly on successful tone decode. Any
data signal on the mic/speaker path shares the channel with the tone and
is subject to the same squelch logic.

The 9600-baud G3RUH-style path is different in kind: it applies direct
FSK at the modulator's varactor, or is tapped directly at the
discriminator on receive, bypassing both pre/de-emphasis and the
squelch-gated audio chain entirely. The signal is not audio in the sense
the mic/speaker path is. (Direwolf, for example, has to manufacture
artificial pre-emphasis when a mic-path transmitter is received by a
discriminator-tap receiver, precisely because the two paths are not
interchangeable.) These are two genuinely different profiles to model,
not one knob with two settings.

Repeater attack time: a real repeater path carries an engineered delay
budget before the channel opens: carrier detect at roughly 20-40 ms,
CTCSS validation at roughly 80-200 ms (tone-frequency dependent, per
TIA-603), and PTT/exciter/PA settle at roughly 10-50 ms, partly running
in parallel with the other two, plus hang time on the tail after the
signal drops. One specific figure sometimes repeated in this space,
that a dedicated audio-delay device in the 256-512 ms range is common on
repeaters, did not hold up under review; the attack-time budget should
be sized from TIA-603 timing rather than that figure.

## 5. VHF/UHF propagation effects

On VHF/UHF land paths, multipath delay spread is microsecond-scale (a
few microseconds typically, up to roughly 10-20 microseconds in severe
mountain terrain). Against a 12.5/25 kHz channel, whose inverse
bandwidth is on the order of 80 microseconds (and audio-passband symbol
periods are longer still), these echoes are far too close together to
produce inter-symbol interference or frequency-selectivity across the
channel. Instead they produce constructive/destructive envelope
fluctuation: ordinary flat Rayleigh/Rician fading, as in Sections 1 and
3. This is the opposite of HF, where millisecond-scale delay spread is
frequency-selective across a few kHz and an explicit multipath model
with distinct delayed paths is needed. The consequence for this
simulator is that no tapped-delay-line stage is modeled for VHF/UHF, by
design. One edge case is worth a note rather than a model stage: the
9600-baud discriminator-tap path, whose roughly 104 microsecond symbol
period is closer to the roughly 20 microsecond severe-terrain delay
spread, can flirt with mild inter-symbol interference in extreme
terrain.

The practically dominant flat-fading regime is mobile flutter, sometimes
called picket fencing: at 146 MHz, a vehicle moving at highway speed
crosses a fade null roughly every meter. This is the same Rayleigh model
described above, just evaluated at a fast Doppler rate, and it is the
regime that is hardest on ARQ turnarounds. Because of that, presets
should be organized by mobility regime (stationary, pedestrian, mobile)
rather than by a raw Doppler frequency number.

Beyond flat fading and threshold/capture effects, the following are
worth modeling explicitly:

1. Log-normal shadowing (a Suzuki channel: Rayleigh fading multiplied by
   a log-normal process) is the slow fading axis: terrain or building
   obstruction as a mobile station moves. It operates on a
   session-relevant timescale of seconds, producing outages like
   "behind a hill for 15 seconds" that stress retry economics. A
   shadowed-Rayleigh land-mobile source:
   <https://www.researchgate.net/publication/3668083_A_deterministic_model_for_a_shadowed_Rayleigh_land_mobile_radio_channel>
2. Impulsive man-made noise dominates the effective VHF noise floor in
   many installs (ignition, power-line, and switching impulses, rather
   than thermal noise), per ITU-R P.372. These impulses interact with
   click noise near the FM threshold, and mobile installations are the
   worst case.
3. Aircraft scatter/reflection is the classic fixed-path VHF fade
   mechanism: an aircraft crossing the path produces a slow, deep,
   quasi-periodic fade with a few-Hz Doppler beat lasting tens of
   seconds. This is a realistic "otherwise solid link, occasional fade
   event" preset for fixed point-to-point cells.
4. Frequency offset/drift should be sized physically from crystal
   tolerance: +/-2.5 ppm at 440 MHz works out to roughly +/-1.1 kHz,
   which is a large fraction of a 12.5 kHz channel. An off-channel
   receiver shifts the recovery point and eats into deviation headroom
   asymmetrically. UHF needs a wider default offset range than VHF; a
   single fixed-offset knob (as in IONOS-SIM) is the right shape but
   undersized for UHF.
5. Adjacent-channel splatter and strong-signal desense (a second-order
   effect): an off-channel strong signal can raise the effective noise
   floor without triggering capture.

Two effects were deliberately excluded, for reasons worth stating
explicitly so they are not "added back" later out of a sense of rigor:
tropospheric ducting/enhancement and rain/foliage loss operate on a
minutes-to-hours timescale, which makes them a per-scenario signal-level
setting rather than something that needs to vary within a session; and
frequency-selective tapped-delay multipath is the wrong regime at these
bandwidths, as explained above.

The organizing principle, consistent with the HF side of the simulator,
is to model what changes within a session: fast fading, shadowing
events, impulsive noise, capture flips, offset drift, and keying/timing.
Anything that changes on a slower timescale is a per-scenario parameter,
not an in-session dynamic.

## 6. Lessons from prior simulators

GNU Radio's fading block originally shipped with a broken
autocorrelation, later found and patched (GNU Radio PR #745). Off-the-shelf
channel blocks should not be assumed correct until independently
validated, whether they come from this project or elsewhere.

IONOS-SIM's linear approach was sufficient to publish comparative
Winlink VARA-FM data, which means the linear-model tier is not without
value: it is a legitimate baseline fidelity tier, and the open question
for a nonlinear model is whether the effects it adds change the outcome
for the cases that matter most (near-threshold marginal operation,
co-channel capture, and click-regime retry economics).

## 7. Model architecture and tiered fidelity

A design that reproduces threshold, click, and capture behavior needs
its physics core to differ from a Watterson-style HF fade model; the
harness plumbing around it (audio-in/audio-out contract, transport
layer, virtual clock, half-duplex/PTT gating, seeded presets,
self-verify tests) can be shared with the HF side of the simulator.

The nonlinear signal path is: audio in, then path shaping (pre-emphasis
for the mic/speaker profile, or a flat pass-through for the data-port
profile), then FM modulation to a complex baseband representation, then
flat fading plus AWGN plus an optional co-channel interferer, then a
limiter and discriminator, then de-emphasis and squelch, then audio out.
From a modem's point of view the interface is still audio in, audio out;
internally it is a modulate-channel-discriminate chain, which is the
only architecture that can produce threshold, click, and capture
behavior.

A practical build order is tiered:

- Tier A: the linear audio fade, equivalent to IONOS-SIM. This is
  cheap to implement, lets modem-side development start immediately,
  and is directly comparable to published VARA-FM numbers.
- Tier B: the nonlinear complex-baseband chain described above.

Tier A and Tier B should agree in the friendly regime (well above
threshold, no co-channel interferer), since FM behaves approximately
linearly there; this agreement is itself a validation gate. The two
tiers are expected to diverge only near or below threshold, and under
co-channel interference.

Two port profiles should exist from the start: mic/speaker (emphasized,
squelch-gated, sharing the channel with a CTCSS tone) and the flat
9600-baud data port (discriminator-tap, bypassing emphasis and squelch
entirely). VARA FM itself ships wide/narrow modes with soundcard-path
assumptions baked in, which is one more indication that the profile
axis matters and should not be collapsed into a single generic "FM
channel."

Keying and repeater timing (Section 4's attack-time budget, hang time,
audio bandpass, and delay) should fold into whatever general keying/timing
model the simulator already uses for half-duplex operation, as an
added repeater profile rather than a separate mechanism.

## 8. Validation approach

No source was found that validates any FM channel simulator against
measured BER-vs-SNR curves for the specific modes of interest here, and
no VARA-FM reference curves beyond the IONOS-SIM comparative goodput
report were located. The best available anchors are:

- 1200-baud AFSK (Bell 202): W6KWF and Bridget Benson, TAPR Digital
  Communications Conference 2014, measured decode-versus-level data.
  This is the strongest published curve found.
  <https://files.tapr.org/meetings/DCC_2014/DCC2014-Amateur-Bell-202-Modem-W6KWF-and-Bridget-Benson.pdf>
- Classical threshold curves (CNR around 10 dB, 1 dB departure
  criterion): a Tier B simulator should reproduce the textbook
  threshold knee, and Rice's click-rate formula gives a quantitative,
  parameter-free check on the click rate's dependence on CNR.
- Comparison against another implementation: IONOS-SIM is
  buyable/buildable hardware, so a Tier A versus IONOS-SIM concordance
  check is a reasonable validation strategy, similar to cross-checking
  against any other independent implementation of the same model class.
- Real-rig spot checks: two handheld or mobile radios plus an SDR at
  attenuated RF remain the ground truth for capture and threshold
  behavior. IEEE land-mobile digital-FM BER literature (for example on
  differential detection in land-mobile channels) exists but is
  paywalled and was not reviewed for this pass; a library pass could
  recover proper reference curves.

## 9. Open questions

1. Does any published architecture implement the full
   modulate-RF-channel-discriminate chain for FM data-modem
   benchmarking? This remains unresolved; a targeted academic or
   paywalled literature pass could settle it before committing further
   engineering effort to what otherwise looks like new territory.
2. Citable 2 m/70 cm Doppler and delay-spread presets (from TIA-603,
   ITU-R P.1546, or academic land-mobile work) are needed before
   propagation scenarios can be considered production-ready rather than
   first-principles estimates.
3. Are there VARA-FM reference measurements beyond the IONOS-SIM report
   to calibrate a VARA-FM-class modem scenario against?
4. Do the nonlinear effects matter for a given modem's actual operating
   point? If a modem's normal operating point is well above threshold
   on fixed point-to-point links, Tier A may be sufficient for most
   scenarios, with Tier B reserved for marginal or interference-heavy
   cases. This is answerable by measuring the Tier A/Tier B divergence
   directly, not a matter for debate.

## Notes on sources

The classical FM physics in Section 2 is settled and traceable to
Rice, "Noise in FM Receivers," in Proceedings of the Symposium on Time
Series Analysis (M. Rosenblatt, ed.), Wiley, 1963, pp. 395-422; the shape
and duration of the near-threshold clicks are characterized in Lindgren,
"Shape and Duration of Clicks in Modulated FM Transmission," IEEE
Transactions on Information Theory, IT-30(5), 1984.
Some of the remaining supporting material still leans on course notes and
aggregator sources rather than a primary textbook or IEEE citation in
every instance, and those citations should be upgraded where convenient.

A few claims examined during research did not hold up and are flagged
here so they are not reintroduced later: the idea that IONOS-SIM is a
Watterson-only tool (it also has flat-fade and offset modes); the
specific claim that a 256-512 ms dedicated repeater audio-delay device
is common (see Section 4, use TIA-603 timing instead); and the folklore
explanation that pre-emphasis exists specifically to prevent limiter
clipping of low frequencies (see Section 4, model the transfer function
without that assumption).
