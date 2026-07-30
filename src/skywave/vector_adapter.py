#!/usr/bin/env python3
"""VectorAdapter -- the Device-Under-Test contract for PHY *mode* characterization.

WHY A SECOND ADAPTER FAMILY. `skywave.modem_adapter.ModemAdapter` is a LINK-level
contract: start_stations -> wait_ready -> link_connect -> transfer. It measures a
modem as a system (ARQ, turnaround, CSMA, goodput over a real link). That is the
wrong instrument for characterizing MODES: an ARQ layer's ACKs ride the data
mode, retransmission integrates over channel draws, CSMA airtime is charged to
the mode, and a transfer deadline turns "slow" into "failed". None of those are
properties of the mode.

Mode characterization is one-way: per-mode frame-decode probability vs SNR, one
frame per trial, zero retries. Goodput, floors, and ladder design all derive from
that curve analytically. So this is a FRAME CODEC contract, not a link contract:
no stations, no connection, no PTT, no wall-clock timing. The two families share
exactly one thing -- `skywave.watterson` -- and nothing else.

THE CONTRACT IS FILES, NOT AN API. An adapter is any program that can write a
sample vector plus a JSON sidecar, and read them back. That deliberately keeps
adapters language-agnostic: modem73's is a C++ binary, armstrong's is a Rust
`cargo run --example`, and a libcodec2 one could be pure Python. There is no FFI
and no in-process contract to satisfy.

  vector    raw float32, mono, little-endian, nominally +-1.0. Sample RATE is
            declared in the sidecar, NOT assumed -- armstrong's PHY runs at
            8 kHz and modem73's at 48 kHz, and the channel stage reads it.
  sidecar   flat JSON, schema below. Locates every frame in the vector so the
            channel can fade each one independently and measure signal power
            over frame regions only.
  outcome   one dict per decode run (see `decode`), which the sweep aggregates
            into a CSV row.

REQUIRED SIDECAR FIELDS
  label           str   canonical mode name, CSV-safe (no commas)
  sample_rate     int   Hz
  payload_bytes   int   bytes delivered per successfully decoded frame
  frames          int   number of frames in the vector
  seed            int   payload seed; the decoder regenerates expected bytes
  frame_offsets   [int] sample index of each frame start, ascending
  frame_lengths   [int] sample count of each frame

RECOMMENDED (consumed when present)
  mode_id, bandwidth_hz, air_s, gap_samples, flush_samples, lead_samples,
  rms_dbfs, peak_dbfs, papr_db, norm

Every adapter inherits `selftest()`, which is a REQUIRED gate, not a courtesy: a
clean round trip must decode every frame, and a deep-noise round trip must decode
none. A harness that only ever reports 0% is indistinguishable from a broken one,
so the negative leg is what makes the positive leg mean anything.
"""
import abc
import json
import os
import struct

import numpy as np

# Canonical on-disk sample format for vectors.
VECTOR_DTYPE = np.float32

REQUIRED_SIDECAR = (
    "label", "sample_rate", "payload_bytes", "frames", "seed",
    "frame_offsets", "frame_lengths",
)


class VectorContractError(Exception):
    """An adapter produced something the contract does not allow."""


# ---------------------------------------------------------------- vector io

def write_vector(path, samples):
    """Write float32 mono. Accepts any array-like; i16-scaled input should be
    normalized by the adapter BEFORE calling (see normalize_i16)."""
    np.asarray(samples, dtype=VECTOR_DTYPE).tofile(path)


def read_vector(path):
    return np.fromfile(path, dtype=VECTOR_DTYPE)


def normalize_i16(samples):
    """i16-domain samples -> float32 in +-1.0. armstrong's `phy::modem::tx`
    returns i16; keeping the on-disk format single-typed means the channel and
    analysis layers never branch on the modem."""
    return np.asarray(samples, dtype=np.float64) / 32768.0


# ---------------------------------------------------------------- payloads

