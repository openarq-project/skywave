# Contract tests for the FM stage of the VectorAdapter path
# (skywave/vector_fm_channel.py).
#
# Every test here pins a property that fails SILENTLY if it regresses: a frame
# shifted off its sidecar offset, an SNR labeled against the wrong signal
# power, or a fade that reports an average over a cycle it never sampled.

import numpy as np
import pytest

FS = 32000          # DART's rate; deliberately not 8k/48k, fs comes from the sidecar


def _sidecar(offsets, lengths, fs=FS, payload=None):
    side = {"sample_rate": fs, "frame_offsets": list(offsets),
            "frame_lengths": list(lengths)}
    if payload:
        side["payload_offsets"], side["payload_lengths"] = payload
    return side


def _tone_frames(n, flen, gap, fs=FS, f0=1500.0, amp=0.2):
    """n tone bursts of flen samples separated by gap. -> (vec, offsets, lens)"""
    total = n * (flen + gap) + gap
    vec = np.zeros(total, dtype=np.float64)
    offs, lens = [], []
    t = np.arange(flen) / fs
    burst = amp * np.sin(2 * np.pi * f0 * t)
    for i in range(n):
        a = gap + i * (flen + gap)
        vec[a:a + flen] = burst
        offs.append(a)
        lens.append(flen)
    return vec, offs, lens


# ---- port chain: alignment and passthrough --------------------------------

def test_data9600_port_is_identity_with_zero_delay():
    from skywave.vector_fm_channel import apply_tx_port, apply_rx_port
    vec = np.random.default_rng(0).standard_normal(4000) * 0.1
    tx, d_tx = apply_tx_port(vec, FS, "data9600")
    rx, d_rx = apply_rx_port(vec, FS, "data9600")
    assert d_tx == 0 and d_rx == 0, "the flat discriminator tap adds no delay"
    assert np.allclose(tx, vec) and np.allclose(rx, vec)


def test_micspk_ports_have_real_delay():
    """Guards the premise of the alignment fix: if these were 0 the trim would
    be untested and a later filter change could reintroduce the shift."""
    from skywave.vector_fm_channel import port_delay
    from skywave import fm_rig
    d_tx = port_delay(lambda: fm_rig.FmPortTx(FS, "micspk"), FS)
    d_rx = port_delay(lambda: fm_rig.FmPortRx(FS, "micspk"), FS)
    assert d_tx > 0 and d_rx > 0, "IIR voice chain must have measurable delay"


@pytest.mark.parametrize("stage", ["tx", "rx"])
def test_micspk_marker_stays_at_its_sidecar_offset(stage):
    """Plant an energy marker at a known offset, push it through the port, and
    require the delay trim to materially re-align it.

    NEGATIVE CONTROL BUILT IN: the untrimmed chain output is measured in the
    same test and the assertion is relative to it. An earlier version of this
    test used a fixed +/-50-sample tolerance and was VACUOUS -- the micspk
    chain's bulk delay (11 tx / 18 rx samples at 32 kHz) is smaller than that,
    so it passed with the trim removed. Never re-tighten this to an absolute
    bound alone.
    """
    from skywave.vector_fm_channel import apply_tx_port, apply_rx_port
    from skywave import fm_rig
    fn = apply_tx_port if stage == "tx" else apply_rx_port
    raw_chain = (fm_rig.FmPortTx if stage == "tx" else fm_rig.FmPortRx)
    mark_at, mlen = 6000, 400
    vec = np.zeros(20000, dtype=np.float64)
    t = np.arange(mlen) / FS
    vec[mark_at:mark_at + mlen] = 0.5 * np.sin(2 * np.pi * 1000.0 * t)

    def find(y):
        return int(np.argmax(np.convolve(y ** 2, np.ones(mlen), mode="valid")))

    untrimmed = find(np.asarray(raw_chain(FS, "micspk").process(vec.copy())))
    out, delay = fn(vec, FS, "micspk")
    trimmed = find(out)

    assert len(out) == len(vec), "the trim must not change vector length"
    shift_raw = abs(untrimmed - mark_at)
    shift_trim = abs(trimmed - mark_at)
    assert shift_raw > 0, "premise: the raw chain does shift the marker"
    assert shift_trim * 2 < shift_raw, (
        f"trim did not halve the misalignment: {shift_raw} -> {shift_trim} "
        f"(measured chain delay {delay})")
    # residual is frequency-dependent dispersion across 300-3000 Hz, which is
    # real channel physics and deliberately NOT removed; only the bulk offset is.
    assert shift_trim <= 10, f"residual {shift_trim} samples is too large"


