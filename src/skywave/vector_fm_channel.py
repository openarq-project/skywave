#!/usr/bin/env python3
"""FM channel stage for VectorAdapter campaigns: port profile + Tier-A flat
fade + AWGN over a sample vector, using its sidecar to locate frames.

This is the FM sibling of `vector_channel.py`. That module is Watterson+AWGN
and has no port chain; an FM cell needs the mic/speaker or discriminator-tap
audio path around the fade, because "the port profiles ARE the rate story"
(FM-MODE-RESEARCH 2a) -- micspk and data9600 are different channels, not one
knob.

Sample rate comes from the SIDECAR, never a constant, exactly as in
vector_channel.

CHAIN ORDER -- MATCHES THE LINK PATH, WHICH IS THE POINT
--------------------------------------------------------
`channel_sim.Link.deliver_block` runs, per block:

    tx audio -> rig_tx (FmPortTx) -> fade (FmFade) -> [delay] ->
                + noise * fade.noise_gain -> rig_rx (FmPortRx) -> squelch

This module inserts two further stages the link path resolves separately, in
their physical positions:

    ... FmPortTx -> [FmDeviationLimit] -> {measure S} -> [FreqShift] -> FmFade ...

The deviation limiter sits AFTER pre-emphasis, because that is where it sits in
a real transmitter and it matters: pre-emphasis raises high-frequency peaks, so
an emphasized path clips a given waveform harder than a flat one. S is measured
AFTER the limiter -- clipping changes the power actually presented to the
channel, and a pre-limiter S would label a clipped cell with the SNR of a
signal that was never transmitted.

This module reproduces that order. If it did not, a mode characterized on the
vector path and the same mode run on the link path would be two different
experiments wearing one name.

Two consequences are load-bearing and easy to get wrong:

1. **S is measured AFTER the TX port, before the fade.** The transmit port is
   not a cosmetic filter: micspk pre-emphasis tilts +6 dB/oct across the voice
   band and the 300-3000 Hz bandpass discards whatever sits outside it. Two
   modes with equal pre-port power but different spectra present *different*
   power to the channel, so an S measured pre-port would label them with the
   same SNR while they sat at different real ones -- the same mode-shape
   constant the Option-A ruling removed from the HF path
   (`vector_channel.clean_signal_power`). S is the mean square of the
   port-shaped signal over the sidecar's payload (or frame) regions, and the
   fade never enters it, so the labeled SNR does not depend on the fade draw.

2. **Noise is added BEFORE the RX port**, so de-emphasis shapes the noise as
   well as the signal -- as it does in a real receiver and in the link path.
   Adding it after would make micspk strictly worse than data9600 with no
   compensating mechanism, and would silently mis-rank the two ports.

   Tier-A caveat, stated because it will otherwise be mis-attributed later:
   the noise added here is WHITE, which is the IONOS-equivalent audio-domain
   convention this tier exists to match. A real FM discriminator delivers
   *triangular* (f^2) noise, and it is against triangular noise that pre/de-
   emphasis buys its classic SNR advantage. That advantage is therefore NOT
   modeled at Tier A. Do not read a micspk-vs-data9600 comparison from this
   stage as the final word on emphasis; that is Tier B's job.

DELAY ALIGNMENT
---------------
The micspk port chain is IIR (Butterworth bandpass, leaky-integrator
de-emphasis), so its output is delayed relative to its input. Left alone that
shifts every frame later than the sidecar's declared `frame_offsets`, while
data9600 (identity) has no such shift -- so a frame-synchronous receiver that
trusts the sidecar reads a smeared boundary on micspk and a clean one on
data9600, and the difference would be scored as port physics. This is the same
failure vector_channel fixed for the Hilbert group delay on 2026-08-25.

Fix: `port_delay()` measures the chain's peak-alignment delay empirically
(argmax of the impulse response magnitude -- operationally, the lag a
correlating receiver would find), the vector is zero-padded by that many
samples before filtering, and the first `d` output samples are dropped. Output
length is unchanged. This is a pure re-slice: only which output sample lands
at which absolute index changes.

Measured for the stock micspk chain at 32 kHz: 11 samples TX, 18 RX
(0.34 / 0.56 ms). Small in absolute terms and exactly why the first version of
the alignment test was VACUOUS -- its +/-50-sample tolerance was looser than
the bug. Small is not negligible: 29 samples is 7x DART's 4-sample cyclic
prefix. The test now measures the untrimmed chain in the same run and asserts
the trim at least halves the misalignment, so it cannot go quiet again.

argmax was chosen over two alternatives that were measured and rejected: an
energy centroid is dragged out to 45 samples by the de-emphasis integrator's
tail, and a broadband cross-correlation reads 9, because de-emphasis weights
the low end. Residual after the trim is +4 (TX) / +6 (RX) samples -- that is
the frequency-dependent dispersion across 300-3000 Hz, which is real
channel physics a receiver has to cope with and stays. Only the bulk offset
is an instrument artifact.

INDEPENDENT PER-FRAME FADING
----------------------------
Same requirement as the HF path: N frames must be N independent trials or a
binomial interval on FER is a lie, and the same trap applies -- a fresh
`FmFade` per frame renormalizes its own short realization to unit mean power
(`env /= sqrt(mean(env**2))`), scaling a deep fade back up and deleting the
event the sweep exists to measure. So: ONE realization, frames read at strided
fade-times, `fade.t` set per frame. `FmFade` keeps no filter history (its
tracks are interpolated by absolute sample index), so unlike WattersonChannel
there is nothing to zero.

The stride depends on which fade is running, and getting this wrong is silent:

  rayleigh / rice (stochastic, Doppler fD)
      stride = max(frame_span, 3/fD). For the Jakes spectrum a 3/fD
      separation leaves envelope correlation negligible -- the HF rule.

  ionos / ionosnc (DETERMINISTIC periodic, rate R)
      The HF rule is actively WRONG here. The track has period 1/R, so a
      stride of 3/R puts every frame at the SAME fade phase and the campaign
      measures one point on the fade cycle while reporting a fade average.
      Instead the frames STRATIFY one full cycle: stride = (1/R)/n, so frame i
      sits at phase i/n. Overlapping fade-time windows are fine and intended
      for a deterministic track -- there is no draw to correlate, and n frames
      become a stratified sample of the cycle, which is the low-variance way
      to get "FER over the fade" out of a finite frame budget.

  static
      No fast fade; only shadowing (if any) varies per frame.

  log-normal shadowing (sigma_db, tau_s)
      Coherence is tau_s, so independence needs stride >= 3*tau_s; the stride
      takes the max with whatever the fast fade asked for. Shadowing is NOT
      power-normalized -- a shadow fade-down is a real SNR loss, and that IS
      the axis (fm_channel docstring).

  ionos|ionosnc + shadowing is REFUSED, not silently approximated. The two
  want incompatible strides off one clock (stratify the cycle vs. decorrelate
  the shadow), and `FmFade` reads both tracks at the same `t`. Rather than
  produce a plausible-looking campaign that samples neither correctly, this
  raises -- the `validate_sidecar` doctrine applied to the channel.

SQUELCH
-------
Optional and off by default; micspk only. When on, the carrier is derived from
the sidecar's frame regions by default (`carrier="frames"`) rather than from a
block-RMS energy detect, because a real FM squelch responds to the KEYED
CARRIER -- which is up for the whole burst including the preamble the receiver
still has to find. That makes the attack window eat the head of each frame,
which is the measurement the acquisition-vs-squelch question wants. Energy
detect is available (`carrier="energy"`) for cells that want it.

`SquelchGate`'s `thresh`/`tail_amp` are in int16 units; this module takes them
in f32 full-scale and converts, because the vector path is f32.
"""
import argparse
import json
import os
import sys
import tempfile