def gen_payload(seed, frame_idx, length):
    """Deterministic in (seed, frame_idx) so a decoder can regenerate the
    expected bytes instead of the sidecar carrying them. The frame index goes in
    the clear in bytes 0..3: a frame that passes CRC but lands on the WRONG index
    is then diagnosable instead of just 'mismatch'.

    Adapters in other languages must reproduce this exactly -- it is part of the
    contract. xorshift32, seeded as below.
    """
    out = bytearray(length)
    i = 0
    if length >= 4:
        out[0:4] = struct.pack(">I", frame_idx & 0xFFFFFFFF)
        i = 4
    x = (seed ^ (0x9E3779B9 * (frame_idx + 1))) & 0xFFFFFFFF
    if x == 0:
        x = 0x1234567
    while i < length:
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= x >> 17
        x ^= (x << 5) & 0xFFFFFFFF
        x &= 0xFFFFFFFF
        out[i] = x & 0xFF
        i += 1
    return bytes(out)


# ---------------------------------------------------------------- sidecar

def validate_sidecar(side, vector_len=None):
    """Raise VectorContractError on anything the channel/sweep layers cannot
    consume. Called by the sweep on every encode, so a broken adapter fails on
    its first cell instead of producing a plausible-looking campaign."""
    missing = [k for k in REQUIRED_SIDECAR if k not in side]
    if missing:
        raise VectorContractError(f"sidecar missing required field(s): {missing}")
    n = int(side["frames"])
    offs = list(side["frame_offsets"])
    lens = list(side["frame_lengths"])
    if n <= 0:
        raise VectorContractError(f"frames must be > 0, got {n}")
    if len(offs) != n or len(lens) != n:
        raise VectorContractError(
            f"frames={n} but {len(offs)} offsets and {len(lens)} lengths")
    if int(side["sample_rate"]) <= 0:
        raise VectorContractError("sample_rate must be > 0")
    if int(side["payload_bytes"]) <= 0:
        raise VectorContractError("payload_bytes must be > 0")
    if "," in str(side["label"]):
        raise VectorContractError(f"label must be CSV-safe: {side['label']!r}")
    prev_end = -1
    for i, (o, ln) in enumerate(zip(offs, lens)):
        if ln <= 0:
            raise VectorContractError(f"frame {i} length {ln} <= 0")
        if o <= prev_end:
            raise VectorContractError(
                f"frame {i} starts at {o}, overlapping the previous frame "
                f"which ends at {prev_end}")
        prev_end = o + ln - 1
    if vector_len is not None and prev_end >= vector_len:
        raise VectorContractError(
            f"last frame ends at sample {prev_end} but the vector is only "
            f"{vector_len} samples -- the encoder truncated it")
    return True


def load_sidecar(path):
    with open(path) as f:
        return json.load(f)


def save_sidecar(path, side):
    with open(path, "w") as f:
        json.dump(side, f, indent=2)


# ---------------------------------------------------------------- adapter