# ---- S convention ---------------------------------------------------------

def test_s_is_measured_on_the_port_shaped_signal():
    """A mode with energy outside the voice band presents LESS power to the
    channel than its raw mean square suggests. If S were taken pre-port the
    labeled SNR would carry that mode-shape error -- the FM analog of the
    Option-A ruling."""
    from skywave.vector_fm_channel import apply_tx_port
    from skywave.vector_channel import clean_signal_power
    flen = 8000
    t = np.arange(flen) / FS
    # half the energy at 1 kHz (passes), half at 7 kHz (rejected by the BPF)
    burst = 0.2 * (np.sin(2 * np.pi * 1000.0 * t) + np.sin(2 * np.pi * 7000.0 * t))
    vec = np.zeros(flen + 2000)
    vec[1000:1000 + flen] = burst
    side = _sidecar([1000], [flen])

    s_raw = clean_signal_power(vec, side)
    tx, _ = apply_tx_port(vec, FS, "micspk")
    s_port = clean_signal_power(tx, side)
    assert s_port < 0.75 * s_raw, (
        "out-of-band energy must not count toward S "
        f"(raw {s_raw:.4g} vs port {s_port:.4g})")


# ---- fade strides: the trap that makes a fade average a lie ---------------

def test_stochastic_stride_decorrelates():
    from skywave.vector_fm_channel import fade_stride_s
    # frames are short relative to the coherence time -> 3/fD governs
    assert fade_stride_s("rayleigh", 2.0, 0.0, 0.1, 20) == pytest.approx(1.5)
    # long frames govern instead
    assert fade_stride_s("rayleigh", 20.0, 0.0, 4.0, 20) == pytest.approx(4.0)


def test_periodic_fade_stratifies_the_cycle_not_three_over_rate():
    """THE trap. ionos has period 1/R, so the HF rule (3/R) would put every
    frame at the SAME fade phase: the cell would report a fade average it
    never sampled. The stride must tile one cycle instead."""
    from skywave.vector_fm_channel import fade_stride_s
    rate, n = 0.1, 20                       # 10 s period
    stride = fade_stride_s("ionos", 0.0, rate, 0.5, n)
    assert stride == pytest.approx((1.0 / rate) / n)
    # and it is emphatically not the stochastic rule
    assert stride < 3.0 / rate
    # n strides cover exactly one period
    assert stride * n == pytest.approx(1.0 / rate)


def test_periodic_fade_frames_span_the_depth():
    """End-to-end version of the same trap, on realized per-frame gains."""
    from skywave.vector_fm_channel import apply_fm_fade, resolve_fade
    n, flen, gap = 16, 4000, 1000
    vec, offs, lens = _tone_frames(n, flen, gap)
    side = _sidecar(offs, lens)
    spec = resolve_fade("ionos:30:0.1")
    _, gains, _ = apply_fm_fade(vec, side, spec, seed=7)
    g = np.array(gains)
    assert len(g) == n
    assert g.max() > -3.0, "some frame must sit near the unfaded peak"
    assert g.min() < -15.0, "some frame must sit deep in the trough"


def test_ionos_shadow_combination_is_refused_not_approximated():
    from skywave.vector_fm_channel import fade_stride_s
    with pytest.raises(SystemExit, match="refusing"):
        fade_stride_s("ionos", 0.0, 0.1, 0.5, 10,
                      shadow_sigma_db=8.0, shadow_tau_s=5.0)


