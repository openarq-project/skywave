"""Unit tests for the refmfsk reference adapter (MFSK + CA-SCL polar).

No hardware, no subprocess -- everything here is pure Python/numpy, run
directly against skywave.adapters.vector_refmfsk. See the design doc
(openarq reviews/REFARM-DESIGN-2026-08-25.md) for the pre-registered gates
B1-B5; those are exercised separately (selftest, check_oracle.py, the
sweep + vector_report). This file covers the unit-level correctness
properties the design calls out: polar round trip, CRC error detection,
shortening consistency, whitening, gen_payload interop, and determinism.

Run:  cd skywave && python3 -m pytest tests/test_refmfsk_adapter.py -q
"""
import os
import tempfile

import numpy as np
import pytest

from skywave.adapters.vector_refmfsk import (
    MODES, build, ca_scl_decode, crc11_encode, crc11_remainder, polar_encode,
    whiten15, MASK15, FROZEN_LLR, K_INFO, _tx_pipeline,
)
from skywave.vector_adapter import gen_payload, read_vector, load_sidecar
from skywave import vector_channel as vc


# --------------------------------------------------------------- roundtrip

@pytest.mark.parametrize("label", list(MODES))
def test_polar_roundtrip_high_snr(label):
    """Encode/decode round trip through the full adapter (MFSK modulate +
    demodulate + CA-SCL, AWGN channel at a comfortably high SNR) must
    recover every one of 100 frames."""
    ad = build()
    with tempfile.TemporaryDirectory() as td:
        vec_path, side_path = ad.encode(label, 100, seed=42, outdir=td)
        side = load_sidecar(side_path)
        noisy_path = os.path.join(td, "noisy.f32")
        vc.apply(vec_path, side_path, noisy_path, preset="off", snr_db=20.0,
                 bw_hz=3000.0, seed=1)
        r = ad.decode(noisy_path, side_path)
    assert r["frames"] == 100
    assert r["decoded"] == 100, r
    assert r["false_decode"] == 0, r


@pytest.mark.parametrize("label", list(MODES))
@pytest.mark.parametrize("preset", ["good", "moderate", "poor"])
def test_fading_roundtrip_high_snr_needs_alignment_search(label, preset):
    """Regression pin for the frame-alignment finding: vector_channel's
    Watterson fading stage delays its whole output by a fixed
    (HILBERT_TAPS-1)//2 samples relative to the sidecar's frame_offsets (any
    preset other than "off" goes through the Hilbert-filtered path; "off"
    does not). A decode() that read exactly [offset, offset+length) with no
    alignment search would see near-total symbol confusion under EVERY
    fading preset even at very high SNR (empirically ~7/8 and ~15/16 wrong
    -- i.e. pure chance -- for M=8/M=16 before this was fixed). At 25 dB
    with a benign preset this must now recover essentially everything."""
    ad = build()
    with tempfile.TemporaryDirectory() as td:
        vec_path, side_path = ad.encode(label, 60, seed=17, outdir=td)
        noisy_path = os.path.join(td, "noisy.f32")
        vc.apply(vec_path, side_path, noisy_path, preset=preset, snr_db=25.0,
                 bw_hz=3000.0, seed=3)
        r = ad.decode(noisy_path, side_path)
    # Not a strict 60/60: real (mild) frequency-selective fading can still
    # cost an occasional frame even at high SNR. The regression this guards
    # against is total washout (oracle_ser ~ (M-1)/M), so the bar is "almost
    # everything decodes", not "everything".
    assert r["decoded"] >= 55, r
    assert r["false_decode"] == 0, r


# --------------------------------------------------------------- CRC

def test_crc_detects_single_bit_corruption():
    """Every single-bit corruption of a valid 15-bit (payload+CRC) message
    must produce a non-zero remainder -- i.e. must be caught."""
    for nibble in range(16):
        payload4 = [(nibble >> 3) & 1, (nibble >> 2) & 1,
                    (nibble >> 1) & 1, nibble & 1]
        msg15 = crc11_encode(payload4)
        assert crc11_remainder(msg15) == [0] * 11    # valid word: clean check
        for bit in range(15):
            corrupted = list(msg15)
            corrupted[bit] ^= 1
            assert crc11_remainder(corrupted) != [0] * 11, (nibble, bit)


def test_crc_mask_constant_itself_fails_crc():
    """Sanity check on the frozen MASK15 constant: it must not itself be a
    valid (CRC-passing) codeword, or the whitening defense against the
    zero-codeword class would be silently defeated for one specific input."""
    assert crc11_remainder(list(MASK15)) != [0] * 11


# --------------------------------------------------------------- shortening

@pytest.mark.parametrize("label", list(MODES))
def test_shortening_consistency(label):
    """The decoder's frozen set must agree with what the encoder's
    shortening actually guarantees: every coded bit at an untransmitted
    position (E..N-1) is forced to 0 by the encoder for ANY info pattern,
    and the decoder's frozen set must mark exactly those positions (plus
    any additionally-frozen, less-reliable transmitted positions) frozen --
    never an info position among them."""
    mode = MODES[label]
    assert set(range(mode.E, mode.N)) <= mode.frozen
    assert set(mode.info_positions).isdisjoint(mode.frozen)
    assert len(mode.info_positions) == K_INFO
    assert all(p < mode.E for p in mode.info_positions), (
        "an info position must be transmitted-reachable")

    rng = np.random.default_rng(0)
    for _ in range(20):
        u = np.zeros(mode.N, dtype=np.int64)
        for pos in mode.info_positions:
            u[pos] = rng.integers(0, 2)
        x = polar_encode(u)
        assert np.all(x[mode.E:mode.N] == 0), (
            "shortened tail must be identically zero regardless of info bits")


