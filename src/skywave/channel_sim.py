#!/usr/bin/env python3
"""Shared half-duplex HF channel simulator.

ONE process replaces the two `arecord | noise_pipe_gain.py | aplay` relays. It owns
all four snd-aloop devices, reading both stations' TX captures and writing both RX
playbacks, and applies the on-air transform in between:

    A_TX (cap plughw:2,0) --\\                 /-> B_RX (play plughw:5,1)
                             >--[ channel ]--<
    B_TX (cap plughw:4,1) --/                 \\-> A_RX (play plughw:3,0)

Per direction the channel is:  level(gain->PEP clip) -> [fade] -> [delay] -> +AWGN.
Half-duplex keying/collision, link delay and Watterson fade are added in later phases;
PHASE 0 is a behavior-equivalent passthrough of the current dual-pipe AWGN rig
(B_RX = clip(gain*A_TX)+noise, A_RX = clip(gain*B_TX)+noise), so it is a pure regression.

The two directions run on INDEPENDENT THREADS — faithfully mirroring the original two
separate pipes. This matters: a half-duplex modem stops driving its TX output stream when
it switches to receive, which stalls that capture's blocking read; coupling the two reads
in one loop would let an idle capture freeze the *other* direction and deadlock the ARQ
handshake (observed: Mercury hung at PENDING). Independent threads keep each direction
flowing on its own clock, exactly as the two pipes did.

Env:
  TXGAIN   transmitter drive (sets the drive level); applied before the PEP clip [1.0]
  SIGMA    fixed channel noise std (int16 LSBs), added after the peak clip          [0.0]
  SEED     base RNG seed; per-direction seeds = SEED+11 (A->B), SEED+22 (B->A)      [1234]
  NP_STATS if set, write per-direction signal stats JSON to <path>.11 / <path>.22
  SIM_BLOCK    pipe block size in frames/channel                                    [1024]
  SIM_FS       cable sample rate in Hz. 48000 (default) is the validated soundcard
               rate; 8000 runs the cable at a modem's native rate for device-free
               sock transports (e.g. mercury -x sock, 8 kHz mono, no resampler).
               All channel physics (rig BPF, Watterson, rig effects, noise BW,
               link delay) re-derive from FS.                                      [48000]
  SIM_VERBOSE  if "1", print per-direction block-time health (p99/worst) on exit

The block loop is allocation-free in the core path incl. link delay + QRM (preallocated numpy buffers, rng.standard_normal(out=)); the optional fx AGC/skew/BPF process() internals still allocate per block (SIM_VERBOSE p99/worst block-time is the canary)
so GC/jitter can't starve the ALSA callbacks during long fading soaks. The real-time-
critical ALSA I/O stays in arecord/aplay (C); Python only keeps the pipes drained.

TRANSPORT:
  SIM_TRANSPORT=alsa (default)  the four arecord/aplay aloop endpoints above.
  SIM_TRANSPORT=sock            framed unix-domain sockets (sock_frames.py), one per
    station at <SIM_SOCK_DIR>/a.sock + b.sock. The channel transform, keying, stats
    and the two independent direction threads are IDENTICAL — only the block I/O
    changes. Station frames may carry PTT in-band (block-exact edges); a frame with
    ptt=255 leaves keying to the stdin PTT relay, unchanged. Wall clock is retained
    in stage 1: pacing comes from whatever paces the stations (the sock<->ALSA shim
    at real time, later the lockstep virtual clock of stage 2).
  SIM_SOCK_DIR     socket directory                       [/tmp/simsock-<pid>]
  SIM_SOCK_SHIM=1  spawn sock_alsa_shim.py for both stations (the stage-1
                   regression topology: real binaries on the aloop cable, sim on
                   sockets; harnesses need no change beyond the two SIM_* keys)
  SIM_SOCK_ACCEPT_S  accept timeout waiting for both stations                [30]
  SIM_SOCK_BUF     SO_SNDBUF/SO_RCVBUF per socket, bytes — sized to mirror the
                   old sim->aplay pipe capacity, keeping transport buffering (and
                   thus turnaround latency) comparable                    [65536]
"""
import os
import sys
import json
import math
import time
import signal
import socket
import threading
import subprocess as sp
import gc
import numpy as np

from skywave import sock_frames
from skywave import _platform

try:
    from skywave.rig_version import RIG_GEN
except ImportError:  # run outside the skywave source dir
    RIG_GEN = -1

# FS is read below, AFTER the profiles apply (so a channel/transport profile can
# set SIM_FS like any other SIM_* knob).
# SIM_PROFILE=<file.toml|.json> pre-populates the SIM_* channel knobs
# from a declarative channel_profile BEFORE the reads below. setdefault semantics: any
# SIM_* already in the environment (campaign/operator) OVERRIDES the profile (explicit env wins).
from skywave import channel_profile as _channel_profile
_PROFILE_NAME = _channel_profile.apply_to_environ()
# SIM_TRANSPORT_PROFILE=<file.toml|.json> pre-populates the transport
# knobs (SIM_TRANSPORT/SIM_SOCK_*/SIM_CLOCK) from a declarative transport_profile BEFORE
# the TRANSPORT reads below. Orthogonal to the channel profile above; same setdefault
# semantics (explicit env wins). Portable, aloop-free selection in one shareable file.
from skywave import transport_profile as _transport_profile
_TRANSPORT_PROFILE_NAME = _transport_profile.apply_to_environ()
# Cable sample rate. 48 kHz is the validated soundcard default; SIM_FS=8000 runs
# the cable at a modem's native rate for device-free sock transports (see the
# module docstring). Everything downstream (rig BPF, Watterson, rig effects,
# noise, link delay, virtual time) derives from FS.
FS = int(os.environ.get("SIM_FS", "48000").strip() or "48000")
# Cable channel count. Default 2 (the validated setup for VARA/Mercury/ARDOP,
# which open via plughw and adapt). FreeDATA opens the RAW snd-aloop hw device at mono and
# cannot adapt, so its harness sets SIM_NCH=1 (a mono cable matches FreeDATA's native mono
# open). channel_sim treats samples as a flat array, so it is channel-count agnostic.
NCH = int(os.environ.get("SIM_NCH", "2").strip() or "2")
GAIN = float(os.environ.get("TXGAIN", "1.0").strip() or "1.0")
SIGMA = float(os.environ.get("SIGMA", "0.0").strip() or "0.0")
SEED = int(os.environ.get("SEED", "1234").strip() or "1234")
STATS = os.environ.get("NP_STATS", "").strip()
# SIM_TXDUMP: if set, dump each direction's post-gain, pre-noise TX stream (the exact bytes the
# act_rms/PAPR stats are computed over) as raw int16 to <SIM_TXDUMP>.<src_name> for offline PAPR
# decomposition (the 13.3 dB-source investigation). Interleaved at SIM_NCH channels.
TXDUMP = os.environ.get("SIM_TXDUMP", "").strip()
# Blocks to skip before tracking robust_peak (the fair PEP), excluding the snd-aloop cold-start
# transient. Default 8 (~170 ms at 21 ms/block) — well before any real burst, well after the glitch.
STATS_SKIP_BLOCKS = int(os.environ.get("SIM_STATS_SKIP_BLOCKS", "8").strip() or "8")
# SIM_PA_P: Rapp soft-PA smoothness. 0 (default) = the historical HARD clip at PEP (bit-exact baseline).
# >0 models a real power amp's AM/AM compression: y = x / (1+(|x|/Vsat)^2p)^(1/2p), so over-driving a
# HIGH-PAPR waveform splatters/distorts sooner than a low-PAPR one (the realism the hard clip misses).
# Drive it via TXGAIN (the drive calibration sets the operating point). SIM_PA_VSAT = PA saturation (LSBs).
# NOTE: real-sample model (adequate for the goodput decode); the spectral/ACPR drive-calibration uses
# the accurate complex-envelope model in `phy::occupied_bandwidth::pa_limited_operating_point` (which
# finds each waveform's max average power within the 2.8 kHz mask). p≈2-3 ≈ a typical SSB PA.
PA_P = float(os.environ.get("SIM_PA_P", "0").strip() or "0")
PA_VSAT = float(os.environ.get("SIM_PA_VSAT", "32767").strip() or "32767")
# RX pad: fixed gain applied to signal+noise
# TOGETHER immediately before the int16 cast. Models the real practice of
# setting RX audio output with headroom below ADC full scale; SNR-invariant by
# construction (applied after noise injection). Default −12 dB clears the
# measured worst-case Watterson fade-up (+10 dB over mean in 10 min, all
# presets) with 2 dB margin. SIM_RX_PAD_DB=0 restores the legacy levels.
RX_PAD_DB = float(os.environ.get("SIM_RX_PAD_DB", "-12").strip() or "-12")
RX_PAD = 10.0 ** (RX_PAD_DB / 20.0)
# Active clipping detector (mirrors codec2 `ch`'s "WARNING output
# clipping" at >0.1%). rail_frac is the passive counter; this makes it
# self-announcing so a level regression can't slip past an operator who forgot
# to grep the stats. A nonzero rail_frac on a no-AGC cell means the RX pad is
# not clearing the fade-ups — an instrument level bug (STOP). On an AGC cell
# burst-head clipping is expected, so the warning is advisory there.
RAIL_WARN_FRAC = float(os.environ.get("SIM_RAIL_WARN_FRAC", "0.001").strip()
                       or "0.001")
BLOCK = int(os.environ.get("SIM_BLOCK", "1024").strip() or "1024")  # frames per channel
VERBOSE = os.environ.get("SIM_VERBOSE", "0").strip() == "1"
# VOX keying + half-duplex gate (default OFF => full-duplex passthrough)
HALF_DUPLEX = os.environ.get("SIM_HALF_DUPLEX", "0").strip() == "1"
KEY_THRESH = float(os.environ.get("SIM_KEY_THRESH", "800").strip() or "800")   # TX RMS to key
# Hangtime holds the key through intra-burst gaps but MUST stay under the modem TX->RX
# turnaround: too long (e.g. 400 ms) keeps a just-finished station "keyed" into the peer's
# next burst -> false collision -> zero goodput (observed). 60-100 ms works for these
# continuous-burst OFDM modems; this is a known-risky VOX tuning parameter.
HANG_MS = float(os.environ.get("SIM_HANG_MS", "80").strip() or "80")           # key hold-over
KEYLOG = os.environ.get("SIM_KEYLOG", "").strip()   # if set, log VOX key transitions to <path>.a/.b
# SIM_PTT: gate on REAL PTT (each modem's host PTT ON/OFF, relayed by the harness onto our
# stdin as "a 1"/"a 0"/"b 1"/"b 0") instead of audio-energy VOX — exact key edges, no
# leading-edge clip. The hangtime tail still applies (covers the audio-pipeline latency on
# key-off). Half-duplex must be on for keying to matter.
SIM_PTT = os.environ.get("SIM_PTT", "0").strip() == "1"
# Watterson multipath fade (ITU-R F.1487 Gaussian-scatter, 2-path Rayleigh; see
# watterson.py). SIM_WATTERSON names a channel (good/moderate/poor/nvis/nvis-max/disturbed/
# nvis-disturbed/high-lat) or "off" (default => no fade). SIM_FADE_DOPPLER_HZ +
# SIM_FADE_DELAY_MS override the named preset. Fading is INDEPENDENT per direction and
# power-normalized, so the AWGN SNR3k axis is unchanged on average. SIM_FADE_DUR_S sizes the
# pre-generated gain sequence (cycled if a run is longer). NOTE (F.1487 Annex 3 §6): a fading
# run must last >= 3000/Doppler_Hz s OR average many seeded repeats, else a single short
# transfer is high-variance noise, not a result.
WATTERSON = (os.environ.get("SIM_WATTERSON", "off").strip().lower() or "off")
FADE_DOPPLER = os.environ.get("SIM_FADE_DOPPLER_HZ", "").strip()
FADE_DELAY = os.environ.get("SIM_FADE_DELAY_MS", "").strip()
FADE_DUR_S = float(os.environ.get("SIM_FADE_DUR_S", "1200").strip() or "1200")
FADE_SEED = int(os.environ.get("SIM_FADE_SEED", str(SEED)).strip() or str(SEED))
# Scheduled fading: a time sequence of
# presets within ONE session, the missing instrument for ADAPTIVE rate-control
# logic (static presets never exercise mode switching). Syntax:
#   SIM_FADE_SCHEDULE="good:120,poor:180,good:0"   (name:seconds, last 0 = rest
# of run; names from watterson.PRESETS or "off"). Segments crossfade linearly
# over SIM_FADE_XFADE_S (default 1 s — a real channel transitions continuously;
# a hard swap would inject an artificial phase/gain step). Each transition is
# logged to stderr with its virtual/wall block time as ground truth for
# scoring mode-switch latency. Per-segment fade processes are independently
# seeded (FADE_SEED + 100*index + direction), so paired-seed A/Bs still see
# identical channel realizations. Overrides SIM_WATTERSON when set.
FADE_SCHEDULE = os.environ.get("SIM_FADE_SCHEDULE", "").strip()
FADE_XFADE_S = float(os.environ.get("SIM_FADE_XFADE_S", "1.0").strip() or "1.0")
# Rig SSB audio passband (TX + RX). A real SSB transceiver band-limits the audio
# to ~2.4-2.9 kHz on BOTH transmit and receive, so a wide mode's edge carriers (FD-OFDM-2438
# spans ~281-2719 Hz) hit the filter skirt + edge group delay. The flat-to-Nyquist sim
# OVERSTATES the wide modes; this is the "realistic rig" profile (research basis:
# docs/BANDWIDTH.md). Default: `data` (the realistic-profile
# default; SIM_RIG_BPF=off restores the legacy flat-to-Nyquist baseline).
# SIM_RIG_BPF = off | default(300-2700) | data(150-2900) | narrow(300-2400); or set
# SIM_RIG_LO / SIM_RIG_HI (Hz) explicitly. SIM_RIG_ORDER = Butterworth order (default 6).
RIG_BPF_PRESETS = {"default": (300.0, 2700.0), "data": (150.0, 2900.0),
                   "narrow": (300.0, 2400.0)}
