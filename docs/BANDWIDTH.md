# Occupied bandwidth and regulatory limits

This document describes how occupied bandwidth is defined and measured for HF and
VHF/UHF amateur radio data transmissions, the regulatory limits that apply in the
major jurisdictions, and why a channel simulator or modem bench should treat
occupied-bandwidth compliance as a first-class measured output, not an assumption
checked once and forgotten.

The numbers below were compiled from FCC, ISED, and IARU primary documents, from
codec2 and FreeDATA project documentation, and from direct measurement of emitted
waveforms. Confidence tags mark how solid each claim is: [HIGH] means verified
against a primary source, [MED] means credible but resting on a single or
secondary source, and [GAP] means not pinned down and worth independent
verification before relying on it.

## 1. Why this matters for a bench

A channel simulator that only reports goodput and bit-error rate is answering
half the question for any wideband HF or VHF data mode. The other half is
whether the waveform is legal to transmit at all, and whether it survives the
audio passband of a real transceiver on the way out. A mode can look excellent
in a flat, wideband bench and still fail a regulator's bandwidth test, or lose
its edge subcarriers to a radio's default SSB filter. Both of those are
measurable, and both belong next to goodput in a bench's output, not in a
separate compliance review done once at the end.

The two questions worth keeping distinct are:

1. Is the emitted waveform legal? This is a spectral measurement against a
   jurisdiction's occupied-bandwidth rule.
2. Is the emitted waveform realistic through a real rig? This is a question
   about how a transceiver's SSB or FM audio passband treats the waveform's
   edges.

Both are covered below.

## 2. How occupied bandwidth is defined and measured

Occupied bandwidth is not a single, universal number for a given signal. It
depends on the measurement convention used: what power reference the
attenuation is measured against (mean power or peak power), and how many dB
down from that reference defines the band edge. Different regulators use
different conventions, and the same waveform will measure to different widths
under each one.

The binding definition for United States amateur HF operation, 47 CFR
Section 97.3(a)(8), reads:

> "Bandwidth. The width of a frequency band outside of which the mean power of
> the transmitted signal is attenuated at least 26 dB below the mean power of
> the transmitted signal within the band."

So the US limit is a -26 dB-below-mean-power occupied bandwidth. Canada's
ISED uses a -26 dB-below-peak convention instead, and the IARU region band
plans use a -6 dB convention. These are not interchangeable: for a low-PAPR
(clipped) waveform where peak power sits roughly 7 to 8 dB above mean power,
the FCC's -26-dB-from-mean measurement is the stricter of the two -26 dB
conventions, producing a wider measured occupied bandwidth than Canada's
-26-dB-from-peak measurement of the same signal. As a result, a waveform that
passes the US -26-dB-from-mean test at or under 2.8 kHz will, with margin,
also pass Canada's -26 dB test and the IARU -6 dB tiers. That makes the US
convention the one to implement first if only one measurement is going to be
built into a bench.

A useful secondary measure is 99 percent occupied bandwidth (the width
containing 99 percent of the signal's total power), which gives a
cross-jurisdiction sanity check independent of any particular dB-down
convention.

## 3. Legal limits: HF data

| Jurisdiction | HF data bandwidth limit | Measurement reference | Notes |
|---|---|---|---|
| US (FCC) | 2.8 kHz | -26 dB below mean power (47 CFR Section 97.3(a)(8)) | FCC 23-93, effective January 8, 2024, Section 97.307(f)(3); replaced the earlier 300-baud symbol-rate limit. [HIGH] |
| Canada (ISED) | 6 kHz for most HF bands | -26 dB below peak amplitude (RBR-4, Bandwidths section) | 60 m is 2.8 kHz, 30 m is 1 kHz. Substantially more headroom than the US limit. [HIGH] |
| IARU Region 2 (band plan) | 2700 Hz "all-modes/ACDS" segments; 500 Hz narrow digital-mode sub-bands | -6 dB points | Voluntary, not statute. A 2438 Hz-wide signal fits the 2700 Hz segments with about 262 Hz of margin. [HIGH for method; segment boundaries MED] |
| IARU Region 1 (UK/Germany) | 2700 Hz data tier (CW/RTTY/data, no SSB); tiers at 200/500/2700 Hz | -6 dB (IARU convention) | Voluntary band plan; national regulators generally defer to it. A 2438 Hz-wide signal fits the 2700 Hz tier. [MED] |
| Australia (ACMA) | not pinned | not pinned | ACMA generally defers to band plan conventions, but this has not been independently confirmed. [GAP] |

An important placement nuance follows from the numbers above: a mode occupying
around 2438 Hz is a wide-data mode. It belongs in the wide "all-modes / ACDS /
2.8 kHz" segments, not in the narrow 500 Hz digital-mode sub-bands (for
example 14070 to 14099 kHz). The honest framing for a comparison is that such
a mode fits the wide-data segments, not that it fits anywhere data operation
is permitted.