def test_shadowing_is_not_power_normalized():
    """A shadow fade-down is a real SNR loss and IS the axis; if the stage
    normalized it away the slow-outage cells would measure nothing."""
    from skywave.vector_fm_channel import apply_fm_fade, resolve_fade
    n, flen, gap = 24, 4000, 1000
    vec, offs, lens = _tone_frames(n, flen, gap)
    side = _sidecar(offs, lens)
    _, gains, _ = apply_fm_fade(vec, side, resolve_fade("static"), seed=3,
                                shadow_sigma_db=8.0, shadow_tau_s=2.0)
    g = np.array(gains)
    assert g.std() > 2.0, f"shadow spread collapsed (std {g.std():.2f} dB)"


def test_deep_fades_survive_per_frame_slicing():
    """The renormalization trap: a fresh FmFade per frame would rescale a
    short deep-fade realization back to unit power and delete the event."""
    from skywave.vector_fm_channel import apply_fm_fade, resolve_fade
    n, flen, gap = 40, 2000, 500
    vec, offs, lens = _tone_frames(n, flen, gap)
    side = _sidecar(offs, lens)
    _, gains, _ = apply_fm_fade(vec, side, resolve_fade("rayleigh:1.0"), seed=11)
    g = np.array(gains)
    assert g.min() < -6.0, f"no deep draw in {n} frames (min {g.min():.1f} dB)"
    assert g.std() > 1.5, "per-frame draws look correlated"


# ---- ionosnc noise track --------------------------------------------------

def test_ionosnc_raises_the_noise_in_the_trough():
    """The IONOS instrument raises noise as the signal fades; the harness
    applies that track to its own noise fill. If the vector stage dropped it,
    a trough would be near-silence instead of loud hiss."""
    from skywave.vector_fm_channel import apply_fm_fade, resolve_fade
    n, flen, gap = 16, 4000, 1000
    vec, offs, lens = _tone_frames(n, flen, gap)
    side = _sidecar(offs, lens)
    _, _, ngain = apply_fm_fade(vec, side, resolve_fade("ionosnc:30:0.1:30"),
                                seed=5)
    inside = np.concatenate([ngain[a:a + l] for a, l in zip(offs, lens)])
    assert inside.max() > 1.5, "noise never rises across the fade cycle"
    assert ngain[0] == pytest.approx(1.0), "gaps stay at unity"


def test_noise_gain_track_actually_modulates_the_noise():
    from skywave.vector_fm_channel import add_awgn_shaped
    quiet = np.zeros(20000)
    ng = np.ones(20000)
    ng[10000:] = 4.0
    out = add_awgn_shaped(quiet, 0.01, 1, ng)
    lo = float(np.std(out[:10000]))
    hi = float(np.std(out[10000:]))
    assert hi / lo > 3.0, f"noise track not applied (ratio {hi / lo:.2f})"


# ---- full stage -----------------------------------------------------------

def _write_case(tmp_path, port="micspk", n=8, flen=4000, gap=1000):
    from skywave.vector_adapter import write_vector, save_sidecar
    vec, offs, lens = _tone_frames(n, flen, gap)
    side = _sidecar(offs, lens)
    vp = str(tmp_path / "v.f32")
    sp = str(tmp_path / "v.json")
    write_vector(vp, vec)
    save_sidecar(sp, side)
    return vp, sp, vec, side


@pytest.mark.parametrize("port", ["micspk", "data9600"])
def test_apply_runs_end_to_end_and_reports(tmp_path, port):
    from skywave.vector_fm_channel import apply
    vp, sp, vec, _ = _write_case(tmp_path, port)
    op = str(tmp_path / "out.f32")
    info = apply(vp, sp, op, port=port, fade="off", snr_db=10.0, seed=1)
    from skywave.vector_adapter import read_vector
    out = read_vector(op)
    assert out.size == vec.size, "stage must preserve vector length"
    assert info["port"] == port and info["signal_power"] > 0
    assert info["sigma"] > 0