import numpy as np

from skywave import fm_channel, fm_rig, rig_effects
from skywave.vector_adapter import load_sidecar, read_vector, write_vector
from skywave.vector_channel import (add_awgn, apply_headroom,
                                    clean_signal_power, frame_tail, sigma_for)

#: Fade-time separation between consecutive frames for STOCHASTIC fades, in
#: units of 1/doppler. Same rule and same reasoning as vector_channel.
STRIDE_COHERENCE_UNITS = 3.0
#: Shadowing decorrelation, in units of tau_s.
SHADOW_COHERENCE_UNITS = 3.0
#: Longest impulse response searched when measuring the port chain's delay.
DELAY_PROBE_LEN = 4096
#: int16 full scale -- SquelchGate speaks int16, this module speaks f32.
I16_FS = 32768.0
#: Squelch state-machine block, in MILLISECONDS. Its timing is
#: block-quantized, so the block must be set by TIME, not by sample count: a
#: fixed 1024 samples was 32 ms at DART's 32 kHz and 128 ms at the 8 kHz modem
#: rate, where `round(30 / 128) = 0` made a 30 ms carrier squelch a NO-OP on
#: the burst and the 150/250 ms classes realize as 0-128 / 128-256 ms
#: depending on which sample of the block the burst starts on (found by the
#: FM-CTRL pre-reg review, 2026-09-01; the 32 kHz tests never saw it).
#: 32 ms keeps every 32 kHz result bit-identical (1024 samples). The realized
#: attack is `wait_blocks * block - phase`, so at 32 ms a 30 ms request lands
#: anywhere in (0, 32] ms by burst phase: a cell that pre-registers an attack
#: time must pin a block SMALL against it (`--fm-squelch-block-ms 4`) and
#: read the `squelch_mute_stats` witness rather than the requested value.
SQUELCH_BLOCK_MS = 32.0
#: The 32 kHz block, kept for callers that named it.
SQUELCH_BLOCK = 1024


