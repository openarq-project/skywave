# HF channel conditions and preset methodology

This is the literature basis for skywave's fading presets: a survey of primary-source HF channel measurements, model-validity critiques, and channel-simulator test methodology.

## Channel conditions beyond the standard presets

- High-latitude/auroral (DAMSON campaign, 4 Scandinavian auroral paths): at 5%-time-exceedance, Doppler spread 2–55 Hz, multipath 1–11 ms, roughly an order of magnitude beyond F.1487 mid-latitude "poor" (1.0 Hz / 2 ms). HIGH confidence on the measured ranges; framed as measured design bounds, not a stated modem-tolerance requirement.
- Model-validity critiques: CCIR Report 549-2 reportedly states the Gaussian-scatter model "almost certainly is not valid for all HF channels" (MEDIUM, secondary source). DSTO (NATO STO MP-IST-056-17) found the fixed-Gaussian/independent-taps assumptions don't fully hold for Australian mid-latitude conditions, and noted the scarcity of mid/low-latitude spread measurements (HIGH that the critique exists).
- Path-class measurements: Catalonia NVIS 97 km (mean delay ~0.3 ms, max ~2.7–2.9 ms, Doppler ≤4 Hz including instrument drift, milder than F.1487 "good"); WHISPER ~170 km (4 ms/±0.5 Hz at 3.9 MHz; 10 ms/±5 Hz at 5.7 MHz; 6 ms/±2 Hz at 6.7 MHz); trans-equatorial 12 760 km (±3.5 ms/±2.5 Hz). No competing preset table published, no sporadic-E sounding dataset found, and no modern SDR re-measurement tabulating against classic presets: these are genuine literature gaps.
- Carrier drift (shift, not spread): about 0.1 Hz steady in daytime, about 1–2 Hz through sunrise/sunset transitions over 10–80 min disturbance periods (two independent studies agree on the magnitude, HIGH on magnitude). No published Hz/min rate and no published shift-ramp test protocol were found; RapidM RS10 ships "time-varying Doppler offset" as a feature.
- Time-varying channels: F.1487 is static per run. No published scripted good-to-poor-to-good methodology exists anywhere (three search angles tried). The closest analogues are a log-normal SNR variation model (σ≈4 dB, ~10 s autocorrelation, IEEE 5235576) and a measured-SNR-time-series framework for adaptive systems (ResearchGate 307513557). A scripted preset-schedule test would be building ahead of the literature.

## ARQ/system-level methodology