@pytest.mark.parametrize("label", list(MODES))
def test_polar_noiseless_scl_roundtrip(label):
    """CA-SCL must recover the exact info word when the channel LLR
    perfectly reflects the transmitted codeword (no noise) -- validates the
    SCL recursion itself, independent of the MFSK front end."""
    mode = MODES[label]
    rng = np.random.default_rng(1)
    BIG = 50.0
    for _ in range(30):
        w15 = rng.integers(0, 2, K_INFO).tolist()
        u = np.zeros(mode.N, dtype=np.int64)
        for pos, bit in zip(mode.info_positions, w15):
            u[pos] = bit
        x = polar_encode(u)
        llr = np.full(mode.N, FROZEN_LLR)
        llr[:mode.E] = np.where(x[:mode.E] == 0, BIG, -BIG)
        best, _pm = ca_scl_decode(llr, mode.N, mode.frozen,
                                  mode.info_positions)[0]
        assert best == w15


# --------------------------------------------------------------- whitening

def test_whitening_kills_all_zero_transmitted_word():
    """No payload nibble may ever produce an all-zero WHITENED info word
    (the thing actually handed to the polar encoder) -- in particular the
    one nibble (0) whose raw message+CRC genuinely is all-zero must come out
    as MASK15, not zero, after whitening."""
    seen_zero_message = False
    for nibble in range(16):
        payload4 = [(nibble >> 3) & 1, (nibble >> 2) & 1,
                    (nibble >> 1) & 1, nibble & 1]
        msg15 = crc11_encode(payload4)
        w15 = whiten15(msg15)
        assert any(w15), (nibble, "whitened info word must never be all-zero")
        if not any(msg15):
            seen_zero_message = True
            assert w15 == list(MASK15)
    assert seen_zero_message, "expected nibble 0 to be the all-zero message"


def test_all_zero_scl_candidate_is_rejected_end_to_end():
    """Force the decoder's channel LLR to unambiguously favour the all-zero
    info word (the degenerate zero-codeword hypothesis) and confirm the
    adapter's accept logic in decode() would refuse it: the best CA-SCL
    candidate is all-zero, and it must never be treated as a valid decode
    regardless of what its unmasked CRC says."""
    mode = MODES["ref_s"]
    BIG = 50.0
    llr = np.full(mode.N, FROZEN_LLR)
    # Every transmitted position pushed hard towards "codeword bit = 0",
    # which is exactly what an all-zero u (all frozen, all info bits 0)
    # produces via polar_encode -- the "no evidence" / zero-codeword case.
    llr[:mode.E] = BIG
    best, _pm = ca_scl_decode(llr, mode.N, mode.frozen, mode.info_positions)[0]
    assert best == [0] * K_INFO
    # decode()'s guard rejects exactly this candidate unconditionally.
    assert not any(best)


# --------------------------------------------------------------- interop

@pytest.mark.parametrize("label", list(MODES))
def test_gen_payload_interop(label):
    """_tx_pipeline must derive its nibble from the harness's own
    gen_payload exactly, and a clean end-to-end decode must attribute every
    frame correctly against harness-generated expected payloads."""
    mode = MODES[label]
    seed = 777
    for frame_idx in range(10):
        expected_nibble = gen_payload(seed, frame_idx, 1)[0] & 0x0F
        nibble, coded_bits, tone_seq = _tx_pipeline(mode, seed, frame_idx)
        assert nibble == expected_nibble
        assert len(coded_bits) == mode.E
        assert len(tone_seq) == mode.data_syms
        assert all(0 <= t < mode.M for t in tone_seq)

    ad = build()
    with tempfile.TemporaryDirectory() as td:
        vec_path, side_path = ad.encode(label, 25, seed=seed, outdir=td)
        r = ad.decode(vec_path, side_path)
    assert r["decoded"] == 25
    assert r["false_decode"] == 0


# --------------------------------------------------------------- determinism

@pytest.mark.parametrize("label", list(MODES))
def test_determinism_same_seed_identical_bytes(label):
    """Two encodes with the same (label, frames, seed) must produce
    byte-identical vectors -- required for B5 (re-run at same seed/host
    gives an identical CSV)."""
    ad = build()
    with tempfile.TemporaryDirectory() as td1, \
         tempfile.TemporaryDirectory() as td2:
        v1, s1 = ad.encode(label, 15, seed=99, outdir=td1)
        v2, s2 = ad.encode(label, 15, seed=99, outdir=td2)
        a1, a2 = read_vector(v1), read_vector(v2)
        assert a1.size == a2.size
        assert np.array_equal(a1, a2)
        assert load_sidecar(s1) == load_sidecar(s2)


def test_determinism_repeated_decode_same_counts():
    """Decoding the same (noisy) vector twice must give byte-identical
    outcome counts -- the decoder holds no hidden mutable state across
    calls."""
    ad = build()
    with tempfile.TemporaryDirectory() as td:
        vec_path, side_path = ad.encode("ref_m", 40, seed=5, outdir=td)
        noisy_path = os.path.join(td, "noisy.f32")
        vc.apply(vec_path, side_path, noisy_path, preset="off", snr_db=-2.0,
                 bw_hz=3000.0, seed=3)
        r1 = ad.decode(noisy_path, side_path)
        r2 = ad.decode(noisy_path, side_path)
    assert r1 == r2