class VectorAdapter(abc.ABC):
    """One implementation per modem. See adapters/vector_modem73.py (external
    binary) and adapters/vector_armstrong.py (cargo example) for the two shapes.
    """

    #: short lowercase identifier, used in filenames and CSV rows
    name = "unnamed"

    # ---- required hooks --------------------------------------------------

    @abc.abstractmethod
    def list_modes(self):
        """-> list of dicts, one per mode this modem can transmit.

        Required keys: label, payload_bytes, sample_rate, air_s.
        Recommended: mode_id, family, bandwidth_hz, rms_dbfs, peak_dbfs,
        papr_db, crc_bits.

        `air_s` and the level stats should be MEASURED from real encoder output,
        not derived from a formula -- the level figures drive the power
        normalization decision and a formula cannot see clipping or TX filtering.

        `family` is the WITHIN-modem class (e.g. ofdm / rdm / mfsk, or
        datac / qam / plh). Report the modem's own classes; do not collapse them
        to the adapter name, which `adapter` already carries. The frontier census
        is per-family, so collapsing them deletes the analysis.

        `crc_bits` is the width of the payload integrity check the modem applies
        to THIS mode. It is what makes the false_decode gate meaningful: a
        CRC-16 mode admits ~1 in 65,536 of the corrupt frames that reach its
        check, so a zero-tolerance gate measures CRC width rather than
        instrument health. Report the width of the check that actually
        adjudicates the payload -- not a stronger check applied at some other
        layer, and not a narrower header check. Omit it if genuinely unknown;
        the scorer then falls back to zero tolerance for that mode and says so.
        """

    @abc.abstractmethod
    def encode(self, label, frames, seed, outdir, **kw):
        """Write `frames` frames of mode `label` into outdir.

        -> (vector_path, sidecar_path). The sidecar must satisfy
        validate_sidecar(); the sweep checks it on every cell.
        """

    @abc.abstractmethod
    def decode(self, vector_path, sidecar_path, cold=False):
        """Decode and score against the sidecar's expected payloads.

        -> dict with at least:
             frames, decoded, false_decode
           and optionally:
             wrong_frame, duplicates, mean_snr_db, mean_ber, sync_count,
             crc_errors, plus any modem-specific counters (they are carried
             through to the CSV as extra columns).

        `decoded` counts frames whose delivered payload matched its expected
        bytes exactly. `false_decode` counts deliveries that passed the modem's
        own CRC but matched NO expected frame -- the metric a link-level test
        can never see, and the one that catches a blind mode-detection ladder
        mislabelling a frame.

        `cold=True` means: give each frame a receiver with no memory of the
        previous one. Implement it by CONSTRUCTING a fresh decoder per frame,
        not by calling a reset method -- reset often leaves last-good-mode
        state behind, which lets a warm decoder ride a previous frame's mode
        through a damaged header and inflates the decode rate exactly at the
        knee being measured.
        """

    # ---- provided --------------------------------------------------------

    def modes_by_label(self):
        return {m["label"]: m for m in self.list_modes()}

    def provenance(self):
        """-> short string identifying the exact driver that produced the rows.

        Recorded per row as `driver_id`. `host` and `arch` pin the machine but
        not the binary, and a campaign that silently swapped drivers mid-run
        would look clean. Override with a content hash of the executable (see
        the adapters). Empty means "not reported", which is a caveat rather than
        an invalidation -- an adapter may have no single binary to hash.
        """
        return ""

    def selftest(self, outdir, labels=None, frames=3, fail_snr_db=-30.0,
                 bw_hz=None, verbose=True):
        """REQUIRED gate. Clean round trip must be perfect; a deep-noise round
        trip must be zero. Returns the number of failed cells (0 == pass).

        `fail_snr_db` defaults to -30 dB, deep enough to defeat even narrow
        low-rate modes. Do not raise it casually: -6 dB in 2500 Hz is +1 dB
        inside a 500 Hz mode's own band, which such a mode decodes perfectly --
        the noise-bandwidth confound bites the selftest first.
        """
        from skywave import vector_channel as vc

        os.makedirs(outdir, exist_ok=True)
        avail = self.modes_by_label()
        if labels is None:
            labels = list(avail)[:4]
        failures = 0
        for label in labels:
            if label not in avail:
                if verbose:
                    print(f"SELFTEST {label:<28} UNKNOWN MODE")
                failures += 1
                continue
            vec, side = self.encode(label, frames, 7, outdir)
            s = load_sidecar(side)
            validate_sidecar(s, vector_len=read_vector(vec).size)

            clean = self.decode(vec, side)
            clean_ok = int(clean.get("decoded", 0)) == frames

            noisy_path = os.path.join(outdir, "selftest-noisy.f32")
            ref_bw = bw_hz or s.get("bandwidth_hz") or 2500.0
            vc.apply(vec, side, noisy_path, preset="off",
                     snr_db=fail_snr_db, bw_hz=float(ref_bw), seed=11)
            noisy = self.decode(noisy_path, side)
            noisy_ok = int(noisy.get("decoded", 0)) == 0

            if verbose:
                print(f"SELFTEST {label:<28} clean "
                      f"{clean.get('decoded')}/{frames} "
                      f"{'PASS' if clean_ok else 'FAIL'}  "
                      f"noisy({fail_snr_db:.0f}dB) {noisy.get('decoded')} "
                      f"{'PASS' if noisy_ok else 'FAIL'}")
            if not (clean_ok and noisy_ok):
                failures += 1
        if verbose:
            print(f"SELFTEST: {failures} cell(s) failed")
        return failures
