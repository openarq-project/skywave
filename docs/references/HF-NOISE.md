# HF noise and interference (literature basis)

This is a literature survey of atmospheric/impulsive noise, man-made noise,
and co-channel interference on HF, gathered to ground skywave's noise and
interference models in published measurements rather than guesswork. See
the channel-model doc (Section 5, the noise model) for how the Gaussian,
P.372, and impulsive layers are actually implemented, and the QRM-model doc
for the full co-channel interference model.

## 1. Atmospheric and impulsive noise (ITU-R P.372)

- The governing relation is `Fa = 10*log10(pn/kT0b)` and
  `Pn(dBW) = Fa + 10*log10(B_Hz) - 204` (P.372-17 Section 1.2).
- Atmospheric Fam is tabulated at 1 MHz by season and 4-hour block on
  contour charts, scaled to HF via companion figures.
- **Vd (rms/average envelope voltage)** is P.372's impulsiveness parameter:
  Vd = 0 dB is Gaussian-like, and larger values are spikier. It is
  tabulated for a 200 Hz reference bandwidth with a bandwidth-conversion
  nomograph; the numeric values live only in graphical figures, so the
  machine-usable source is the ITU-R Study Group 3 reference software
  (github.com/ITU-R-Study-Group-3/ITU-R-HF), which is preferable to
  hand-digitizing the charts, though even that software mainly confirms the
  charts exist rather than making the numbers easy to extract.
- Qualitative Vd behavior: quieter conditions (daytime, higher HF,
  temperate latitudes) push Vd higher (spikier), while noisy summer-night
  low-HF conditions push toward Gaussian, consistent with a central-limit
  effect over many distant strikes. This direction is less firmly
  established than the existence of the Vd parameter itself.
- **Generator models** used in the literature: the Hall model (a closed-form
  amplitude-probability-distribution fit, the basis of P.372's own curves);
  Middleton Class A (a Poisson-Gaussian mixture parameterized by an
  impulsive index A and a Gaussian/impulsive power ratio Gamma, the
  standard practical software generator, though HF-specific calibrations
  are scarce and most published fits are for power-line communications);
  Markov-Middleton (adds hidden-Markov burst states to model impulse
  clustering, the right upgrade if burst timing matters); and alpha-stable
  models (a good tail fit, harder to map onto P.372 parameters).
- **Gaussian-vs-impulsive ranking sensitivity** is a literature gap: no
  published HF-modem bakeoff runs the same modems under both noise types.
  What the adjacent coding-theory literature does show: AWGN-optimized
  coding and interleaving degrades severely under bursty impulsive noise,
  with a rule of thumb to interleave across roughly 20x the maximum impulse
  duration. The PLC/OFDM literature shows bit-error-rate floors scaling
  proportional to A*N (impulse index times block size), meaning
  interleaver and FFT-size choices matter more under impulsive noise than
  under Gaussian noise; a short-interleaver design that looks fine under
  Gaussian noise can hide a much worse impulsive floor. This mechanism is
  established in PLC/OFDM contexts and is expected to transfer to HF, but
  has not been directly measured there.
- **Standards posture**: ITU-R F.1487 Annex 2 sets signal-to-noise ratio
  with "band limited Gaussian noise"; MIL-STD-188-110B conformance testing
  uses AWGN plus ITU-R Poor only; STANAG 4539 practice follows the same
  pattern; and Winlink's IONOS-SIM modem comparison likewise tests only
  Gaussian conditions. The upshot: impulsive-noise robustness is untested
  territory across the entire published-standard ecosystem, and no
  external golden curve exists for it.

## 2. Man-made noise (ITU-R P.372 Part 6)

The median man-made noise floor above kT0, for a lossless short vertical
monopole, follows

    Fam(dB) = c - d*log10(f_MHz)

(add 10*log10(B) for in-band power). Coefficients by environment category:

| Category      | c    | d    | @3.5 MHz | @7 MHz | @14 MHz |
|---------------|------|------|----------|--------|---------|
| City          | 76.8 | 27.7 | 61.7     | 53.4   | 45.1    |
| Residential   | 72.5 | 27.7 | 57.4     | 49.1   | 40.8    |
| Rural         | 67.2 | 27.7 | 52.1     | 43.8   | 35.5    |
| Quiet rural   | 53.6 | 28.6 | 38.0     | 29.4   | 20.8    |
| Galactic floor| 52.0 | 23.0 | 39.5     | 32.6   | 25.6    |

P.372's Table 2 also gives decile spreads over time (City 11.0/6.7 dB,
Residential 10.6/5.3 dB, Rural 9.2/4.6 dB) and a location-to-location
deviation of 8.4/5.8/6.8 dB across the same three categories.