def squelch_block(fs, block_ms=SQUELCH_BLOCK_MS):
    """Samples per squelch block at `fs` for a `block_ms` block."""
    return max(16, int(round(fs * block_ms / 1000.0)))

PORTS = ("micspk", "data9600")
DETERMINISTIC_KINDS = ("ionos", "ionosnc")


# --------------------------------------------------------------------------
# port chain
# --------------------------------------------------------------------------

def port_delay(make_chain, fs, probe_len=DELAY_PROBE_LEN):
    """Peak-alignment delay of a port chain, in samples.

    `make_chain` builds a FRESH chain (its state must not be the one the
    caller will use). The delay is argmax|h| of the chain's impulse response:
    operationally the lag a correlating receiver finds, which is the alignment
    the sidecar's frame_offsets have to mean. 0 for an identity chain.
    """
    chain = make_chain()
    imp = np.zeros(probe_len, dtype=np.float64)
    imp[0] = 1.0
    h = np.asarray(chain.process(imp), dtype=np.float64)
    peak = float(np.max(np.abs(h)))
    if peak <= 0.0:
        return 0
    return int(np.argmax(np.abs(h)))


def _run_aligned(make_chain, vec, delay):
    """Run `vec` through a fresh chain and undo the chain's bulk delay.

    Zero-pads by `delay`, filters the whole thing, then drops the first
    `delay` output samples -- a pure re-slice, so the filter's own physics per
    sample are untouched and only absolute indexing changes. Output length
    equals input length.
    """
    x = np.asarray(vec, dtype=np.float64)
    if delay <= 0:
        return np.asarray(make_chain().process(x.copy()), dtype=np.float64)
    padded = np.concatenate([x, np.zeros(delay, dtype=np.float64)])
    y = np.asarray(make_chain().process(padded), dtype=np.float64)
    return y[delay:delay + x.size]


def apply_tx_port(vec, fs, port, order=6, ctcss_hz=0.0, ctcss_amp=0.0):
    """Transmit port shaping, delay-aligned to the sidecar. -> (out, delay)."""
    if port not in PORTS:
        raise SystemExit(f"vector_fm_channel: unknown port '{port}' "
                         f"(use {'|'.join(PORTS)})")

    def make():
        return fm_rig.FmPortTx(fs, port, order, ctcss_hz, ctcss_amp)

    # The CTCSS tone is an ADDED signal, not a filter, so it must not enter the
    # delay probe (an impulse response with a tone riding on it has its argmax
    # wherever the tone peaks). Measure the filter chain alone.
    d = port_delay(lambda: fm_rig.FmPortTx(fs, port, order), fs)
    return _run_aligned(make, vec, d), d