RIG_BPF = (os.environ.get("SIM_RIG_BPF", "data").strip().lower() or "off")
RIG_LO = os.environ.get("SIM_RIG_LO", "").strip()
RIG_HI = os.environ.get("SIM_RIG_HI", "").strip()
RIG_ORDER = int(os.environ.get("SIM_RIG_ORDER", "6").strip() or "6")
# skywave's FM port profiles (the FM port design; classes in
# fm_rig.py). SIM_FM_PORT = off (default) | micspk | data9600. When set, the FM
# port shaping REPLACES the SSB rig BPF stage in the rig_tx/rig_rx slots
# (micspk: pre/de-emphasis + 300-3000 Hz voice BPF + gated squelch; data9600:
# flat discriminator tap) — an EXPLICIT SIM_RIG_BPF or any Watterson fade knob
# alongside it is a config conflict (exit 2): the FM fade axis is SIM_FM_FADE
# (fm_channel.py, Phase 1 Tier A), never the HF knobs. Squelch (micspk only, default
# 'gated'; SIM_FM_SQL=off disables): carrier attack SIM_FM_SQL_OPEN_MS
# (TIA-603 carrier-detect class, default 30) + CTCSS decode SIM_FM_SQL_TONE_MS
# (default 120, applied only when SIM_FM_CTCSS_HZ is set) + optional closing
# tail burst SIM_FM_SQL_TAIL_MS at SIM_FM_SQL_TAIL_AMP. Carrier source = the
# transmitter's delayed rf_up under SIM_HALF_DUPLEX (PTT keys the carrier
# regardless of audio); full-duplex runs fall back to block-RMS energy detect
# at SIM_FM_SQL_THRESH. CTCSS: SIM_FM_CTCSS_HZ tone at SIM_FM_CTCSS_AMP
# (int16 units, default 3277 ~= 10% full scale) summed onto the TX path after
# the voice filter (the sub-300 Hz path-sharing case from the research doc).
FM_PORT = (os.environ.get("SIM_FM_PORT", "off").strip().lower() or "off")
FM_ORDER = int(os.environ.get("SIM_FM_ORDER", "6").strip() or "6")
FM_SQL = (os.environ.get("SIM_FM_SQL", "gated").strip().lower() or "gated")
FM_SQL_OPEN_MS = float(os.environ.get("SIM_FM_SQL_OPEN_MS", "30").strip() or "30")
FM_SQL_TONE_MS = float(os.environ.get("SIM_FM_SQL_TONE_MS", "120").strip() or "120")
FM_SQL_TAIL_MS = float(os.environ.get("SIM_FM_SQL_TAIL_MS", "0").strip() or "0")
FM_SQL_TAIL_AMP = float(os.environ.get("SIM_FM_SQL_TAIL_AMP", "2000").strip() or "2000")
FM_SQL_THRESH = float(os.environ.get("SIM_FM_SQL_THRESH", "800").strip() or "800")
FM_CTCSS_HZ = float(os.environ.get("SIM_FM_CTCSS_HZ", "0").strip() or "0")
FM_CTCSS_AMP = float(os.environ.get("SIM_FM_CTCSS_AMP", "3277").strip() or "3277")
# skywave's FM port: Tier A flat fade + shadowing + IONOS noise shaping
# (fm_channel.py; see the FM port design "Tier A"). All require SIM_FM_PORT.
#   SIM_FM_FADE   = off | fixed|pedestrian|mobile-urban|mobile-highway (regime
#                   presets, Doppler picked by SIM_FM_BAND per the
#                   preset table) | ionos:<depth_db>:<rate_hz> (deterministic
#                   IONOS periodic fade; the published VARA-FM cells are
#                   ionos:30:0.1|1|3) | rayleigh:<fD> | rice:<fD>[:<K_dB>] |
#                   static. Per-direction independent realizations seeded
#                   FADE_SEED+11/+22 (Watterson convention).
#   SIM_FM_BAND   = 2m (default) | 70cm — preset Doppler column.
#   SIM_FM_SHADOW = off | <sigma_db>:<tau_s> — Suzuki log-normal shadowing
#                   (median 0 dB, NOT power-normalized: shadow loss is real),
#                   AR(1) with tau_s e-folding; sigma cites P.1546-6 A5 sec.12.
#                   Composes onto any SIM_FM_FADE (incl. fixed/static).
#   SIM_FM_NOISE_BW = off | 3000 | 6000 — band-limit the noise stream to the
#                   IONOS instrument's 3/6 kHz shaped-WGN bandwidths (FIR per
#                   the manual's passband/stopband spec, re-derived at FS).
#                   The published VARA-FM methodology is 6000.
FM_FADE = (os.environ.get("SIM_FM_FADE", "off").strip().lower() or "off")
FM_BAND = (os.environ.get("SIM_FM_BAND", "2m").strip().lower() or "2m")
FM_SHADOW = (os.environ.get("SIM_FM_SHADOW", "off").strip().lower() or "off")
FM_NOISE_BW = (os.environ.get("SIM_FM_NOISE_BW", "off").strip().lower() or "off")
# Fixed one-way link delay (propagation + sequencer turnaround), ms. Applied to the
# faded TX signal BEFORE the half-duplex deliver gate, so deafness stays evaluated at the
# signal's ARRIVAL time (the receiver is deaf NOW while IT transmits, hearing what was sent
# LINK_DELAY_MS ago). Default: 3 ms (the realistic profile; SIM_LINK_DELAY_MS=0
# restores the legacy zero-delay baseline).
LINK_DELAY_MS = float(os.environ.get("SIM_LINK_DELAY_MS", "3").strip() or "0")
LINK_DELAY_SAMP = int(round(LINK_DELAY_MS * 1e-3 * FS))

BUF = ["--buffer-time=60000", "--period-time=15000"]
NSAMP = BLOCK * NCH           # interleaved int16 samples per block
NBYTES = NSAMP * 2
BLOCK_MS = 1000.0 * BLOCK / FS
HANG_BLOCKS = max(1, int(round(HANG_MS / BLOCK_MS)))
# Real-radio T/R (transmit<->receive) switch latency, the environment-relative axis.
# SIM_TR_KEY_MS: PTT-assert -> RF-actually-out settle (relay/PA). While settling, the
#   transmitter's signal does NOT reach the peer (the burst's leading edge is clipped) ->
#   this sets the FLOOR on how short the modem's TX lead-in silence can safely be.
# SIM_TR_UNKEY_MS: TX-end -> RX-ready recovery. The station is DEAF this long after it
#   stops keying (relay release + RX unmute), on top of the deaf-while-emitting rule.
# Defaults: 15/25 ms (the realistic profile, relay-keyed class; ~tens of ms relay,
# SDR/CAT faster, slow sequencers more). Set both to 0 for the legacy no-latency baseline.
TR_KEY_MS = float(os.environ.get("SIM_TR_KEY_MS", "15").strip() or "0")
TR_UNKEY_MS = float(os.environ.get("SIM_TR_UNKEY_MS", "25").strip() or "0")
TR_KEY_BLOCKS = int(round(TR_KEY_MS / BLOCK_MS))
TR_UNKEY_BLOCKS = int(round(TR_UNKEY_MS / BLOCK_MS))
# T/R-latency JITTER (the SUT sits 1-2
# blocks from a T/R cliff, and fixed constants phase-lock every keydown to
# the same grid offset). SIM_TR_JITTER_MS=J draws each settle latency PER KEY
# EDGE as uniform nominal±J ms (clamped >= 0, then block-quantized) instead
# of the fixed constant. SEEDED per direction (Link seed + 700 — a dedicated
# stream, so the noise/fade draw sequences are untouched and paired-seed
# A/Bs see identical jitter). Default 0 = OFF: nothing is constructed and
# the keying path stays bit-exact to the fixed constants.
TR_JITTER_MS = float(os.environ.get("SIM_TR_JITTER_MS", "0").strip() or "0")
# Realism knobs. ALL default OFF: nothing is constructed and the baseline
# stays bit-exact. Differential knobs (foff/ppm) apply +x on A->B, -x on B->A
# (two stations' opposing LO / sample-clock errors).
FOFF_HZ = float(os.environ.get("SIM_FOFF_HZ", "0").strip() or "0")
# Slow carrier-drift ramp: |f| ramps linearly from
# FOFF_HZ to SIM_FOFF_RAMP_HZ over SIM_FOFF_RAMP_S seconds, then holds. Models
# the measured ~1–2 Hz greyline drift (magnitude literature-cited; the ramp
# schedule is OURS, documented as such). Differential like FOFF_HZ (+ on A->B,
# − on B->A). Unset = static offset (bit-exact legacy path).
FOFF_RAMP_HZ = os.environ.get("SIM_FOFF_RAMP_HZ", "").strip()
FOFF_RAMP_S = float(os.environ.get("SIM_FOFF_RAMP_S", "600").strip() or "600")
CLOCK_PPM = float(os.environ.get("SIM_CLOCK_PPM", "0").strip() or "0")
CLOCK_SLACK_MS = float(os.environ.get("SIM_CLOCK_SLACK_MS", "20").strip() or "20")
ALC_DB = float(os.environ.get("SIM_ALC_OVERSHOOT_DB", "0").strip() or "0")
ALC_SETTLE_MS = float(os.environ.get("SIM_ALC_SETTLE_MS", "10").strip() or "10")
# Literature ALC presets:
# SIM_ALC_PRESET=modern -> 0.8 dB / 25 ms (IC-7610-class bench measurement);
#                =legacy -> 7 dB / 2 ms with a ~5 s re-arm-after-silence
# (IC-706MKII-class QEX measurement; the re-arm cadence matches HD ARQ
# turnaround). Preset overrides the raw knobs above when set.
ALC_PRESETS = {"modern": (0.8, 25.0, None), "legacy": (7.0, 2.0, 5.0)}
ALC_PRESET = (os.environ.get("SIM_ALC_PRESET", "off").strip().lower() or "off")
if ALC_PRESET != "off":
    if ALC_PRESET not in ALC_PRESETS:
        raise SystemExit(f"channel_sim: unknown SIM_ALC_PRESET='{ALC_PRESET}' "
                         f"(use {list(ALC_PRESETS)})")
    ALC_DB, ALC_SETTLE_MS, _alc_rearm_s = ALC_PRESETS[ALC_PRESET]
