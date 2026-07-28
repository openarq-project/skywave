"""Harness generation stamp.

RIG_GEN versions the measurement harness (the channel model, transport, and drivers), so
every result row can record the harness generation it ran on. Bump it on any change to
channel semantics, keying/PTT relay behavior, scoring, or calibration, so a corpus can be
tied to the harness that produced it.

Gen 8 (2026-07-28): default Doppler-shaping filter flipped codec2 -> milstd
(MIL-STD-188-110C App E time-domain Gaussian taps; realized 2-sigma spread ==
nominal within 0.5% at 0.1-30 Hz, full-dwell-qualified on all named presets).
The gen<=7 realization is frozen as SIM_FADE_FILTER=codec2-2016 — exact byte
reproduction of a pre-gen-8 fading cell additionally needs SIM_FADE_DUR_S=1200
(the old default; the dur feeds the sequential noise-draw layout and the
realization-level hf_gain, verified hash-identical vs gen-7 with both set).
Its measured
realized/nominal spread was SPREAD-DEPENDENT: 6.03x @0.1 Hz, 1.33x @0.5,
0.91x @1, 0.79x @>=2 Hz — so gen<=7 "good" cells faded ~6x faster than
labeled and fast cells ~21% slower. Fading rows are NOT comparable across
this boundary; filter corpora on rig_gen (the fade_filter banner tag names
the convention per cell). Also in gen 8: SIM_FADE_DUR_S default 1200->3600 s
(only previously-WRAPPED tails change), SIM_COMPLIANCE reference-channel
switch, snr2k7 column, Otnes/flat presets.
"""
RIG_GEN = 8
