# Contract tests for the DART VectorAdapter (skywave/adapters/vector_dart.py).
#
# These need a Dart SDK and an HTCommander checkout, neither of which is a
# skywave dependency, so the whole module skips unless both are configured:
#
#   DART_SRC=/path/to/HTCommander   DART_SDK=/path/to/dart-sdk   pytest
#
# The skip is deliberate and deliberately LOUD in its reason: a silently
# skipped adapter suite reads as a passing one.

import os
import shutil

import pytest

from skywave.vector_adapter import (load_sidecar, read_vector, validate_sidecar,
                                    write_vector)

_SRC = os.environ.get("DART_SRC")
_SDK = os.environ.get("DART_SDK")


def _have_dart():
    if not _SRC:
        return False, "DART_SRC not set (path to an HTCommander checkout)"
    if _SDK and os.path.isfile(os.path.join(_SDK, "bin", "dart")):
        return True, ""
    if shutil.which("dart"):
        return True, ""
    return False, "no Dart SDK (set DART_SDK or put `dart` on PATH)"


_ok, _why = _have_dart()
pytestmark = pytest.mark.skipif(not _ok, reason=f"DART adapter: {_why}")


@pytest.fixture(scope="module")
def adapter():
    from skywave.adapters.vector_dart import build
    return build()


# ---- mode list ------------------------------------------------------------

def test_modes_are_complete_and_measured(adapter):
    modes = adapter.list_modes()
    assert len(modes) == 7, "six OFDM rungs plus the constant-envelope fallback"
    for m in modes:
        for k in ("label", "payload_bytes", "sample_rate", "air_s"):
            assert k in m, f"{m.get('label')} missing required key {k}"
        assert m["sample_rate"] == 32000
        assert m["air_s"] > 0
        assert "," not in m["label"], "label must be CSV-safe"
        # Levels must be MEASURED, not formula-derived: a formula cannot see
        # the peak normalization DartOfdm.toPcm applies per burst.
        assert -60 < m["rms_dbfs"] < 0 and -60 < m["peak_dbfs"] <= 0
        assert m["papr_db"] > 0
        # The check that adjudicates the PAYLOAD is the CRC-32, not the 16-bit
        # header CRC. Getting this wrong makes the false_decode gate measure
        # CRC width instead of instrument health.
        assert m["crc_bits"] == 32

    families = {m["family"] for m in modes}
    assert families == {"ofdm", "cpfsk"}, (
        "the SC-FDMA ladder and the constant-envelope fallback are different "
        "families; collapsing them deletes the per-family frontier census")


def test_labels_carry_the_payload_size(adapter):
    """DART's rate is dominated by fixed per-frame overhead, so a mode has no
    single throughput. The payload size is part of the mode identity."""
    pb = adapter.payload_bytes
    for m in adapter.list_modes():
        assert m["label"].endswith(f"_b{pb}")
        assert m["payload_bytes"] == pb


def test_nominal_bps_is_the_real_net_rate(adapter):
    """Not DART's published mode-table figure, which is asymptotic."""
    for m in adapter.list_modes():
        expect = m["payload_bytes"] * 8.0 / m["air_s"]
        assert abs(m["nominal_bps"] - expect) < 1e-6


# ---- encode / sidecar -----------------------------------------------------

@pytest.mark.parametrize("label_key", ["m0", "m2", "mF"])
def test_encode_produces_a_valid_sidecar(adapter, tmp_path, label_key):
    label = next(m["label"] for m in adapter.list_modes()
                 if m["label"].startswith(f"dart_{label_key}_"))
    vec_p, side_p = adapter.encode(label, 3, seed=99, outdir=str(tmp_path))
    side = load_sidecar(side_p)
    vec = read_vector(vec_p)
    validate_sidecar(side, vector_len=vec.size)   # raises on any violation
    assert side["frames"] == 3
    assert side["sample_rate"] == 32000
    assert side["seed"] == 99
    assert len(side["frame_offsets"]) == 3


def test_encode_is_deterministic_in_the_seed(adapter, tmp_path):
    label = adapter.list_modes()[1]["label"]
    outs = []
    for i in range(2):
        v, _ = adapter.encode(label, 2, seed=7, outdir=str(tmp_path / f"r{i}"))
        outs.append(read_vector(v))
    assert (outs[0] == outs[1]).all(), "same seed must give the same vector"


# ---- decode / scoring -----------------------------------------------------

@pytest.mark.parametrize("cold", [False, True])
def test_clean_channel_decodes_every_frame(adapter, tmp_path, cold):
    label = next(m["label"] for m in adapter.list_modes()
                 if m["label"].startswith("dart_m2_"))
    vec_p, side_p = adapter.encode(label, 4, seed=5, outdir=str(tmp_path))
    r = adapter.decode(vec_p, side_p, cold=cold)
    assert r["frames"] == 4
    assert r["decoded"] == 4, f"clean channel lost frames: {r}"
    assert r["false_decode"] == 0 and r["wrong_frame"] == 0
    assert r["sync_count"] == 4


def test_noise_destroys_frames_without_faking_decodes(adapter, tmp_path):
    """Negative control for the scoring path: if `decoded` stayed at 4 under
    obliterating noise, the adapter would be reporting its own expectations
    rather than what came back."""
    import numpy as np
    label = next(m["label"] for m in adapter.list_modes()
                 if m["label"].startswith("dart_m5_"))
    vec_p, side_p = adapter.encode(label, 4, seed=5, outdir=str(tmp_path))
    vec = read_vector(vec_p)
    rng = np.random.default_rng(0)
    noisy = str(tmp_path / "noisy.f32")
    write_vector(noisy, vec + rng.standard_normal(vec.size).astype("float32"))
    r = adapter.decode(noisy, side_p)
    assert r["decoded"] == 0, f"decoded frames out of pure noise: {r}"
    assert r["false_decode"] == 0, "a CRC-32 should not admit noise this easily"


def test_extras_are_integer_sum_and_count_not_means(adapter, tmp_path):
    """vector_sweep.accumulate_extra folds extras with int(v) and SUMS them
    across batches. A fractional mean would truncate to 0 and look measured;
    a per-batch mean would be added to another per-batch mean. Sum+count is
    the only shape that survives both."""
    label = next(m["label"] for m in adapter.list_modes()
                 if m["label"].startswith("dart_m0_"))
    vec_p, side_p = adapter.encode(label, 3, seed=5, outdir=str(tmp_path))
    r = adapter.decode(vec_p, side_p)
    core = {"frames", "decoded", "false_decode", "wrong_frame", "duplicates",
            "crc_errors", "sync_count", "mean_snr_db"}
    for k, v in r.items():
        if k in core:
            continue
        assert isinstance(v, int), f"extra {k}={v!r} must be int to survive int()"
        assert int(v) == v
    assert r["qual_n"] == 3
    corr = r["qual_corr_sum_x1000"] / 1000 / r["qual_n"]
    assert 0.5 < corr <= 1.01, f"clean-channel preamble correlation {corr}"


def test_provenance_pins_the_modem_under_test(adapter):
    p = adapter.provenance()
    assert p and (p.startswith("git:") or p.startswith("md5:")), p
