"""Rig SSB passband filter pins (RigBPF): response correctness + block continuity."""
import numpy as np
import pytest
from scipy.signal import sosfilt, sosfreqz

from conftest import load_sim, make_link, feed, interleave


def test_blockwise_equals_oneshot():
    """State must carry across blocks: chunked filtering == whole-signal filtering."""
    cs = load_sim(SIM_RIG_BPF="default")
    bpf = cs.RigBPF(300.0, 2700.0, 6, cs.FS)
    rng = np.random.default_rng(8)
    x = rng.standard_normal(48000)
    y_chunks = np.concatenate([bpf.process(x[k:k + 1024]) for k in range(0, len(x), 1024)])
    zi = np.zeros((bpf.sos.shape[0], 2))
    y_full, _ = sosfilt(bpf.sos, x, zi=zi)
    assert np.allclose(y_chunks, y_full, atol=1e-9)


@pytest.mark.parametrize("freq", [1000.0, 1500.0, 2400.0, 100.0, 3500.0])
def test_tone_gains_match_designed_response(freq):
    """Measured steady-state tone gain == the designed Butterworth magnitude."""
    cs = load_sim(SIM_RIG_BPF="default")
    bpf = cs.RigBPF(300.0, 2700.0, 6, cs.FS)
    n = np.arange(48000)
    x = np.sin(2 * np.pi * freq * n / cs.FS)
    y = bpf.process(x)
    meas = np.sqrt(np.mean(y[24000:] ** 2)) / np.sqrt(0.5)   # skip transient
    w, h = sosfreqz(bpf.sos, worN=[freq], fs=cs.FS)
    want = float(np.abs(h[0]))
    assert meas == pytest.approx(want, abs=max(0.02, 0.05 * want)), (
        f"{freq} Hz: measured {meas:.4f} vs designed {want:.4f}")


def test_end_to_end_link_band_limits():
    """With the rig profile on, an out-of-band carrier must not survive the link."""
    cs = load_sim(SIM_RIG_BPF="default")
    band = cs._resolve_rig_band()
    assert band == (300.0, 2700.0)
    rig_tx = cs.RigBPF(*band, cs.RIG_ORDER, cs.FS)
    rig_rx = cs.RigBPF(*band, cs.RIG_ORDER, cs.FS)
    link = make_link(cs, rig_tx=rig_tx, rig_rx=rig_rx)
    n_blocks = 40
    inband = outband = 0.0
    for k in range(n_blocks):
        n = np.arange(cs.BLOCK) + k * cs.BLOCK
        mono = 5000 * np.sin(2 * np.pi * 1000 * n / cs.FS)
        y = feed(link, interleave(cs, mono))[0::cs.NCH].astype(float)
        if k >= 4:
            inband += float(np.dot(y, y))
    link2 = make_link(cs, rig_tx=cs.RigBPF(*band, cs.RIG_ORDER, cs.FS),
                      rig_rx=cs.RigBPF(*band, cs.RIG_ORDER, cs.FS))
    for k in range(n_blocks):
        n = np.arange(cs.BLOCK) + k * cs.BLOCK
        mono = 5000 * np.sin(2 * np.pi * 3600 * n / cs.FS)
        y = feed(link2, interleave(cs, mono))[0::cs.NCH].astype(float)
        if k >= 4:
            outband += float(np.dot(y, y))
    assert inband > 100 * outband, "3.6 kHz must be crushed vs 1 kHz through TX+RX filters"
