"""Tests for the versioned campaign-corpus contract.

The load-bearing test is the DRIFT GUARD: sweep_runner's DictWriter fieldnames must BE
results_schema.COLUMNS, so a column rename can't silently desync the writer from the
manifest and downstream readers. The rest cover the manifest declaration, the
write/read round-trip, and the reader-side type casting (read_corpus / cast_row).

Run:  cd skywave && python3 -m pytest tests/test_results_schema.py -q
"""
import csv
import json

import sweep_runner                       # the writer side
import results_schema
from results_schema import (
    COLUMNS, COLUMN_TYPES, RESULTS_SCHEMA,
    manifest, write_manifest, read_manifest, read_corpus, cast_row, manifest_path,
)


def test_sweep_runner_uses_schema_columns():
    # sweep_runner binds COLUMNS from results_schema and uses it verbatim as the
    # DictWriter fieldnames (main(): `cols = COLUMNS`), so they cannot drift apart.
    assert sweep_runner.COLUMNS is COLUMNS


def test_every_column_has_a_type_and_no_extras():
    assert set(COLUMN_TYPES) == set(COLUMNS)          # 1:1, no missing/extra
    assert len(COLUMNS) == len(set(COLUMNS))          # no dupes


def test_manifest_declares_schema_columns_and_types():
    m = manifest(modem="testmodem", tag="t")
    assert m["schema"] == RESULTS_SCHEMA
    assert m["columns"] == COLUMNS
    assert m["column_types"]["got"] == "int"
    assert m["column_types"]["goodput"] == "float"
    assert m["column_types"]["intact"] == "str"        # stays text, callers .lower()
    assert m["modem"] == "testmodem" and m["tag"] == "t"


def test_write_read_manifest_roundtrip(tmp_path):
    out = tmp_path / "corpus.csv"
    path = write_manifest(str(out), schema=RESULTS_SCHEMA, modem="mercury",
                          tag="demo", cells=3, runs=9, rig_gen=7)
    assert path == manifest_path(str(out)) and (tmp_path / "corpus.csv.manifest.json").exists()
    m = read_manifest(str(out))
    assert m["schema"] == RESULTS_SCHEMA
    assert m["modem"] == "mercury" and m["cells"] == 3 and m["runs"] == 9
    assert m["rig_gen"] == 7
    assert m["columns"] == COLUMNS
    # sidecar is valid, sorted, newline-terminated JSON
    raw = (tmp_path / "corpus.csv.manifest.json").read_text()
    assert raw.endswith("\n") and json.loads(raw)["generated_by"] == "sweep_runner"


def test_read_manifest_absent_is_none(tmp_path):
    assert read_manifest(str(tmp_path / "no_such.csv")) is None


def test_read_corpus_casts_declared_types(tmp_path):
    out = tmp_path / "c.csv"
    row = {c: "" for c in COLUMNS}
    row.update(modem="testmodem", tag="t", sigma="8000", snr3k="9.3", act_rms="8198.0",
               txgain="1.0", watterson="off", payload="4096", rep="2",
               got="4096", total="4096", intact="true", goodput="512.5",
               peak_bps="1200", sn_med="26.8", elapsed="12.3", status="ok",
               rc="0", log="x.log", rig_gen="7")
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader(); w.writerow(row)
    got = list(read_corpus(str(out)))
    assert len(got) == 1
    r = got[0]
    assert r["got"] == 4096 and isinstance(r["got"], int)
    assert r["goodput"] == 512.5 and isinstance(r["goodput"], float)
    assert r["payload"] == 4096 and r["rep"] == 2 and r["rc"] == 0 and r["rig_gen"] == 7
    assert r["intact"] == "true" and isinstance(r["intact"], str)   # NOT coerced to bool
    assert r["modem"] == "testmodem"


def test_cast_row_is_tolerant():
    # blank passes through as "", an un-castable value falls back to raw, unknown column
    # stays str -- a partial/odd corpus row never crashes a reader.
    r = cast_row({"got": "", "goodput": "n/a", "extra_col": "hi"})
    assert r["got"] == "" and r["goodput"] == "n/a" and r["extra_col"] == "hi"


def test_written_corpus_header_matches_columns(tmp_path):
    # end-to-end: a DictWriter over COLUMNS (what sweep_runner does) yields a header that
    # is exactly the schema's columns, so a reader keying on COLUMNS lines up.
    out = tmp_path / "h.csv"
    with open(out, "w", newline="") as f:
        csv.DictWriter(f, fieldnames=COLUMNS).writeheader()
    with open(out, newline="") as f:
        header = next(csv.reader(f))
    assert header == COLUMNS
