"""Gate and metric tests for skywave.vector_report.

These exist because the vector family shipped with none, and every defect they
pin was found by hand-auditing a finished 5,632-cell campaign rather than by a
test. Each test names the failure it prevents.
"""
import csv
import io

import pytest

from skywave import vector_report as vr

FIELDS = [
    "adapter", "label", "family", "preset", "snr_db", "bw_hz", "sample_rate",
    "frames", "decoded", "fer", "goodput_bps", "payload_bytes", "air_s",
    "nominal_bps", "false_decode", "wrong_frame", "crc_errors", "crc_bits",
    "extra_json", "host", "arch", "driver_id", "mode_class", "clip_gain",
    "label_base", "papr_db",
]


def write_csv(tmp_path, rows):
    p = tmp_path / "sweep.csv"
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    return str(p)


def cell(label="M", preset="off", snr=0.0, frames=150, decoded=150, **kw):
    fer = 1.0 - decoded / frames
    base = dict(
        adapter="a", label=label, family=kw.pop("family", "fam"), preset=preset,
        snr_db=f"{snr:.2f}", bw_hz="2500", sample_rate="8000", frames=frames,
        decoded=decoded, fer=f"{fer:.6f}",
        goodput_bps=f"{100 * (1 - fer):.2f}", payload_bytes=100, air_s="1.0000",
        nominal_bps="800.00", false_decode=0, wrong_frame=0, crc_errors=0,
        crc_bits=16, host="h", arch="x86_64", driver_id="deadbeefcafe",
        mode_class=kw.pop("mode_class", "production"),
    )
    base.update(kw)
    return base


def curve(label, preset, fers, snr0=0.0, step=2.0, **kw):
    """One cell per FER, ascending SNR. FER is realized exactly as k/150."""
    out = []
    for i, fer in enumerate(fers):
        dec = round((1.0 - fer) * 150)
        out.append(cell(label=label, preset=preset, snr=snr0 + i * step,
                        frames=150, decoded=dec, **kw))
    return out


def run(tmp_path, rows, *args):
    path = write_csv(tmp_path, rows)
    return vr.main(["--sweep", path, *args])


# --------------------------------------------------------------- floor metric

def test_onset_boundary_curve_is_not_censored_by_float_noise():
    """3/150 == 0.02 exactly. Without an epsilon on the onset gate, whether such
    a curve gets a floor depends on whether the value arrived rounded from a CSV
    or recomputed in float -- same data, opposite verdict."""
    exact = 1.0 - 147 / 150          # 0.020000000000000018 in binary float
    assert exact > 0.02              # the trap: it really is greater
    pts = [(0.0, 0.5, 1.0), (2.0, exact, 1.0)]
    val, status = vr.floor_of(pts)
    assert status == "ok", "a curve reaching exactly the onset must keep its floor"
    assert val is not None


def test_target_boundary_is_not_treated_as_truncated():
    """A curve that reached the onset and whose TOP point sits on the target must
    not be called truncated_high. The epsilon also has to absorb float noise a
    hair above the target -- the same class of bug as the onset boundary, on the
    other gate."""
    on_target = [(0.0, 0.5, 1.0), (2.0, 0.01, 1.0), (4.0, 0.10, 1.0)]
    assert vr.floor_of(on_target)[1] == "ok"
    a_hair_over = [(0.0, 0.5, 1.0), (2.0, 0.01, 1.0), (4.0, 0.10 + 5e-10, 1.0)]
    assert vr.floor_of(a_hair_over)[1] == "ok"


def test_a_real_plateau_is_still_censored():
    """The epsilon must not become a licence to accept genuine plateaus: a curve
    whose best FER is 0.10 never reached the 0.02 onset and has no floor."""
    assert vr.floor_of([(0.0, 0.5, 1.0), (2.0, 0.10, 1.0)])[1] == "never_worked"