- P.372 separates man-made noise into AWGN-like, impulsive, and
  single/multi-carrier ("SCN") components.
- A 2024 peer-reviewed gap analysis of the underlying ITU-R noise data bank
  notes that roughly 80% of the measurements behind these curves come from
  Japan and the model has gone essentially unrevised for decades. The
  categories are best read as relative guides (city is noisier than quiet
  rural) rather than as absolute predictions for present-day conditions.
- Modern switch-mode power supply and photovoltaic-inverter noise trends
  toward single/multi-carrier structure (combs and humps) close to a
  single source, but looks more AWGN-like in aggregate at a distance; this
  observation is anecdotal rather than a systematic measurement campaign.

## 3. Co-channel interference (QRM)

- A published HF interference simulator (IEEE Transactions on
  Electromagnetic Compatibility; arXiv:2402.04742) models congestion
  probability Qk as a logistic function of frequency, hour, week, and
  sunspot number. Its worked example, an ARRL Field Day measurement across
  the whole amateur HF allocation, gives Poisson interferer arrivals at
  about 6.68 per second, an exponential mean hold duration of about 10 s,
  Hall-model amplitude parameters (alpha_k around -16 to -9, beta_k around
  -0.1, average interferer power -160 to -90 dBm), uniform frequency within
  each allocation, uniform phase, and a modeled CW-Morse "PARIS" keying
  envelope that the paper's authors note is extensible to other keying
  patterns.
- The benchmarkable real-world behavior is how modems and TNCs handle a
  busy channel rather than raw robustness to interference: ARDOP's
  `BUSYDET` energy threshold (0-10 scale, default 5, 0 disables it),
  IONOS-SIM's FFT busy detector (roughly 43 Hz bins, an IIR-averaged
  floor estimate, a 3-40 dB above-floor threshold, about 2 dB of
  hysteresis, narrowband and wideband detection without decoding), and
  ALE's link-quality analysis (a SINAD/PBER composite channel ranking).
- No formal modem conformance standard includes a QRM or interference leg;
  channel access under interference is left to implementation-specific
  busy-detect and backoff logic rather than being part of any published
  test methodology.

## How this maps to skywave

These findings inform three separate, independently-toggled layers in
skywave rather than a single combined noise model:

1. **Pure Gaussian AWGN stays the comparability baseline.** Every formal
   conformance methodology surveyed above (F.1487, MIL-STD-188-110B,
   STANAG 4539, IONOS-SIM) is Gaussian-only at its core, so skywave's
   default noise and its comparability ladders stay pure Gaussian,
   uncontaminated by any other noise layer, so that figures produced here
   stay comparable to published modem results elsewhere in the field.
2. **The P.372 man-made floor is an opt-in environment preset**,
   `SIM_NOISE_ENV`, with presets for city, residential, rural, and quiet
   (labeled "quiet rural" in the table above), applying the
   `Fam = c - d*log10(f_MHz)` formula with the coefficients in Section 2.
   It is off by default. The roughly 24 dB city-to-quiet-rural spread
   exceeds most other measured knob effects in the model, which is why
   environment choice is treated as a first-class, separately-labeled
   axis rather than folded into a single noise number.
3. **The impulsive/Vd layer is opt-in**, `SIM_NOISE_VD` (with
   `SIM_NOISE_VD_K_DB`, default 26), a Vd-calibrated Gaussian-plus-impulse
   mixture rather than a hand-digitized P.372 curve. It is run against
   both clean and faded channel bases, but is never mixed into the
   Gaussian comparability ladders from item 1 above. The exact calibration
   behavior and reachable Vd range are documented in the channel-model
   doc; this doc does not restate those numbers.
