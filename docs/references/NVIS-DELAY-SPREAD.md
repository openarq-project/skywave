# NVIS delay spread and guard-interval sizing (literature basis)

This is the literature basis for interpreting skywave's NVIS delay-spread
presets: see the channel-model doc's preset table (Section 4) for `NVIS`,
`nvis-max`, and `nvis-disturbed`, and the channel-conditions doc for the
broader measurement survey these presets are drawn from. Specifically,
this page asks whether the 7 ms / 1 Hz `nvis-disturbed` preset is a
realistic per-path target or a standard's own worst-case tail, and how it
relates to measured NVIS delay spread, as background for sizing a
waveform's guard interval or cyclic prefix against it.

## Verdict: 7 ms is a real standard channel, but the worst-case tier, not typical

- The 7 ms / 1 Hz figure is ITU-R F.1487 Annex 3, Section 3.4's
  "Disturbed near-vertical-incidence" channel, confirmed directly against
  the ITU-R F.1487-0 (05/2000) text. The preset has legitimate standard
  provenance. But the standard's own section header calls it "Disturbed,"
  and there is no "quiet" or "moderate" NVIS tier to compare it against:
  it is explicitly the tail, worst-case NVIS condition.
- It sits 3.5x above the same standard's non-NVIS mid-latitude
  "Disturbed" oblique-path value of 2 ms (CCIR Rec. 520-2 Section 2.2.3
  "Poor" = 2 ms / 1 Hz; numerically identical to F.1487 Section 3.3).

## What real measurements show (all well below 7 ms)

| source | condition | delay spread |
|---|---|---|
| Male/Riera/Porte, *Sensors* 21(6):2210 (2021), 97 km mid-latitude NVIS link, 5.4 MHz, 12 days | mean (typical) | ~0.3 ms (O 0.33 / X 0.31) |
| same campaign | observed max (peak) | ~2.9 ms |
| HamSCI South Texas (2024), single-hop NVIS | typical | ~1.9 ms (290 km round trip) |
| same, 3-hop reverberation (3F2-1F2) | multi-hop max | ~3.8 ms |
| CCIR 520 / F.1487 mid-latitude "Poor/Disturbed" (oblique, non-NVIS) | standard worst | 2 ms |
| trans-equatorial 12,760 km oblique (non-NVIS, long multi-hop) | extreme tail | ~7 ms (+/-3.5 ms window) |

So measured short-path NVIS delay spread runs about 0.3 ms typical and
about 3-4 ms at the observed maximum. 7 ms-class delays do occur in
nature, but on long multi-hop trans-equatorial paths, not NVIS geometry
(a single-study result; see the caveats below for how far it
generalizes).

## What real waveforms design to

- MIL-STD-188-110C gates wideband-waveform BER on the 2 ms / 1 Hz "Poor"
  channel (explicitly equal to F.1487 Mid-Latitude-Disturbed). It
  reserves larger spreads (a 3-path 0/3/9 ms static profile) only for a
  separate static-multipath stress test, not routine performance
  verification. So the closest precedent for a modem tolerating 6-9 ms is
  framed by the standard itself as a robustness stress test, not a
  "typical channel."
- High-latitude, auroral, and polar HF paths have delay and Doppler
  spreads that significantly exceed mid-latitude figures (multiple field
  campaigns, 2003-2024), so F.1487's 7 ms "Disturbed NVIS" is plausibly a
  high-latitude / severe-storm / safety-margin design point rather than a
  mid-latitude operating figure.
- Rule of thumb (Witvliet and Alsina-Pages, *Telecom Systems*, 2017):
  symbol/guard length of about 10x the delay spread for an ISI-free
  channel (a rule of thumb, less firmly established than the measured
  figures above).

## Recommendation

A guard interval of roughly 3-5 ms is a defensible target for a waveform
sized to measured NVIS conditions: it covers the observed maximum (about
3-4 ms) with margin, and matches or exceeds the standard mid-latitude
"Poor" tier (2 ms). Sizing a guard interval or cyclic prefix past 7 ms
buys conformance to F.1487's own worst-case "Disturbed NVIS" tail and the
high-latitude/storm margin implied by it, not resilience against a
typical NVIS path. That is a real, throughput-priced design tradeoff (a
longer guard interval costs a fixed fraction of every symbol), and a
legitimate goal specifically for waveforms meant to survive marginal,
near-critical, or high-latitude conditions, separate from the goal of
covering ordinary regional NVIS traffic.

The armstrong modem project, for example, uses skywave's NVIS presets
this way: its OFDM cyclic prefix tolerates delay spread up to about
5 ms, which already covers every typical and observed-max NVIS figure
measured above and matches the standard mid-latitude "Poor" tier with
margin, without reaching for F.1487's disturbed-NVIS worst case.

## Caveats and gaps

- The strong measured figure (0.3 ms / 2.9 ms) comes from one study: a
  single path and season (97 km, Spain, December 2019, near solar
  minimum), mid-latitude, quiet-to-moderate conditions. It is not a
  broad multi-site percentile study, and generalizing it to a universal
  "NVIS ceiling" is not supported by the data; solar-minimum timing may
  bias the figure low.
- No confirmed provenance exists for how F.1487 derived its 7 ms figure;
  treat it as a committee-adopted design/margin point, not a
  statistically-derived percentile.
- No data was found on STANAG 4539/4285/5069 guard-interval assumptions;
  this remains a gap in the available literature.
- High-latitude NVIS specifically was not measured (only high-latitude
  oblique paths); whether near-vertical geometry protects against
  auroral irregularities is an open question.

## Sources

- ITU-R F.1487-0 (05/2000):
  https://www.itu.int/dms_pubrec/itu-r/rec/f/R-REC-F.1487-0-200005-I!!PDF-E.pdf
- CCIR/ITU-R Rec. 520-2 (1992)
- MIL-STD-188-110B/C
- Male, Riera, Porte, *Sensors* 21(6):2210 (2021):
  https://pmc.ncbi.nlm.nih.gov/articles/PMC8004164/
- HamSCI 2024 multipath analysis
- Witvliet and Alsina-Pages, *Telecom Systems* (2017)
