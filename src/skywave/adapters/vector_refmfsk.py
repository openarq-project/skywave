#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""REFARM: a frozen, fully-classical, independently-implemented reference
VectorAdapter -- MFSK + CA-SCL polar, self-contained (numpy only).

Design doc (pre-registered, read BEFORE this file was written):
    ~/Documents/openarq/reviews/REFARM-DESIGN-2026-08-25.md

PURPOSE. A stable cross-delivery review anchor and a runnable, public
artifact anyone can execute: clean-room textbook DSP, no armstrong
internals, no third-party material. It is deliberately a PHYSICS-BOUND
instrument -- the vector harness is frame-synchronous and zero-CFO by
construction, so this adapter measures detector + code physics only.

MODES (mirrors a collaborator delivery's geometry; exact integer sample
grids at fs=8000 Hz, carrier centered at 1500 Hz):

  mode   M   samp/sym   Rs=df       data_syms  coded_bits  N(mother)  E
  ref_s  8   168        47.619 Hz   11         33          64         33
  ref_m  16  264        30.303 Hz   16         64          64         64
  ref_l  16  280        28.571 Hz   33         132         256        132

Tone k sits at 1500 + (k - (M-1)/2)*df. Because df == Rs, a rectangular
per-symbol window's DFT lands each tone exactly on an FFT bin (orthogonal,
matched filter) -- see `_bin_base`. Bits map to tone index via a Gray code
(adjacent tones differ by one bit) and phase is carried continuously across
symbol boundaries (constant envelope; no pulse shaping in v1). Bursts are
DATA ONLY: no preamble, no sync -- `air_s` is the true burst length.

CODE (pinned). Info = 4 payload bits + CRC-11 (3GPP 38.212 g(D) =
D^11+D^10+D^9+D^5+1) = 15 bits. CA-SCL polar, list L=8, mother N=64 (s,m)
or N=256 (l), rate-matched by SHORTENING: only coded positions 0..E-1 are
transmitted (natural order); positions E..N-1 are never sent and are known
to the decoder as frozen-0 (LLR = +inf). The information set is the K=15
most-reliable positions from the 3GPP 38.212 universal reliability
sequence (Table 5.3.1.2-1, Q_0^1023), restricted to the transmitted range
[0, E) -- see `build_polar_sets`. A fixed PN mask whitens the 15
info+CRC bits before encoding, and a decoded candidate whose RAW (still
masked) info word is all-zero is explicitly rejected regardless of what
its CRC says: an all-zero info word means every info AND frozen bit came
out 0, the classic degenerate "zero-codeword" hypothesis that trivially
satisfies any linear code/CRC no matter what was actually sent, and it is
refused unconditionally rather than trusted to fail the mask/CRC
combination by luck. A genuine payload of 0000 does not trip this guard:
its true (masked) info word is MASK15 itself, not zero.

SHORTENING DERIVATION (why "positions E..N-1 frozen" is exactly right, not
approximate). This adapter encodes with the natural-order (non-bit-reversed)
Arikan transform x = u @ F^{\\otimes n}, implemented recursively as
`v1 = u1 XOR u2; v2 = u2; x = concat(encode(v1), encode(v2))` where u1, u2
are the low/high halves of u. That matrix is lower-triangular under the
bitwise-subset partial order: x_j = XOR of u_i over every i that is a
bitwise SUPERSET of j, and bitwise superset implies i >= j. So if u_i = 0
for every i >= E, then for every j >= E, every i contributing to x_j
satisfies i >= j >= E and is therefore already zero, forcing x_j = 0
identically -- the SAME index range does both jobs (freezing inputs >= E
guarantees the corresponding outputs >= E are always 0), which is why
"transmit 0..E-1, freeze E..N-1" is self-consistent without a bit-reversal
permutation or a separate rate-matching pattern.

RX (pinned). Per-symbol DFT at the M tone bins, noncoherent powers
m_k = |X_k|^2. Per-burst noise sigma^2 = mean of the (M-1) non-argmax bin
powers, averaged over the whole burst. Bitwise max-log LLR:
    LLR_b = (max_{k: bit_b(k)=0} m_k - max_{k: bit_b(k)=1} m_k) / sigma^2 * C
with C a single constant calibrated once on AWGN (see CALIBRATED_C below)
and then frozen for good.

CONTRACT DETAILS. `payload_bytes` is 1 on the wire (contract minimum), but
only the LOW 4 BITS of that byte are ever encoded or decoded -- the high
nibble is never transmitted and is reported back as 0 on RX. Attribution is
POSITIONAL, not content-addressed (documented deviation, same rationale as
`vector_swctrl.py`'s: a 4-bit alphabet makes content-addressing unreliable
by construction -- a wrong nibble collides with some other frame's expected
value about 1 time in 16 by chance). `decoded` at position i requires BOTH
CRC-11 pass and the recovered nibble equal to `expected(i)`'s low nibble;
`crc_bits: 11` is reported so vector_report's false-decode gate is armed
correctly.