def test_realized_snr_matches_the_label(tmp_path):
    """Pins the SNR CONVENTION, not just monotonicity.

    sigma^2 = S*fs / (2*bw*10^(snr/10)), so inside a frame the realized
    wideband S/N is snr + 10log10(2*bw/fs) -- here 2500 Hz at 32 kHz, i.e.
    8.06 dB below the label. An absolute-noise-floor test cannot see this:
    apply_headroom rescales each vector jointly, which preserves SNR within a
    vector but not noise level across vectors.
    """
    from skywave.vector_fm_channel import apply
    from skywave.vector_adapter import read_vector
    vp, sp, _, side = _write_case(tmp_path, "data9600")
    bw, offset_db = 2500.0, 10 * np.log10(2 * 2500.0 / FS)
    for label_db in (30.0, 10.0, 0.0):
        op = str(tmp_path / f"o{label_db}.f32")
        apply(vp, sp, op, port="data9600", snr_db=label_db, bw_hz=bw, seed=1)
        out = read_vector(op)
        a, ln = side["frame_offsets"][1], side["frame_lengths"][1]
        p_frame = float(np.mean(np.asarray(out[a:a + ln], dtype=np.float64) ** 2))
        g = a + ln + 100                     # gap: pure noise
        p_noise = float(np.mean(np.asarray(out[g:g + 500], dtype=np.float64) ** 2))
        realized = 10 * np.log10(max(p_frame - p_noise, 1e-30) / p_noise)
        assert abs(realized - (label_db + offset_db)) < 1.0, (
            f"label {label_db:+.0f} dB -> realized {realized:+.2f} dB, "
            f"expected {label_db + offset_db:+.2f}")


def test_noise_passes_through_the_rx_port(tmp_path):
    """Ordering check: the link path adds noise BEFORE FmPortRx, so on micspk
    the delivered noise is band-limited by the RX voice filter. If the stage
    added noise after the port, out-of-band noise would survive."""
    from skywave.vector_fm_channel import apply
    from skywave.vector_adapter import read_vector
    vp, sp, _, side = _write_case(tmp_path, "micspk")
    op = str(tmp_path / "out.f32")
    apply(vp, sp, op, port="micspk", snr_db=0.0, seed=1)
    out = read_vector(op)
    a = side["frame_offsets"][0] + side["frame_lengths"][0] + 200
    seg = np.asarray(out[a:a + 8192], dtype=np.float64)
    spec = np.abs(np.fft.rfft(seg * np.hanning(seg.size))) ** 2
    freqs = np.fft.rfftfreq(seg.size, 1.0 / FS)
    inband = spec[(freqs > 500) & (freqs < 2500)].mean()
    outband = spec[(freqs > 6000) & (freqs < 12000)].mean()
    assert inband > 50 * outband, (
        f"noise not band-limited by the RX port (in {inband:.3g} / "
        f"out {outband:.3g}) -- is noise being added after FmPortRx?")


def test_squelch_mutes_the_frame_head(tmp_path):
    """Carrier-derived squelch: the attack window eats the head of each burst,
    which is exactly the acquisition-vs-squelch measurement."""
    from skywave.vector_fm_channel import apply_squelch
    n, flen, gap = 4, 16000, 4000
    vec, offs, lens = _tone_frames(n, flen, gap, amp=0.5)
    side = _sidecar(offs, lens)
    out = apply_squelch(vec, side, FS, open_ms=30.0, carrier="frames")
    a = offs[1]
    head = float(np.sqrt(np.mean(out[a:a + 512] ** 2)))
    body = float(np.sqrt(np.mean(out[a + 4000:a + 8000] ** 2)))
    assert head < 0.1 * body, "squelch attack did not mute the burst head"
    assert body > 0.1, "squelch never opened"


def test_squelch_rejected_on_the_flat_port(tmp_path):
    from skywave.vector_fm_channel import apply
    vp, sp, _, _ = _write_case(tmp_path, "data9600")
    with pytest.raises(SystemExit, match="squelch"):
        apply(vp, sp, str(tmp_path / "o.f32"), port="data9600",
              snr_db=10.0, squelch=True)


def test_determinism(tmp_path):
    from skywave.vector_fm_channel import apply
    from skywave.vector_adapter import read_vector
    vp, sp, _, _ = _write_case(tmp_path, "micspk")
    outs = []
    for i in range(2):
        op = str(tmp_path / f"o{i}.f32")
        apply(vp, sp, op, port="micspk", fade="mobile-urban", band="2m",
              snr_db=6.0, seed=42)
        outs.append(read_vector(op))
    assert np.array_equal(outs[0], outs[1]), "same seed must be byte-identical"