def test_first_and_last_crossing_differ_only_on_a_re_crossing_curve():
    mono = [(0.0, 0.5, 1.0), (2.0, 0.05, 1.0), (4.0, 0.01, 1.0)]
    assert vr.floor_of(mono)[0] == pytest.approx(vr.floor_of(mono, last=True)[0])
    # dips under, pops back above, settles under again
    wob = [(0.0, 0.5, 1.0), (2.0, 0.05, 1.0), (4.0, 0.30, 1.0), (6.0, 0.01, 1.0)]
    first = vr.floor_of(wob)[0]
    last = vr.floor_of(wob, last=True)[0]
    assert last > first, "the last crossing must be the conservative one"


def test_median_is_a_true_median_for_even_n():
    assert vr.median([1.0, 2.0, 3.0, 4.0]) == pytest.approx(2.5)
    assert vr.median([1.0, 2.0, 3.0]) == pytest.approx(2.0)


# --------------------------------------------------- false_decode Poisson gate

def test_single_false_decode_on_a_crc16_mode_is_within_chance(tmp_path, capsys):
    """The modem73 case: 1 event against ~4.5k failed CRC-16 checks. A
    zero-tolerance gate invalidates a whole campaign for arithmetic."""
    rows = curve("M", "off", [0.5, 0.05, 0.01])
    rows[0]["crc_errors"] = 4519
    rows[0]["false_decode"] = 1
    rc = run(tmp_path, rows)
    out = capsys.readouterr().out
    assert "within CRC-width chance" in out
    assert rc == 0, "a CRC-16 arithmetic collision must not invalidate"


def test_same_event_count_on_a_crc32_mode_does_invalidate(tmp_path, capsys):
    """Identical observation, 32-bit check -> a ~1-in-24,000 surprise. The gate
    must key on width, which is the whole point of it."""
    rows = curve("M", "off", [0.5, 0.05, 0.01], crc_bits=32)
    rows[0]["crc_errors"] = 4519
    rows[0]["false_decode"] = 1
    rc = run(tmp_path, rows)
    assert rc == 1
    assert "INVALIDATION" in capsys.readouterr().out


def test_missing_crc_bits_falls_back_to_zero_tolerance(tmp_path, capsys):
    rows = curve("M", "off", [0.5, 0.05, 0.01], crc_bits="")
    rows[0]["crc_errors"] = 4519
    rows[0]["false_decode"] = 1
    rc = run(tmp_path, rows)
    out = capsys.readouterr().out
    assert rc == 1
    assert "ZERO TOLERANCE" in out


def test_clean_corpus_passes_every_gate(tmp_path, capsys):
    rc = run(tmp_path, curve("M", "off", [0.5, 0.05, 0.01]))
    assert rc == 0
    assert "VERDICT: CLEAN" in capsys.readouterr().out


# ------------------------------------------------------------------- frontier

def test_frontier_excludes_a_fast_mode_that_never_holds_the_bar(tmp_path, capsys):
    """The defect that made an unusable mode top a frontier while the floor
    table called it never_worked: goodput is rate*(1-FER), so a high-rate mode
    at FER 0.42 books 58% of a big number."""
    slow = curve("SLOW", "off", [0.05, 0.01, 0.01])
    for r in slow:
        r["goodput_bps"] = "100.00"
    fast = curve("FAST", "off", [0.42, 0.42, 0.42])
    for r in fast:
        r["goodput_bps"] = "580.00"
    rc = run(tmp_path, slow + fast)
    out = capsys.readouterr().out
    top = [ln for ln in out.splitlines() if "top:" in ln][0]
    assert "SLOW" in top and "FAST" not in top
    # ...but the raw frontier is still reported, labelled, for the other question
    raw = [ln for ln in out.splitlines() if "raw (ideal-retransmission)" in ln][0]
    assert "FAST" in raw
    assert rc == 0