def apply_rx_port(vec, fs, port, order=6, deemph_corner_hz=75.0):
    """Receive port shaping, delay-aligned to the sidecar. -> (out, delay)."""
    if port not in PORTS:
        raise SystemExit(f"vector_fm_channel: unknown port '{port}' "
                         f"(use {'|'.join(PORTS)})")

    def make():
        return fm_rig.FmPortRx(fs, port, order, deemph_corner_hz)

    d = port_delay(make, fs)
    return _run_aligned(make, vec, d), d


def apply_deviation_limit(vec, side, fs, headroom_db, kind="hard"):
    """TX deviation limiting. -> (out, clipped_fraction, ceiling).

    The reference RMS comes from the sidecar's active regions, so inter-burst
    silence cannot dilute it and the headroom knob means the same thing
    whatever gap the encoder chose.
    """
    ref = float(np.sqrt(clean_signal_power(vec, side)))
    lim = fm_rig.FmDeviationLimit(fs, headroom_db, kind)
    ceiling = lim.set_reference(ref)
    frac = lim.clipped_fraction(np.asarray(vec, dtype=np.float64))
    return lim.process(np.asarray(vec, dtype=np.float64)), frac, ceiling


def apply_cfo(vec, fs, foff_hz, hilbert_taps=255):
    """Static carrier-frequency offset, delay-aligned to the sidecar.

    FreqShift is Hilbert-based and delays its output by the filter's group
    delay, the same trap the port chain has: uncorrected it would shift every
    frame off its declared offset, and only in the cells where CFO is on. Pad,
    filter, drop the first gdelay samples -- a pure re-slice.
    """
    if not foff_hz:
        return np.asarray(vec, dtype=np.float64).copy(), 0
    fx = rig_effects.FreqShift(fs, float(foff_hz), hilbert_taps=hilbert_taps)
    d = fx.gdelay
    x = np.asarray(vec, dtype=np.float64)
    padded = np.concatenate([x, np.zeros(d, dtype=np.float64)])
    y = np.asarray(fx.process(padded), dtype=np.float64)
    return y[d:d + x.size], d


def apply_codec(vec, fs, runner, tmpdir=None, max_lag=4096):
    """Round-trip the vector through a lossy audio CODEC. -> (out, lag).

    This is the Bluetooth hop, and it is the stage that separates a bench from
    the path DART actually runs on: every frame crosses SBC twice, app->radio
    and radio->app, and the two directions are not symmetric.

    `runner(in_path, out_path)` does the round trip; skywave stays codec-
    agnostic and the adapter supplies the backend (the DART adapter uses
    HTCommander's own Dart SBC, so the app-side encode is the real one rather
    than a lookalike). Any PCM-in/PCM-out codec works -- SBC, a vocoder, Opus.

    Why a codec is not just "more noise": a subband codec quantizes per
    subband, so its noise is SHAPED, and at 32 kHz with 8 subbands the band
    edges land at 2000 Hz intervals. A waveform whose carriers straddle a
    subband boundary gets a STEP in SNR through the middle of itself, which is
    exactly what DFT-spread precoding converts into a uniform per-symbol
    penalty. Modelling it as AWGN would miss the mechanism entirely.

    The lag is MEASURED by cross-correlation rather than taken from the codec's
    nominal figure: filterbank delay and the caller's framing both contribute,
    and a frame-synchronous receiver reading the sidecar needs the real one.
    Trimmed as a pure re-slice, as with the port chain.
    """
    import subprocess  # noqa: F401  (runner may shell out; kept explicit)
    d = tmpdir or tempfile.mkdtemp(prefix="skyw-codec-")
    ip = os.path.join(d, "codec_in.f32")
    op = os.path.join(d, "codec_out.f32")
    x = np.asarray(vec, dtype=np.float64)
    write_vector(ip, x)
    runner(ip, op)
    y = np.asarray(read_vector(op), dtype=np.float64)
    if y.size == 0:
        raise SystemExit("vector_fm_channel: codec produced an empty vector")
    n = min(x.size, y.size, max_lag * 8)
    xc = x[:n] - x[:n].mean()
    yc = y[:n] - y[:n].mean()
    c = np.correlate(yc, xc, mode="full")
    lo = max(0, (n - 1) - max_lag)
    hi = min(c.size, (n - 1) + max_lag + 1)
    lag = int(np.argmax(np.abs(c[lo:hi]))) + lo - (n - 1)
    out = np.zeros_like(x)
    if lag >= 0:
        m = min(x.size, y.size - lag)
        if m > 0:
            out[:m] = y[lag:lag + m]
    else:
        m = min(x.size + lag, y.size)
        if m > 0:
            out[-lag:-lag + m] = y[:m]
    return out, lag


