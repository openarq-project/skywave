# Transceiver chain: AGC, ALC, and PA nonlinearity (literature basis)

This is the measured and literature basis for skywave's optional
receiver-AGC, transmitter-ALC, and PA-nonlinearity stages. See the
channel-model doc's transmit chain (Section 6) and receive chain (Section 7)
for how each stage is actually implemented and where it sits in the signal
path; this page exists to keep the source data and its attributions in one
place.

## Topic 1: HF SSB receiver AGC dynamics

| Parameter | Value | Confidence | Source |
|---|---|---|---|
| MIL-STD-188-141C "data service" AGC attack / release | <=10 ms / <=25 ms (to within 3 dB) | measured, spec Section 5.4.3 | hflink.com MIL_STD_188-141C.pdf |
| MIL-STD-188-141C "nondata" (voice) attack / release | <=30 ms / 800-1200 ms | measured, spec | same |
| MIL-STD-188-110B App C recommended RX AGC for its QAM data waveform | the slow "non-data" mode; fast AGC pumps gain mid-burst and corrupts amplitude-bearing constellations | measured, standard text Section C.7 | n2ckh.com 110b_test_plan.pdf |
| 110B App C preamble reservation for AGC settling | 0-7 blocks x 184 8-PSK symbols before sync/Doppler symbols | measured, Section C.5.2.1.1 | same |
| IC-7300 SSB AGC time constants (FAST/MID/SLOW) | 0.3 / 1.6 / 6.0 s | measured, manual | dxengineering IC-7300 manual |
| R&S EK895 attack / decay | <15 ms / 25 ms-3 s selectable | measured, datasheet | usermanual.wiki EK895 |
| Elecraft K3 AGC hold/hang | 0-300 ms | measured | n9adg.com K3 AGC params |
| PowerSDR presets | attack fixed 2 ms; decay 50/250/500/2000 ms | measured, spec table | docslib PowerSDR notes |
| Ten-Tec Orion decay | 5-1000 dB/s programmable; presets 20-60 dB/s | measured, app note | tentec 565Optimize.pdf |
| Generic comms-RX attack rule of thumb | ~1-5 ms | typical | qsl.net/k9gdt AGC page |
| Professional simulator doctrine | "No simulated radio filters or AGC should be used" in the channel simulator | measured, direct quote (Furman and Nieto, Harris/HFIA, "Understanding HF Channel Simulator Requirements in Order to Reduce HF Modem Performance Measurement Variability") | yumpu HFIA paper mirror |
| F.1487 stance | level/gain control is an external system effect, not part of the channel model | measured | ITU F.1487 |
| Practitioner ARQ convention | AGC off, manual RF gain (ARDOP usage docs) | typical | github pflarue/ardop |
| Burst-mode OFDM AGC architecture (non-HF) | gain set once from preamble, held for the burst | measured, patent | patents.google US7529178 |

Note: the sought "Understanding AGC" Furman/Nieto title does not exist; the
verified substitute is their HFIA channel-simulator-requirements paper
quoted above. Three other Furman/Nieto HFIA papers were checked and do not
discuss AGC.

## Topic 2: HF transmitter ALC dynamics

| Parameter | Value | Confidence | Source |
|---|---|---|---|
| IC-7610 onset overshoot @100 W PEP | about 0.6 dB, ~30 ms after key-up | measured, bench | ab4oj 7610notes Table 21 |
| IC-7610 @20 W PEP | about 1.0-1.1 dB, ~20 ms | measured | same |
| IC-7610 data-tone vs voice onset | essentially identical; loop dynamics, not waveform | measured, Table 25 | same |
| IC-7610 sustained drive | no overshoot; onset-only | measured | same |
| IC-756Pro3 exciter ALC attack/decay | ~500 ns / ~750 ms | measured, scope | ab4oj 756pro3 |
| IC-706MKII first-transmission spike | about 7 dB peak (25 W set, 130-145 W out), <2 ms, re-arms after ~5 s silence | measured, calibrated (ARRL QEX) | arrl.org QEX Aug-Sep 2018 Salas |
| Generality of first-key-down spike | occurs "on other models and other brands"; a generic ALC-loop-latency artifact | measured / expert | same |
| IC-7300 field reports | 0-13 dB (conflicting; a bench-controlled test showed 0) | anecdotal | febo.com, klop.solutions |
| Mechanism | overshoot, then gain cap, then settle; produces both AM and PM distortion (splatter) | expert | g4dbn.uk |
| VARA operating guidance | ALC ~1/3 scale; RMS ~= 1/4 rated PEP | typical, vendor guides | aggregated |

