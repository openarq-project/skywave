

# ---- Option-A payload-region S + headroom guard (E1 boundary) --------------

def test_payload_region_s_excludes_the_head():
    import numpy as np
    from skywave.vector_channel import clean_signal_power, legacy_signal_power
    vec = np.zeros(120, dtype=np.float64)
    vec[0:50] = 0.5    # "head": loud, excluded by the Option-A region
    vec[50:100] = 0.1  # payload region
    side = {"frame_offsets": [0], "frame_lengths": [100],
            "payload_offsets": [50], "payload_lengths": [50]}
    s_new = clean_signal_power(vec, side)
    s_old = legacy_signal_power(vec, side)
    assert abs(s_new - 0.01) < 1e-12, "new S is payload-only mean square"
    assert s_old > 5 * s_new, "legacy S is inflated by the head"
    # sidecar without the arrays falls back to the legacy region
    side_legacy = {"frame_offsets": [0], "frame_lengths": [100]}
    assert clean_signal_power(vec, side_legacy) == s_old


def test_headroom_scales_down_only_and_preserves_ratios():
    import numpy as np
    from skywave.vector_channel import apply_headroom, HEADROOM_PEAK
    loud = np.array([0.5, -0.25, 0.1])
    out = apply_headroom(loud)
    assert abs(float(np.max(np.abs(out))) - HEADROOM_PEAK) < 1e-12
    # joint scale: element ratios (SNR) unchanged
    assert abs(out[1] / out[0] - loud[1] / loud[0]) < 1e-12
    quiet = np.array([0.001, -0.002])
    assert (apply_headroom(quiet) == quiet).all(), "never amplifies"