else:
    _alc_rearm_s = None
# Literature AGC presets: SIM_RX_AGC=1 keeps the legacy
# 2/100 ms follower; =data -> 10/25 ms (MIL-STD-188-141C data service);
# =voice -> 30/1000 ms (141C non-data — the mode MIL-STD-188-110B App C
# recommends for QAM data, with documented burst-onset pumping).
AGC_PRESETS = {"data": (10.0, 25.0), "voice": (30.0, 1000.0)}
RX_AGC_MODE = (os.environ.get("SIM_RX_AGC", "0").strip().lower() or "0")
RX_AGC = RX_AGC_MODE not in ("0", "", "off")
RX_AGC_ATTACK_MS = float(os.environ.get("SIM_RX_AGC_ATTACK_MS", "2").strip() or "2")
RX_AGC_RELEASE_MS = float(os.environ.get("SIM_RX_AGC_RELEASE_MS", "100").strip() or "100")
if RX_AGC_MODE in AGC_PRESETS:
    RX_AGC_ATTACK_MS, RX_AGC_RELEASE_MS = AGC_PRESETS[RX_AGC_MODE]
elif RX_AGC and RX_AGC_MODE != "1":
    raise SystemExit(f"channel_sim: unknown SIM_RX_AGC='{RX_AGC_MODE}' "
                     f"(use 1|data|voice)")
RX_AGC_TARGET = float(os.environ.get("SIM_RX_AGC_TARGET", "8000").strip() or "8000")
RX_AGC_MAXGAIN_DB = float(os.environ.get("SIM_RX_AGC_MAXGAIN_DB", "30").strip() or "30")
NOISE_VD = float(os.environ.get("SIM_NOISE_VD", "0").strip() or "0")
NOISE_VD_K_DB = float(os.environ.get("SIM_NOISE_VD_K_DB", "26").strip() or "26")
# QRM: occupancy-keyed in-channel CW-Morse +
# sweeper, levels as INR (dB over the in-channel noise power sigma^2). The
# retired lambda/SNR knobs HARD-ERROR so a stale driver fails loud instead of
# silently re-running the mis-scaled model (env doctrine): the old whole-band
# lambda applied in-passband was ~250x contest density and blew the RX-pad
# rail budget.
_QRM_RETIRED = [k for k in ("SIM_QRM_CW_LAMBDA", "SIM_QRM_CW_SNR_DB",
                            "SIM_QRM_SWEEP_SNR_DB")
                if os.environ.get(k, "").strip()]
if _QRM_RETIRED:
    raise SystemExit(
        f"channel_sim: {'/'.join(_QRM_RETIRED)} retired by "
        "use SIM_QRM_OCC (in-channel occupancy) "
        "/ SIM_QRM_INR_DB / SIM_QRM_INR_SPREAD_DB / SIM_QRM_INR_MAX_DB / "
        "SIM_QRM_SWEEP_INR_DB")
QRM_OCC = float(os.environ.get("SIM_QRM_OCC", "0").strip() or "0")
QRM_INR_DB = float(os.environ.get("SIM_QRM_INR_DB", "10").strip() or "10")
QRM_INR_SPREAD_DB = float(os.environ.get("SIM_QRM_INR_SPREAD_DB", "6").strip() or "6")
QRM_INR_MAX_DB = float(os.environ.get("SIM_QRM_INR_MAX_DB", "16").strip() or "16")
QRM_SWEEP = os.environ.get("SIM_QRM_SWEEP", "0").strip() == "1"
QRM_SWEEP_INR_DB = float(os.environ.get("SIM_QRM_SWEEP_INR_DB", "10").strip() or "10")
QRM_SWEEP_RATE = float(os.environ.get("SIM_QRM_SWEEP_RATE", "10").strip() or "10")
# Virtual sweep span (Hz): the OTHR chirps this wide; the passband sees only
# the crossing, so in-channel duty = 2400/span (10% at the 24 kHz default).
QRM_SWEEP_BAND_HZ = float(os.environ.get("SIM_QRM_SWEEP_BAND_HZ", "24000").strip() or "24000")
if not 0.0 <= QRM_OCC < 1.0:
    raise SystemExit(f"channel_sim: SIM_QRM_OCC={QRM_OCC:g} outside [0, 1)")
if QRM_SWEEP and QRM_SWEEP_BAND_HZ < 2400.0:
    raise SystemExit(
        f"channel_sim: SIM_QRM_SWEEP_BAND_HZ={QRM_SWEEP_BAND_HZ:g} narrower "
        "than the 2400 Hz passband — the virtual sweep span must cover the "
        "channel (24000 default; 2400 = the retired continuous-jammer shape)")


def qrm_rail_room_amp(sigma, rx_pad, fading):
    """Linear amplitude headroom available to QRM under the pre-pad rail
    threshold, alongside worst-case signal and noise peaks. May be <= 0: no room.

        thr = 32768/rx_pad;  room = thr - sig_peak - 4.9*sigma
        sig_peak = 32767 (x10^(10/20) when the channel fades: the measured
        constructive Watterson fade-up the RX pad budget was sized for)
    """
    thr = 32768.0 / rx_pad
    sig_peak = 32767.0 * (10.0 ** 0.5 if fading else 1.0)
    return thr - sig_peak - 4.9 * sigma


def qrm_amp(sigma, inr_db):
    """Carrier peak amplitude at inr_db over the noise power sigma^2."""
    return sigma * (10.0 ** (inr_db / 20.0)) * math.sqrt(2.0)
# P.372 man-made environment scaling: when set, SIGMA is
# re-interpreted as the QUIET-RURAL @ 7 MHz anchor and scaled by the P.372
# Part-6 median man-made noise delta for the chosen environment and band:
# Fam(dB) = c − d·log10(f_MHz) (city 76.8/27.7, residential 72.5/27.7, rural
# 67.2/27.7, quiet 53.6/28.6). Categories are RELATIVE guides (the underlying
# dataset is dated — 2024 gap analysis); default off leaves SIGMA untouched.
P372_ENV = {"city": (76.8, 27.7), "residential": (72.5, 27.7),
            "rural": (67.2, 27.7), "quiet": (53.6, 28.6)}
NOISE_ENV = (os.environ.get("SIM_NOISE_ENV", "off").strip().lower() or "off")
BAND_MHZ = float(os.environ.get("SIM_BAND_MHZ", "7").strip() or "7")
if NOISE_ENV != "off":
    if NOISE_ENV not in P372_ENV:
        raise SystemExit(f"channel_sim: unknown SIM_NOISE_ENV='{NOISE_ENV}' "
                         f"(use {list(P372_ENV)})")
    _c, _d = P372_ENV[NOISE_ENV]
    _cq, _dq = P372_ENV["quiet"]
    _fam_delta = (_c - _d * math.log10(BAND_MHZ)) - (_cq - _dq * math.log10(7.0))
    SIGMA *= 10.0 ** (_fam_delta / 20.0)
# --- Per-direction / per-station ASYMMETRY overrides (2026-07-19) ---------------------
# Default = the symmetric globals, so an UNSET knob reproduces the current symmetric
# channel byte-for-byte. Noise is a per-DIRECTION (receiver) property; audio drive is a
# per-STATION (transmitter) property. A->B carries (drive=TXGAIN_A, noise=SIGMA_AB); B->A
# carries (drive=TXGAIN_B, noise=SIGMA_BA). Use case: asymmetric ARQ paths (weak ACK
# path), QRP<->QRO imbalance (an SNR effect here), one station over-driving (a drive/
# distortion effect). NOTE: RF power in watts is NOT a distinct axis in an audio-domain
# sim — it collapses to SNR (SIGMA); SIM_TXGAIN_* is the transmit AUDIO drive, the
# nonlinear-distortion axis. SIM_SIGMA_* override the post-noise-env SIGMA absolutely.
SIGMA_AB = float(os.environ.get("SIM_SIGMA_AB", "").strip() or SIGMA)
SIGMA_BA = float(os.environ.get("SIM_SIGMA_BA", "").strip() or SIGMA)
GAIN_A = float(os.environ.get("SIM_TXGAIN_A", "").strip() or GAIN)
GAIN_B = float(os.environ.get("SIM_TXGAIN_B", "").strip() or GAIN)
ASYM = (SIGMA_AB != SIGMA_BA) or (GAIN_A != GAIN_B)
# Virtual-rig stage 1 — framed unix-socket transport (see TRANSPORT in the header).
TRANSPORT = (os.environ.get("SIM_TRANSPORT", "alsa").strip().lower() or "alsa")
SOCK_DIR = os.environ.get("SIM_SOCK_DIR", "").strip() or f"/tmp/simsock-{os.getpid()}"
SOCK_SHIM = os.environ.get("SIM_SOCK_SHIM", "0").strip() == "1"
SOCK_ACCEPT_S = float(os.environ.get("SIM_SOCK_ACCEPT_S", "30").strip() or "30")
SOCK_BUF = int(os.environ.get("SIM_SOCK_BUF", "65536").strip() or "65536")
# AF_UNIX addresses are OS-capped: sockaddr_un.sun_path holds 108 bytes on Linux,
# 104 on macOS/BSD (the last is the NUL terminator). A socket path over the limit
# fails bind()/connect() with a cryptic "AF_UNIX path too long" -- easy to hit on
# macOS, where a deep SIM_SOCK_DIR (or a long system tempdir) overflows fast.
_SUN_PATH_MAX = 108 if sys.platform.startswith("linux") else 104
# Virtual-rig stage 2 — block-lockstep virtual clock.
# SIM_CLOCK=virt_time (requires SIM_TRANSPORT=sock, a station's `--audio sock` backend):
# the sim is the clock master — per block it sends both stations their RX frame
# (header virtual_now_ms = the block's END time), BARRIERS on both TX replies
# (neither station computes ahead of the other), applies the per-direction
# transform chain unchanged, and loops immediately: no wall pacing anywhere, the
# run goes as fast as the slower station computes. PTT comes from the in-band
# header field (block-exact); SIM_MAX_VIRTUAL_S bounds the run in VIRTUAL seconds
# (a TO=900 cell means 900 virtual seconds), exiting with a VIRTUAL-TIMEOUT
# marker; 0 = unbounded (the driver's wall timeout is the hang backstop).
SIM_CLOCK = (os.environ.get("SIM_CLOCK", "real_time").strip().lower() or "real_time")
MAX_VIRTUAL_S = float(os.environ.get("SIM_MAX_VIRTUAL_S", "0").strip() or "0")
# Virtual now (seconds) for stats in lockstep mode; module-level so write_stats
# can report it (drivers score virtual goodput off this).
VIRT_NOW_S = 0.0

# device map (matches the per-harness relay convention):
#   A TX -> B RX : cap plughw:2,0  play plughw:5,1
#   B TX -> A RX : cap plughw:4,1  play plughw:3,0
CAP_A, PLAY_B = "plughw:2,0", "plughw:5,1"   # direction A->B
CAP_B, PLAY_A = "plughw:4,1", "plughw:3,0"   # direction B->A

ACT_THRESH = 200.0   # ~0.6% FS; excludes silence/gaps between bursts (matches noise_pipe_gain)


