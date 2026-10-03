"""tools/qrm_scenario_select.py (B2, real-world channel campaign): the scenario filters, the Q6 ends rule, no
capture reused, a deterministic natural draw, and env blocks that channel_sim's own spec parser reads back.

Synthetic features rows only (the real corpus is not in the repo). pandas is a tools-only dependency."""
import json
import os
import sys

import pytest

pd = pytest.importorskip("pandas")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import qrm_scenario_select as qs  # noqa: E402

COLS = dict(centre_khz=0.0, seconds=120.0, adc_ov=0, slice_khz="", busy10=0.0, detect3=0.0, inr_p50=1.0,
            inr_p90=2.0, inr_max=3.0, first10s_inr=1.0, carrier_frac=0.0, carrier_khz=0.0, carrier_narrow=0,
            bw_carrier=0.0, bw_narrow=0.0, bw_wide=0.0, bw_broad=0.0, wide_frac=0.0, ac15=0.0, ac_peak_lag=0,
            ac_peak=0.0, peak_sigma=5.0, floor_dbm_hz=-130.0, contest=0, weekend=0)
CENTRE = {("20m", "gw"): (14097.0, -2100), ("20m", "park"): (14108.0, -1500), ("30m", "park"): (10133.0, -1500),
          ("30m", "ft8"): (10133.0, 2000), ("40m", "park"): (7107.0, 2000), ("40m", "gw"): (7101.9, -1800)}


def row(i, station, band, role, hour, **kw):
    centre, dial = CENTRE[(band, role)]
    r = dict(COLS, file=f"{station}_{band}_{role}_{i:04d}", dir="out4/A", station=station, band=band,
             centre_khz=centre, dial_hz=dial, role="park" if role == "ft8" else role, local_hour=hour,
             start_utc=f"2026-09-{1 + i % 28:02d}T{hour:02d}:00:00")
    r.update(kw)
    return r


def corpus():
    rows, i = [], 0
    busy = dict(busy10=0.3)
    for st in ("coventryOH", "youngsvilleNC", "elizabethcityNC", "laurelspringsNC"):
        for k in range(6):
            i += 1; rows.append(row(i, st, "20m", "gw", 9 + k, **busy))            # S1 candidates
            i += 1; rows.append(row(i, st, "20m", "park", 10 + k, **busy))         # S2
            i += 1; rows.append(row(i, st, "30m", "park", 8 + k))                  # S3 natural (quiet by day)
            i += 1; rows.append(row(i, st, "30m", "ft8", 8 + k, busy10=0.5))       # excluded from S3
            i += 1; rows.append(row(i, st, "40m", "park", 9 + k, **busy))          # S4
            i += 1; rows.append(row(i, st, "40m", "park", (20 + 2 * k) % 24, **busy))  # S5 (wraps midnight)
            i += 1; rows.append(row(i, st, "40m", "park", 21, contest=1, weekend=1, **busy))  # S6
            i += 1; rows.append(row(i, st, "40m", "gw", 3, inr_p90=0.5))           # S7 quiet
        # traps: right stratum, wrong hour / day type
        i += 1; rows.append(row(i, st, "20m", "gw", 18, **busy))                   # 18:00 is outside 08-18
        i += 1; rows.append(row(i, st, "20m", "gw", 10, weekend=1, **busy))        # weekend
        i += 1; rows.append(row(i, st, "40m", "park", 4, **busy))                  # 04:00 is outside 20-04
    return pd.DataFrame(rows)


def cut(per=4, seed=7):
    return qs.cut(qs.prepare(corpus()), per, "~/qrm-replay", seed)


def entries(s):
    return (s["ab"] or []) + (s["ba"] or [])