4. **The QRM family is opt-in**, `SIM_QRM_OCC` (occupancy), `SIM_QRM_INR_DB`
   (interferer level), and `SIM_QRM_SWEEP` (an optional swept-carrier/OTHR
   model), parameterized from the arXiv:2402.04742 interference simulator
   above but re-keyed to occupancy fractions and channel-referenced INR so
   the model stays meaningful across arbitrary signal levels rather than
   reproducing the source paper's whole-band absolute-dBm parameterization
   directly. The full model, including the swept-carrier behavior and the
   rail-budget interaction with occupancy and INR, is documented in
   QRM-MODEL.md.
5. **Fading cells stay Gaussian-based for comparability**, with a subset
   optionally re-run over the P.372 floor as a realism cross-check rather
   than a replacement for the Gaussian baseline.

Across all of this, one literature gap is worth restating plainly:
impulsive-noise robustness is untested territory across the entire
published HF-modem conformance ecosystem (Section 1). There is no external
golden curve for it, which is exactly why skywave's impulsive layer is
calibrated against the ITU-R reference software and kept as a clearly
separate, opt-in cell family rather than presented as an established
benchmark.

## Sources

- ITU-R P.372-17: https://www.itu.int/dms_pubrec/itu-r/rec/p/R-REC-P.372-17-202408-I!!PDF-E.pdf
- ITU-R SG3 reference software: https://github.com/ITU-R-Study-Group-3/ITU-R-HF
- ITU-R F.1487: https://www.itu.int/dms_pubrec/itu-r/rec/f/R-REC-F.1487-0-200005-I!!PDF-E.pdf
- RSGB HF noise leaflet: https://rsgb.org/main/files/2017/12/221216-Noise-leaflet-issue-2.pdf
- RSGB noise-floor study: https://rsgb.org/main/technical/propagation/noise-floor-study/
- ITU data-bank gap analysis (2024): https://pmc.ncbi.nlm.nih.gov/articles/PMC11548504/ , https://www.mdpi.com/1424-8220/24/21/6832
- HF interference simulator (IEEE TEMC): https://arxiv.org/abs/2402.04742
- MIL-STD-188-110B conformance procedures: http://www.n2ckh.com/MARS_ALE_FORUM/110b_test_plan.pdf
- MIL-STD-188-141C: https://hflink.com/standards/MIL_STD_188-141C.pdf
- RapidM waveform performance: https://www.rapidm.com/standard-hf-waveform-performances/
- IONOS SIM manual (busy detector, Section 8): https://winlink.org/sites/default/files/downloads/ionos_simulator_manual_22may20.pdf
- ARDOP TNC spec (BUSYDET): https://winlink.org/sites/default/files/downloads/ardop_tnc_host_mode_interface_spec.pdf
- Winlink IONOS-SIM modem comparison (2020): https://winlink.org/sites/default/files/downloads/a_winlink_digital_mode_performance_comparison_based_on_the_ionis_sim_hf_vhf_channel_simulator_-_november_2_2020_0.pdf
- Middleton Class-A simulation: https://link.springer.com/article/10.1007/s11235-020-00746-x
- Class-A effects on OFDM: https://link.springer.com/article/10.1007/s11235-022-00975-2
- Markov-Middleton: https://ieeexplore.ieee.org/document/6575205/
- Alpha-stable PLC noise: https://www.researchgate.net/publication/273303264
- Interleaving under impulsive noise: https://www.researchgate.net/publication/274101991
- IET HF noise-tolerant modem: https://digital-library.theiet.org/doi/book/10.1049/conferences-CP392
- Vd statistics (VLF): https://agupubs.onlinelibrary.wiley.com/doi/full/10.1029/2009RS004336
- ARRL OnAllBands noise floors: https://www.onallbands.com/ham-radio-operating-insights-why-are-noise-floors-rising-what-can-we-do-about-it/
- ALE background (DTIC): https://apps.dtic.mil/sti/tr/pdf/ADA232909.pdf
