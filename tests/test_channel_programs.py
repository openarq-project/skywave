"""tools/channel_programs.py (B4, real-world campaign): the program shapes, the SIM_ATTEN_SCHEDULE emitter (read back
through channel_sim's own parser), and the WSPR-walk selection rule on synthetic series."""
import json
import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import channel_programs as cp  # noqa: E402


def schedule_at(sched, t_s):
    """Atten in force at t_s under channel_sim's own parser and segment rule."""
    from skywave.channel_sim import parse_atten_schedule
    segs = parse_atten_schedule(sched)
    elapsed = 0.0
    for db, secs in segs:
        if secs <= 0.0 or t_s < elapsed + secs:
            return db
        elapsed += secs
    return segs[-1][0]


def test_ou_is_seeded_and_has_the_stated_statistics():
    a = cp.ou_knots(5.0, 480.0, 1234)
    assert a == cp.ou_knots(5.0, 480.0, 1234) and a != cp.ou_knots(5.0, 480.0, 1241)
    assert a[-1][0] == cp.WINDOW_S
    x = np.array([v for _, v in cp.ou_knots(5.0, 480.0, 99, dur_s=400 * 480.0)])
    assert abs(x.std() - 5.0) < 0.4
    lag = int(480 / 10)
    r = np.corrcoef(x[:-lag], x[lag:])[0, 1]
    assert abs(r - math.exp(-1)) < 0.06, r


def test_drop_ramp_becomes_a_fine_staircase():
    a0, sub = 37.0, 0.5
    s = cp.atten_schedule(cp.step_knots(5.0, -5.0), a0, sub)
    assert schedule_at(s, 0) == a0 - 5 and schedule_at(s, 179) == a0 - 5
    assert schedule_at(s, 300) == a0 + 5 and schedule_at(s, 779) == a0 + 5 and schedule_at(s, 5000) == a0 + 5
    vals = [schedule_at(s, t) for t in range(0, 800)]
    assert max(abs(b - a) for a, b in zip(vals, vals[1:])) <= sub + 1e-9, "a ramp step exceeds sub_db"
    assert sum(1 for a, b in zip(vals, vals[1:]) if a != b) == 20, "10 dB in 0.5 dB steps"
    assert s.endswith(":0")


def test_fadeout_holds_exactly_out_seconds():
    for mins in (2, 6):
        s = cp.atten_schedule(cp.fadeout_knots(60.0 * mins), 37.0)
        deep = [t for t in range(0, 2000) if schedule_at(s, t) == 57.0]
        assert deep[0] == 300 and len(deep) == 60 * mins, (mins, deep[0], len(deep))
        assert schedule_at(s, 300 + 60 * mins) == 37.0


def test_negative_atten_refuses():
    with pytest.raises(ValueError):
        cp.atten_schedule(cp.step_knots(5.0, -5.0), 3.0)


def write_series(d, key, values, t0=1786060800):
    """values: per 2-min slot, a float (decoded), None (censored) or 'x' (inactive)."""
    os.makedirs(os.path.join(d, "spots"), exist_ok=True)
    with open(os.path.join(d, "spots", key + ".series.tsv"), "w") as f:
        f.write("epoch\tstatus\tsnr_norm_db\n")
        for i, v in enumerate(values):
            if v == "x":
                continue
            f.write(f"{t0 + i * cp.SLOT_S}\t{1 if v is None else 2}\t{'' if v is None else f'{v:.0f}'}\n")


def test_walk_selection_on_synthetic_series(tmp_path):
    """Windows of known shape: the rule must put falling windows in 'closing', rising in 'opening', flat-but-noisy
    in 'wandering'; a window with an inactive slot or < 60 % decoded never qualifies; decoded slots are shifted to
    mean 0 and censored slots sit at -20 dB."""
    rng = np.random.default_rng(3)
    n = cp.WINDOW_S // cp.SLOT_S                       # 15 slots per window
    vals = []
    for w in range(60):
        kind = w % 6
        if kind == 0:
            win = list(np.linspace(0, -15, n) + rng.normal(0, 0.5, n))       # closing
        elif kind == 1:
            win = list(np.linspace(-15, 0, n) + rng.normal(0, 0.5, n))       # opening
        elif kind == 2:
            win = list(-8 + 4 * np.sin(np.arange(n)) + rng.normal(0, 0.5, n))  # wandering
        elif kind == 3:
            win = list(-8 + rng.normal(0, 0.3, n))                           # flat, quiet
            win[7] = "x"                                                     # an inactive slot: never a candidate
        elif kind == 4:
            win = [None] * 7 + list(-8 + rng.normal(0, 0.3, n - 7))          # 8/15 decoded: < 60 %
        else:
            win = list(-8 + rng.normal(0, 0.3, n))
            win[3] = None                                                    # one censored slot
        vals += win
    write_series(str(tmp_path), "14_RX_TX", vals)
    ws = cp.wspr_windows(str(tmp_path))["20m"]
    assert len(ws) == 40, "the inactive-slot and <60%-decoded windows must be dropped"
    sel, rule = cp.classify(ws)
    assert all(w["delta"] < -10 for w in sel["closing"]) and all(w["delta"] > 10 for w in sel["opening"])
    assert len(sel["wandering"]) == 2 and all(w["resid_std"] > 2 for w in sel["wandering"])
    k = cp.walk_knots(sel["closing"][0])
    dec = [v for (t, v) in k[1:-1]]
    assert abs(np.mean(dec)) < 1e-6 and k[0][0] == 0 and k[-1][0] == cp.WINDOW_S
    cens = [w for w in ws if any(s[0] == 1 for s in w["slots"])][0]
    kc = cp.walk_knots(cens)
    assert kc[1 + 3][1] == cp.CENSORED_DB


def test_library_is_deterministic_and_emits_parseable_schedules(tmp_path):
    rng = np.random.default_rng(5)
    for key in ("14_A_B", "7_C_D"):
        write_series(str(tmp_path), key, list(np.cumsum(rng.normal(0, 1.5, 15 * 40)) % 20 - 15))
    p1, r1 = cp.library(str(tmp_path), 3)
    p2, _ = cp.library(str(tmp_path), 3)
    assert json.dumps(p1, sort_keys=True) == json.dumps(p2, sort_keys=True)
    ids = set(p1)
    assert {f"P1-20m-s{1234 + 7 * r}" for r in range(3)} <= ids and {"P2dn", "P2up", "P5-2min", "P5-6min"} <= ids
    assert sum(1 for i in ids if i.startswith("P4-20m-")) == 6 and sum(1 for i in ids if i.startswith("P4-40m-")) == 6
    for pid, pr in p1.items():
        s = cp.atten_schedule(pr["knots"], 40.0)
        assert schedule_at(s, 0) == pytest.approx(40.0 - pr["knots"][0][1], abs=0.25 + 1e-9), pid
