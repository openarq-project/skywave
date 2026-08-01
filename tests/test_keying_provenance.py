"""Keying-mode provenance in the stats JSON.

A driver must be able to read back WHICH keying gate actually ran, rather than
trusting the environment it believes it set. The failure mode is real: a ladder
driver once set SIM_HALF_DUPLEX=1, ran full-duplex/VOX, and recorded HD+PTT
provenance in its corpus. `key_duty`/`key_bursts` differ between the arms only
by inference, which is not provenance — so the resolved keying config is
emitted explicitly.
"""
import json

import numpy as np

from conftest import load_sim, make_link, feed


def _stats(tmp_path, cs, name="np_stats.json"):
    path = str(tmp_path / name)
    # PTT arm reads the key state off a PttState; VOX arm ignores it.
    link = make_link(cs, stats_path=path, ptt=cs.PttState())
    feed(link, np.zeros(cs.NSAMP, dtype="<i2"))
    link.write_stats()
    return json.load(open(path))


def test_vox_arm_reports_its_own_parameters(tmp_path):
    cs = load_sim(SIM_HALF_DUPLEX=1, SIM_PTT=0, SIM_HANG_MS=123,
                  SIM_KEY_THRESH=456)
    d = _stats(tmp_path, cs)
    assert d["sim_ptt"] is False
    assert d["hang_ms"] == 123
    assert d["key_thresh"] == 456
    assert d["half_duplex"] is True


def test_ptt_arm_is_distinguishable_from_the_vox_arm(tmp_path):
    """The two V0 arms must be told apart from the artifact alone."""
    vox = _stats(tmp_path, load_sim(SIM_HALF_DUPLEX=1, SIM_PTT=0), "vox.json")
    ptt = _stats(tmp_path, load_sim(SIM_HALF_DUPLEX=1, SIM_PTT=1), "ptt.json")
    assert vox["sim_ptt"] is False and ptt["sim_ptt"] is True
    assert vox["sim_ptt"] != ptt["sim_ptt"]


def test_hang_defaults_are_reported_not_omitted(tmp_path):
    """Defaults must appear too — an absent key is indistinguishable from an
    old instrument that never wrote one."""
    cs = load_sim(SIM_HALF_DUPLEX=1)
    d = _stats(tmp_path, cs)
    assert set(("sim_ptt", "hang_ms", "key_thresh")) <= set(d)
    assert d["hang_ms"] == cs.HANG_MS
    assert d["key_thresh"] == cs.KEY_THRESH


def test_tr_geometry_is_reported_in_ms_and_in_blocks(tmp_path):
    """Cell V1 sweeps SIM_TR_UNKEY_MS, so it must be readable back — and the
    APPLIED block count must be readable beside the requested ms, because these
    knobs are block-quantized. Reading the ms alone is how a sweep reports
    distinct rungs that ran identical physics."""
    cs = load_sim(SIM_HALF_DUPLEX=1, SIM_TR_KEY_MS=15, SIM_TR_UNKEY_MS=400)
    d = _stats(tmp_path, cs)
    assert d["tr_key_ms"] == 15 and d["tr_unkey_ms"] == 400
    assert d["tr_key_blocks"] == cs.TR_KEY_BLOCKS
    assert d["tr_unkey_blocks"] == cs.TR_UNKEY_BLOCKS
    assert d["hang_blocks"] == cs.HANG_BLOCKS
    assert d["block_ms"] == cs.BLOCK_MS


def test_block_quantization_collapse_is_visible(tmp_path):
    """The concrete trap this exists to expose: at SIM_FS=8000 one block is
    128 ms, so 40 ms and 80 ms of hang are the SAME single block. The ms fields
    differ (and would read as two rungs); `hang_blocks` must show they did not."""
    a = _stats(tmp_path, load_sim(SIM_HALF_DUPLEX=1, SIM_FS=8000,
                                  SIM_HANG_MS=40), "h40.json")
    b = _stats(tmp_path, load_sim(SIM_HALF_DUPLEX=1, SIM_FS=8000,
                                  SIM_HANG_MS=80), "h80.json")
    assert a["hang_ms"] != b["hang_ms"]
    assert a["hang_blocks"] == b["hang_blocks"] == 1
    # ...and at 48 kHz the same two values are genuinely distinct rungs, so the
    # field discriminates rather than always collapsing.
    c = _stats(tmp_path, load_sim(SIM_HALF_DUPLEX=1, SIM_HANG_MS=40), "f40.json")
    e = _stats(tmp_path, load_sim(SIM_HALF_DUPLEX=1, SIM_HANG_MS=80), "f80.json")
    assert c["hang_blocks"] != e["hang_blocks"]


def test_resolved_clock_is_reported(tmp_path):
    """The third knob that used to mis-resolve in silence. A corpus must be
    able to say which clock produced it without trusting the driver."""
    d = _stats(tmp_path, load_sim(SIM_HALF_DUPLEX=1), "rt.json")
    assert d["clock"] == "real_time"
    v = _stats(tmp_path, load_sim(SIM_HALF_DUPLEX=1, SIM_TRANSPORT="sock",
                                  SIM_CLOCK="virt_time"), "vt.json")
    assert v["clock"] == "virt_time"