# arecord/aplay inherit this process's group (no setsid) so the harness can tear the whole
# rig down with one killpg on the channel_sim handle (which bench_pipes makes a session leader).
def arecord(dev):
    return sp.Popen(["arecord", "-D", dev, "-f", "S16_LE", "-r", str(FS), "-c", str(NCH),
                     *BUF], stdout=sp.PIPE, stderr=sp.DEVNULL, bufsize=0)


def aplay(dev):
    return sp.Popen(["aplay", "-D", dev, "-f", "S16_LE", "-r", str(FS), "-c", str(NCH),
                     *BUF], stdin=sp.PIPE, stderr=sp.DEVNULL, bufsize=0)


def read_exact(f, mv):
    """Fill mv from f (raw pipe may short-read). Returns bytes read; < len(mv) means EOF."""
    n, L = 0, len(mv)
    while n < L:
        k = f.readinto(mv[n:])
        if not k:
            return n
        n += k
    return n


def write_all(fd, buf):
    mv = memoryview(buf)
    while mv:
        try:
            k = os.write(fd, mv)
        except (BrokenPipeError, OSError):
            return False
        mv = mv[k:]
    return True


class PttState:
    """External per-station PTT (SIM_PTT mode): set by the stdin listener from the harness's
    relay of each modem's host PTT ON/OFF, read by `_update_key` as the `active` source in
    place of the VOX RMS threshold. Bool attr read/write is atomic under the GIL."""

    def __init__(self):
        self.a = False
        self.b = False


def ptt_listener(ptt):
    """Read 'a 1'/'a 0'/'b 1'/'b 0' lines from stdin (written by the harness) and update the
    shared PttState. Exits on EOF (harness closed the pipe). Run as a daemon thread."""
    for line in sys.stdin:
        p = line.split()
        if len(p) >= 2 and p[0] in ("a", "b"):
            setattr(ptt, p[0], p[1] == "1")


class Keys:
    """Shared half-duplex keying state. Each direction's thread writes its OWN source
    station's flag and reads the PEER's; bool attr read/write is atomic under the GIL,
    so no lock is needed (single writer per attribute)."""

    def __init__(self):
        self.a = False           # raw 'active' (emitting RF this block) — peer deafness
        self.b = False
        # T/R model: RF actually up (past key settle) and station switched back to RX (past
        # unkey recovery). Default mirrors the no-latency case (rf_up==keyed, rx_ready==idle).
        self.a_rf_up = False
        self.b_rf_up = False
        self.a_rx_ready = True
        self.b_rx_ready = True


try:
    from scipy.signal import butter as _butter, sosfilt as _sosfilt
except ImportError:  # scipy is only needed when fading or the rig BPF is enabled
    _butter = _sosfilt = None


def _resolve_rig_band():
    """(lo_hz, hi_hz) for the rig passband, or None when disabled (the default)."""
    if RIG_LO and RIG_HI:
        return (float(RIG_LO), float(RIG_HI))
    return RIG_BPF_PRESETS.get(RIG_BPF)  # None for 'off' / unknown


class RigBPF:
    """Stateful SSB audio bandpass (Butterworth, second-order-sections). Applied once on
    TX (the transmitting rig's filter, before the channel) and once on RX (the receiving
    rig's filter, after noise). Carries filter state across blocks so there is no
    per-block edge artifact; operates on the mono path (broadcast to NCH by the caller)."""

    def __init__(self, lo_hz, hi_hz, order, fs):
        if _butter is None:
            raise RuntimeError("SIM_RIG_BPF / SIM_RIG_LO needs scipy (pip install scipy)")
        self.sos = _butter(order, [lo_hz, hi_hz], btype="band", fs=fs, output="sos")
        self.zi = np.zeros((self.sos.shape[0], 2))

    def process(self, mono):
        y, self.zi = _sosfilt(self.sos, mono, zi=self.zi)
        return y


