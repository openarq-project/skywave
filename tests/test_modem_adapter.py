"""Tests for the ModemAdapter DUT contract.

The load-bearing test is round-trip compatibility: an AdapterResult emitted by the base
class must parse with sweep_runner's OWN regexes and classify the way sweep_runner would,
so an adapter written on this base is a drop-in for the existing framework. The rest
cover config parsing, the JSON forward contract, the intact/partial/connect-fail paths,
and seed-deterministic payloads -- all hardware-free via the in-process LoopbackAdapter.

Run:  cd skywave && python3 -m pytest tests/test_modem_adapter.py -q
"""
import contextlib
import io
import json
import re

import pytest

from skywave import sweep_runner                       # the framework side -- reuse its real parsers
from skywave.modem_adapter import AdapterConfig, AdapterResult, ModemAdapter, run_adapter
from skywave.adapters.example import LoopbackAdapter


def _run_capture(adapter_cls, argv, env):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run_adapter(adapter_cls, argv=argv, env=env)
    return rc, buf.getvalue()


def _parse_like_sweep_runner(txt):
    """Replicate run_cell()'s parse: find RESULT, then apply the RES_* regexes to the seg."""
    m = re.search(r"\bRESULT\b", txt)
    assert m, "no RESULT token in adapter output"
    seg = txt[m.start():m.start() + 400]
    got, tot = sweep_runner.RES_BYTES.search(seg).groups()
    return {
        "got": int(got), "total": int(tot),
        "in_s": float(sweep_runner.RES_IN.search(seg).group(1)),
        "intact": sweep_runner.RES_INTACT.search(seg).group(1),
        "goodput": float(sweep_runner.RES_GP.search(seg).group(1)),
        "peak": int(sweep_runner.RES_PEAK.search(seg).group(1)),
        "sn": float(sweep_runner.RES_SN.search(seg).group(1)),
    }


def test_config_from_env_and_argv():
    cfg = AdapterConfig.from_env(
        argv=["8192", "90"],
        env={"SIGMA": "2000", "TXGAIN": "0.8", "SEED": "77", "SIM_HALF_DUPLEX": "1",
             "SIM_PTT": "1", "SIM_WATTERSON": "poor", "NP_STATS": "/tmp/x"})
    assert cfg.payload_bytes == 8192 and cfg.timeout_s == 90.0
    assert cfg.sigma == "2000" and cfg.txgain == "0.8" and cfg.seed == 77
    assert cfg.half_duplex and cfg.ptt and cfg.watterson == "poor"
    assert cfg.np_stats == "/tmp/x"


def test_config_defaults():
    cfg = AdapterConfig.from_env(argv=[], env={})
    assert cfg.payload_bytes == 4096 and cfg.timeout_s == 120.0
    assert cfg.sigma == "0" and cfg.txgain == "1.0" and not cfg.half_duplex


def test_result_line_round_trips_through_sweep_runner():
    """A clean transfer's RESULT line parses with sweep_runner's regexes AND classifies
    as `ok` under its own logic (got>=total and intact truthy)."""
    rc, out = _run_capture(LoopbackAdapter, ["4096", "10"], {"SIGMA": "0", "SEED": "1"})
    p = _parse_like_sweep_runner(out)
    assert p["got"] == 4096 and p["total"] == 4096
    assert p["intact"].lower() in ("true", "1")           # sweep_runner's ok test
    assert p["goodput"] > 0 and p["peak"] == 600 and p["sn"] == 12.0
    assert rc == 0                                          # base: intact -> exit 0


def test_result_json_forward_contract():
    _, out = _run_capture(LoopbackAdapter, ["2048", "10"], {"SIGMA": "0", "SEED": "1"})
    line = next(l for l in out.splitlines() if l.startswith("RESULT_JSON "))
    d = json.loads(line[len("RESULT_JSON "):])
    assert d["schema"] == "modem-adapter-result/1"
    assert d["got"] == 2048 and d["total"] == 2048 and d["intact"] is True
    assert set(d) >= {"schema", "got", "total", "seconds", "intact", "goodput",
                      "peak_bitrate", "sn_med"}


def test_partial_transfer_classifies_partial():
    """An absurd noise level drops half the payload: got>0 but not intact -> sweep_runner
    would call this `partial`, and the base returns exit 2."""
    rc, out = _run_capture(LoopbackAdapter, ["4096", "10"], {"SIGMA": "40000", "SEED": "1"})
    p = _parse_like_sweep_runner(out)
    assert 0 < p["got"] < p["total"]
    assert p["intact"].lower() not in ("true", "1")
    assert rc == 2


def test_fail_connect_emits_noconn_token():
    """A station that never comes ready -> the base prints the NOCONN token sweep_runner
    keys `fail_connect` on, and returns exit 1."""
    class DeadAdapter(LoopbackAdapter):
        def wait_ready(self, deadline):
            return False

    rc, out = _run_capture(DeadAdapter, ["4096", "10"], {"SIGMA": "0"})
    assert "NOCONN" in out                                 # sweep_runner: fail_connect trigger
    assert not re.search(r"\bRESULT\b", out)               # no result on a connect failure
    assert rc == 1


def test_seeded_payload_deterministic():
    a = LoopbackAdapter(AdapterConfig(payload_bytes=1000, seed=5)).make_payload()
    b = LoopbackAdapter(AdapterConfig(payload_bytes=1000, seed=5)).make_payload()
    c = LoopbackAdapter(AdapterConfig(payload_bytes=1000, seed=6)).make_payload()
    assert a == b and a != c and len(a) == 1000


def test_result_line_exact_format():
    """Guard the literal token grammar the framework depends on."""
    line = AdapterResult(got=100, total=100, seconds=2.5, intact=True,
                         goodput=40.0, peak_bitrate=600, sn_med=12.0).result_line()
    assert line == ("RESULT: 100/100 B in 2.5s intact=True goodput=40.0 B/s "
                    "| peak_bitrate=600bps | SN_med=12.0")