def test_filters_hold_per_scenario():
    scen, _ = cut()
    for sid in ("S1", "S2", "S3", "S4", "S5", "S6", "S7"):
        es = entries(scen[sid])
        assert es, sid
        assert not any(e["station"] == "laurelspringsNC" for e in es), sid
    hours = lambda sid: {e["local_hour"] for e in entries(scen[sid])}
    assert all(e["band"] == "20m" for e in entries(scen["S1"]) + entries(scen["S2"]))
    assert hours("S1") <= set(range(8, 18)) and hours("S2") <= set(range(8, 18))
    assert all(e["band"] == "30m" and e["dial_hz"] != 2000 for e in entries(scen["S3"])), "FT8 slice leaked into S3"
    assert hours("S5") <= {20, 21, 22, 23, 0, 1, 2, 3}
    assert hours("S5") & {0, 1, 2, 3}, "the 20-04 window must wrap midnight"
    assert all(e["busy10"] > 0 and e["band"] == "40m" for e in entries(scen["S6"]))
    assert all(e["inr_p90"] < 1.0 for e in entries(scen["S7"]))


def test_no_capture_reused_across_scenarios():
    scen, _ = cut()
    files = [e["file"] for sid in ("S1", "S2", "S3", "S4", "S5", "S6", "S7") for e in entries(scen[sid])]
    assert len(files) == len(set(files))


def test_ends_rule_and_q6_forced_shared():
    scen, notes = cut()
    for sid in ("S2", "S3", "S4", "S5", "S7"):
        s = scen[sid]
        assert s["ends"] == "two-site", sid
        assert s["station_a_hears"] == ["coventryOH"], sid                      # Ohio end
        assert len(s["station_b_hears"]) == 1 and s["station_b_hears"][0].endswith("NC"), sid
        assert "SIM_QRM_REPLAY_BA" in s["env"]
    for sid in ("S1", "S6"):
        assert scen[sid]["ends"] == "shared" and scen[sid]["ba"] is None and "SIM_QRM_REPLAY_BA" not in scen[sid]["env"]
    assert any(n.startswith("S1:") for n in notes) and any(n.startswith("S6:") for n in notes)


def test_shared_when_a_site_is_thin():
    """S4's daytime 40 m candidates left at ONE site: no second end has >= 4, so the list is shared."""
    d = corpus()
    s4_like = (d.band == "40m") & (d.contest == 0) & (d.inr_p90 >= 1) & (d.local_hour >= 8) & (d.local_hour < 18)
    scen, _ = qs.cut(qs.prepare(d[~s4_like | d.station.eq("coventryOH")]), 4, "~/qrm-replay", 7)
    assert scen["S4"]["ends"] == "shared"
    assert scen["S4"]["ends_by_rule"] == "shared" and scen["S5"]["ends"] == "two-site"


def test_natural_draw_is_deterministic_and_seeded():
    a, _ = cut(seed=7)
    b, _ = cut(seed=7)
    c, _ = cut(seed=8)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert [e["file"] for e in entries(a["S3"])] != [e["file"] for e in entries(c["S3"])]


def test_twins_alias_the_base_lists_with_moderate_fading():
    scen, _ = cut()
    for twin, base in qs.TWINS:
        t, b = scen[twin], scen[base]
        assert t["alias_of"] == base and t["env"]["SIM_WATTERSON"] == "moderate"
        for k in ("SIM_QRM_REPLAY", "SIM_QRM_REPLAY_BA"):
            assert t["env"].get(k) == b["env"].get(k)
    assert scen["S4"]["env"]["SIM_FADE_DOPPLER_HZ"] == "0.05" and "SIM_WATTERSON" not in scen["S4"]["env"]


def test_env_specs_parse_back_through_channel_sim():
    """Every env spec reads back through channel_sim's own parser as (path, dial) pairs matching the manifest."""
    from skywave.channel_sim import qrm_replay_files
    scen, _ = cut()
    for sid, s in scen.items():
        if "alias_of" in s:
            continue
        for key, es in (("SIM_QRM_REPLAY", s["ab"]), ("SIM_QRM_REPLAY_BA", s["ba"])):
            if es is None:
                continue
            got = qrm_replay_files(s["env"][key])
            want = sorted((os.path.expanduser(f"~/qrm-replay/{e['dir']}/{e['file']}.wav"), float(e["dial_hz"]))
                          for e in es)
            assert got == want, (sid, key)