class Link:
    """One direction of the channel: source TX capture -> transform -> sink RX playback.
    Runs on its own thread so an idle capture can't stall the other direction."""

    def __init__(self, name, src_proc, sink_fd, seed, stats_path, stop,
                 src_name, sink_name, keys, ptt=None, fade=None, link_delay_samp=0,
                 rig_tx=None, rig_rx=None, fx=None, squelch=None, gain=None, sigma=None):
        self.name = name
        # Per-direction/per-station asymmetry (default = the symmetric globals, so an
        # unset knob is byte-identical to the earlier single-global behavior).
        self.gain = GAIN if gain is None else gain    # this station's TX audio drive
        self.sigma = SIGMA if sigma is None else sigma  # this direction's noise floor (RX)
        self.fade = fade                          # WattersonChannel or None (per-direction)
        self.rig_tx = rig_tx                      # RigBPF / fm_rig.FmPortTx or None
        self.rig_rx = rig_rx                      # RigBPF / fm_rig.FmPortRx or None
        self.squelch = squelch                    # fm_rig.SquelchGate or None (micspk RX)
        self.noise_lpf = None                     # fm_channel.NoiseShaper or None
                                                  # (assigned post-construction; FM Tier A)
        # Effects bundle (SimpleNamespace with alc/foff/skew/agc/imp/qrm,
        # each None when its knob is off) — see rig_effects.py.
        self.fx = fx
        # Interleaved bulk link delay (LINK_DELAY_SAMP per channel -> *NCH interleaved).
        self.dl = np.zeros(link_delay_samp * NCH, dtype=np.float64)
        # persistent FIFO scratch so the delay path is
        # allocation-free (was a per-block np.concatenate).
        self._dlscratch = np.empty(self.dl.size + NSAMP, dtype=np.float64)
        # persistent QRM block buffer (was a per-block np.zeros).
        self._qrmbuf = np.empty(BLOCK, dtype=np.float64)
        # HD deliver-gate alignment: the TX side of the gate is evaluated at
        # TRANSMIT time (a block leaving the delay line left the transmitter
        # link_delay_samp ago), so rf_up is FIFO-delayed by the same whole
        # blocks. The sub-block residual (< 1 block, gate leads the signal) is
        # masked by the hangtime tail, exactly as the pre-fix <= HANG_MS case
        # was. An empty FIFO (delay < 1 block) degenerates to "rf_up at now" —
        # the prior rule, bit-identical at the default delay 0.
        self._rfq = [False] * (link_delay_samp // BLOCK)
        self.src = src_proc.stdout
        self.sink_fd = sink_fd
        self.rng = np.random.default_rng(seed)
        # T/R jitter stream (SIM_TR_JITTER_MS; None when off => fixed constants)
        self.trj = np.random.default_rng(seed + 700) if TR_JITTER_MS > 0 else None
        self.stats_path = stats_path
        self.stop = stop
        self.src_name = src_name      # 'a' or 'b' — this direction's transmitter
        self.sink_name = sink_name    # the receiver served by this direction
        self.keys = keys
        self.ptt = ptt                # PttState (SIM_PTT mode); None => VOX keying
        self.keyed = False            # transmission in progress (raw active OR within hangtime)
        self.active = False           # emitting RF THIS block (raw, no hangtime) -> deafness
        self.hang = 0
        self._prev_active = False     # for T/R rising/falling-edge detection
        self.rf_settle = 0            # blocks until RF up after key-on (SIM_TR_KEY_MS)
        self.rx_recover = 0           # blocks until RX ready after key-off (SIM_TR_UNKEY_MS)
        self.rf_up = False            # RF actually transmitting (keyed AND past key settle)
        self.keyed_blocks = 0         # count of keyed blocks -> key duty (VOX validation)
        self._prev_keyed = False
        self.keylog = open(KEYLOG + "." + src_name, "w", buffering=1) if KEYLOG else None
        self.txdump = open(TXDUMP + "." + src_name, "wb") if TXDUMP else None
        # preallocated, reused every block (no per-block allocation in the hot loop)
        self.inbuf = bytearray(NBYTES)
        self.inmv = memoryview(self.inbuf)
        self.xin = np.frombuffer(self.inbuf, dtype="<i2")     # int16 view onto inbuf
        self.work = np.empty(NSAMP, dtype=np.float64)
        self.noise = np.empty(NSAMP, dtype=np.float64)
        self.outbuf = bytearray(NBYTES)
        self.xout = np.frombuffer(self.outbuf, dtype="<i2")   # int16 view onto outbuf
        # stats accumulators (transmitted = post-gain, pre-noise), mirror noise_pipe_gain.py
        self.peak = 0.0
        self.robust_peak = 0.0     # peak excluding the cold-start transient (fair PEP; see _accum)
        self._accum_blocks = 0
        self.sumsq = 0.0
        self.nsamp = 0
        self.act_sumsq = 0.0
        self.act_n = 0
        self.nclip = 0
        self.nrail = 0     # post-channel rail hits (fade peaks over int16 full scale)
        self._rail_warned = False   # active clipping warning fires once per direction
        self.last_stats = 0.0
        # health
        self.nblocks = 0
        self.worst = 0.0
        self.times = []

    def read_block(self):
        """Fill self.inbuf with one source block; False on EOF/error (wind down)."""
        return read_exact(self.src, self.inmv) == NBYTES

    def write_block(self):
        """Deliver self.outbuf to the sink; False on EOF/error (wind down)."""
        return write_all(self.sink_fd, self.outbuf)

    def run(self):
        last = 0.0
        while not self.stop.is_set():
            if not self.read_block():
                break                       # capture ended
            t0 = time.monotonic()
            self.process()
            if not self.write_block():
                break                       # playback ended
            dt = (time.monotonic() - t0) * 1000.0
            if dt > self.worst:
                self.worst = dt
            self.nblocks += 1
            if VERBOSE and len(self.times) < 400000:
                self.times.append(dt)
            if STATS:
                now = time.monotonic()
                if now - last > 2.0:
                    self.write_stats()
                    last = now
        self.stop.set()                     # tell the peer direction to wind down too
        self.write_stats()
        if self.txdump is not None:
            self.txdump.flush()
            self.txdump.close()

    def process(self):
        """Transform self.xin (filled) into self.outbuf.
        PHASE 0 (full-duplex): gain->clip(PEP)->+noise, always delivered.
        PHASE 1 (half-duplex): the receiver hears the transmitted signal ONLY when this
        direction's station is keyed (hangtime-bridged) AND the receiver is not ACTIVELY
        transmitting this block (deaf only while it actually emits, incl. the both-active
        collision); otherwise it hears the noise floor only."""
        self.deliver_block(self.tx_shape())

    def tx_shape(self):
        """First half of `process`: shape the transmitted signal (gain -> ALC ->
        PA/clip), accumulate TX stats, and publish this station's keying/T-R
        state. Returns the shaped float work buffer. Split from `deliver_block`
        so the lockstep sim (SIM_CLOCK=virt_time) can run BOTH directions'
        keying updates before either deliver gate reads the peer's flags — a
        consistent same-block snapshot instead of the wall rig's benign
        thread-race interleaving. Single-transport callers see the exact
        statement order the old monolithic process() had."""
        w = self.work
        np.multiply(self.xin, self.gain, out=w)       # int16 * per-station drive -> float TX signal
        if self.fx is not None and self.fx.alc is not None:
            self.fx.alc.process(w)                    # burst-onset ALC overshoot drives the PA harder
        if PA_P > 0:                                  # Rapp soft PA: compress the envelope, not a hard clip
            ax = np.abs(w) / PA_VSAT
            # nclip under Rapp = samples driven past Vsat (the >=1.5 dB compression region);
            # under the hard clip below it counts actual ceiling hits. Feeds stats clip_frac.
            self.nclip += int(np.count_nonzero(ax > 1.0))
            np.divide(w, np.power(1.0 + np.power(ax, 2.0 * PA_P), 1.0 / (2.0 * PA_P)), out=w)
        else:
            self.nclip += int(np.count_nonzero((w > 32767.0) | (w < -32768.0)))
        np.clip(w, -32768.0, 32767.0, out=w)          # PEP ceiling (no-op under Rapp; hard clip when PA off)
        self._accum(w)                                # TX-level stats (the drive calibration), as before
        if self.txdump is not None:
            self.txdump.write(w.astype("<i2").tobytes())  # exact pre-noise TX stream the stats see
        if HALF_DUPLEX:
            self._update_key(w)                       # publish this station's keying + T/R state
        return w

    def deliver_block(self, w):
        """Second half of `process`: the half-duplex deliver gate + the channel
        chain (fade/foff/skew/delay/noise/BPF/AGC) into self.outbuf."""
        deliver = True
        carrier_at_tx = None                      # FM squelch carrier (HD keying); None => energy detect
        if HALF_DUPLEX:
            # Deliver only when the transmitter's RF was up AT TRANSMIT TIME (past the key
            # settle, FIFO-delayed by the link delay's whole blocks — the block leaving the
            # delay line below left the transmitter LINK_DELAY_MS ago) AND the receiver is
            # RX-ready NOW, at arrival (not emitting, and past its unkey recovery). With
            # SIM_TR_*_MS == 0 and delay 0 this reduces to the prior rule
            # (deliver = keyed AND peer-not-active).
            rf_up_at_tx = self.rf_up
            if self._rfq:
                self._rfq.append(self.rf_up)
                rf_up_at_tx = self._rfq.pop(0)
            deliver = rf_up_at_tx and getattr(self.keys, self.sink_name + "_rx_ready")
            carrier_at_tx = rf_up_at_tx
        # Channel impairments on the TRANSMITTED signal (after keying, before noise). Keying
        # is read off the PRE-fade signal above; the Doppler clock advances every block (the
        # channel fades whether or not this block is delivered). Fade the deinterleaved mono
        # modem signal once and broadcast to all cable channels (one RF path), then bulk-delay.
        # Rig TX audio filter (the transmitting rig's SSB passband) shapes the signal BEFORE
        # the channel impairments, attenuating any carriers outside the rig's passband.
        if self.rig_tx is not None:
            m = self.rig_tx.process(w[0::NCH])
            for c in range(NCH):
                w[c::NCH] = m
        if self.fade is not None:
            f0 = self.fade.process(w[0::NCH])
            # The channel is LINEAR — no clip here. A constructive Watterson fade-up legitimately exceeds
            # int16 full scale (measured +10 dB over mean); it is carried in
            # float and brought back under the rail by the RX pad before the
            # cast. An earlier rail clip here was the silent distortion that
            # collapsed the fading cells. Rail
            # excursions are counted ONCE, at the final guard below (the single
            # int16 boundary) — counting here too would double-count now that
            # this stage no longer clips.
            for c in range(NCH):
                w[c::NCH] = f0
        if self.fx is not None and self.fx.foff is not None:
            m = self.fx.foff.process(w[0::NCH])       # LO offset (one RF path -> all channels)
            for c in range(NCH):                      # linear stage: no clip
                w[c::NCH] = m
        if self.fx is not None and self.fx.skew is not None:
            m = self.fx.skew.process(np.ascontiguousarray(w[0::NCH]))
            for c in range(NCH):
                w[c::NCH] = m
        if self.dl.size:
            s = self._dlscratch                       # [dl | w], allocation-free
            m = self.dl.size
            s[:m] = self.dl
            s[m:] = w
            w = s[:NSAMP]                             # signal delayed by the link delay
            self.dl[:] = s[NSAMP:]
            # The HD deliver gate above is aligned to this delay: its TX side uses
            # rf_up_at_tx (rf_up FIFO-delayed by the delay's whole blocks), its RX side
            # rx_ready at arrival = now. Sub-block residual masked by the hangtime
            # (pinned in tests/test_link_delay.py at 150 ms > HANG_MS).
        # Additive noise + QRM: linear, NO clip (the channel adds noise
        # to the signal, it does not saturate; the RX pad + guard below is the
        # only int16-boundary clip).
        qrm = self.fx.qrm if self.fx is not None else None
        if deliver:
            if self.sigma > 0.0:
                self._fill_noise(self.noise)
                w += self.noise
            if qrm is not None:
                self._add_qrm(w, qrm)
        else:
            # receiver hears the noise floor (and any QRM — an independent
            # transmitter) only; no peer signal reaches it
            if self.sigma > 0.0:
                self._fill_noise(w)
            else:
                w[:] = 0.0
            if qrm is not None:
                self._add_qrm(w, qrm)
        # Rig RX audio filter (the receiving rig's SSB passband) band-limits the
        # delivered signal AND the in-band noise — linear, no clip.
        if self.rig_rx is not None:
            m = self.rig_rx.process(w[0::NCH])
            for c in range(NCH):
                w[c::NCH] = m
        # FM gated squelch (fm_rig.SquelchGate): time-gated mute on the RX audio
        # AFTER de-emphasis (the speaker mute), before the receiver AGC. Under
        # HD the carrier is the transmitter's delayed rf_up (a keyed FM carrier
        # is up regardless of audio content); otherwise the gate energy-detects.
        # A closed squelch mutes the idle noise floor too — that IS its job.
        if self.squelch is not None:
            m = self.squelch.process(w[0::NCH], carrier_at_tx)
            for c in range(NCH):
                w[c::NCH] = m
        if self.fx is not None and self.fx.agc is not None:
            # Receiver AGC gain (no internal clip here): the burst-head
            # over-amplification is carried in float; the pad then the guard
            # below model the audio-level pot + ADC rail where that
            # over-amplification actually clips. For an AGC cell rail_frac is
            # therefore EXPECTED nonzero — it IS the modeled burst-head damage,
            # not an instrument fault.
            w = self.fx.agc.process(np.ascontiguousarray(w))
        # RX pad: fixed audio-level headroom on
        # signal AND noise together, immediately before the int16 boundary.
        # SNR-invariant (both scaled identically, after all noise). This is the
        # real station's RX-audio-out setting that keeps fade-up peaks off the
        # ADC rail. RX_PAD == 1.0 (SIM_RX_PAD_DB=0) restores the legacy levels.
        if RX_PAD != 1.0:
            np.multiply(w, RX_PAD, out=w)
        # Final rail guard = the ONE int16-boundary clip. After the pad a
        # no-AGC cell must read rail_frac ≈ 0 (fade +10 dB − pad 12 dB ⇒ −2 dB
        # peak); a nonzero value there flags an instrument level problem
        # (mirrors codec2 `ch`'s >0.1% output-clipping warning). Also converts
        # any residual overflow (e.g. the never-clipped skew path) from a
        # cast-WRAP to a clip.
        self.nrail += int(np.count_nonzero((w > 32767.0) | (w < -32768.0)))
        np.clip(w, -32768.0, 32767.0, out=w)
        np.copyto(self.xout, w, casting="unsafe")     # float -> int16 into outbuf (no alloc)

    def _fill_noise(self, out):
        """Noise floor into `out`: Gaussian (baseline, bit-exact path) or the
        Vd-calibrated impulsive mixture when SIM_NOISE_VD is set."""
        imp = self.fx.imp if self.fx is not None else None
        if imp is not None:
            imp.fill(self.rng, out)
        else:
            self.rng.standard_normal(NSAMP, out=out)
            np.multiply(out, self.sigma, out=out)
        if self.noise_lpf is not None:
            # FM Tier A: IONOS-equivalent noise bandwidth shaping (stateful
            # per-channel FIR; SIGMA stays the in-band per-sample sigma).
            self.noise_lpf.process(out)

    def _add_qrm(self, w, qrm):
        """One RF interference path, broadcast to all cable channels."""
        q = self._qrmbuf                              # persistent buffer
        q.fill(0.0)
        qrm.fill(q)
        for c in range(NCH):
            w[c::NCH] += q

    def _tr_blocks(self, nominal_ms):
        """Per-edge jittered T/R settle latency, in blocks (SIM_TR_JITTER_MS):
        uniform nominal±J ms, clamped at 0, block-quantized exactly like the
        fixed-constant path. One draw per key edge from the dedicated seeded
        stream (`self.trj`)."""
        ms = nominal_ms + self.trj.uniform(-TR_JITTER_MS, TR_JITTER_MS)
        return max(0, int(round(ms / BLOCK_MS)))

    def _update_key(self, w):
        """VOX keying on the transmitted signal RMS. `active` = emitting RF this block (raw);
        `keyed` = active OR within the hangtime tail. We publish the RAW `active` for the peer's
        deafness check: a station is deaf ONLY while actually emitting, NOT during its hangtime
        tail — so the hangtime bridges THIS transmitter's intra-burst gaps without ever forcing a
        false collision on a peer that has already stopped (a real turnaround-stall failure mode: a
        false collision against a peer at ~80 ms hangtime, which tight windowed-ARQ modems hit
        and looser stop-and-wait modems do not)."""
        rms = (np.dot(w, w) / NSAMP) ** 0.5 if NSAMP else 0.0
        # SIM_PTT: `active` comes from the modem's real PTT (no leading-edge clip); else VOX RMS.
        self.active = getattr(self.ptt, self.src_name) if SIM_PTT else rms > KEY_THRESH
        if self.active:
            self.keyed = True
            self.hang = HANG_BLOCKS
        elif self.hang > 0:
            self.hang -= 1                            # hold the key through intra-burst gaps
        else:
            self.keyed = False
        if self.keyed:
            self.keyed_blocks += 1
        # T/R switch model. Rising edge -> start RF-up settle; falling edge -> start RX
        # recovery. rf_up gates whether OUR signal reaches the peer (leading-edge clip while
        # the relay/PA settles); rx_ready gates whether we can HEAR the peer (deaf while
        # emitting OR recovering). With TR_*_BLOCKS == 0 this reduces to the prior rule.
        if self.active and not self._prev_active:
            self.rf_settle = (TR_KEY_BLOCKS if self.trj is None
                              else self._tr_blocks(TR_KEY_MS))
        elif (not self.active) and self._prev_active:
            self.rx_recover = (TR_UNKEY_BLOCKS if self.trj is None
                               else self._tr_blocks(TR_UNKEY_MS))
        # Gates are evaluated BEFORE the countdown decrements so SIM_TR_*_MS of one block
        # really costs one block (decrement-first made TR_KEY_BLOCKS=1 a no-op: the edge
        # block set 1, decremented to 0, and rf_up came up the same block — off-by-one).
        self.rf_up = self.keyed and self.rf_settle == 0
        rx_ready = (not self.active) and self.rx_recover == 0
        if self.active:
            if self.rf_settle > 0:
                self.rf_settle -= 1
        elif self.rx_recover > 0:
            self.rx_recover -= 1
        self._prev_active = self.active
        if self.keylog is not None and self.keyed != self._prev_keyed:
            self.keylog.write(f"{time.time():.4f} {int(self.keyed)} {rms:.0f}\n")
            self._prev_keyed = self.keyed
        setattr(self.keys, self.src_name, self.active)   # peer reads RAW active for deafness
        setattr(self.keys, self.src_name + "_rf_up", self.rf_up)
        setattr(self.keys, self.src_name + "_rx_ready", rx_ready)

    def _accum(self, w):
        a = np.abs(w)
        m = float(a.max()) if a.size else 0.0
        if m > self.peak:
            self.peak = m
        # robust_peak EXCLUDES the first STATS_SKIP_BLOCKS blocks: the snd-aloop/ALSA loopback
        # cold-start emits a deterministic full-scale transient (~0.2 ms) BEFORE the modem's first
        # real burst (verified byte-identical across runs; the modem's own TX is silence at startup).
        # That transient becomes the session-global peak and makes peak-normalization anchor on a
        # glitch, understating a codec2 modem's mean power by ~5 dB. robust_peak is the fair PEP.
        if self._accum_blocks >= STATS_SKIP_BLOCKS and m > self.robust_peak:
            self.robust_peak = m
        self._accum_blocks += 1
        self.sumsq += float(np.dot(w, w))
        self.nsamp += w.size
        act = w[a > ACT_THRESH]
        if act.size:
            self.act_sumsq += float(np.dot(act, act))
            self.act_n += act.size

    def write_stats(self):
        # Active clipping detector (fires once per direction, independent of
        # whether NP_STATS is set — it is a safety alarm, not a stats feature).
        if self.nsamp and not self._rail_warned:
            frac = self.nrail / self.nsamp
            if frac > RAIL_WARN_FRAC:
                self._rail_warned = True
                print(f"channel_sim: WARNING output clipping on {self.name}: "
                      f"rail_frac={frac:.2e} (>{RAIL_WARN_FRAC:.1e}) — the RX "
                      f"pad is not clearing fade-ups; instrument level bug "
                      f"unless this is an AGC cell", file=sys.stderr, flush=True)
        if not self.stats_path or self.nsamp == 0:
            return
        rms = (self.sumsq / self.nsamp) ** 0.5
        act_rms = (self.act_sumsq / self.act_n) ** 0.5 if self.act_n else 0.0
        rpeak = self.robust_peak if self.robust_peak > 0 else self.peak
        # act_rms_at_pep: the modem's active mean power if peak-normalized to full scale off the
        # FAIR (transient-excluded) peak — the number the cross-modem calibration should compare.
        act_rms_at_pep = act_rms * (32768.0 / rpeak) if rpeak > 0 else act_rms
        tmp = self.stats_path + ".tmp"
        stats = {"peak": int(self.peak), "robust_peak": int(rpeak), "rms": rms,
                 "act_rms": act_rms, "act_rms_at_pep": act_rms_at_pep,
                 "duty": self.act_n / self.nsamp, "n": int(self.nsamp),
                 "clip_frac": self.nclip / self.nsamp,
                 "rail_frac": self.nrail / self.nsamp, "gain": self.gain, "sigma": self.sigma,
                 "papr_db": (20.0 * np.log10(self.peak / act_rms) if act_rms > 0 else 0.0),
                 "papr_robust_db": (20.0 * np.log10(rpeak / act_rms) if act_rms > 0 else 0.0),
                 "half_duplex": HALF_DUPLEX, "keyed_now": self.keyed, "rig_gen": RIG_GEN,
                 "key_duty": (self.keyed_blocks / self.nblocks if self.nblocks else 0.0)}
        if SIM_CLOCK == "virt_time":
            stats["virtual_s"] = VIRT_NOW_S    # drivers score virtual goodput off this
        with open(tmp, "w") as f:
            json.dump(stats, f)
        os.replace(tmp, self.stats_path)


class _NoSrc:
    """Placeholder src_proc for transports that don't read a subprocess pipe."""
    stdout = None


class SockLink(Link):
    """One channel direction over the framed unix-socket transport
    (SIM_TRANSPORT=sock). The transform, keying and stats are the parent's,
    untouched; only block I/O differs: station->sim TX frames are read from the
    source station's socket (PTT in-band when the station provides it), sim->
    station RX frames are written to the sink station's socket. Across the two
    directions each socket has exactly one reader and one writer thread, so no
    lock is needed — the same single-writer discipline as the pipe transport."""

    def __init__(self, name, src_sock, sink_sock, seed, stats_path, stop,
                 src_name, sink_name, keys, ptt=None, fade=None,
                 link_delay_samp=0, rig_tx=None, rig_rx=None, fx=None,
                 squelch=None, gain=None, sigma=None):
        super().__init__(name, _NoSrc(), -1, seed, stats_path, stop,
                         src_name, sink_name, keys, ptt, fade, link_delay_samp,
                         rig_tx, rig_rx, fx, squelch, gain=gain, sigma=sigma)
        self.src_file = src_sock.makefile("rb")
        self.sink_sock = sink_sock
        self.tx_seq = 0

    def read_block(self):
        try:
            hdr = sock_frames.recv_into(self.src_file, sock_frames.HDR_STA, self.inmv)
        except (EOFError, ValueError, OSError) as e:
            print(f"channel_sim {self.name}: sock read: {e}", file=sys.stderr, flush=True)
            return False
        if hdr is None:
            return False                    # station closed cleanly
        _seq, ptt_v, n = hdr
        if n != BLOCK:
            print(f"channel_sim {self.name}: frame n={n} != BLOCK={BLOCK}",
                  file=sys.stderr, flush=True)
            return False
        # Block-exact in-band PTT (the virtual-rig replacement for the stdin
        # relay). ptt=255 = station can't see the modem's PTT (the stage-1
        # shim): leave PttState to the stdin listener, exactly as before.
        if ptt_v != sock_frames.PTT_UNKNOWN and self.ptt is not None:
            setattr(self.ptt, self.src_name, ptt_v == 1)
        return True

    def write_block(self):
        # Stage 1 keeps wall time in the clock field; stage 2 replaces this
        # with the lockstep virtual clock.
        now_ms = int(time.time() * 1000)
        try:
            self.sink_sock.sendall(
                sock_frames.pack_sim(self.tx_seq, now_ms, BLOCK, self.outbuf))
        except OSError:
            return False
        self.tx_seq += 1
        return True


def _sock_rig(procs):
    """SIM_TRANSPORT=sock: bind + accept both station sockets (spawning the
    sock<->ALSA shims first when SIM_SOCK_SHIM=1, so the existing harnesses run
    unchanged). Returns (conn_a, conn_b, closeables)."""
    os.makedirs(SOCK_DIR, exist_ok=True)
    lst = {}
    for st in ("a", "b"):
        path = os.path.join(SOCK_DIR, st + ".sock")
        if len(os.fsencode(path)) >= _SUN_PATH_MAX:
            raise RuntimeError(
                f"socket path too long: '{path}' is {len(os.fsencode(path))} "
                f"bytes but this platform caps AF_UNIX paths at "
                f"{_SUN_PATH_MAX - 1}; set SIM_SOCK_DIR to a shorter directory "
                f"(default /tmp/simsock-<pid>)")
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(path)
        s.listen(1)
        s.settimeout(SOCK_ACCEPT_S)
        lst[st] = s
    if SOCK_SHIM:
        import skywave
        here = os.path.dirname(os.path.abspath(__file__))
        shim = os.path.join(here, "sock_alsa_shim.py")
        cenv = skywave.child_env()          # src root on PYTHONPATH for a source checkout
        for st in ("a", "b"):
            # inherits our process group -> the harness's killpg teardown
            # reaches the shims and their arecord/aplay children, as before
            procs.append(sp.Popen(
                [sys.executable, "-u", shim, "--station", st,
                 "--sock", os.path.join(SOCK_DIR, st + ".sock")],
                env=cenv, stderr=sys.stderr))
    conns = {}
    for st in ("a", "b"):
        try:
            c, _ = lst[st].accept()
        except socket.timeout:
            raise RuntimeError(
                f"station '{st}' did not connect within {SOCK_ACCEPT_S:.0f}s "
                f"(sock dir {SOCK_DIR}, shim={'on' if SOCK_SHIM else 'off'})")
        # Modest kernel buffers: mirror the old sim->aplay pipe capacity so the
        # transport adds comparable (not unbounded) buffering/latency.
        c.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCK_BUF)
        c.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCK_BUF)
        conns[st] = c
    return conns["a"], conns["b"], list(lst.values()) + [conns["a"], conns["b"]]


