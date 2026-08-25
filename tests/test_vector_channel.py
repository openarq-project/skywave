

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


# ---- Watterson group-delay alignment (2026-08-25 fix) ----------------------
#
# apply_fade() used to hand the caller ch.process()'s raw output, which is the
# input delayed by the Hilbert group delay (127 samples at HILBERT_TAPS=255) --
# see the module docstring. That shifted every faded frame 127 samples later
# than the sidecar's frame_offsets while the "off" path had no such shift.
# These tests plant an energy marker at a known offset and locate it in the
# output via a sliding-energy envelope, which is robust to the random
# fade-gain phase (unlike checking a raw single-sample amplitude peak).

def _marker_vector(fs, frame_len, marker_start, marker_len, total_len,
                    freq_hz=1500.0):
    import numpy as np
    vec = np.zeros(total_len, dtype=np.float64)
    n = np.arange(marker_len)
    vec[marker_start:marker_start + marker_len] = \
        0.9 * np.sin(2.0 * np.pi * freq_hz * n / fs)
    side = {"sample_rate": fs, "frame_offsets": [0], "frame_lengths": [frame_len]}
    return vec, side


def _envelope_peak_start(vec, marker_len):
    """-> sample index where a sliding sum-of-squares window of length
    marker_len is maximized -- the marker's detected start, robust to phase."""
    import numpy as np
    energy = vec.astype(np.float64) ** 2
    window_sums = np.convolve(energy, np.ones(marker_len), mode="valid")
    return int(np.argmax(window_sums))


def test_off_path_marker_stays_at_its_offset():
    """The AWGN-only ("off") path never touched the vector's timing; this is
    the negative control the fading assertions below are compared against."""
    from skywave.vector_channel import apply_fade
    fs, frame_len, marker_start, marker_len = 8000, 3000, 1000, 300
    vec, side = _marker_vector(fs, frame_len, marker_start, marker_len, 4500)
    faded, gains = apply_fade(vec, side, "off", seed=7)
    assert gains == []
    peak = _envelope_peak_start(faded, marker_len)
    assert peak == marker_start


def test_fading_marker_realigns_to_its_offset_zero_delay_preset():
    """"flat" has zero differential multipath delay, so a correctly
    time-aligned channel stage must place the marker at EXACTLY its original
    offset -- any residual shift here can only be the Hilbert group delay
    bug (127 samples), not physics."""
    from skywave.vector_channel import apply_fade
    fs, frame_len, marker_start, marker_len = 8000, 3000, 1000, 300
    vec, side = _marker_vector(fs, frame_len, marker_start, marker_len, 4500)
    faded, gains = apply_fade(vec, side, "flat", seed=7)
    assert len(gains) == 1
    peak = _envelope_peak_start(faded, marker_len)
    assert peak == marker_start, (
        f"marker moved from {marker_start} to {peak} "
        f"(delta {peak - marker_start}) -- group-delay regression?")


def test_fading_marker_realigns_within_multipath_delay_not_hilbert_delay():
    """"poor" (2 ms differential delay -> 16 samples at 8 kHz) may smear the
    marker by the PHYSICAL multipath delay, but must land nowhere near the
    127-sample Hilbert group delay the pre-fix code leaked into the output."""
    from skywave.vector_channel import apply_fade
    fs, frame_len, marker_start, marker_len = 8000, 3000, 1000, 300
    vec, side = _marker_vector(fs, frame_len, marker_start, marker_len, 4500)
    faded, gains = apply_fade(vec, side, "poor", seed=7)
    assert len(gains) == 1
    peak = _envelope_peak_start(faded, marker_len)
    delay_samples = int(round(2.0e-3 * fs))  # "poor" = 2.0 ms / 1.0 Hz
    assert abs(peak - marker_start) <= delay_samples, (
        f"marker at {peak}, expected within {delay_samples} samples of "
        f"{marker_start} -- got a {peak - marker_start} sample shift "
        f"(127 would indicate the Hilbert-group-delay regression)")