## 4. Legal limits: VHF/UHF and FM

US VHF/UHF baud-rate limits are still in force. The FCC's 2024 order (FCC
23-93) only removed the HF symbol-rate limit; a companion proposal to remove
the VHF/UHF baud limit was floated for comment, but no final rule has been
confirmed. [HIGH] This means a wideband OFDM-style HF mode is not
automatically authorized for VHF/UHF (2 m / 70 cm) data operation under
current US rules, which matters for any project considering an FM-based data
profile.

An FM data signal is constrained by the FM channel rather than by a 2.8 kHz
audio bandwidth cap. Applying Carson's rule with 2.5 to 5 kHz of deviation and
roughly 3 kHz of audio gives a channel width around 11 to 16 kHz. Usable audio
bandwidth through a typical FM rig's flat-audio or 9600-baud data port is
roughly 300 to 3000 Hz; the microphone audio path is narrower still once
pre-emphasis and limiting are accounted for. [MED, standard engineering
practice, not independently re-verified]

Open items here: the exact FM-data emission designators and per-band
authorized bandwidths under 97.307/97.305, and the audio response of a
9600-baud data port, were not pinned down and are worth revisiting before
building an FM data profile. [GAP]

## 5. HF SSB rig audio passband

In single-sideband operation, the audio passband width is approximately equal
to the occupied RF bandwidth, since SSB is a direct frequency translation of
the audio signal.

| Rig / setting | TX/RX audio passband | Width |
|---|---|---|
| Kenwood TS-590 SSB default | 300-2700 Hz | 2400 Hz [MED] |
| Kenwood TS-590SG SSB-DATA | low cut 10-500 Hz (default 300), high cut 2500-3000 Hz (default 2700) | up to about 2990 Hz [MED] |
| Icom 746Pro/756Pro/7600/7700/7800 TX | Wide 2.9 kHz / Mid 2.4 kHz / Narrow 2.1 kHz | 2.1-2.9 kHz [MED] |
| VARA recommended rig filter | 0-3000 Hz (Narrow/Standard); 100-2900 Hz (Tactical 2750) | [MED] |
| General SSB "communications" filter | roughly 2.4-2.8 kHz | de facto default [HIGH] |

The practical consequence for a wide mode: codec2-derived OFDM signals are
typically centered around 1500 Hz of audio, so a 2438 Hz-wide mode spans
roughly 281 to 2719 Hz. That range sits at or beyond the edges of a default
300-2700 Hz SSB filter, meaning the lowest and highest subcarriers can be
attenuated unless the rig's DATA or wide filter setting (roughly 150-2900 Hz
or wider) is used instead. This is why a flat, wideband channel simulator can
be optimistic about a wide mode's real-rig performance, and why a realistic
rig model should include both a default (roughly 300-2700 Hz, worst case) and
a data/wide (roughly 150-2900 Hz) passband option. SSB filter roll-off is
steep, on the order of tens of dB per octave, so a realistic model should use
a steep bandpass response with edge group delay rather than a brick-wall
cutoff. [synthesis of the above, not independently measured]

## 6. Case study: rig passband effect on wide-mode goodput

To test whether the SSB rig passband actually costs a wide OFDM mode any
goodput in practice, skywave includes a configurable SSB
passband filter: a stateful Butterworth filter with
selectable presets (off, default 300-2700 Hz, data 150-2900 Hz, narrow
300-2400 Hz), applied on both the transmit and receive sides.

The default (300-2700 Hz, order 6) filter response against a roughly
2438 Hz-wide OFDM mode spanning about 281-2719 Hz showed the passband was
flat (about 0 dB) from 700-2400 Hz, down about 3 dB at the 300/2700 Hz edges,
down 5.6 dB at 281 Hz, down 3.3 dB at 2719 Hz, and down about 41 dB at 150 Hz.
So the default rig filter measurably attenuates the wide mode's two edge
subcarriers by a few dB.

Clean-channel goodput (no noise, half-duplex with push-to-talk, 32 kB
payload, riding a clipped wide mode):

| Rig passband | Goodput | vs off |
|---|---|---|
| off (flat bench) | 132.1 | (baseline) |
| data (150-2900 Hz) | 135.1 | +2% |
| default (300-2700 Hz) | 132.9 | about 0% |
| narrow (300-2400 Hz) | 133.3 | +1% |

Marginal-SNR goodput (at the noise level where the wide mode is near its
performance cliff):

| Rig passband | Goodput |
|---|---|
| off | 142.9 |
| data (150-2900 Hz) | 132.4 |
| default (300-2700 Hz) | 132.6 |
| narrow (300-2400 Hz) | 157.6 |

