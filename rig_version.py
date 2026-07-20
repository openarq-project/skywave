"""Harness generation stamp.

RIG_GEN versions the measurement harness (the channel model, transport, and drivers), so
every result row can record the harness generation it ran on. Bump it on any change to
channel semantics, keying/PTT relay behavior, scoring, or calibration, so a corpus can be
tied to the harness that produced it.
"""
RIG_GEN = 7