def test_a_mode_already_below_target_is_not_called_an_error_floor(tmp_path, capsys):
    """Caught by a live smoke, not by unit tests: a curve that is under the target
    at its LOWEST measured SNR has status truncated_low, not never_worked. Keying
    the plateau note on 'no ok floor' alone reported a mode whose best FER was
    0.000 as an 'irreducible error floor'."""
    rows = curve("EASY", "off", [0.0, 0.0])
    rc = run(tmp_path, rows)
    out = capsys.readouterr().out
    assert "irreducible" not in out
    assert "BELOW the sweep's lowest SNR" in out
    assert rc == 0


def test_plateau_member_is_named_not_reported_as_contradiction(tmp_path, capsys):
    """A mode can clear the 0.10 bar yet never reach the 0.02 onset. It belongs
    on the frontier AND has no floor; that is an irreducible error floor, and the
    report must say so rather than leaving the two sections contradicting."""
    rows = curve("PLATEAU", "off", [0.30, 0.05, 0.05])
    rc = run(tmp_path, rows)
    out = capsys.readouterr().out
    assert "irreducible" in out
    assert "scorer inconsistency" not in out
    assert rc == 0


# ------------------------------------------------------------------ censoring

def test_censoring_census_and_paired_subset_are_reported(tmp_path, capsys):
    """The naive per-channel median compares different mode populations. Both
    the census and a paired common-subset row must appear."""
    rows = []
    # A works everywhere; B only on AWGN -> populations differ per channel.
    rows += curve("A", "off", [0.5, 0.05, 0.01])
    rows += curve("A", "poor", [0.5, 0.5, 0.05])
    rows += curve("B", "off", [0.5, 0.05, 0.01])
    rows += curve("B", "poor", [0.9, 0.9, 0.9])
    out_rc = run(tmp_path, rows)
    out = capsys.readouterr().out
    assert "CENSORING CENSUS" in out
    assert "common subset" in out
    assert "PAIRED" in out
    assert "NAIVE" in out
    assert out_rc == 0


# ----------------------------------------------------------------- provenance

def test_two_driver_binaries_in_one_corpus_invalidate(tmp_path, capsys):
    rows = curve("M", "off", [0.5, 0.05, 0.01])
    rows[-1]["driver_id"] = "0123456789ab"
    rc = run(tmp_path, rows)
    assert rc == 1
    assert "distinct driver binaries" in capsys.readouterr().out


def test_absent_driver_id_is_a_note_not_an_invalidation(tmp_path, capsys):
    rows = curve("M", "off", [0.5, 0.05, 0.01])
    for r in rows:
        r["driver_id"] = ""
    rc = run(tmp_path, rows)
    out = capsys.readouterr().out
    assert rc == 0
    assert "no driver_id" in out


def test_mixed_reference_bandwidth_invalidates(tmp_path, capsys):
    rows = curve("M", "off", [0.5, 0.05, 0.01])
    rows[-1]["bw_hz"] = "3000"
    rc = run(tmp_path, rows)
    assert rc == 1
    assert "different reference" in capsys.readouterr().out


# --------------------------------------------------- frontier eligibility

def test_bench_ablation_is_held_off_the_production_frontier(tmp_path, capsys):
    """Decision 2: one corpus, but the label must be load-bearing. A -NOCLIP
    ablation that outranks a shipping mode must not be quotable as the winner."""
    prod = curve("QAM16C2", "off", [0.05, 0.01, 0.01])
    for r in prod:
        r["goodput_bps"] = "100.00"
    abl = curve("QAM16C2-NOCLIP", "off", [0.05, 0.01, 0.01], mode_class="bench")
    for r in abl:
        r["goodput_bps"] = "900.00"
    rc = run(tmp_path, prod + abl)
    out = capsys.readouterr().out
    top = [l for l in out.splitlines() if "top:" in l][0]
    assert "QAM16C2-NOCLIP" not in top and "QAM16C2" in top
    assert "HELD OFF" in out
    assert "would have made this frontier" in out   # still reported, not hidden
    assert rc == 0