def run_lockstep(ab, ba, ptt, stop):
    """SIM_CLOCK=virt_time block-lockstep loop (single thread, no wall pacing).

    Per block: send both stations their RX frame, BARRIER on both TX replies,
    set PttState from the in-band header, run both directions' keying updates
    (tx_shape) and THEN both deliver gates — a consistent same-block keying
    snapshot. The two Links' transform chains, stats, and rng streams are the
    wall rig's, untouched. Returns 0 on a clean end (station EOF or virtual
    timeout)."""
    global VIRT_NOW_S
    silence = bytes(NBYTES)
    out_a, out_b = silence, silence      # next RX block for each station
    max_v_ms = int(MAX_VIRTUAL_S * 1000) if MAX_VIRTUAL_S > 0 else None
    k = 0
    while not stop.is_set():
        vnow_ms = ((k + 1) * BLOCK * 1000) // FS     # block END time, exact
        if max_v_ms is not None and vnow_ms > max_v_ms:
            print(f"channel_sim: VIRTUAL-TIMEOUT at {vnow_ms / 1000.0:.1f}s "
                  f"(SIM_MAX_VIRTUAL_S={MAX_VIRTUAL_S:g})", file=sys.stderr, flush=True)
            break
        try:
            ba.sink_sock.sendall(sock_frames.pack_sim(k, vnow_ms, BLOCK, out_a))
            ab.sink_sock.sendall(sock_frames.pack_sim(k, vnow_ms, BLOCK, out_b))
        except OSError as e:
            print(f"channel_sim: lockstep send: {e}", file=sys.stderr, flush=True)
            break
        # Barrier: both stations must answer block k before time advances.
        died = False
        for L in (ab, ba):
            try:
                hdr = sock_frames.recv_into(L.src_file, sock_frames.HDR_STA, L.inmv)
            except (EOFError, ValueError, OSError) as e:
                print(f"channel_sim: lockstep {L.name}: {e}", file=sys.stderr, flush=True)
                died = True
                break
            if hdr is None or hdr[2] != BLOCK:
                print(f"channel_sim: lockstep {L.name}: station ended "
                      f"(hdr={hdr})", file=sys.stderr, flush=True)
                died = True
                break
            if hdr[1] != sock_frames.PTT_UNKNOWN:
                setattr(ptt, L.src_name, hdr[1] == 1)
        if died:
            break
        # Keying for BOTH directions first, then both deliver gates.
        w_ab = ab.tx_shape()
        w_ba = ba.tx_shape()
        ab.deliver_block(w_ab)
        ba.deliver_block(w_ba)
        out_b = bytes(ab.outbuf)             # A->B output is B's next RX
        out_a = bytes(ba.outbuf)             # B->A output is A's next RX
        ab.nblocks += 1
        ba.nblocks += 1
        k += 1
        VIRT_NOW_S = vnow_ms / 1000.0
        if STATS:                            # every block: drivers poll virtual_s
            ab.write_stats()
            ba.write_stats()
    VIRT_NOW_S = (k * BLOCK * 1000) // FS / 1000.0   # end of the last completed block
    ab.write_stats()
    ba.write_stats()
    return 0


