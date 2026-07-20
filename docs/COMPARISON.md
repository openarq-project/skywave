# skywave versus the open-source HF channel simulator landscape

*A comparison of skywave, an open-source HF channel simulator, against the
open-source HF channel simulators used to test HF/shortwave data modems.
Compiled from a primary-source survey: project source code and standards
documents fetched and read directly.*

---

## TL;DR

The open-source HF-simulator landscape is crowded and converged on channel
physics, and almost silent on the radio/station chain. Nearly every serious
tool (codec2 `ch`, PathSim, DRM/Dream, Mercury, IONOS/HFSimulator) implements the
same 1970 Watterson two-path complex-Gaussian model and reproduces the CCIR 520-2
/ ITU-R F.1487 delay/Doppler test conditions near-verbatim. Where they stop is the
transmit/receive chain: AGC is modeled by none of them, PA nonlinearity by none
beyond an ideal hard clip, PTT/half-duplex turnaround by exactly one (Mercury, and
only inside its test harness), and impulsive noise and QRM by none at all.

skywave is, on the channel-physics axis, a peer of the field (same Watterson
core, comparable presets) with two rigor features almost nobody ships: a
statistical self-verification harness for the fade realization, and scheduled
fading for exercising adaptive rate control. Its fade fidelity is now externally
cross-calibrated to within 0.11 dB of codec2 `ch` and 0.14 dB of an independent
PathSim implementation at the canonical "Poor" cell (Section 4). On the
station-chain axis it is the most complete open simulator surveyed, the only one
that combines, in one tool, a full TX shaping chain (ALC overshoot + soft-PA +
PEP clip), a receive chain (SSB band-pass + AGC + level pad + rail alarm),
two-station half-duplex keying with PTT/collision/T-R latency, cross-station
frequency and clock offsets, and non-Gaussian noise (P.372 impulsive + man-made
environment scaling + Poisson-CW/OTHR QRM).