The result: the rig passband had no measurable goodput impact on the wide
mode, at either clean or marginal SNR. The spread across rows is consistent
with normal run-to-run variance for a single sample per cell, and the
revealing detail is that the effect is not monotonic in filter width; the
narrowest filter produced the highest goodput, the opposite of what a simple
edge-subcarrier attenuation story would predict. Two effects appear to
cancel: forward error correction spread across many subcarriers absorbs the
few dB of edge attenuation, and a narrower receive filter also removes some
out-of-band noise, which slightly helps the effective SNR. So a flat,
wideband channel simulator was not meaningfully optimistic about wide-mode
goodput after all, at least for this mode and this test.

Caveats on that conclusion: this is a goodput result only, separate from the
occupied-bandwidth compliance question in Section 9, where the same mode is
legal but with a thin margin. The measurement was a single sample per cell,
so the conclusion rests on the absence of any trend with filter width rather
than on the precision of the absolute numbers. Fading combined with rig
passband was not tested and could interact more strongly. And codec2-style
modes are designed with their subcarrier layout tuned to fit an SSB passband,
so the edge subcarriers may never have been as marginal as the raw span
suggests, which is consistent with the null result.

## 7. Reference: occupied bandwidth of comparable HF data modes

| Modem / mode | Occupied bandwidth (Hz) | Source/confidence |
|---|---|---|
| codec2 DATAC13 | 200 | codec2 README_data [HIGH] |
| codec2 DATAC4 | 250 | [HIGH] |
| codec2 DATAC0 / DATAC3 | 500 | [HIGH] |
| codec2 DATAC1 | 1700 | [HIGH] |
| FreeDATA data_ofdm_500 | about 500 | by geometry [MED] |
| FreeDATA data_ofdm_2438 | about 2438 | by geometry/name [MED] |
| VARA HF Narrow / Standard / Tactical | 500 / 2300 / 2750 | [HIGH] |
| ARDOP | 200 / 500 / 1000 / 2000 | comparison source [MED] |
| PACTOR 1-4 | about 2.4 kHz (P3/P4) | not pinned [GAP] |

For scale: VARA Tactical, at 2750 Hz, sits 50 Hz under the US 2.8 kHz cap. A
2438 Hz-wide mode is 362 Hz under the cap and narrower than VARA's widest
tier. In other words, a 2438 Hz mode is not an outlier; the widest mainstream
HF data mode in current use (VARA Tactical) is wider than it.

## 8. Clipping and PAPR effects on occupied bandwidth

codec2 clips (compresses) its waveforms by default to reduce peak-to-average
power ratio. From the codec2 README_data: "Clipping (compression) is enabled
by default on each modem waveform to maximise the PAPR... For a given peak
power, clipping increases SNR over the channel by 3-4dB." [HIGH]

The DATAC0/1/3/4/13 modes ship with clipping enabled by default in codec2's
own configuration (`ofdm_mode.c`, `clip_en=true`, not overridden). Only
FreeDATA's wider additions (data_ofdm_500, data_ofdm_2438, qam16c2) set
`clip_en=false`. [HIGH, confirmed against source] So enabling clipping on a
wide mode is returning it to codec2's own default behavior, not a novel or
risky deviation from it.

Why FreeDATA left clipping off for its wide modes is unresolved. [GAP] David
Rowe noted in 2021 that he "[hadn't] tuned the compression (PAPR) of the data
modes to get maximum performance out of them for a given peak power," and
would "work on that next," which reads as unfinished tuning rather
than a documented spectral objection. No source establishes a spectral or
regrowth-related rationale for leaving clipping off on those modes; this is
worth checking directly against FreeDATA's own source or issue tracker before
treating "it's safe because they left it off" as established fact.

The interaction between clipping and filtering is real and has been
acknowledged upstream. David Rowe: "filtering to constrain the frequency
spread brings the PAPR up again." [HIGH] codec2 handles this with a sequence
of clip, transmit bandpass filter, gain restore, and a final peak clip
(`ofdm_hilbert_clipper` in `ofdm.c`). When that transmit bandpass filter is
enabled, out-of-band regrowth from clipping is filtered at the source, so the
emitted occupied bandwidth ends up bounded by the transmit filter rather than
by the raw clip operation.

## 9. Case study: measured occupied bandwidth of emitted waveforms

To turn "should be compliant" into a measured result, one project measured
the actual emitted waveform (post-clip, post-transmit-filter) of several
modes using Welch power spectral density estimation at an 8 kHz sample rate
with a 2048-point FFT. Since audio occupied bandwidth equals RF occupied
bandwidth in SSB, this measurement is directly comparable to the regulatory
figures above.