def test_family_exclusion_keeps_plh_off_the_frontier(tmp_path, capsys):
    """Decision 4: PLH is goodput-dominated by construction here, so it is
    excluded from the primary frontier rather than annotated."""
    base = curve("QAM64C", "off", [0.05, 0.01, 0.01], family="qam")
    for r in base:
        r["goodput_bps"] = "100.00"
    plh = curve("PLH-QAM64C", "off", [0.05, 0.01, 0.01], family="plh")
    for r in plh:
        r["goodput_bps"] = "900.00"
    rc = run(tmp_path, base + plh, "--frontier-exclude-family", "plh")
    out = capsys.readouterr().out
    top = [l for l in out.splitlines() if "top:" in l][0]
    assert "PLH-QAM64C" not in top
    assert rc == 0


def test_eligibility_defaults_to_everything_when_unlabelled(tmp_path, capsys):
    """A corpus predating mode_class must not silently lose its frontier."""
    rows = curve("M", "off", [0.5, 0.05, 0.01])
    for r in rows:
        r["mode_class"] = ""
    rc = run(tmp_path, rows)
    out = capsys.readouterr().out
    assert "all series eligible" in out
    assert rc == 0


# ------------------------------------------------- equal-PEP lens (Decision 1)

def test_equal_pep_table_shifts_by_papr_delta(tmp_path, capsys):
    """floor_equalPEP = floor + papr - papr_ref. LO (papr 6) floors at 2 dB, HI
    (papr 10) at ~3.78 dB; with LO as the auto reference (lowest PAPR), HI's
    equal-PEP floor must read 3.78 + (10-6) = 7.8."""
    rows = curve("LO", "off", [0.5, 0.05, 0.01, 0.0], papr_db="6.00")
    rows += curve("HI", "off", [0.5, 0.5, 0.05, 0.01], papr_db="10.00")
    rc = run(tmp_path, rows)
    out = capsys.readouterr().out
    assert "EQUAL-PEP floor table" in out
    assert "ref LO @ 6.00 dB PAPR" in out
    pep_rows = out.split("EQUAL-PEP floor table")[1].splitlines()
    hi = [ln for ln in pep_rows if ln.strip().startswith("HI")][0]
    assert "7.8" in hi
    assert rc == 0


def test_lens_order_flip_is_reported(tmp_path, capsys):
    """The pre-registered escalation trigger: a clipped mode loses at equal
    average power but wins at equal PEP. A floors 1.9 dB better than B, but
    carries 4 dB more PAPR -> order flips; the report must name the pair."""
    rows = curve("NOCLIP", "off", [0.5, 0.05, 0.01, 0.0], papr_db="12.00")   # floor ~1.8
    rows += curve("CLIP", "off", [0.5, 0.5, 0.05, 0.01], papr_db="8.00")  # ~2 dB later
    rc = run(tmp_path, rows)
    out = capsys.readouterr().out
    assert "LENS DISAGREEMENTS" in out
    assert rc == 0


def test_missing_papr_skips_pep_lens_without_failing(tmp_path, capsys):
    rows = curve("M", "off", [0.5, 0.05, 0.01], papr_db="")
    rc = run(tmp_path, rows)
    out = capsys.readouterr().out
    assert "equal-PEP lens: SKIPPED" in out
    assert rc == 0


# ------------------------------------------------ paired series (Decision 4)

def write_pairs(tmp_path, spec):
    import json
    p = tmp_path / "pairs.json"
    p.write_text(json.dumps(spec))
    return str(p)


def test_identical_config_pair_that_ties_passes_the_gate(tmp_path, capsys):
    """Same FER curve on both sides -> no Wilson-disjoint point, gate PASS."""
    fers = [0.9, 0.5, 0.05, 0.01]
    rows = curve("PLH-X", "off", fers, family="plh", papr_db="9.00")
    rows += curve("X", "off", fers, papr_db="9.60")
    pairs = write_pairs(tmp_path, [{"a": "PLH-X", "b": "X",
                                    "config_identical": True}])
    rc = run(tmp_path, rows, "--pairs", pairs,
             "--frontier-exclude-family", "plh")
    out = capsys.readouterr().out
    assert "TIE GATE: PASS" in out
    assert "CAVEAT" not in out
    assert rc == 0