Caveats: it shares the field's fundamental Watterson validity limits
(Gaussian-scatter is not certified valid for all HF paths); it is a fixed two-tap
engine (it cannot reproduce DRM's 4-tap ETSI profiles); and it is a bespoke test
rig, not a portable community tool like `ch`.

---

## 1. What skywave is

skywave is a real-time, two-station, half-duplex HF link emulator that sits
between two live modem instances and carries both directions independently. It
has three parts.

A link process owns both directions (A to B and B to A) over one of three
transports: audio-loopback cables, framed unix sockets, or a deterministic
block-lockstep virtual clock (faster-than-wall-clock, fully reproducible). Per
direction the on-air transform is a full chain:

```
  int16 TX in
     │  gain
     │  [ALC overshoot]
     │  [soft-PA | hard PEP clip]  ──► TX stats
     │  [half-duplex deliver gate: keyed & peer-not-deaf]
     │  [SSB TX filter]
     │  [Watterson fade]
     │  [freq offset +/- drift ramp]
     │  [clock-skew resample]
     │  [link delay]
     │  + AWGN (Gaussian | P.372 impulsive)  + [QRM]
     │  [SSB RX filter]
     │  [RX AGC]
     │  RX level pad
     │  rail guard
     ▼
  int16 RX out          ([...] = an off-by-default impairment knob)
```

Half-duplex keying (VOX or real PTT), hangtime, deaf-while-transmitting,
collision, and T/R key/unkey latency are first-class. Everything past the TX
stats defaults off, so the baseline is a bit-exact AWGN pipe; each impairment is
an independent knob.

A fading engine provides a streaming ITU-R F.1487 two-path Gaussian-scatter fade
(the codec2 doppler-spread recipe: FIR-shaped complex Gaussian, Gaussian Doppler
PSD, 2-sigma spread). It forms the analytic signal with an FIR Hilbert
transformer, applies two independent equal-power Rayleigh taps across a
differential delay (frequency-selective, not flat), and normalizes to unit
*average* power so the AWGN SNR axis is preserved. It ships 11 named presets
plus a *scheduled-fading* mode (a timed sequence of presets with linear
crossfade, for testing mode-switching logic).

A rig-effects layer models the station chain the rest of the field omits:
differential LO offset with an optional slow drift ramp, a per-burst clock-skew
resampler, TX burst-onset ALC overshoot (modern/legacy presets), receiver AGC
(MIL-STD data/voice envelope follower, modeling burst-head gain error), P.372
Vd-calibrated impulsive noise, and a QRM generator (Poisson CW interferers plus
a swept over-the-horizon-radar carrier).

Separately, a lightweight, dependency-free test implementation (sum-of-sinusoids
Doppler) plus AWGN provides deterministic unit and golden-vector tests at a
fixed 8 kHz sample rate. It is lighter than the main engine, and not
statistically self-verified.

---

## 2. The field, at a glance

Two views of the same primary-source survey (project source code and standards
PDFs fetched directly; disagreements between sources flagged in the closing
sourcing note). Table A is a capability matrix, who models what; Table B holds
the textual detail. skywave heads both.

Table A: what each tool models. `✓` modeled, `~` crude/partial, `✗` no,
`?` unconfirmed. Columns: Fade = Watterson multipath; AWGN = additive noise;
Δf = carrier/frequency offset; PA = power-amp nonlinearity (`~` = ideal hard
clip only); SSB = rig band-pass filter (`~` = generic, not rig-specific); AGC =
receiver AGC; HD = PTT / half-duplex turnaround; Imp = impulsive (non-Gaussian)
noise; QRM = co-/adjacent-channel interference; SV = statistical fade
self-verification.

| Simulator | Fade | AWGN | Δf | PA | SSB | AGC | HD | Imp | QRM | SV |
|---|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| skywave | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| codec2 `ch` | ✓ | ✓ | ✓ | ~ | ~ | ✗ | ✗ | ✗ | ✗ | ✗ |
| PathSim | ✓ | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| GNU Radio | ✓ | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| NTIA/ITS | ✓ | ✓ | ? | ✗ | ✗ | ? | ? | ✗ | ✗ | ✗ |
| Mercury | ✓ | ✓ | ✓ | ~ | ~ | ✗ | ✓ | ✗ | ✗ | ✗ |
| FreeDATA | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| DRM / Dream | ✓ | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| IONOS | ✓ | ✓ | ? | ✗ | ✗ | ? | ? | ✗ | ✗ | ✗ |
| ardopcf | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |

The pattern is consistent: the physics columns (Fade / AWGN / Δf) are `✓` across
the field; the rig-chain and interference columns (PA through SV) are `✗` for
every tool except skywave, whose row is filled across. ITU-R F.1487 is the
*standard* the Fade column implements, so it sits in Table B rather than here.

Table B: reference detail.

| Simulator | License | Channel model & standard profiles | Usage |
|---|---|---|---|
| skywave | Apache-2.0 | 2-path F.1487 Gaussian-scatter, self-verified, 32x tap update; 11 presets (good to auroral-max incl. flutter) + scheduled fading | two-station HD link; realtime (loopback/socket) or deterministic virtual clock |
| codec2 `ch.c` (Rowe) | LGPL-2.1 | 2-path Gaussian-Doppler PSD (Watterson-inspired); `mpg` 0.1Hz/0.5ms, `mpp` 1Hz/2ms, `mpd` 2Hz/4ms | offline file/pipe filter |
| PathSim (AE4JY / OK1IAK fork) | GPL (v2 orig; v3 fork) | 2-3 path Watterson; CCIR 520-2 exactly, incl. 10 Hz flutter | live soundcard or WAV batch |
| GNU Radio gr-channels | GPLv3 | generic Rayleigh/Rician + tapped PDP (not HF-specific); no shipped profiles | flowgraph, RT or offline |
| ITU-R F.1487 / CCIR 520-2 | (free text) | *defines* the reference Watterson 2-path model; Good/Moderate/Poor/Flutter + regional tables | standard, not a tool |
| NTIA/ITS HF Simulator | not open today | Watterson + FED-STD-1045 ALE modem; Good/Poor (Poor drifted to 2 ms/2 Hz) | 1990s standalone SW |
| Mercury (Rhizomatica) | GPL-3.0 (+LGPL codec2) | reuses `ch.c` + own 4-path Watterson + PathSim cross-check; good/moderate/poor/flutter | offline tool + Go/loopback harness |
| FreeDATA (DJ2LS) | GPL-3.0 | none at the DSP layer (protocol-level frame-drop only) | Python unit tests |
| DRM / Dream | GPLv2(+) | Watterson FIR-tap, up to 4 taps; ETSI ES 201 980 Ch. 1-6, modes A-D | offline batch loop |
| IONOS / HFSimulator (ARSFI/Winlink) | MIT | Watterson, 1-4 paths; scripted/standardized conditions | HW box + SW, RT audio |
| ardopcf | license unconfirmed | none (clean round-trip only) | C/Python unit tests |

---

## 3. Preset cross-reference

skywave's fade presets against the authoritative standards tables:

| skywave preset | delay / Doppler | Standards anchor |
|---|---|---|
| `good` | 0.5 ms / 0.1 Hz | CCIR 520-2 Good = F.1487 Mid-lat Quiet ✓ |
| `moderate` | 1.0 ms / 0.5 Hz | CCIR 520-2 Moderate = F.1487 Mid-lat Moderate ✓ |
| `poor` | 2.0 ms / 1.0 Hz | CCIR 520-2 Poor = F.1487 Mid-lat Disturbed = MIL-STD-188-110C "Poor". Matches codec2 `ch --mpp`, PathSim, DRM Ch. 4 ✓ |
| `low-lat-moderate` | 2.0 ms / 1.5 Hz | F.1487 Low-lat Moderate (a hotter-Doppler companion to `poor`) ✓ |
| `flutter` | 0.5 ms / 10 Hz | CCIR 520-2 Flutter fading ✓ |
| `nvis` | 3.0 ms / 1.0 Hz | measured mid-lat NVIS (realistic) |
| `nvis-max` | 4.0 ms / 1.0 Hz | observed-max NVIS stress, just under a ~5 ms cyclic-prefix cliff |
| `disturbed` | 6.0 ms / 10 Hz | F.1487 Low-lat Disturbed ✓ |
| `nvis-disturbed` | 7.0 ms / 1.0 Hz | F.1487 Mid-lat Disturbed NVI ✓ |
| `high-lat` | 7.0 ms / 30 Hz | F.1487 High-lat Disturbed ✓ |
| `auroral-max` | 11.0 ms / 55 Hz | DAMSON 5%-exceedance auroral (beyond F.1487; the one measured regime outside the mid-lat table) |

Coverage is broad and grounded: the full CCIR 520-2 set (Good / Moderate / Poor /
Flutter) plus the F.1487 regional tails and the measured NVIS and DAMSON-auroral
cells. Any other delay/Doppler pair is reachable through custom delay/Doppler
overrides.

---

## 4. Where skywave leads

These are the axes where skywave is ahead of the *entire* surveyed open-source
field, not just one competitor.

1. Full station chain in one tool. No other open simulator combines TX shaping
   (ALC + PA + PEP clip), an RX chain (SSB BPF + AGC + level pad), *and* the
   channel between them. codec2 has a clip and a generic SSB FIR; everyone else
   has neither. skywave models the whole antenna-to-antenna-to-audio path.

2. AGC: modeled by nobody else. codec2, PathSim, GNU Radio, DRM/Dream, Mercury,
   and FreeDATA all apply a static gain or nothing. skywave's receiver-AGC
   model reproduces the burst-head over-amplification (a fresh burst after a
   quiet gap hits max gain until the attack settles) with MIL-STD-188-141C
   `data`/`voice` presets, a real cost a fixed-gain sim never charges, and
   directly relevant to short-preamble modes.

3. PA nonlinearity beyond a hard clip. The field's best PA model is codec2's
   ideal magnitude limiter. skywave adds a soft-PA (Rapp AM/AM compression) so
   over-driving a *high-PAPR* waveform splatters sooner than a low-PAPR one,
   the PAPR-dependent behavior a hard clip misses, with a complex-envelope
   companion model for spectral/ACPR calibration.

4. Two-station half-duplex architecture. skywave is a *link* simulator with
   VOX/PTT keying, hangtime, deaf-while-transmitting, collision physics, and
   T/R key/unkey latency. Only Mercury models PTT timing at all, and only
   inside its integration harness. Every other tool is stateless one-way
   (codec2, PathSim, DRM) or bypasses TX/RX entirely (FreeDATA). This is the
   single biggest architectural differentiator, and it is load-bearing:
   half-duplex artifacts (ACK deafness, turnaround stalls) are exactly the
   failures a one-way pipe cannot surface.

5. Impulsive noise and QRM: modeled by nobody else. Every other tool's noise is
   literally Gaussian. skywave ships P.372 Vd-calibrated impulsive noise
   (envelope voltage-deviation solved to target at init, total power held to
   σ² so the SNR axis is unchanged) and a QRM generator (Poisson-onset CW
   interferers with raised-cosine keying + swept OTHR carrier, levels relative
   to the noise floor). Interleaver/FEC weaknesses that are structurally
   invisible to Gaussian testing become measurable.

6. P.372 man-made-noise environment scaling. The noise floor can be
   re-interpreted as a quiet-rural anchor and scaled to city/residential/rural
   by the P.372 Part-6 median man-made-noise delta for the band. The
   city-to-quiet spread (~24 dB) dwarfs most measured knob effects and makes
   "realistic profile" cells physically grounded.

7. Statistical self-verification of the fade. F.1487 specifies *no*
   implementation-verification procedure, and cross-simulator variance is a
   named problem in the literature (Furman & Nieto found two "CCIR
   Poor"-conformant hardware sims differing by >2.3 dB). skywave's
   self-verification tests check the Doppler PSD is Gaussian of the specified
   width (Welch overlay), the tap envelope is Rayleigh (mean/rms = √(π/4)), the
   two taps are uncorrelated, and average power is preserved. The tap-gain
   process is generated at ≥32x the 2-sigma Doppler spread (MIL-STD-188-110C
   Appendix E's implementation rule) before interpolation. Together this turns
   "F.1487-conformant" from an assertion into a tested claim, a rigor step
   almost none of the field ships. And it now has an *external* anchor: an
   earlier cross-calibration at the canonical 2 ms / 1.0 Hz "Poor" cell
   measured skywave's faded BER within 0.11 dB (ΔSNR@10% PER) of codec2's
   community-standard `ch`, a shared-lineage port-fidelity check, and within
   0.14 dB of PathSim, an *independently* implemented Watterson, both inside
   their pre-registered gates (≤1 dB port-fidelity, ≤2 dB independence). That
   same run surfaced a latent non-Rayleigh bug in PathSim as shipped (an
   unused Hilbert quadrature branch), for which the project contributed an
   upstream fix, precisely the cross-simulator variance Furman & Nieto warn
   of, caught and corrected.

8. Scheduled fading for adaptive rate control. A scheduled-fading mode plays a
   timed sequence of channels within one session, crossfaded, with each
   transition logged as ground truth. Static presets never exercise
   mode-switching; every modern modem is adaptive. No published methodology
   appears to do this.

9. Determinism at scale. Seeded, paired-seed A/B, and a block-lockstep *virtual
   clock* that runs faster than real time while staying bit-reproducible (same
   seed leads to the same *result*, not just same channel). codec2/`ch` is
   deterministic per fading file; nobody else offers a deterministic
   faster-than-realtime two-station link.

10. Explicit level/PEP/PAPR accounting plus a clipping alarm. Explicit
    peak/PEP, PAPR, and clip/rail metrics, plus a self-announcing "output
    clipping" warning that mirrors codec2 `ch`'s >0.1% alarm. A silent level
    regression (the class of bug that can make a linear fading channel quietly
    clip and collapse) cannot slip past an operator.

---

## 5. Where skywave does not lead (honest caveats)

1. Same fundamental Watterson model, same validity limits. skywave's engine is
   the same 1970 Gaussian-scatter two-path model as the rest of the field.
   Documented DSTO/CCIR critiques note that Gaussian-scatter is "almost
   certainly not valid for all HF channels." The self-verification harness
   (Section 4, point 7) mitigates *implementation* variance; it does not fix
   *model* validity. Nobody in the open landscape does better here.

2. Two taps only. skywave uses two equal-power paths (F.1487 canonical).
   DRM/Dream, IONOS/HFSimulator, and Mercury support up to four taps, so DRM's
   4-tap ETSI profiles (US Consortium 0/0.7/1.5/2.2 ms; Channel 6 0/2/4/6 ms)
   cannot be reproduced by skywave as-is. For the amateur HF target this
   rarely matters; for DRM-profile conformance it does.

3. Not a portable/general-purpose tool. codec2 `ch` is the de-facto community
   channel filter (a pipe stage any modem can drop in); PathSim is the classic
   GUI; both are widely used across projects. skywave is tightly bound to its
   own bench harness (device map, socket framing, and result contract). It is
   a superior *bench*, not a drop-in *utility*.

4. No mature LDPC/BER test-vector pipeline like codec2's. codec2 ships
   `fading_files.sh` + `ofdm_fade.sh` and integrates the CML coded-modulation
   library for standardized coded-BER gates. skywave's correctness lives in
   its own tests and goodput drivers, which is fine internally but is not the
   community-shared, reproducible BER flow codec2 offers.

5. A secondary test channel is lighter and unverified. The secondary,
   dependency-free channel is a sum-of-sinusoids Watterson good enough for
   golden-vector determinism, but it does not carry the main engine's
   statistical self-verification. It ships a deliberately small preset subset
   (good / moderate / poor / deep); the shared names agree with the main
   engine (good 0.5/0.1, moderate 1/0.5, poor 2/1.0), so a cell labeled the
   same is the same channel across both.

---

## 6. Comparability discipline

Two points that matter whenever a skywave number is quoted against another
tool's.

- Citing a delay/Doppler pair does not guarantee comparable BER. Furman &
  Nieto (Nordic HF 2001) is the canonical warning: two hardware simulators
  both nominally "CCIR Poor" (2 ms / 1.0 Hz) differed by >2.3 dB from
  un-standardized filter shape, tap-update rate, and interpolation. skywave
  pins the two variables behind that spread (the tap-update rate, ≥32x,
  MIL-STD-188-110C App E, and the preset Doppler, `poor` = canonical 2 ms /
  1.0 Hz, matching codec2 `ch --mpp`, PathSim, and DRM Ch. 4), but the
  analog-filter shape is still implementation-specific across tools. Any
  external number should therefore state the full config (SNR convention,
  exposure length, tap rate, filter), not just the channel name.

- SNR and exposure conventions align with the standards. skywave pins
  mean-signal/mean-noise in 3 kHz (MIL-STD-188-110 / F.1487 convention) and a
  fading exposure rule of ≥3000/Doppler seconds, which matches F.1487 Section
  6's test-length recommendation. State both explicitly whenever a fading
  number is published so a reader can reproduce it.

---

## 7. Limitations and possible extensions

The physics-parity items (canonical `poor`, ≥32x tap update, and a named
`flutter` preset) are in the current build. Beyond that:

1. External cross-calibration: done (see Section 4). Completed at the
   canonical 2 ms / 1.0 Hz "Poor" cell: measured BER agreement within 0.11 dB
   of codec2 `ch` (a shared-lineage port-fidelity check) and 0.14 dB of an
   independently-implemented PathSim, both inside their pre-registered gates.
   What remains is polish, not proof: a finer SNR grid to tighten the
   absolute crossing and a formal verdict writeup carrying the full per-cell
   configuration.

2. Generalized N-tap fading. The engine is a fixed two-tap model today; the
   summation already supports more paths, so an N-tap version would allow
   DRM's 4-tap ETSI profiles and richer measured power-delay profiles. This is
   largely a *broadcast* concern rather than an amateur one: DRM (Digital
   Radio Mondiale) is a shortwave-broadcasting standard, and its 4-tap
   profiles target broadcast reception. The channel conditions that matter to
   amateur and professional HF data modems (ITU-R F.1487, CCIR 520-2, and
   MIL-STD-188-110 Appendix E) are all specified as two-tap, so the current
   two-tap model already covers that target; N-tap is mainly of value for
   DRM-profile conformance.

3. Reporting discipline. Because a delay/Doppler label alone does not
   guarantee comparable results across tools (Section 6), any cross-tool
   number should carry its full configuration.

---

## 8. Bottom line

On channel physics, skywave is a well-grounded peer of a converged field, with
two rigor features (fade self-verification, now externally cross-calibrated
against codec2 `ch` and an independent PathSim, both inside their
pre-registered gates, and scheduled fading) that put it slightly ahead of most.
On the radio/station chain (AGC, PA compression, ALC, PTT/half-duplex
turnaround, SSB filtering, clock/frequency offset, impulsive noise, and QRM),
skywave is the most complete open-source HF simulator surveyed, because the
rest of the landscape simply does not model those things. The field is strong
on the ionosphere between the antennas and near-silent on the radios at each
end; skywave models both. The gaps that remain (shared Watterson validity
limits, the two-tap ceiling, and bespoke-not-portable) are either shared with
the whole field or a scoped extension away.

---

## Sources

Standards and primary references (fetched directly during research):

- ITU-R F.1487 (05/2000): <https://www.itu.int/dms_pubrec/itu-r/rec/f/R-REC-F.1487-0-200005-I!!PDF-E.pdf>
- CCIR Rec. 520-2: <https://www.itu.int/dms_pubrec/itu-r/rec/f/R-REC-F.520-2-199203-W!!PDF-E.pdf>
- Watterson, Juroshek & Bensema, "Experimental Confirmation of an HF Channel Model," *IEEE Trans. Commun. Technology*, COM-18(6), Dec 1970, 792-803.
- Furman & Nieto, "Understanding HF Channel Simulator Requirements...," Nordic HF Conference (HF01), 2001.
- MIL-STD-188-110C w/Change 1, Appendix E ("Characteristics of HF Channel Simulators").
- ETSI ES 201 980 V3.1.1 (DRM), Annex B channel table: <https://www.etsi.org/deliver/etsi_es/201900_201999/201980/03.01.01_60/es_201980v030101p.pdf>

Tools (source code fetched directly):

- codec2 `ch.c` / `doppler_spread.m` / `ch_fading.m` / `channel_lib.m` (LGPL-2.1): <https://github.com/drowe67/codec2>
- PathSim (AE4JY): <https://www.moetronix.com/ae4jy/pathsim.htm>; modern port: <https://github.com/bubnikv/pathsim>
- GNU Radio gr-channels (GPLv3): <https://www.gnuradio.org/doc/doxygen/page_channels.html>
- Mercury (Rhizomatica, GPL-3.0): <https://github.com/Rhizomatica/mercury>
- FreeDATA (DJ2LS, GPL-3.0): <https://github.com/DJ2LS/FreeDATA>
- DRM / Dream (GPLv2+): <https://sourceforge.net/projects/drm/>, mirror <https://github.com/rafael2k/dream>
- IONOS / HFSimulator (ARSFI, MIT): <https://github.com/ARSFI/HFSimulator>
- NTIA/ITS software index: <https://its.ntia.gov/software/high-frequency/>

*Note on sourcing: every tool claim above was taken from directly-fetched source
code or standards text, not secondary summaries. Where sources disagreed (the
F.1487 Sections 2.2 and 3.3 "Poor" footnote ambiguity, codec2's stale README
`--fast`/1 ms figure, NTIA's 2 ms/2 Hz Poor drift), the discrepancy is called
out rather than smoothed. Two items were not verifiable and are marked as such:
whether IONOS/HFSimulator models AGC/PTT, and ardopcf's exact license.*