def build_channel_effects():
    """Build the per-direction fade / rig / fx / squelch objects from the module
    config -- the exact construction main() used inline, extracted so the in-process
    Channel API shares ONE builder with the subprocess. Reads
    module globals only (no args). Returns a SimpleNamespace of the built objects plus
    the banner descs, OR the int 2 on a config error (already printed to stderr exactly
    as main() did inline), so main() propagates the exit code unchanged."""
    # Resolve the Watterson fade: SIM_FADE_SCHEDULE beats an explicit
    # doppler/delay override beats the named preset.
    fade_ab = fade_ba = None
    fdelay = fdop = None
    fade_desc = "fade=off"
    if FADE_SCHEDULE:
        from skywave import watterson
        segs = []
        for tok in FADE_SCHEDULE.split(","):
            name, _, secs = tok.strip().partition(":")
            name = name.strip().lower()
            if name not in watterson.PRESETS:
                print(f"channel_sim: unknown fade segment '{name}' in "
                      f"SIM_FADE_SCHEDULE (use {list(watterson.PRESETS)})",
                      file=sys.stderr, flush=True)
                return 2
            segs.append((name, float(secs) if secs.strip() else 0.0))

        def _mk_transition_logger(direction):
            def _log(t_s, frm, to):
                print(f"channel_sim: [fade-schedule {direction}] t={t_s:.2f}s "
                      f"{frm} -> {to}", file=sys.stderr, flush=True)
            return _log
        fade_ab = watterson.ScheduledFade(FS, segs, FADE_DUR_S, FADE_SEED + 11,
                                          FADE_XFADE_S, _mk_transition_logger("A->B"))
        fade_ba = watterson.ScheduledFade(FS, segs, FADE_DUR_S, FADE_SEED + 22,
                                          FADE_XFADE_S, _mk_transition_logger("B->A"))
        fade_desc = ("fade=schedule[" + ",".join(f"{n}:{s:g}" for n, s in segs)
                     + f"]xf={FADE_XFADE_S:g}s")
    elif FADE_DOPPLER and FADE_DELAY:
        fdelay, fdop = float(FADE_DELAY), float(FADE_DOPPLER)
        fade_name = "custom"
    elif WATTERSON != "off":
        from skywave import watterson
        p = watterson.PRESETS.get(WATTERSON)
        if p is None:
            print(f"channel_sim: unknown SIM_WATTERSON='{WATTERSON}' (use {list(watterson.PRESETS)})",
                  file=sys.stderr, flush=True)
            return 2
        fdelay, fdop = p
        fade_name = WATTERSON
    if fdop is not None:
        from skywave import watterson
        fade_ab = watterson.WattersonChannel(FS, fdelay, fdop, FADE_DUR_S, FADE_SEED + 11)
        fade_ba = watterson.WattersonChannel(FS, fdelay, fdop, FADE_DUR_S, FADE_SEED + 22)
        fade_desc = f"fade={fade_name}({fdelay}ms/{fdop}Hz)"

    # Resolve the rig SSB passband. Each direction gets its OWN stateful TX + RX filter
    # (4 total; the state must not be shared across legs). Same band both ends (symmetric
    # link); a real link cascades the transmitter's TX filter and the receiver's RX filter.
    rig_band = _resolve_rig_band()
    rig_desc = "rig_bpf=off"
    rig_ab_tx = rig_ab_rx = rig_ba_tx = rig_ba_rx = None
    sql_ab = sql_ba = None
    noise_lpf_ab = noise_lpf_ba = None
    if FM_PORT == "off" and (FM_FADE != "off" or FM_SHADOW != "off"
                             or FM_NOISE_BW != "off"):
        print("channel_sim: SIM_FM_FADE/SIM_FM_SHADOW/SIM_FM_NOISE_BW need "
              "SIM_FM_PORT set (the FM port owns these axes)",
              file=sys.stderr, flush=True)
        return 2
    if FM_PORT != "off":
        # The FM port profile OWNS the rig_tx/rig_rx
        # slots. Explicit HF knobs alongside it are a config conflict, not a
        # silent override (provenance doctrine); the DEFAULT SIM_RIG_BPF
        # ('data', env unset) is superseded with a banner note instead.
        if FM_PORT not in ("micspk", "data9600"):
            print(f"channel_sim: unknown SIM_FM_PORT='{FM_PORT}' "
                  "(use off|micspk|data9600)", file=sys.stderr, flush=True)
            return 2
        if os.environ.get("SIM_RIG_BPF", "").strip().lower() not in ("", "off"):
            print("channel_sim: SIM_FM_PORT replaces the SSB rig BPF stage — "
                  "unset SIM_RIG_BPF (or set it to 'off')",
                  file=sys.stderr, flush=True)
            return 2
        if WATTERSON != "off" or FADE_SCHEDULE or (FADE_DOPPLER and FADE_DELAY):
            print("channel_sim: the Watterson fade knobs (SIM_WATTERSON/"
                  "SIM_FADE_SCHEDULE/SIM_FADE_DOPPLER) are the HF channel — "
                  "the FM fade axis is SIM_FM_FADE (+SIM_FM_SHADOW)",
                  file=sys.stderr, flush=True)
            return 2
        from skywave import fm_rig
        rig_ab_tx = fm_rig.FmPortTx(FS, FM_PORT, FM_ORDER, FM_CTCSS_HZ, FM_CTCSS_AMP)
        rig_ba_tx = fm_rig.FmPortTx(FS, FM_PORT, FM_ORDER, FM_CTCSS_HZ, FM_CTCSS_AMP)
        rig_ab_rx = fm_rig.FmPortRx(FS, FM_PORT, FM_ORDER)
        rig_ba_rx = fm_rig.FmPortRx(FS, FM_PORT, FM_ORDER)
        if FM_PORT == "micspk" and FM_SQL == "gated":
            _tone_ms = FM_SQL_TONE_MS if FM_CTCSS_HZ > 0 else 0.0
            sql_ab = fm_rig.SquelchGate(FS, BLOCK, FM_SQL_OPEN_MS, _tone_ms,
                                        FM_SQL_TAIL_MS, FM_SQL_TAIL_AMP,
                                        FM_SQL_THRESH, SEED + 55)
            sql_ba = fm_rig.SquelchGate(FS, BLOCK, FM_SQL_OPEN_MS, _tone_ms,
                                        FM_SQL_TAIL_MS, FM_SQL_TAIL_AMP,
                                        FM_SQL_THRESH, SEED + 66)
            _sq = (f" sql=gated(open{FM_SQL_OPEN_MS:g}ms"
                   + (f"+tone{_tone_ms:g}ms" if _tone_ms else "")
                   + (f",tail{FM_SQL_TAIL_MS:g}ms@{FM_SQL_TAIL_AMP:g}"
                      if FM_SQL_TAIL_MS else "") + ")")
        else:
            _sq = " sql=off"
        _ct = (f" ctcss={FM_CTCSS_HZ:g}Hz@{FM_CTCSS_AMP:g}"
               if FM_CTCSS_HZ > 0 else "")
        if FM_PORT == "micspk":
            rig_desc = (f"fm_port=micspk(emph,300-3000Hz,ord{FM_ORDER})"
                        f"{_sq}{_ct}")
        else:
            rig_desc = "fm_port=data9600(flat) sql=none"
        # Tier A: FM flat fade + shadowing occupy the fade slot
        # (mutually exclusive with the Watterson knobs, checked above).
        if FM_FADE != "off" or FM_SHADOW != "off":
            from skywave import fm_channel
            try:
                fspec = fm_channel.resolve_fade_spec(FM_FADE, FM_BAND)
                shspec = fm_channel.resolve_shadow_spec(FM_SHADOW)
            except ValueError as e:
                print(f"channel_sim: {e}", file=sys.stderr, flush=True)
                return 2
            kind, fd, kdb, depth, rate, shape = ("static", 0.0, 0.0, 0.0,
                                                 0.0, "sin")
            fdesc = "static"
            if fspec is not None:
                kind, fd, kdb, depth, rate, shape, fdesc = fspec
            ssig, stau = shspec if shspec is not None else (0.0, 0.0)
            fade_ab = fm_channel.FmFade(FS, kind, FADE_DUR_S, FADE_SEED + 11,
                                        fd, kdb, depth, rate, ssig, stau,
                                        ionos_shape=shape)
            fade_ba = fm_channel.FmFade(FS, kind, FADE_DUR_S, FADE_SEED + 22,
                                        fd, kdb, depth, rate, ssig, stau,
                                        ionos_shape=shape)
            fade_desc = f"fm_fade={fdesc}"
            if shspec is not None:
                fade_desc += f" fm_shadow={ssig:g}dB/tau{stau:g}s"
        if FM_NOISE_BW != "off":
            from skywave import fm_channel
            try:
                _bw = float(FM_NOISE_BW)
                noise_lpf_ab = fm_channel.NoiseShaper(FS, _bw, NCH)
                noise_lpf_ba = fm_channel.NoiseShaper(FS, _bw, NCH)
            except ValueError as e:
                print(f"channel_sim: {e}", file=sys.stderr, flush=True)
                return 2
            rig_desc += f" fm_noise_bw={_bw:g}Hz(ionos-fir)"
    elif rig_band is not None:
        lo, hi = rig_band
        rig_ab_tx = RigBPF(lo, hi, RIG_ORDER, FS)
        rig_ab_rx = RigBPF(lo, hi, RIG_ORDER, FS)
        rig_ba_tx = RigBPF(lo, hi, RIG_ORDER, FS)
        rig_ba_rx = RigBPF(lo, hi, RIG_ORDER, FS)
        label = "custom" if (RIG_LO and RIG_HI) else RIG_BPF
        rig_desc = f"rig_bpf={label}({lo:.0f}-{hi:.0f}Hz,ord{RIG_ORDER})"

    # Per-direction effects bundles (independent state). Differential
    # knobs get opposite signs per direction; the impulsive-noise calibration is
    # shared (stateless after init); QRM gets its own rng per direction so the
    # noise-stream determinism is untouched.
    import types as _types
    fx_ab = _types.SimpleNamespace(alc=None, foff=None, skew=None, agc=None,
                                   imp=None, qrm=None)
    fx_ba = _types.SimpleNamespace(alc=None, foff=None, skew=None, agc=None,
                                   imp=None, qrm=None)
    fx_desc = []
    _foff_ramp = float(FOFF_RAMP_HZ) if FOFF_RAMP_HZ else None
    if any((FOFF_HZ, _foff_ramp, CLOCK_PPM, ALC_DB, RX_AGC, NOISE_VD,
            QRM_OCC, QRM_SWEEP)):
        from skywave import rig_effects as fxm
        if ALC_DB:
            fx_ab.alc = fxm.AlcOvershoot(FS, ALC_DB, ALC_SETTLE_MS, nch=NCH,
                                         rearm_s=_alc_rearm_s)
            fx_ba.alc = fxm.AlcOvershoot(FS, ALC_DB, ALC_SETTLE_MS, nch=NCH,
                                         rearm_s=_alc_rearm_s)
            _tag = f"alc={ALC_PRESET}" if ALC_PRESET != "off" else "alc"
            fx_desc.append(f"{_tag}=+{ALC_DB:g}dB/{ALC_SETTLE_MS:g}ms"
                           + (f"/rearm{_alc_rearm_s:g}s" if _alc_rearm_s else ""))
        if FOFF_HZ or _foff_ramp is not None:
            fx_ab.foff = fxm.FreqShift(FS, +FOFF_HZ, ramp_to_hz=_foff_ramp,
                                       ramp_s=FOFF_RAMP_S)
            fx_ba.foff = fxm.FreqShift(FS, -FOFF_HZ, ramp_to_hz=_foff_ramp,
                                       ramp_s=FOFF_RAMP_S)
            if _foff_ramp is not None:
                fx_desc.append(f"foff=+-{FOFF_HZ:g}->{_foff_ramp:g}Hz/{FOFF_RAMP_S:g}s")
            else:
                fx_desc.append(f"foff=+-{FOFF_HZ:g}Hz")
        if CLOCK_PPM:
            fx_ab.skew = fxm.ClockSkew(FS, +CLOCK_PPM, CLOCK_SLACK_MS)
            fx_ba.skew = fxm.ClockSkew(FS, -CLOCK_PPM, CLOCK_SLACK_MS)
            fx_desc.append(f"ppm=+-{CLOCK_PPM:g}")
        if RX_AGC:
            fx_ab.agc = fxm.RxAgc(FS, RX_AGC_ATTACK_MS, RX_AGC_RELEASE_MS,
                                  RX_AGC_TARGET, RX_AGC_MAXGAIN_DB)
            fx_ba.agc = fxm.RxAgc(FS, RX_AGC_ATTACK_MS, RX_AGC_RELEASE_MS,
                                  RX_AGC_TARGET, RX_AGC_MAXGAIN_DB)
            _atag = RX_AGC_MODE if RX_AGC_MODE in AGC_PRESETS else "rx_agc"
            fx_desc.append(f"{_atag}={RX_AGC_ATTACK_MS:g}/{RX_AGC_RELEASE_MS:g}ms")
        if NOISE_VD and (SIGMA_AB > 0.0 or SIGMA_BA > 0.0):
            # Per-direction impulsive noise: each direction's mixture is calibrated to its
            # OWN floor (Vd is a shape metric; total power tracks that sigma). Default
            # SIGMA_AB==SIGMA_BA => two identical mixtures (same fixed-RNG calibration),
            # behaviourally the same as the prior single shared object.
            fx_ab.imp = fxm.ImpulsiveNoise(SIGMA_AB, NOISE_VD, NOISE_VD_K_DB)
            fx_ba.imp = fxm.ImpulsiveNoise(SIGMA_BA, NOISE_VD, NOISE_VD_K_DB)
            fx_desc.append(f"noise_vd={NOISE_VD:g}dB(p={fx_ab.imp.p:.2g})")
        if QRM_OCC or QRM_SWEEP:
            # Rail-budget gate: the
            # channel is linear until the pad+guard, so the QRM allowance must
            # fit under the pre-pad rail with worst-case signal + noise. Fail
            # loud, never clamp below the requested median.
            # Rail-budget gate on the WORST-CASE floor (higher sigma => less rail room),
            # so the conservative cap holds for both directions when they differ.
            _gate_sigma = max(SIGMA_AB, SIGMA_BA)
            if _gate_sigma <= 0.0:
                print("channel_sim: SIM_QRM_* is sigma-relative and needs "
                      "SIGMA>0 (the qrm cell axis) — refusing to run inert",
                      file=sys.stderr, flush=True)
                return 2
            _fading = WATTERSON != "off" or bool(FADE_SCHEDULE)
            _room = qrm_rail_room_amp(_gate_sigma, RX_PAD, _fading)
            _sw_amp = qrm_amp(_gate_sigma, QRM_SWEEP_INR_DB) if QRM_SWEEP else 0.0
            _cw_room = _room - _sw_amp
            if (_room <= 0.0 or _sw_amp > _room
                    or (QRM_OCC and _cw_room <= qrm_amp(_gate_sigma, QRM_INR_DB))):
                print(f"channel_sim: QRM rail budget exhausted (room "
                      f"{_room:.0f} vs median amp "
                      f"{qrm_amp(_gate_sigma, QRM_INR_DB):.0f} + sweep "
                      f"{_sw_amp:.0f} at sigma={_gate_sigma:g}, rx_pad="
                      f"{RX_PAD_DB:g}dB, fading={_fading}) — deepen "
                      "SIM_RX_PAD_DB or lower SIM_QRM_INR_DB "
                      "",
                      file=sys.stderr, flush=True)
                return 2
            _qrm_cap = QRM_INR_MAX_DB
            if QRM_OCC:
                _qrm_cap = min(QRM_INR_MAX_DB,
                               20.0 * math.log10(_cw_room / (_gate_sigma * math.sqrt(2.0))))
            fx_ab.qrm = fxm.QrmGenerator(
                FS, np.random.default_rng(SEED + 33), SIGMA_AB,
                occupancy=QRM_OCC, inr_db=QRM_INR_DB,
                inr_spread_db=QRM_INR_SPREAD_DB, inr_max_db=_qrm_cap,
                sweep=QRM_SWEEP, sweep_inr_db=QRM_SWEEP_INR_DB,
                sweep_rate=QRM_SWEEP_RATE, sweep_band_hz=QRM_SWEEP_BAND_HZ)
            fx_ba.qrm = fxm.QrmGenerator(
                FS, np.random.default_rng(SEED + 44), SIGMA_BA,
                occupancy=QRM_OCC, inr_db=QRM_INR_DB,
                inr_spread_db=QRM_INR_SPREAD_DB, inr_max_db=_qrm_cap,
                sweep=QRM_SWEEP, sweep_inr_db=QRM_SWEEP_INR_DB,
                sweep_rate=QRM_SWEEP_RATE, sweep_band_hz=QRM_SWEEP_BAND_HZ)
            _qtag = (f"qrm=occ{QRM_OCC:g}@{QRM_INR_DB:g}dB"
                     f"(+-{QRM_INR_SPREAD_DB:g},cap{_qrm_cap:.1f})"
                     if QRM_OCC else "qrm")
            _swtag = (f"+sweep@{QRM_SWEEP_INR_DB:g}dB/"
                      f"{QRM_SWEEP_BAND_HZ / 1000.0:g}kHz"
                      f"x{QRM_SWEEP_RATE:g}" if QRM_SWEEP else "")
            fx_desc.append(_qtag + _swtag)

    return _types.SimpleNamespace(
        fade_ab=fade_ab,
        fade_ba=fade_ba,
        rig_ab_tx=rig_ab_tx,
        rig_ab_rx=rig_ab_rx,
        rig_ba_tx=rig_ba_tx,
        rig_ba_rx=rig_ba_rx,
        sql_ab=sql_ab,
        sql_ba=sql_ba,
        noise_lpf_ab=noise_lpf_ab,
        noise_lpf_ba=noise_lpf_ba,
        fx_ab=fx_ab,
        fx_ba=fx_ba,
        fade_desc=fade_desc,
        rig_desc=rig_desc,
        fx_desc=fx_desc,
    )