def test_identical_config_pair_with_a_real_gap_fails_the_gate(tmp_path, capsys):
    """~2 dB of curve shift at N=150 separates far beyond the Wilson interval.
    The gate must call it an INSTRUMENT fault and the verdict must carry the
    caveat -- while staying exit 0, per the pre-registered gate list (caveat,
    not invalidation)."""
    rows = curve("PLH-X", "off", [0.9, 0.9, 0.5, 0.05, 0.01], family="plh",
                 papr_db="9.00")
    rows += curve("X", "off", [0.5, 0.05, 0.01, 0.0, 0.0], papr_db="9.60")
    pairs = write_pairs(tmp_path, [{"a": "PLH-X", "b": "X",
                                    "config_identical": True}])
    rc = run(tmp_path, rows, "--pairs", pairs,
             "--frontier-exclude-family", "plh")
    out = capsys.readouterr().out
    assert "TIE GATE: FAILED" in out
    assert "INSTRUMENT fault" in out
    assert "CAVEAT" in out
    assert rc == 0, "tie-gate failure is a caveat, not an invalidation"


def test_non_identical_pair_carries_no_tie_gate(tmp_path, capsys):
    """A real A/B (different configs) reports deltas but must not be judged
    against the tie expectation."""
    rows = curve("PLH-Y", "off", [0.9, 0.5, 0.05, 0.01], family="plh",
                 papr_db="8.40")
    rows += curve("Y", "off", [0.5, 0.05, 0.01, 0.0], papr_db="8.10")
    pairs = write_pairs(tmp_path, [{"a": "PLH-Y", "b": "Y",
                                    "config_identical": False}])
    rc = run(tmp_path, rows, "--pairs", pairs,
             "--frontier-exclude-family", "plh")
    out = capsys.readouterr().out
    assert "a real A/B" in out
    assert "TIE GATE" not in out
    assert rc == 0


def test_pair_member_missing_from_corpus_is_said_not_dropped(tmp_path, capsys):
    rows = curve("X", "off", [0.5, 0.05, 0.01])
    pairs = write_pairs(tmp_path, [{"a": "GHOST", "b": "X",
                                    "config_identical": True}])
    rc = run(tmp_path, rows, "--pairs", pairs)
    out = capsys.readouterr().out
    assert "NOT IN CORPUS" in out
    assert rc == 0


def test_pair_deltas_report_both_lenses_and_airtime(tmp_path, capsys):
    """Decision 4's fields: dfloor under BOTH lenses and the airtime deficit
    must appear for a pair where both sides have floors."""
    rows = curve("PLH-Z", "off", [0.9, 0.5, 0.05, 0.01], family="plh",
                 papr_db="9.00", air_s="2.8260")
    rows += curve("Z", "off", [0.5, 0.05, 0.01, 0.0], papr_db="9.60",
                  air_s="2.6000")
    pairs = write_pairs(tmp_path, [{"a": "PLH-Z", "b": "Z",
                                    "config_identical": False}])
    rc = run(tmp_path, rows, "--pairs", pairs,
             "--frontier-exclude-family", "plh")
    out = capsys.readouterr().out
    assert "airtime  2.826 vs 2.600" in out
    assert "d(avgpwr)" in out and "d(PEP)" in out
    assert rc == 0


def test_unreadable_pairs_file_is_incomplete_not_crash(tmp_path, capsys):
    rows = curve("M", "off", [0.5, 0.05, 0.01])
    rc = run(tmp_path, rows, "--pairs", str(tmp_path / "nope.json"))
    out = capsys.readouterr().out
    assert rc == 2
    assert "INCOMPLETE" in out
