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


# ---- deviation limiting --------------------------------------------------

def test_limiter_engages_only_below_the_modes_papr():
    """The headroom knob has to mean the same thing across modes, which is the
    whole reason it is specified in dB above RMS rather than as an absolute
    level. Above the signal's own PAPR nothing should clip."""
    from skywave.vector_fm_channel import apply_deviation_limit
    n, flen, gap = 6, 8000, 2000
    rng = np.random.default_rng(3)
    vec, offs, lens = _tone_frames(n, flen, gap)
    # give it a real crest factor: noise-like bursts, not a pure tone
    for a, l in zip(offs, lens):
        vec[a:a + l] = 0.15 * rng.standard_normal(l)
    side = _sidecar(offs, lens)
    inb = np.concatenate([vec[a:a + l] for a, l in zip(offs, lens)])
    papr = 20 * np.log10(np.max(np.abs(inb)) / np.sqrt(np.mean(inb ** 2)))
    _, frac_hi, _ = apply_deviation_limit(vec, side, FS, papr + 3)
    _, frac_lo, _ = apply_deviation_limit(vec, side, FS, papr - 6)
    assert frac_hi == 0.0, f"clipped {frac_hi} above the signal's PAPR"
    assert frac_lo > 0.01, f"barely clipped {frac_lo} at 6 dB below PAPR"


def test_limiter_reference_ignores_inter_frame_silence():
    """Reference RMS comes from the sidecar's active regions. If it were taken
    over the whole vector, the gap length the encoder happened to choose would
    change how hard every cell clips."""
    from skywave.vector_fm_channel import apply_deviation_limit
    out = []
    for gap in (1000, 20000):
        vec, offs, lens = _tone_frames(4, 8000, gap, amp=0.3)
        _, _, ceiling = apply_deviation_limit(_ := vec, _sidecar(offs, lens),
                                              FS, 6.0)
        out.append(ceiling)
    assert abs(out[0] - out[1]) / out[0] < 0.01, (
        f"ceiling moved with gap length: {out}")


def test_soft_and_hard_limiters_differ_but_share_the_ceiling():
    from skywave.vector_fm_channel import apply_deviation_limit
    rng = np.random.default_rng(1)
    vec, offs, lens = _tone_frames(4, 8000, 1000)
    for a, l in zip(offs, lens):
        vec[a:a + l] = 0.2 * rng.standard_normal(l)
    side = _sidecar(offs, lens)
    hard, _, c1 = apply_deviation_limit(vec, side, FS, 3.0, "hard")
    soft, _, c2 = apply_deviation_limit(vec, side, FS, 3.0, "soft")
    assert abs(c1 - c2) < 1e-12
    assert float(np.max(np.abs(hard))) <= c1 * 1.000001
    assert float(np.max(np.abs(soft))) < c1, "tanh only approaches the asymptote"
    assert not np.allclose(hard, soft)


# ---- carrier frequency offset --------------------------------------------

def test_cfo_shifts_a_tone_by_the_requested_amount():
    from skywave.vector_fm_channel import apply_cfo
    n = 1 << 15
    t = np.arange(n) / FS
    x = np.sin(2 * np.pi * 1500.0 * t)
    y, d = apply_cfo(x, FS, 40.0)
    assert d > 0, "FreqShift is Hilbert-based and must report a group delay"

    def peak(v):
        # settle past the Hilbert transient before measuring
        v = np.asarray(v[2 * d:], dtype=np.float64)
        m = v.size
        sp = np.abs(np.fft.rfft(v * np.hanning(m)))
        return np.fft.rfftfreq(m, 1 / FS)[int(np.argmax(sp))]

    got = peak(y) - peak(x)
    assert abs(got - 40.0) < 5.0, f"tone moved {got:.1f} Hz, wanted +40"


def test_cfo_is_delay_aligned_like_the_ports():
    """Uncorrected, FreqShift's Hilbert delay would move every frame off its
    sidecar offset -- and only in cells with CFO enabled, so it would read as
    a CFO effect."""
    from skywave.vector_fm_channel import apply_cfo
    mark_at, mlen = 8000, 400
    vec = np.zeros(24000)
    t = np.arange(mlen) / FS
    vec[mark_at:mark_at + mlen] = 0.5 * np.sin(2 * np.pi * 1200.0 * t)
    find = lambda y: int(np.argmax(np.convolve(y ** 2, np.ones(mlen), "valid")))
    y, d = apply_cfo(vec, FS, 10.0)
    assert len(y) == len(vec)
    assert abs(find(y) - mark_at) <= 8, (
        f"marker moved {find(y) - mark_at} samples (gdelay {d})")


def test_zero_cfo_is_a_passthrough():
    from skywave.vector_fm_channel import apply_cfo
    x = np.random.default_rng(0).standard_normal(4000) * 0.1
    y, d = apply_cfo(x, FS, 0.0)
    assert d == 0 and np.allclose(x, y), "off must cost nothing and change nothing"


# ---- codec-in-the-loop -----------------------------------------------------