def main():
    procs = []
    stop = threading.Event()

    def _stop(_sig, _frm):
        stop.set()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    sp_a = (STATS + ".11") if STATS else ""
    sp_b = (STATS + ".22") if STATS else ""
    keys = Keys()
    ptt = PttState()
    if SIM_PTT:
        threading.Thread(target=ptt_listener, args=(ptt,), name="ptt", daemon=True).start()

    eff = build_channel_effects()
    if isinstance(eff, int):
        return eff        # config error already printed to stderr by the builder
    fade_ab, fade_ba = eff.fade_ab, eff.fade_ba
    rig_ab_tx, rig_ab_rx = eff.rig_ab_tx, eff.rig_ab_rx
    rig_ba_tx, rig_ba_rx = eff.rig_ba_tx, eff.rig_ba_rx
    sql_ab, sql_ba = eff.sql_ab, eff.sql_ba
    noise_lpf_ab, noise_lpf_ba = eff.noise_lpf_ab, eff.noise_lpf_ba
    fx_ab, fx_ba = eff.fx_ab, eff.fx_ba
    fade_desc, rig_desc, fx_desc = eff.fade_desc, eff.rig_desc, eff.fx_desc

    if SIM_CLOCK == "virt_time" and (TRANSPORT != "sock" or SOCK_SHIM):
        print("channel_sim: SIM_CLOCK=virt_time requires SIM_TRANSPORT=sock with "
              "virtual-clock stations (a station's --audio sock backend), not the ALSA "
              "transport or the wall-paced shim (SIM_SOCK_SHIM)",
              file=sys.stderr, flush=True)
        return 2

    closeables = []
    if TRANSPORT == "sock":
        try:
            conn_a, conn_b, closeables = _sock_rig(procs)
        except RuntimeError as e:
            print(f"channel_sim: {e}", file=sys.stderr, flush=True)
            for p in procs:
                p.kill()
            return 2
        ab = SockLink("A->B", conn_a, conn_b, SEED + 11, sp_a, stop,
                      "a", "b", keys, ptt, fade_ab, LINK_DELAY_SAMP,
                      rig_ab_tx, rig_ab_rx, fx_ab,
                      squelch=sql_ab, gain=GAIN_A, sigma=SIGMA_AB)   # A TX -> B RX
        ba = SockLink("B->A", conn_b, conn_a, SEED + 22, sp_b, stop,
                      "b", "a", keys, ptt, fade_ba, LINK_DELAY_SAMP,
                      rig_ba_tx, rig_ba_rx, fx_ba,
                      squelch=sql_ba, gain=GAIN_B, sigma=SIGMA_BA)   # B TX -> A RX
    else:
        alsa_err = _platform.alsa_rig_error()
        if alsa_err:
            print(f"channel_sim: {alsa_err}", file=sys.stderr, flush=True)
            return 2
        rec_a = arecord(CAP_A); procs.append(rec_a)
        rec_b = arecord(CAP_B); procs.append(rec_b)
        play_a = aplay(PLAY_A); procs.append(play_a)
        play_b = aplay(PLAY_B); procs.append(play_b)
        ab = Link("A->B", rec_a, play_b.stdin.fileno(), SEED + 11, sp_a, stop,
                  "a", "b", keys, ptt, fade_ab, LINK_DELAY_SAMP,
                  rig_ab_tx, rig_ab_rx, fx_ab, squelch=sql_ab,
                  gain=GAIN_A, sigma=SIGMA_AB)   # A_TX -> B_RX
        ba = Link("B->A", rec_b, play_a.stdin.fileno(), SEED + 22, sp_b, stop,
                  "b", "a", keys, ptt, fade_ba, LINK_DELAY_SAMP,
                  rig_ba_tx, rig_ba_rx, fx_ba, squelch=sql_ba,
                  gain=GAIN_B, sigma=SIGMA_BA)   # B_TX -> A_RX
    ab.noise_lpf = noise_lpf_ab
    ba.noise_lpf = noise_lpf_ba
    keying = "PTT" if SIM_PTT else f"VOX(thresh={KEY_THRESH:.0f})"
    mode = (f"half-duplex keying={keying} hang={HANG_MS:.0f}ms={HANG_BLOCKS}blk"
            if HALF_DUPLEX else "full-duplex passthrough")
    tr = f"tr_key={TR_KEY_MS:.0f}ms={TR_KEY_BLOCKS}blk tr_unkey={TR_UNKEY_MS:.0f}ms={TR_UNKEY_BLOCKS}blk"
    if TR_JITTER_MS > 0:
        tr += f" tr_jitter=±{TR_JITTER_MS:.0f}ms(seeded per-edge)"
    chan = f"{fade_desc} {rig_desc} link_delay={LINK_DELAY_MS:.0f}ms"
    if fx_desc:
        chan += " fx[" + ",".join(fx_desc) + "]"
    pad_desc = f"rx_pad={RX_PAD_DB:g}dB"
    env_desc = (f" noise_env={NOISE_ENV}@{BAND_MHZ:g}MHz" if NOISE_ENV != "off"
                else "")
    chan += f" {pad_desc}{env_desc}"
    transport = (f"sock({SOCK_DIR},shim={'on' if SOCK_SHIM else 'off'})"
                 if TRANSPORT == "sock" else "alsa")
    if SIM_CLOCK == "virt_time":
        transport += f" clock=virt_time(max={MAX_VIRTUAL_S:g}s)"
    _level_desc = (f"gain(A/B)={GAIN_A:g}/{GAIN_B:g} sigma(AB/BA)={SIGMA_AB:g}/{SIGMA_BA:g} ASYM"
                   if ASYM else f"gain={GAIN:g} sigma={SIGMA:g}")
    _prof_desc = f"profile={_PROFILE_NAME}  " if _PROFILE_NAME else ""
    print(f"channel_sim[gen{RIG_GEN}]: {_prof_desc}transport={transport}  {mode}  {tr}  {chan}  "
          f"block={BLOCK}f/{BLOCK_MS:.1f}ms  {_level_desc}",
          file=sys.stderr, flush=True)

    gc.disable()
    t_ab = t_ba = None
    try:
        if SIM_CLOCK == "virt_time":
            run_lockstep(ab, ba, ptt, stop)
        else:
            t_ab = threading.Thread(target=ab.run, name="A->B", daemon=True)
            t_ba = threading.Thread(target=ba.run, name="B->A", daemon=True)
            t_ab.start(); t_ba.start()
            while not stop.is_set():
                stop.wait(0.5)
    finally:
        stop.set()
        for p in procs:                      # unblock any thread parked in read/write
            try:
                p.kill()
            except (OSError, ProcessLookupError):
                pass
        for s in closeables:                 # unblock sock threads parked in recv
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass
        if t_ab is not None:
            t_ab.join(timeout=2.0)
        if t_ba is not None:
            t_ba.join(timeout=2.0)
        if VERBOSE:
            period_ms = 1000.0 * BLOCK / FS
            for L in (ab, ba):
                p99 = (sorted(L.times)[int(len(L.times) * 0.99)] if L.times else 0.0)
                print(f"channel_sim {L.name}: {L.nblocks} blocks, period={period_ms:.1f}ms, "
                      f"p99={p99:.2f}ms worst={L.worst:.2f}ms", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