| Mode | -26 dB/mean (FCC) | -26 dB/peak | -6 dB (IARU) | 99% OBW |
|---|---|---|---|---|
| DATAC13 | 414 | 379 | 172 | 266 |
| DATAC1 | 2047 | 2023 | 1668 | 1680 |
| FD-OFDM-500 | 641 | 625 | 473 | 523 |
| QAM16C2 | 2469 | 2410 | 2141 | 2141 |
| FD-OFDM-2438 | 2738 | 2719 | 2422 | 2430 |

All of the modes measured pass the US -26-dB-from-mean, 2.8 kHz test, but
FD-OFDM-2438 comes closest to the ceiling at 2738 Hz, only 62 Hz (2.2 percent)
under the 2.8 kHz limit. Its nominal "2438 Hz" name describes roughly the -6
dB width (measured at 2422 Hz); at the legally binding -26 dB measurement the
skirts add about 300 Hz. The numbers are internally consistent: DATAC13's -6
dB width of 172 Hz is close to its nominal 200 Hz name, DATAC1's 99% OBW of
1680 Hz is close to its nominal 1700 Hz, and FD-OFDM-500's 99% OBW of 523 Hz
is close to its nominal 500 Hz.

One caveat on precision: a 2048-point FFT at 8 kHz gives roughly 3.9 Hz
resolution bandwidth, finer than a real spectrum analyzer's typical 100 to
300 Hz resolution bandwidth. A finer resolution bandwidth tends to measure
slightly wider skirts than a coarser one would, so these -26 dB widths are
somewhat conservative relative to what a real analyzer would show. That
conservative bias is the right direction for a compliance gate to err toward.

A clipped-versus-unclipped comparison found that clipping does not
meaningfully widen the emitted occupied bandwidth once the transmit bandpass
filter is in the signal path: FD-OFDM-2438 measured 2738 Hz clipped versus
2723 Hz unclipped (a 16 Hz difference), and FD-OFDM-500 measured 641 Hz
clipped versus 629 Hz unclipped (a 12 Hz difference). This matches the
expectation from Section 8 that the transmit filter removes clipping's
out-of-band regrowth. (A larger clipped-versus-unclipped delta measured for
QAM16C2, 2469 Hz versus 4000 Hz, reflects a difference in whether the
transmit bandpass filter was enabled in that particular comparison rather
than a clean clip-only effect, and should not be read as a clip effect.)

The overall result: compliance confirmed for all modes measured, with
FD-OFDM-2438 on a thin margin. A measurement like this is well suited to
becoming an automated regression gate that fails a build if a future
geometry, clipping, or gain change pushes a mode's occupied bandwidth over
the legal limit, which matters most for a mode already this close to the
ceiling.

## 10. Recommendations for a bench or simulator

Legal status and real-rig realism are separable questions, and a bench should
answer both rather than assuming one from the other.

On legality: the measurement to implement is the -26 dB-below-mean-power
occupied bandwidth, following the FCC 47 CFR Section 97.3(a)(8) method,
computed against each mode's actual clipped, filtered, emitted waveform, and
asserted to stay under 2.8 kHz with margin for US HF operation. Reporting the
-6 dB width (the IARU convention) and a 99% power occupied bandwidth alongside
it gives useful cross-jurisdiction context, since Canada and the IARU regions
use different conventions and thresholds. This turns "should be compliant"
into a measured, repeatable compliance gate, and a clipped-versus-unclipped
comparison run alongside it answers whether clipping widened the occupied
bandwidth at all.

On real-rig realism: a wideband mode sitting near the edges of a default SSB
filter needs modeling of the rig's audio passband, not just a flat, wideband
channel. The case study in Section 6 found no measurable goodput cost from a
realistic rig passband for one wide mode, but that null result rests on a
small sample and untested interactions with fading, so it should be treated
as a starting point for further measurement rather than a settled conclusion
that rig passband never matters.

More generally: both of these are the kind of measurement that degrades
silently if left as a one-time manual check. Wiring them into a bench as
first-class, automatically computed outputs, alongside goodput and
bit-error-rate, is what keeps a future change to a mode's geometry, clipping,
or gain from accidentally shipping a non-compliant or unrealistic waveform.

## 11. Open questions

The following were not pinned down and are worth follow-up:

- Australia (ACMA) specific occupied-bandwidth rules and measurement
  convention.
- UK and Germany statutory bandwidth limits, as distinct from the voluntary
  IARU Region 1 band plan.
- FM-data emission designators and per-band authorized bandwidths under FCC
  Part 97.307/97.305, and the audio response of a typical 9600-baud data
  port.
- PACTOR and WINMOR occupied bandwidth figures.
- FreeDATA's documented rationale, if any, for leaving clipping off on its
  wide OFDM modes.
- The precise per-band segment edges within the IARU Region 1 and Region 2
  2700 Hz data tiers; the existence of the 2700 Hz tier itself is well
  supported, but the exact segment boundaries were not independently
  confirmed.