# --------------------------------------------------------------------------
# fade
# --------------------------------------------------------------------------

def resolve_fade(fade_str, band="2m"):
    """-> None for 'off', else the fm_channel.resolve_fade_spec tuple."""
    if not fade_str or fade_str.strip().lower() == "off":
        return None
    return fm_channel.resolve_fade_spec(fade_str, band)


def fade_stride_s(kind, fd_hz, rate_hz, frame_span_s, n_frames,
                  shadow_sigma_db=0.0, shadow_tau_s=0.0):
    """Fade-time separation between consecutive frames, in seconds.

    See the module docstring: stochastic fades decorrelate, deterministic
    periodic fades STRATIFY the cycle, and shadowing raises the floor.
    """
    n = max(int(n_frames), 1)
    shadowing = shadow_sigma_db > 0.0
    if shadowing and kind in DETERMINISTIC_KINDS:
        raise SystemExit(
            "vector_fm_channel: refusing ionos/ionosnc combined with "
            "shadowing -- the periodic fade wants frames stratified across "
            "its cycle and the shadow process wants them 3*tau apart, and "
            "FmFade reads both tracks off one clock. Run them as separate "
            "cells (see the module docstring).")

    if kind in DETERMINISTIC_KINDS:
        if rate_hz <= 0.0:
            raise SystemExit(f"vector_fm_channel: {kind} needs rate_hz > 0")
        return (1.0 / rate_hz) / n              # stratify one full cycle

    stride = float(frame_span_s)
    if kind in ("rayleigh", "rice"):
        if fd_hz <= 0.0:
            raise SystemExit(f"vector_fm_channel: {kind} needs fD > 0")
        stride = max(stride, STRIDE_COHERENCE_UNITS / fd_hz)
    if shadowing:
        if shadow_tau_s <= 0.0:
            raise SystemExit("vector_fm_channel: shadowing needs tau_s > 0")
        stride = max(stride, SHADOW_COHERENCE_UNITS * shadow_tau_s)
    return stride


def apply_fm_fade(vec, side, spec, seed, shadow_sigma_db=0.0,
                  shadow_tau_s=0.0):
    """Fade each frame with its own slice of one realization.

    -> (faded float64 array, per-frame realized gain in dB, noise-gain array).

    The noise-gain array is all-ones except where an `ionosnc` fade raises the
    noise in a trough; the caller multiplies its AWGN by it, exactly as
    `Link._fill_noise` does per block.
    """
    out = np.asarray(vec, dtype=np.float64).copy()
    ngain = np.ones(out.size, dtype=np.float64)
    if spec is None and shadow_sigma_db <= 0.0:
        return out, [], ngain

    fs = int(side["sample_rate"])
    offsets = list(side["frame_offsets"])
    lengths = list(side["frame_lengths"])
    n = min(len(offsets), len(lengths))
    if n == 0:
        raise SystemExit("vector_fm_channel: sidecar named no frames")
    tail = frame_tail(fs, max(lengths) if lengths else 0)

    if spec is None:
        kind, fd, k_db, depth, rate, shape, sn0 = ("static", 0.0, 0.0,
                                                   0.0, 0.0, "sin", 0.0)
    else:
        kind, fd, k_db, depth, rate, shape, sn0 = spec[:7]

    frame_span_s = (max(lengths) + tail) / fs
    stride_s = fade_stride_s(kind, fd, rate, frame_span_s, n,
                             shadow_sigma_db, shadow_tau_s)
    # +2 strides of headroom plus one frame span so the interpolation grid of a
    # stochastic track never wraps; a wrap replays the realization and silently
    # re-correlates the draws.
    dur_s = stride_s * (n + 2) + frame_span_s

    fade = fm_channel.FmFade(
        fs, kind, dur_s, seed, fd_hz=fd, k_db=k_db,
        ionos_depth_db=depth, ionos_rate_hz=rate,
        shadow_sigma_db=shadow_sigma_db, shadow_tau_s=shadow_tau_s,
        ionos_shape=shape, ionos_sn0_db=sn0)

    stride_samples = int(round(stride_s * fs))
    gains_db = []
    for i in range(n):
        a = offsets[i]
        b = min(a + lengths[i] + tail, out.size)
        if b <= a:
            gains_db.append(-99.0)
            continue
        block = np.asarray(vec[a:b], dtype=np.float64)
        fade.t = i * stride_samples           # FmFade holds no filter history
        faded = fade.process(block)
        out[a:b] = faded
        if fade.noise_gain is not None:
            ngain[a:b] = fade.noise_gain
        clean = np.asarray(vec[a:a + lengths[i]], dtype=np.float64)
        cp = float(np.dot(clean, clean))
        fseg = faded[:lengths[i]]
        fp = float(np.dot(fseg, fseg))
        gains_db.append(10.0 * np.log10(fp / cp) if cp > 0 and fp > 0 else -99.0)
    return out, gains_db, ngain