## Topic 3: HF PA nonlinearity

| Parameter | Value | Confidence | Source |
|---|---|---|---|
| IMD3 convention | "below one tone" vs "below 2-tone PEP" differ by 6 dB; the ARRL QST chart uses below-PEP | measured, documented | remeeus productreview.pdf |
| IC-7300 IMD3 (below PEP) | -46/-45/-39/-31 dBc @3.6/14.1/28.1/50.1 MHz | measured | pc5e 7300notes |
| FT-991 IMD3 (one-tone) | -18 to -27 dBc; -22 to -27 dBc backing 100 to 80 W | measured | ab4oj ft991notes |
| K3 / TS-590S 3rd-order (below PEP) | -27 / -29 dB | measured, QST reviews | qsl.net K3 review; radiomanual TS-590S |
| Overall measured rig spread | about -24 to -46 dBc below PEP | synthesis | above |
| Rapp equation / purpose | Vout = Vin / (1+(\|Vin\|/Vsat)^2p)^(1/2p); models solid-state PAs | textbook | ieee802.org 16 slide |
| Rapp p, typical SSPA | p = 2 (common default; "usually 2 <= p <= 3"; up to 10 to mimic near-hard-clip) | typical | arxiv 1510.01397; MathWorks; MDPI |
| Plain-Rapp caveat | mis-models low-level AM/AM of real class-AB (Honkanen and Haggman) | measured, secondhand | ieee802 slide |
| PSK BER vs PA point | <0.5 dB penalty near saturation (AWGN); out-of-band splatter is the sharp penalty | measured | a-star peak-to-green |
| Serial-tone vs multicarrier backoff | multicarrier needed 1.4-1.8 dB more backoff; the deciding factor for STANAG 4539 serial-tone (DERA 2000) | measured, cited in MILCOM'09 WBHF paper | nmsu WBHF.pdf |
| Recommended backoff, 110B/4539 family | ~3-6 dB | typical | ieee 5206458 (paywalled) |
| ANDVT multitone optimal deliberate clipping | 8.0 dB (16-tone) / 9.5 dB (39-tone DPSK) | measured, DTIC 1980 | dtic ADA092114 |
| FreeDV OFDM PAPR | ~4.5 dB | anecdotal | freedv.org |

Not found or flagged: a numeric PA-linearity clause in the 110B/4539
primary text (controlled); published VARA/ARDOP backoff percentages; the
oft-repeated "36-40 dB transmitter linearity" figure could not be traced
and should be treated as a search-summarization artifact rather than a
sourced claim.

## How this maps to skywave

**Receiver AGC.** `SIM_RX_AGC=1` with `SIM_RX_AGC_MODE` selects one of two
literature presets: `data` (10 ms attack / 25 ms release, the
MIL-STD-188-141C data-service timing) or `voice` (30 ms attack / 1000 ms
release, 141C non-data timing). This is an opt-in receiver-emulation stage
modeling burst-head gain error after a quiet gap; AGC stays out of the core
channel path by default, matching the field doctrine quoted in Topic 1
(the Furman and Nieto simulator-requirements paper, and F.1487's own
external-effect stance). Post-silence gain pumping into a short preamble
is a standards-documented mechanism, not a modeling artifact: 110B
reserves preamble blocks for exactly this settling.