- STANAG 5066 throughput convention (Isode whitepaper): report bps and percent of waveform capacity (e.g., 8000 bps = 83% at a 9600 bps waveform); sample in steady state after queue fill; span multiple ARQ cycles (the ~127.5 s max-transmit/flow-control cycle makes instantaneous throughput a roughly 2-minute sawtooth). No published connect-time or message-completion statistic was found for 5066.
- MIL-STD-188-141B ALE conformance: Watterson Good (0.5 ms) / Poor (2.0 ms) presets; a probability-of-linking pass table (Table A-II) over multiple trials; link-setup time formally defined as calling-transmission start to link established. The methodology pattern is portable; the numeric thresholds are not.
- Amateur comparisons: the only substantive one is Winlink's own IONOS-SIM study (developer-authored: PACTOR 2/3/4, WINMOR, ARDOP, VARA; net bytes/min after retries; PACTOR 4 leads at realistic SNR, VARA wins only at roughly 20–30 dB and above; the authors' own caveat: "No simulator can create all the band conditions... interference... aurora"). No independent peer-reviewed comparison exists. Counter-anecdotes exist (a single-trial jamming incident, GhostNet #14). Supportable criticisms: developer conflict-of-interest, non-reproducibility, closed-source verification limits.
- Contended-channel testing: not found anywhere in HF ARQ/ALE literature (two independent passes); CSMA/hidden-terminal methodology exists only in 802.11 literature. Confirmed gap.

## Simulator engineering practices

- F.1487 specifies no implementation-verification procedure (HIGH, primary read). Watterson/Juroshek/Bensema 1970 validates the model against nature, not an implementation against the model.
- Reusable implementation-verification template (MathWorks Watterson doc, verified firsthand): estimate the empirical Doppler spectrum of simulated tap gains via Welch's method and overlay the theoretical bi-Gaussian F.1487 spectrum, plus explicit RNG seeding (mt19937ar + seed) for reproducible realizations.
- Cross-implementation variance is a named problem (directionally credible via NTIA/ITS Johnson HF-simulator literature; LOW-MEDIUM at the primary-source level).
- IONOS/HFSimulator self-test is I/O-level calibration only; no statistical fading verification was found in any open simulator checked.
- Seeded-fading paired comparison has real precedent as an engineering practice, but is not elevated to a formal named methodology anywhere in the literature found. A paired-seed doctrine for fading comparisons is ahead of published practice and should be documented as such.

## Candidate features

These are the features evaluated during skywave's design. Several of the strongest have since been implemented (the high-latitude/auroral preset, scripted preset schedules, and the Watterson self-verification harness — see the channel-model doc); the table records the original evaluation.

| Feature | Justification strength | Complexity |
|---|---|---|
| High-lat/auroral preset (DAMSON 5%-exceedance) | STRONG (measured, outside current coverage) | LOW |
| Benign short-NVIS preset | MEDIUM (marginal vs existing good) | LOW |
| Slow carrier-shift ramp (~1–2 Hz over minutes) | MEDIUM (magnitude cited; protocol not sourced from literature) | LOW |
| Scripted preset schedules (adaptive-modem test) | STRONG engineering / no literature precedent | MEDIUM |
| Watterson self-verification harness | STRONG (template exists; variance documented) | MEDIUM |
| Steady-state sampling convention | MEDIUM (generalizes to any windowed ARQ) | LOW |
| Formal link-setup-time metric | MEDIUM (141B definition) | LOW |
| Multi-station contended channel | STRONG gap / ZERO precedent | HIGH (own subsystem) |

Not worth adding: a full ITU latitude x condition matrix (no clear use case); a sporadic-E preset (no source data, fabrication would violate a measured-and-pinned discipline); military numeric pass/fail thresholds (cargo-culting); CSMA adaptation framed as a "channel knob" (misrepresents the lift); replicating the Winlink study's tiers/numbers as ground truth (non-independent); hardcoding a derived Hz/min drift rate (uncited arithmetic).

## Sources

- ITU-R F.1487: https://www.itu.int/dms_pubrec/itu-r/rec/f/R-REC-F.1487-0-200005-I!!PDF-E.pdf
- DAMSON high-lat study: https://research.birmingham.ac.uk/en/publications/measurements-of-doppler-and-multipath-spread-on-oblique-high-lati/
- NATO STO DSTO critique: https://publications.sto.nato.int/publications/STO%20Meeting%20Proceedings/RTO-MP-IST-056/MP-IST-056-17.pdf
- Catalonia NVIS sounding: https://pmc.ncbi.nlm.nih.gov/articles/PMC8004164/
- Ionospheric sounding review: https://www.mdpi.com/1424-8220/20/9/2486
- Diurnal Doppler shift: https://www.sciencedirect.com/science/article/abs/pii/S0273117707006990
- Frontiers 2026 HF Doppler variability: https://www.frontiersin.org/journals/astronomy-and-space-sciences/articles/10.3389/fspas.2026.1713968/full
- RapidM RS10 datasheet: https://www.cyntony.com/hubfs/533635/RS10_HFM_EN_stamped.pdf
- Intermediate-duration variation model: https://ieeexplore.ieee.org/document/5235576
- Empirical channel-quality variation: https://www.researchgate.net/publication/307513557
- Isode 5066 performance whitepaper: https://www.isode.com/whitepaper/stanag-5066-performance-measurements-over-hf-radio/
- MIL-STD-188-141B conformance: http://www.n2ckh.com/MARS_ALE_FORUM/MIL-STD-188-141B%20Conformance%20Test%20Procedures.pdf
- Winlink modem comparison (2020): https://winlink.org/sites/default/files/downloads/a_winlink_digital_mode_performance_comparison_based_on_the_ionis_sim_hf_vhf_channel_simulator_-_november_2_2020_0.pdf
- GhostNet #14 anecdote: https://github.com/s2underground/GhostNet/issues/14
- Watterson 1970: https://ieeexplore.ieee.org/document/1090438/
- MathWorks Watterson verification template: https://www.mathworks.com/help/comm/ug/hf-ionospheric-channel-models.html
- NTIA/ITS HF simulator (Johnson): https://its.ntia.gov/umbraco/surface/download/publication?reportNumber=HF+Simulator.pdf
- IONOS simulator manual: https://winlink.org/sites/default/files/downloads/ionos_simulator_manual_22may20.pdf