def add_awgn_shaped(vec, sigma, seed, noise_gain=None):
    """AWGN with an optional per-sample gain track (the ionosnc noise rise)."""
    if noise_gain is None:
        return add_awgn(vec, sigma, seed)
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(vec.size) * sigma
    return vec + noise * noise_gain


# --------------------------------------------------------------------------
# squelch
# --------------------------------------------------------------------------

def carrier_from_frames(side, total, block):
    """Per-block carrier flags from the sidecar's frame regions.

    A keyed FM carrier is up for the whole burst regardless of audio content,
    so the flag is True for any block overlapping a frame region. This is what
    makes the squelch attack window land on the frame HEAD, which is the point.
    """
    nblocks = (total + block - 1) // block
    flags = np.zeros(nblocks, dtype=bool)
    for off, ln in zip(side["frame_offsets"], side["frame_lengths"]):
        lo = max(0, off // block)
        hi = min(nblocks, (off + ln + block - 1) // block)
        flags[lo:hi] = True
    return flags


def apply_squelch(vec, side, fs, open_ms=30.0, tone_ms=0.0, tail_ms=0.0,
                  tail_amp=2000.0 / I16_FS, thresh=800.0 / I16_FS,
                  seed=0, carrier="frames", block=None,
                  block_ms=SQUELCH_BLOCK_MS):
    """Gated squelch over the vector. `thresh`/`tail_amp` are f32 full-scale.
    `block` (samples) overrides `block_ms`; the default is time-based so the
    attack quantization is the same at every sample rate."""
    if block is None:
        block = squelch_block(fs, block_ms)
    gate = fm_rig.SquelchGate(fs, block, open_ms, tone_ms, tail_ms,
                              tail_amp=tail_amp * I16_FS,
                              thresh=thresh * I16_FS, seed=seed)
    flags = (carrier_from_frames(side, len(vec), block)
             if carrier == "frames" else None)
    out = np.empty_like(vec)
    for bi, lo in enumerate(range(0, len(vec), block)):
        hi = min(lo + block, len(vec))
        chunk = np.asarray(vec[lo:hi], dtype=np.float64)
        if flags is None:
            # energy detect: SquelchGate compares RMS in int16 units
            g = gate.process(chunk * I16_FS) / I16_FS
        else:
            g = gate.process(chunk, bool(flags[bi]))
        out[lo:hi] = g
    return out


def squelch_mute_stats(before, after, side, fs):
    """Engagement witness for a squelch arm: how much of each burst's HEAD the
    gate actually muted, measured on the frame regions (never the gaps, which
    a carrier-derived squelch mutes by construction). A squelch row whose mean
    muted head is 0 ms did not exercise the squelch and is VOID, not a datum
    -- the quantization no-op above produced exactly such rows while the
    provenance columns said "squelch on, 30 ms".

    Returns {muted_frames, mean_mute_ms, min_mute_ms, max_mute_ms}: the count
    of frames with any muted head, and the leading muted run per frame in ms.
    """
    before = np.asarray(before); after = np.asarray(after)
    runs = []
    for off, ln in zip(side["frame_offsets"], side["frame_lengths"]):
        b = before[off:off + ln]; a = after[off:off + ln]
        muted = (a == 0) & (b != 0)
        # leading run only: the attack window sits at the head
        run = int(np.argmin(muted)) if not muted.all() else int(muted.size)
        if muted.size and not muted[0]:
            run = 0
        runs.append(1000.0 * run / fs)
    if not runs:
        return {"muted_frames": 0, "mean_mute_ms": 0.0,
                "min_mute_ms": 0.0, "max_mute_ms": 0.0}
    return {"muted_frames": int(sum(1 for r in runs if r > 0)),
            "mean_mute_ms": float(np.mean(runs)),
            "min_mute_ms": float(min(runs)), "max_mute_ms": float(max(runs))}


# --------------------------------------------------------------------------
# full stage
# --------------------------------------------------------------------------

def apply(vector_path, sidecar_path, out_path, port="micspk", fade="off",
          band="2m", snr_db=0.0, bw_hz=2500.0, seed=1, order=6,
          ctcss_hz=0.0, ctcss_amp=0.0, shadow_sigma_db=0.0, shadow_tau_s=0.0,
          squelch=False, squelch_open_ms=30.0, squelch_tone_ms=0.0,
          squelch_carrier="frames", headroom_db=None, limit_kind="hard",
          foff_hz=0.0, codec_tx=None, codec_rx=None, report_path=None):
    """TX port -> measure S -> fade -> AWGN -> RX port -> squelch -> headroom.

    -> info dict. Chain order matches `channel_sim.Link.deliver_block`; see the
    module docstring for why each seam sits where it does.
    """
    side = load_sidecar(sidecar_path) if isinstance(sidecar_path, str) \
        else sidecar_path
    vec = read_vector(vector_path)
    if vec.size == 0:
        raise SystemExit(f"vector_fm_channel: {vector_path} is empty")
    fs = int(side["sample_rate"])
    spec = resolve_fade(fade, band)

    # app -> radio codec hop, BEFORE the radio's own audio shaping
    lag_tx = lag_rx = 0
    if codec_tx is not None:
        vec, lag_tx = apply_codec(vec, fs, codec_tx)
    tx, d_tx = apply_tx_port(vec, fs, port, order, ctcss_hz, ctcss_amp)
    clip_frac, ceiling = 0.0, None
    if headroom_db is not None:
        tx, clip_frac, ceiling = apply_deviation_limit(
            tx, side, fs, headroom_db, limit_kind)
    S = clean_signal_power(tx, side)      # post-port, post-limiter, pre-fade
    sigma = sigma_for(S, fs, bw_hz, snr_db)
    tx, d_cfo = apply_cfo(tx, fs, foff_hz)
    faded, gains_db, ngain = apply_fm_fade(tx, side, spec, seed,
                                           shadow_sigma_db, shadow_tau_s)
    noisy = add_awgn_shaped(faded, sigma, seed + 1,
                            ngain if spec and spec[0] == "ionosnc" else None)
    rx, d_rx = apply_rx_port(noisy, fs, port, order)
    if squelch:
        if port != "micspk":
            raise SystemExit("vector_fm_channel: squelch is a micspk stage "
                             "(data9600 is the squelchless discriminator tap)")
        rx = apply_squelch(rx, side, fs, squelch_open_ms, squelch_tone_ms,
                           seed=seed, carrier=squelch_carrier)
    # radio -> app codec hop, LAST: the radio encodes what it demodulated.
    if codec_rx is not None:
        rx, lag_rx = apply_codec(rx, fs, codec_rx)
    write_vector(out_path, apply_headroom(rx))

    info = {
        "stage": "fm",
        "port": port,
        "fade": fade,
        "fade_kind": spec[0] if spec else None,
        "fade_desc": spec[7] if spec else None,
        "band": band if spec else None,
        "doppler_hz": spec[1] if spec else None,
        "shadow_sigma_db": shadow_sigma_db,
        "shadow_tau_s": shadow_tau_s,
        "snr_db": snr_db, "bw_hz": bw_hz, "sample_rate": fs,
        "signal_power": S, "sigma": sigma,
        "tx_port_delay": d_tx, "rx_port_delay": d_rx, "cfo_delay": d_cfo,
        "headroom_db": headroom_db, "limit_kind": limit_kind if headroom_db is not None else None,
        "clipped_fraction": clip_frac, "clip_ceiling": ceiling,
        "foff_hz": foff_hz,
        "codec_tx": codec_tx is not None, "codec_rx": codec_rx is not None,
        "codec_lag_tx": lag_tx, "codec_lag_rx": lag_rx,
        "ctcss_hz": ctcss_hz,
        "squelch": bool(squelch),
        "fade_seed": seed, "noise_seed": seed + 1,
        "stride_coherence_units": STRIDE_COHERENCE_UNITS,
        "frame_gain_db": gains_db,
    }
    if report_path:
        with open(report_path, "w") as f:
            json.dump(info, f, indent=2)
    return info


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--sidecar", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--port", default="micspk", choices=list(PORTS))
    ap.add_argument("--fade", default="off",
                    help="off | fixed | pedestrian | mobile-urban | "
                         "mobile-highway | ionos:<depth>:<rate>[:sin|sq] | "
                         "ionosnc:<depth>:<rate>:<sn0> | rayleigh:<fD> | "
                         "rice:<fD>[:<K_dB>] | static")
    ap.add_argument("--band", default="2m", choices=list(fm_channel.BANDS))
    ap.add_argument("--snr", type=float, required=True)
    ap.add_argument("--bw", type=float, default=2500.0,
                    help="noise bandwidth for the SNR convention; matches "
                         "vector_channel's default so HF and FM cells mean "
                         "the same thing. micspk's occupied band is "
                         "300-3000 Hz -- pass --bw 2700 for noise-in-voice-band.")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--order", type=int, default=6)
    ap.add_argument("--ctcss-hz", type=float, default=0.0)
    ap.add_argument("--ctcss-amp", type=float, default=0.0)
    ap.add_argument("--shadow-sigma-db", type=float, default=0.0)
    ap.add_argument("--shadow-tau-s", type=float, default=0.0)
    ap.add_argument("--squelch", action="store_true")
    ap.add_argument("--squelch-open-ms", type=float, default=30.0)
    ap.add_argument("--squelch-tone-ms", type=float, default=0.0)
    ap.add_argument("--squelch-carrier", default="frames",
                    choices=("frames", "energy"))
    ap.add_argument("--headroom-db", type=float, default=None,
                    help="TX deviation ceiling above the signal RMS; below the "
                         "mode's PAPR it clips. Omit for no limiting.")
    ap.add_argument("--limit-kind", default="hard", choices=("hard", "soft"))
    ap.add_argument("--foff-hz", type=float, default=0.0,
                    help="static carrier-frequency offset")
    ap.add_argument("--report", default="")
    a = ap.parse_args()

    info = apply(a.inp, a.sidecar, a.out, a.port, a.fade, a.band, a.snr, a.bw,
                 a.seed, a.order, a.ctcss_hz, a.ctcss_amp, a.shadow_sigma_db,
                 a.shadow_tau_s, a.squelch, a.squelch_open_ms,
                 a.squelch_tone_ms, a.squelch_carrier, a.headroom_db,
                 a.limit_kind, a.foff_hz, a.report or None)
    g = info["frame_gain_db"]
    msg = (f"vector_fm_channel: port={info['port']} "
           f"fade={info['fade_desc'] or 'off'}"
           + (f" shadow={info['shadow_sigma_db']:g}dB/{info['shadow_tau_s']:g}s"
              if info["shadow_sigma_db"] > 0 else "")
           + f" snr={a.snr:.2f} dB / {a.bw:.0f} Hz @ {info['sample_rate']} Hz"
             f"  S={info['signal_power']:.3e} sigma={info['sigma']:.3e}"
             f"  delay tx/rx={info['tx_port_delay']}/{info['rx_port_delay']}")
    if g:
        arr = np.array(g)
        msg += (f"  draws: {arr.size}, median {np.median(arr):+.1f} dB, "
                f"min {arr.min():+.1f}, deep(<-10dB) {int((arr < -10).sum())}")
    print(msg + f" -> {a.out}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