**Transmitter ALC.** `SIM_ALC_PRESET` selects `modern` (0.8 dB overshoot,
25 ms settle) or `legacy` (7 dB overshoot, 2 ms settle, with a ~5 s
re-arm-after-silence). The raw knobs `SIM_ALC_OVERSHOOT_DB` and
`SIM_ALC_SETTLE_MS` are also available for a custom envelope. The model is
a burst-onset decaying-gain envelope ahead of the PA stage: onset-only,
independent of drive discipline, and the ~5 s re-arm lines up with
half-duplex ARQ turnaround cadence. It is amplitude-only and does not
model the accompanying AM/PM distortion; that is an acknowledged gap, not
an oversight.

**PA nonlinearity.** `SIM_PA_P` sets the Rapp sharpness parameter
(default 2, documented sweep range ~1.5-5) and `SIM_PA_VSAT` sets the
saturation level. The IMD3 validation band, -24 to -46 dBc below PEP
across the measured rigs in Topic 3, is the literature-consistency check
for the model, using the "below PEP" convention. The serial-tone-versus-
multicarrier backoff finding (DERA, Topic 3) means PA-backoff sensitivity
should be measured separately for OFDM-like and serial-tone-like
waveforms rather than pooled, with occupied-bandwidth compliance treated
as a first-class output alongside goodput.

For anything not covered by the exact values above, see the channel-model
doc.

## Sources

(URLs are copied exactly as found.)
- https://hflink.com/standards/MIL_STD_188-141C.pdf
- http://www.n2ckh.com/MARS_ALE_FORUM/110b_test_plan.pdf
- https://www.yumpu.com/en/document/view/43230354/understanding-hf-channel-simulator-requirements-
- https://www.hfindustry.com/public-presentations
- https://www.itu.int/dms_pubrec/itu-r/rec/f/R-REC-F.1487-0-200005-I!!PDF-E.pdf
- https://patents.google.com/patent/US7529178
- https://github.com/pflarue/ardop/blob/master/docs/USAGE_linux.md
- https://docslib.org/doc/5109710/powersdr-openhpsdr-user-notes-table-of-contents
- https://usermanual.wiki/Document/RohdeSchwarzEK895HFrecieverDatasheet.4145913481/html
- http://www.tentec.com/wp-content/uploads/2016/05/565Optimize.pdf
- http://n9adg.com/wp-content/uploads/2017/03/Elecraft-K3-Transceiver-AGC-Parameters-and-S-meter.pdf
- https://static.dxengineering.com/global/images/instructions/ico-ic-7300_nc.pdf
- https://www.qsl.net/k9gdt/radio/AGC.htm
- https://www.qsl.net/ab4oj/icom/ic7610/7610notes.pdf
- https://www.qsl.net/ab4oj/icom/ic756pro3/alc.html
- https://www.arrl.org/files/file/QEX_Next_Issue/Aug-Sep2018/Salas1.pdf
- https://blog.febo.com/?p=321
- https://klop.solutions/alc-and-the-ic-7300-about-talk-power-and-modifications/
- https://www.g4dbn.uk/?p=783
- https://pc5e.nl/downloads/ic7300/Reviews/7300notes.pdf
- https://www.qsl.net/ab4oj/test/docs/ft991notes.pdf
- https://www.remeeus.eu/hamradio/pa1hr/productreview.pdf
- https://www.qsl.net/4/4z4tl/pub/K3%20QST%20prod%20rev%201.pdf
- http://www.radiomanual.info/schemi/KENW_HF/TS-590S_review_QST_2011.pdf
- https://www.ieee802.org/16/tg1/phy/pres/802161pp-00_15.pdf
- https://arxiv.org/pdf/1510.01397
- https://www.mathworks.com/help/rf/ref/rf.amplifier-system-object.html
- https://oar.a-star.edu.sg/storage/y/yqd5zndyeq/peak-to-green.pdf
- http://wireless.nmsu.edu/hf/papers/WBHF.pdf
- https://apps.dtic.mil/sti/citations/ADA092114
- https://www.sigidwiki.com/wiki/STANAG_4539
