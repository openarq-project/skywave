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


def test_resolved_clock_is_reported(tmp_path):
    """The third knob that used to mis-resolve in silence. A corpus must be
    able to say which clock produced it without trusting the driver."""
    d = _stats(tmp_path, load_sim(SIM_HALF_DUPLEX=1), "rt.json")
    assert d["clock"] == "real_time"
    v = _stats(tmp_path, load_sim(SIM_HALF_DUPLEX=1, SIM_TRANSPORT="sock",
                                  SIM_CLOCK="virt_time"), "vt.json")
    assert v["clock"] == "virt_time"