def test_codec_stage_is_transparent_for_an_identity_runner():
    """A pass-through codec must change nothing and report zero lag, or every
    codec cell would carry an unattributed offset."""
    import shutil
    from skywave.vector_fm_channel import apply_codec
    x = np.random.default_rng(0).standard_normal(20000) * 0.1
    out, lag = apply_codec(x, FS, lambda i, o: shutil.copyfile(i, o))
    assert lag == 0
    assert np.allclose(np.asarray(out, np.float32), np.asarray(x, np.float32),
                       atol=1e-6)


def test_codec_stage_measures_and_removes_a_known_delay():
    """The lag is MEASURED, not taken from a codec's nominal figure: filterbank
    delay and the caller's framing both contribute, and a frame-synchronous
    receiver reading the sidecar needs the real one."""
    from skywave.vector_adapter import read_vector, write_vector
    from skywave.vector_fm_channel import apply_codec
    D = 73          # the delay HTCommander measured for SBC, and we reproduce

    def delaying(i, o):
        v = np.asarray(read_vector(i), dtype=np.float64)
        write_vector(o, np.concatenate([np.zeros(D), v])[:v.size])

    mark, mlen = 6000, 300
    x = np.zeros(20000)
    t = np.arange(mlen) / FS
    x[mark:mark + mlen] = 0.5 * np.sin(2 * np.pi * 1200.0 * t)
    out, lag = apply_codec(x, FS, delaying)
    assert lag == D, f"measured lag {lag}, planted {D}"
    found = int(np.argmax(np.convolve(np.asarray(out) ** 2,
                                      np.ones(mlen), "valid")))
    assert abs(found - mark) <= 4, "marker not restored to its sidecar offset"


def test_codec_stage_preserves_length():
    from skywave.vector_adapter import read_vector, write_vector
    from skywave.vector_fm_channel import apply_codec

    def truncating(i, o):
        v = np.asarray(read_vector(i), dtype=np.float64)
        write_vector(o, v[:-500])          # codecs drop a partial last frame

    x = np.random.default_rng(1).standard_normal(20000) * 0.1
    out, _ = apply_codec(x, FS, truncating)
    assert out.size == x.size, "stage must return a vector the sidecar still fits"


def test_squelch_block_is_time_based_so_30ms_engages_at_8k():
    """At 8 kHz a fixed 1024-sample block was 128 ms: round(30/128) = 0 made
    a 30 ms carrier squelch a NO-OP on the burst (found 2026-09-01, in the
    FM-CTRL pre-reg review). The block is now 32 ms at any rate."""
    from skywave.vector_fm_channel import (apply_squelch, squelch_block,
                                           squelch_mute_stats, SQUELCH_BLOCK)
    assert squelch_block(32000) == SQUELCH_BLOCK      # DART path unchanged
    assert squelch_block(8000) == 256
    fs, flen, gap = 8000, 2044, 2400
    for off in (13332, 20479, 20480):                 # three block phases
        vec = np.zeros(off + flen + gap, dtype=np.float32)
        vec[off:off + flen] = 0.5
        side = {"frame_offsets": [off], "frame_lengths": [flen]}
        # The realized attack is wait_blocks*block - phase, so at the 32 ms
        # default a 30 ms request lands anywhere in (0, 32] ms depending on
        # the burst's phase in the block. A cell that pre-registers an
        # attack time pins a block SMALL against it (FM-CTRL: 4 ms) and reads
        # the witness; at 4 ms the realization is within one block.
        out = apply_squelch(vec, side, fs, open_ms=30.0, carrier="frames",
                            block_ms=4.0)
        st = squelch_mute_stats(vec, out, side, fs)
        assert st["muted_frames"] == 1, (off, st)
        assert 26.0 <= st["mean_mute_ms"] <= 34.0, (off, st)
        # default block: engages (not the old no-op) but phase-coarse
        outd = apply_squelch(vec, side, fs, open_ms=30.0, carrier="frames")
        std = squelch_mute_stats(vec, outd, side, fs)
        assert 0.0 < std["mean_mute_ms"] <= 32.0, (off, std)
        # the legacy sample-count block at 8 kHz: the no-op, witnessed
        out0 = apply_squelch(vec, side, fs, open_ms=30.0, carrier="frames",
                             block=1024)
        st0 = squelch_mute_stats(vec, out0, side, fs)
        assert st0["muted_frames"] == 0 and st0["mean_mute_ms"] == 0.0


def test_squelch_mute_stats_reads_the_head_only():
    from skywave.vector_fm_channel import squelch_mute_stats
    fs = 8000
    before = np.ones(4000, dtype=np.float32)
    after = before.copy()
    after[1000:1000 + 240] = 0.0            # 30 ms muted head of frame 1
    side = {"frame_offsets": [0, 1000, 3000], "frame_lengths": [500, 1500, 500]}
    st = squelch_mute_stats(before, after, side, fs)
    assert st["muted_frames"] == 1
    assert abs(st["mean_mute_ms"] - 10.0) < 1e-6   # 30 ms over 3 frames
    assert st["max_mute_ms"] == 30.0 and st["min_mute_ms"] == 0.0