Pre-registered gates B1-B5 live in the design doc; this file exists to pass
them, not to re-litigate them.
"""
import hashlib
import math
import os

import numpy as np

from skywave.vector_adapter import VectorAdapter, gen_payload
from skywave.vector_channel import HILBERT_TAPS as _HILBERT_TAPS

# Coarse-alignment compensation for a shared-harness property, not a REFARM
# design choice: skywave.vector_channel's Watterson fading stage is a causal
# FIR Hilbert filter, so ANY preset other than "off" delays its whole output
# by exactly (HILBERT_TAPS-1)//2 samples relative to the frame_offsets the
# sidecar declares -- documented in docs/VECTOR-ADAPTER-CONTRACT.md ("Group
# delay ... identical across all cells, so it biases nothing") but only
# actually harmless to a receiver that either (a) never sees fading, or
# (b) re-acquires timing itself. A truly frame-synchronous, no-acquisition
# receiver (this one, by design -- see the module docstring) reading exactly
# [offset, offset+length) under a fading preset instead reads ~76% of the
# PREVIOUS symbol and ~24% of the current one for ref_s's geometry, which
# is indistinguishable from noise-limited failure until you plot argmax(s)
# against truth(s-1) and see it line up. Since HILBERT_TAPS is a single
# shared constant (not per-mode, per-preset, or per-call), the true content
# sits at EXACTLY one of two candidate offsets -- 0 (preset "off") or this
# gdelay (any fading preset) -- so a two-way blind pick, scored on the data
# symbols' own peak-vs-runner-up margin (no dedicated preamble, no ground
# truth needed), resolves it. See `_align_and_demod`.
_SYNC_GDELAY = (_HILBERT_TAPS - 1) // 2

FS = 8000.0
CARRIER_HZ = 1500.0
AMPL = 0.9                 # nominal +-1.0 vector convention, small headroom

# ---------------------------------------------------------------------------
# 3GPP TS 38.212 Table 5.3.1.2-1 -- the universal polar reliability sequence
# Q_0^1023, LEAST reliable bit-channel index first, MOST reliable last.
# Verified (not just transcribed): permutation of 0..1023, and it satisfies
# the polar partial order for every one of the ~1.05M ordered pairs (i, j)
# with i's bits a bitwise subset of j's bits: index i then always precedes
# index j in this list, as any valid reliability sequence must.
# ---------------------------------------------------------------------------
Q1024 = (
    0, 1, 2, 4, 8, 16, 32, 3, 5, 64, 9, 6, 17, 10, 18, 128,
    12, 33, 65, 20, 256, 34, 24, 36, 7, 129, 66, 512, 11, 40, 68, 130,
    19, 13, 48, 14, 72, 257, 21, 132, 35, 258, 26, 513, 80, 37, 25, 22,
    136, 260, 264, 38, 514, 96, 67, 41, 144, 28, 69, 42, 516, 49, 74, 272,
    160, 520, 288, 528, 192, 544, 70, 44, 131, 81, 50, 73, 15, 320, 133, 52,
    23, 134, 384, 76, 137, 82, 56, 27, 97, 39, 259, 84, 138, 145, 261, 29,
    43, 98, 515, 88, 140, 30, 146, 71, 262, 265, 161, 576, 45, 100, 640, 51,
    148, 46, 75, 266, 273, 517, 104, 162, 53, 193, 152, 77, 164, 768, 268, 274,
    518, 54, 83, 57, 521, 112, 135, 78, 289, 194, 85, 276, 522, 58, 168, 139,
    99, 86, 60, 280, 89, 290, 529, 524, 196, 141, 101, 147, 176, 142, 530, 321,
    31, 200, 90, 545, 292, 322, 532, 263, 149, 102, 105, 304, 296, 163, 92, 47,
    267, 385, 546, 324, 208, 386, 150, 153, 165, 106, 55, 328, 536, 577, 548, 113,
    154, 79, 269, 108, 578, 224, 166, 519, 552, 195, 270, 641, 523, 275, 580, 291,
    59, 169, 560, 114, 277, 156, 87, 197, 116, 170, 61, 531, 525, 642, 281, 278,
    526, 177, 293, 388, 91, 584, 769, 198, 172, 120, 201, 336, 62, 282, 143, 103,
    178, 294, 93, 644, 202, 592, 323, 392, 297, 770, 107, 180, 151, 209, 284, 648,
    94, 204, 298, 400, 608, 352, 325, 533, 155, 210, 305, 547, 300, 109, 184, 534,
    537, 115, 167, 225, 326, 306, 772, 157, 656, 329, 110, 117, 212, 171, 776, 330,
    226, 549, 538, 387, 308, 216, 416, 271, 279, 158, 337, 550, 672, 118, 332, 579,
    540, 389, 173, 121, 553, 199, 784, 179, 228, 338, 312, 704, 390, 174, 554, 581,
    393, 283, 122, 448, 353, 561, 203, 63, 340, 394, 527, 582, 556, 181, 295, 285,
    232, 124, 205, 182, 643, 562, 286, 585, 299, 354, 211, 401, 185, 396, 344, 586,
    645, 593, 535, 240, 206, 95, 327, 564, 800, 402, 356, 307, 301, 417, 213, 568,
    832, 588, 186, 646, 404, 227, 896, 594, 418, 302, 649, 771, 360, 539, 111, 331,
    214, 309, 188, 449, 217, 408, 609, 596, 551, 650, 229, 159, 420, 310, 541, 773,
    610, 657, 333, 119, 600, 339, 218, 368, 652, 230, 391, 313, 450, 542, 334, 233,
    555, 774, 175, 123, 658, 612, 341, 777, 220, 314, 424, 395, 673, 583, 355, 287,
    183, 234, 125, 557, 660, 616, 342, 316, 241, 778, 563, 345, 452, 397, 403, 207,
    674, 558, 785, 432, 357, 187, 236, 664, 624, 587, 780, 705, 126, 242, 565, 398,
    346, 456, 358, 405, 303, 569, 244, 595, 189, 566, 676, 361, 706, 589, 215, 786,
    647, 348, 419, 406, 464, 680, 801, 362, 590, 409, 570, 788, 597, 572, 219, 311,
    708, 598, 601, 651, 421, 792, 802, 611, 602, 410, 231, 688, 653, 248, 369, 190,
    364, 654, 659, 335, 480, 315, 221, 370, 613, 422, 425, 451, 614, 543, 235, 412,
    343, 372, 775, 317, 222, 426, 453, 237, 559, 833, 804, 712, 834, 661, 808, 779,
    617, 604, 433, 720, 816, 836, 347, 897, 243, 662, 454, 318, 675, 618, 898, 781,
    376, 428, 665, 736, 567, 840, 625, 238, 359, 457, 399, 787, 591, 678, 434, 677,
    349, 245, 458, 666, 620, 363, 127, 191, 782, 407, 436, 626, 571, 465, 681, 246,
    707, 350, 599, 668, 790, 460, 249, 682, 573, 411, 803, 789, 709, 365, 440, 628,
    689, 374, 423, 466, 793, 250, 371, 481, 574, 413, 603, 366, 468, 655, 900, 805,
    615, 684, 710, 429, 794, 252, 373, 605, 848, 690, 713, 632, 482, 806, 427, 904,
    414, 223, 663, 692, 835, 619, 472, 455, 796, 809, 714, 721, 837, 716, 864, 810,
    606, 912, 722, 696, 377, 435, 817, 319, 621, 812, 484, 430, 838, 667, 488, 239,
    378, 459, 622, 627, 437, 380, 818, 461, 496, 669, 679, 724, 841, 629, 351, 467,
    438, 737, 251, 462, 442, 441, 469, 247, 683, 842, 738, 899, 670, 783, 849, 820,
    728, 928, 791, 367, 901, 630, 685, 844, 633, 711, 253, 691, 824, 902, 686, 740,
    850, 375, 444, 470, 483, 415, 485, 905, 795, 473, 634, 744, 852, 960, 865, 693,
    797, 906, 715, 807, 474, 636, 694, 254, 717, 575, 913, 798, 811, 379, 697, 431,
    607, 489, 866, 723, 486, 908, 718, 813, 476, 856, 839, 725, 698, 914, 752, 868,
    819, 814, 439, 929, 490, 623, 671, 739, 916, 463, 843, 381, 497, 930, 821, 726,
    961, 872, 492, 631, 729, 700, 443, 741, 845, 920, 382, 822, 851, 730, 498, 880,
    742, 445, 471, 635, 932, 687, 903, 825, 500, 846, 745, 826, 732, 446, 962, 936,
    475, 853, 867, 637, 907, 487, 695, 746, 828, 753, 854, 857, 504, 799, 255, 964,
    909, 719, 477, 915, 638, 748, 944, 869, 491, 699, 754, 858, 478, 968, 383, 910,
    815, 976, 870, 917, 727, 493, 873, 701, 931, 756, 860, 499, 731, 823, 922, 874,
    918, 502, 933, 743, 760, 881, 494, 702, 921, 501, 876, 847, 992, 447, 733, 827,
    934, 882, 937, 963, 747, 505, 855, 924, 734, 829, 965, 938, 884, 506, 749, 945,
    966, 755, 859, 940, 830, 911, 871, 639, 888, 479, 946, 750, 969, 508, 861, 757,
    970, 919, 875, 862, 758, 948, 977, 923, 972, 761, 877, 952, 495, 703, 935, 978,
    883, 762, 503, 925, 878, 735, 993, 885, 939, 994, 980, 926, 764, 941, 967, 886,
    831, 947, 507, 889, 984, 751, 942, 996, 971, 890, 509, 949, 973, 1000, 892, 950,
    863, 759, 1008, 510, 979, 953, 763, 974, 954, 879, 981, 982, 927, 995, 765, 956,
    887, 985, 997, 986, 943, 891, 998, 766, 511, 988, 1001, 951, 1002, 893, 975, 894,
    1009, 955, 1004, 1010, 957, 983, 958, 987, 1012, 999, 1016, 767, 989, 1003, 990, 1005,
    959, 1011, 1013, 895, 1006, 1014, 1017, 1018, 991, 1020, 1007, 1015, 1019, 1021, 1022, 1023,
)

# ---------------------------------------------------------------------------
# CRC-11, 3GPP 38.212 g(D) = D^11 + D^10 + D^9 + D^5 + 1 (degree-11..0 coeffs)
# ---------------------------------------------------------------------------
CRC11_POLY = (1, 1, 1, 0, 0, 0, 1, 0, 0, 0, 0, 1)


def crc11_remainder(bits):
    """Mod-2 polynomial division of `bits` (list/array of 0/1, len >= 11) by
    CRC11_POLY. -> list of 11 ints. Standard bitwise CRC long division: pad
    the message with 11 zero bits and divide to get the CRC (encode side);
    divide the full message+CRC as-is and expect an all-zero remainder
    (check side)."""
    reg = list(int(b) for b in bits)
    n = len(reg)
    for i in range(n - 11):
        if reg[i]:
            for j, p in enumerate(CRC11_POLY):
                reg[i + j] ^= p
    return reg[n - 11:]


def crc11_encode(payload4):
    """payload4: 4 bits (MSB first) -> 15-bit message (payload + CRC-11)."""
    crc = crc11_remainder(list(payload4) + [0] * 11)
    return list(payload4) + crc


# ---------------------------------------------------------------------------
# Fixed PN whitening mask over the 15 info+CRC bits (pinned constant).
# Arbitrary but FIXED, non-zero, roughly balanced (weight 8/15). Whitening
# means a decoder that degenerates toward the all-frozen (all-zero) pattern
# unmasks to this constant, not to zero, so it fails CRC instead of
# spuriously passing it; the residual case (decoded word IS this mask, so it
# unmasks to all-zero) is caught by the explicit all-zero-reject below.
# ---------------------------------------------------------------------------
MASK15 = [1, 1, 0, 1, 0, 0, 1, 0, 1, 1, 0, 1, 0, 0, 1]
assert len(MASK15) == 15 and any(MASK15)


def whiten15(bits15):
    return [b ^ m for b, m in zip(bits15, MASK15)]


# ---------------------------------------------------------------------------
# Polar code construction: frozen/info sets per (N, E), K=15 info bits.
# ---------------------------------------------------------------------------
K_INFO = 15
LIST_SIZE = 8
FROZEN_LLR = 1.0e6          # stand-in for +inf; safely > any real per-bit LLR


def build_polar_sets(N, E, K=K_INFO):
    """-> (info_positions sorted ascending by bit-index, frozen_set).

    `reachable` = the sub-sequence of Q1024 restricted first to values < N
    (the reliability order for a length-N mother code -- the standard
    "nested" restriction that makes one universal sequence work for every
    N), then further restricted to values < E (the transmitted-reachable
    positions after shortening -- see the module docstring's derivation).
    The K most-reliable of those become the information set; everything
    else (including the unconditionally-shortened tail E..N-1) is frozen.
    """
    seq_n = [v for v in Q1024 if v < N]
    reachable = [v for v in seq_n if v < E]
    if len(reachable) < K:
        raise ValueError(f"N={N} E={E}: only {len(reachable)} reachable "
                          f"positions, need K={K}")
    info = sorted(reachable[-K:])
    frozen = set(range(N)) - set(info)
    return tuple(info), frozen


def polar_encode(u):
    """x = u @ F^(kron n), natural order (no bit-reversal). u: array-like of
    0/1, length a power of 2. Recursive Plotkin form: v1 = u1^u2, v2 = u2,
    x = concat(encode(v1), encode(v2)); see the module docstring for why
    this exact split makes trailing-tail shortening self-consistent."""
    u = np.asarray(u, dtype=np.int64)
    n = u.size
    if n == 1:
        return u.copy()
    half = n // 2
    v1 = u[:half] ^ u[half:]
    v2 = u[half:]
    return np.concatenate([polar_encode(v1), polar_encode(v2)])


def _f_combine(a, b):
    """Min-sum approximation of the check-node LLR combine (box-plus)."""
    return np.sign(a) * np.sign(b) * np.minimum(np.abs(a), np.abs(b))


def _scl_recurse(llr, u, pm, lo, hi, frozen):
    """One node of the CA-SCL recursion. llr: (Lc, n); u: (Lc, N) decided
    bits so far; pm: (Lc,) path metrics (lower = better). Returns
    (x_hat, u, pm, mapping) where mapping[k] is the INPUT row (0..Lc-1)
    that output row k descends from -- needed by the caller to realign its
    own (a, b) LLR halves after this call may have branched/pruned/reordered
    the path set. See the module docstring's shortening note for why every
    leaf (including the shortened tail) is handled by the same frozen path,
    with no special-casing."""
    Lc, n = llr.shape
    if n == 1:
        idx = lo
        l = llr[:, 0]
        if idx in frozen:
            add = np.where(l >= 0, 0.0, np.abs(l))
            u2 = u.copy()
            u2[:, idx] = 0
            pm2 = pm + add
            xhat = np.zeros((Lc, 1), dtype=np.int64)
            mapping = np.arange(Lc)
            return xhat, u2, pm2, mapping
        add0 = np.where(l >= 0, 0.0, np.abs(l))
        add1 = np.where(l >= 0, np.abs(l), 0.0)
        u0 = u.copy(); u0[:, idx] = 0
        u1 = u.copy(); u1[:, idx] = 1
        u2 = np.concatenate([u0, u1], axis=0)
        pm2 = np.concatenate([pm + add0, pm + add1])
        xhat2 = np.concatenate(
            [np.zeros(Lc, dtype=np.int64), np.ones(Lc, dtype=np.int64)]
        ).reshape(-1, 1)
        mapping = np.concatenate([np.arange(Lc), np.arange(Lc)])
        if pm2.shape[0] > LIST_SIZE:
            keep = np.argsort(pm2, kind="stable")[:LIST_SIZE]
            u2, pm2, xhat2, mapping = u2[keep], pm2[keep], xhat2[keep], mapping[keep]
        return xhat2, u2, pm2, mapping

    m = n // 2
    a, b = llr[:, :m], llr[:, m:]
    fllr = _f_combine(a, b)
    x1, u, pm, map1 = _scl_recurse(fllr, u, pm, lo, lo + m, frozen)
    a2, b2 = a[map1], b[map1]
    gllr = b2 + (1.0 - 2.0 * x1) * a2
    x2, u, pm, map2 = _scl_recurse(gllr, u, pm, lo + m, hi, frozen)
    x1_aligned = x1[map2]
    xhat = np.concatenate([x1_aligned ^ x2, x2], axis=1)
    mapping_total = map1[map2]
    return xhat, u, pm, mapping_total


def ca_scl_decode(llr, N, frozen, info_positions):
    """CA-SCL decode. llr: length-N array (channel LLR, transmitted
    positions carrying real values, shortened tail already set to
    +FROZEN_LLR by the caller). -> list of (u_vector, pm) candidates,
    best (lowest pm) first."""
    llr = np.asarray(llr, dtype=np.float64).reshape(1, N)
    u0 = np.zeros((1, N), dtype=np.int64)
    pm0 = np.zeros(1, dtype=np.float64)
    _, u, pm, _ = _scl_recurse(llr, u0, pm0, 0, N, frozen)
    order = np.argsort(pm, kind="stable")
    return [(u[i, list(info_positions)].tolist(), float(pm[i])) for i in order]


# ---------------------------------------------------------------------------
# Gray mapping (bits <-> tone index)
# ---------------------------------------------------------------------------

def _gray_encode(v):
    return v ^ (v >> 1)


def _gray_decode(g):
    v = g
    shift = 1
    while (g >> shift):
        v ^= (g >> shift)
        shift += 1
    return v


def _bits_to_int(bits):
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return v


def _int_to_bits(v, n):
    return [(v >> (n - 1 - i)) & 1 for i in range(n)]


# ---------------------------------------------------------------------------
# Mode geometry (design doc section 3)
# ---------------------------------------------------------------------------

class ModeSpec:
    def __init__(self, label, M, samp_per_sym, data_syms, N, E, mode_id):
        self.label = label
        self.M = M
        self.nbits = int(round(math.log2(M)))
        self.samp_per_sym = samp_per_sym
        self.data_syms = data_syms
        self.Rs = FS / samp_per_sym
        self.N = N
        self.E = E
        self.mode_id = mode_id
        assert self.nbits * data_syms == E, (label, self.nbits, data_syms, E)
        self.tone_freqs = [CARRIER_HZ + (k - (M - 1) / 2.0) * self.Rs
                            for k in range(M)]
        base = CARRIER_HZ * samp_per_sym / FS - (M - 1) / 2.0
        assert abs(base - round(base)) < 1e-6, (label, base)
        self.bin_base = int(round(base))
        # tone k -> its Gray-decoded raw bit pattern (MSB-first bit list)
        self.tone_bits = [_int_to_bits(_gray_decode(k), self.nbits)
                           for k in range(M)]
        self.info_positions, self.frozen = build_polar_sets(N, E)
        self.air_s = data_syms * samp_per_sym / FS
        self.bandwidth_hz = M * self.Rs


MODES = {
    m.label: m for m in (
        ModeSpec("ref_s", M=8, samp_per_sym=168, data_syms=11, N=64, E=33, mode_id=1),
        ModeSpec("ref_m", M=16, samp_per_sym=264, data_syms=16, N=64, E=64, mode_id=2),
        ModeSpec("ref_l", M=16, samp_per_sym=280, data_syms=33, N=256, E=132, mode_id=3),
    )
}

# LLR scale constant C. Design doc: "calibrated once on AWGN and then
# frozen." Investigated (reviews/refarm-v1/check_oracle.py plus a dedicated
# invariance check, not shipped -- see the REFARM report) rather than picked
# arbitrarily: for THIS decoder, C is a global multiplicative factor applied
# identically to every bit LLR, and nothing downstream clips or thresholds
# an LLR's raw magnitude (frozen/shortened positions are looked up by
# POSITION via the `frozen` set, never by comparing against FROZEN_LLR's
# size). _f_combine and the g-update are both exactly linear in a uniform
# positive rescale of their inputs, so every LLR in the whole SCL tree scales
# by the same factor C; hard-decision signs and path-metric RANK ORDER (all
# that argsort-based pruning ever uses) are therefore invariant to C for any
# C > 0. Verified empirically: identical decode() output (frames, decoded,
# crc_errors) for C in {1e-6, 1e-3, 1, 1e3, 1e6} at both an easy and a
# knee-region SNR. So there is nothing to calibrate here -- C=1.0 is frozen
# as the simplest value, not tuned. (A design that clipped or saturated LLRs
# before combining, e.g. armstrong's own decoder, would NOT have this
# invariance and would need a real calibration; this one provably does.)
CALIBRATED_C = 1.0


# ---------------------------------------------------------------------------
# TX-side bit pipeline, shared verbatim by encode() and decode()'s
# ground-truth reconstruction so the two can never diverge.
# ---------------------------------------------------------------------------

def _tx_pipeline(mode, seed, frame_idx):
    """-> (payload_nibble:int, coded_bits:list[int] len E, tone_seq:list[int]
    len data_syms). Deterministic in (seed, frame_idx); reproduces exactly
    what encode() transmitted for this frame, from gen_payload alone."""
    payload_byte = gen_payload(seed, frame_idx, 1)[0]
    nibble = payload_byte & 0x0F
    payload4 = _int_to_bits(nibble, 4)
    msg15 = crc11_encode(payload4)
    w15 = whiten15(msg15)
    u = np.zeros(mode.N, dtype=np.int64)
    for pos, bit in zip(mode.info_positions, w15):
        u[pos] = bit
    x = polar_encode(u)
    coded_bits = x[:mode.E].tolist()
    tone_seq = []
    for s in range(mode.data_syms):
        chunk = coded_bits[s * mode.nbits:(s + 1) * mode.nbits]
        v = _bits_to_int(chunk)
        tone_seq.append(_gray_encode(v))
    return nibble, coded_bits, tone_seq


def _modulate(mode, tone_seq):
    Nw = mode.samp_per_sym
    n = np.arange(Nw)
    out = np.empty(Nw * mode.data_syms, dtype=np.float64)
    phase = 0.0
    for s, k in enumerate(tone_seq):
        f_k = mode.tone_freqs[k]
        seg = AMPL * np.sin(2.0 * np.pi * f_k * n / FS + phase)
        out[s * Nw:(s + 1) * Nw] = seg
        phase = (phase + 2.0 * np.pi * f_k * Nw / FS) % (2.0 * np.pi)
    return out


def _symbol_powers(mode, seg):
    """-> length-M array of noncoherent bin powers |X_k|^2 for one symbol
    window. Rectangular-window DFT at the exact tone bins (df == Rs makes
    every tone land on an FFT bin of a length-samp_per_sym FFT)."""
    spec = np.fft.fft(seg, n=mode.samp_per_sym)
    bins = spec[mode.bin_base:mode.bin_base + mode.M]
    return (bins.real ** 2 + bins.imag ** 2)


def _demod_burst(mode, burst):
    """-> (llr_E, argmax_seq, confidence) using the TRUE noise-only bins for
    sigma^2 (design section 5): per burst, mean of the (M-1) non-argmax bin
    powers, averaged over every symbol in the burst. `confidence` is the
    burst-summed (top bin - runner-up bin) margin -- large and positive when
    the window is correctly aligned to symbol boundaries (clean single-tone
    peaks), collapsing toward 0 when it is not (energy smeared across two
    adjacent tones' worth of two different symbols); see _SYNC_GDELAY."""
    Nw = mode.samp_per_sym
    noise_acc, noise_n = 0.0, 0
    all_powers = []
    argmax_seq = []
    margin_acc = 0.0
    for s in range(mode.data_syms):
        seg = burst[s * Nw:(s + 1) * Nw]
        p = _symbol_powers(mode, seg)
        all_powers.append(p)
        order = np.argsort(p)
        am = int(order[-1])
        argmax_seq.append(am)
        margin_acc += float(p[order[-1]] - p[order[-2]])
        noise_acc += (p.sum() - p[am])
        noise_n += (mode.M - 1)
    sigma2 = noise_acc / max(noise_n, 1)
    sigma2 = max(sigma2, 1e-30)
    llr = []
    for p in all_powers:
        group0 = [[] for _ in range(mode.nbits)]
        group1 = [[] for _ in range(mode.nbits)]
        for k in range(mode.M):
            bits_k = mode.tone_bits[k]
            for b in range(mode.nbits):
                (group0[b] if bits_k[b] == 0 else group1[b]).append(p[k])
        for b in range(mode.nbits):
            m0 = max(group0[b]) if group0[b] else 0.0
            m1 = max(group1[b]) if group1[b] else 0.0
            llr.append((m0 - m1) / sigma2 * CALIBRATED_C)
    return llr, argmax_seq, margin_acc / sigma2


def _align_and_demod(mode, window):
    """Try both candidate whole-burst alignments (see _SYNC_GDELAY) and keep
    whichever the data symbols themselves back more strongly. `window` must
    be at least burst_len + _SYNC_GDELAY samples (shorter windows -- e.g.
    right at the end of a vector with no trailing margin -- just skip the
    shifted candidate, matching a preset "off" vector where it can never
    win anyway)."""
    burst_len = mode.samp_per_sym * mode.data_syms
    best = None
    candidates = [0]
    if window.size >= burst_len + _SYNC_GDELAY:
        candidates.append(_SYNC_GDELAY)
    for shift in candidates:
        burst = window[shift:shift + burst_len]
        llr, argmax_seq, conf = _demod_burst(mode, burst)
        if best is None or conf > best[0]:
            best = (conf, llr, argmax_seq)
    return best[1], best[2]


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class RefMfskAdapter(VectorAdapter):
    name = "refmfsk"

    def list_modes(self, env=None):
        out = []
        for label, mode in MODES.items():
            vec, side = self._encode_frames(mode, frames=1, seed=1)
            frame = vec[side["frame_offsets"][0]:
                        side["frame_offsets"][0] + side["frame_lengths"][0]]
            rms = float(np.sqrt(np.mean(frame ** 2))) if frame.size else 0.0
            peak = float(np.max(np.abs(frame))) if frame.size else 0.0
            out.append({
                "label": label,
                "mode_id": mode.mode_id,
                "family": "ref-mfsk-polar",
                "crc_bits": 11,
                "payload_bytes": 1,
                "sample_rate": int(FS),
                "air_s": side["frame_lengths"][0] / FS,
                "bandwidth_hz": mode.bandwidth_hz,
                "nominal_bps": 4.0 / (side["frame_lengths"][0] / FS),
                "rms_dbfs": 20.0 * math.log10(max(rms, 1e-12)),
                "peak_dbfs": 20.0 * math.log10(max(peak, 1e-12)),
                "papr_db": 20.0 * math.log10(max(peak, 1e-12) / max(rms, 1e-12)),
                "M": mode.M,
                "samp_per_sym": mode.samp_per_sym,
                "data_syms": mode.data_syms,
                "N_mother": mode.N,
                "E_coded": mode.E,
            })
        return out

    def provenance(self):
        """md5 of this adapter file itself -- there is no separate binary to
        hash (this IS the driver)."""
        h = hashlib.md5()
        with open(os.path.abspath(__file__), "rb") as f:
            h.update(f.read())
        return h.hexdigest()[:12]

    # ---- internal: build a vector of `frames` bursts of `mode` ----------

    def _encode_frames(self, mode, frames, seed, gap_ms=300):
        gap_samples = max(1, int(round(gap_ms * FS / 1000.0)))
        burst_len = mode.samp_per_sym * mode.data_syms
        chunks = []
        frame_offsets, frame_lengths = [], []
        cursor = 0
        for i in range(frames):
            _, _, tone_seq = _tx_pipeline(mode, seed, i)
            burst = _modulate(mode, tone_seq)
            chunks.append(burst)
            frame_offsets.append(cursor)
            frame_lengths.append(burst_len)
            cursor += burst_len
            if i != frames - 1:
                chunks.append(np.zeros(gap_samples, dtype=np.float64))
                cursor += gap_samples
        vec = np.concatenate(chunks) if chunks else np.zeros(0)
        side = {
            "label": mode.label,
            "sample_rate": int(FS),
            "payload_bytes": 1,
            "frames": frames,
            "seed": seed,
            "frame_offsets": frame_offsets,
            "frame_lengths": frame_lengths,
            "mode_id": mode.mode_id,
            "bandwidth_hz": mode.bandwidth_hz,
            "air_s": burst_len / FS,
            "gap_samples": gap_samples,
            "norm": "peak",
        }
        return vec.astype(np.float32), side

    # ---- contract ---------------------------------------------------------

    def encode(self, label, frames, seed, outdir, gap_ms=300, env=None, **kw):
        if label not in MODES:
            raise ValueError(f"refmfsk: unknown mode {label!r}")
        mode = MODES[label]
        vec, side = self._encode_frames(mode, frames, seed, gap_ms=gap_ms)
        os.makedirs(outdir, exist_ok=True)
        vec_path = os.path.join(outdir, "clean.f32")
        side_path = os.path.join(outdir, "clean.json")
        from skywave.vector_adapter import save_sidecar, write_vector
        write_vector(vec_path, vec)
        save_sidecar(side_path, side)
        return vec_path, side_path

    def decode(self, vector_path, sidecar_path, cold=False):
        from skywave.vector_adapter import load_sidecar, read_vector
        side = load_sidecar(sidecar_path)
        mode = MODES[side["label"]]
        vec = read_vector(vector_path)
        seed = int(side["seed"])
        offsets = side["frame_offsets"]
        lengths = side["frame_lengths"]

        decoded = false_decode = crc_errors = 0
        syms = sym_errs = 0
        for i in range(int(side["frames"])):
            a = offsets[i]
            # Read with margin for the two-way alignment search (see
            # _SYNC_GDELAY); _align_and_demod degrades gracefully to the
            # unshifted candidate if the vector has no trailing margin here
            # (only possible for the very last frame).
            window = vec[a:min(a + lengths[i] + _SYNC_GDELAY, vec.size)] \
                .astype(np.float64)
            expected_nibble, expected_coded, expected_tones = _tx_pipeline(
                mode, seed, i)
            llr_e, argmax_seq = _align_and_demod(mode, window)
            syms += mode.data_syms
            sym_errs += sum(1 for t, e in zip(argmax_seq, expected_tones)
                             if t != e)

            llr_full = np.full(mode.N, FROZEN_LLR, dtype=np.float64)
            llr_full[:mode.E] = llr_e
            candidates = ca_scl_decode(llr_full, mode.N, mode.frozen,
                                       mode.info_positions)

            accepted = False
            any_crc_pass = False
            for w15_hat, _pm in candidates:
                if not any(w15_hat):
                    continue                          # explicit all-zero reject:
                    # w15_hat is the raw SCL info-word BEFORE unmasking. All
                    # info bits decoding to 0 is the classic degenerate
                    # "zero-codeword" hypothesis (every frozen AND info bit
                    # is then 0, satisfying any linear code/CRC trivially
                    # regardless of what was sent) -- rejected unconditionally
                    # here, independent of whether the mask/CRC combination
                    # would also have caught it. A genuine payload nibble of
                    # 0 does NOT trigger this: its true info word is MASK15
                    # (whitening a true message of all-zero bits), not zero.
                m15_hat = whiten15(w15_hat)          # mask is its own inverse
                if crc11_remainder(m15_hat) != [0] * 11:
                    continue
                any_crc_pass = True
                nibble_hat = _bits_to_int(m15_hat[:4])
                if nibble_hat == expected_nibble:
                    decoded += 1
                else:
                    false_decode += 1
                accepted = True
                break
            if not accepted:
                crc_errors += 1
            # any_crc_pass without accepted only happens via the all-zero
            # guard rejecting every CRC-passing candidate; already counted
            # as crc_errors above, which is the correct (safe) bucket.
            del any_crc_pass

        return {
            "frames": int(side["frames"]),
            "decoded": decoded,
            "false_decode": false_decode,
            "crc_errors": crc_errors,
            "wrong_frame": 0,
            "duplicates": 0,
            "sync_count": int(side["frames"]),
            "syms": syms,
            "sym_errs": sym_errs,
            "oracle_ser": f"{(sym_errs / syms) if syms else 0.0:.6f}",
        }


def build():
    return RefMfskAdapter()
