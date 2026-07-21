"""PTT relay plumbing: harness line formats -> sim stdin -> PttState."""
import io
import types

from conftest import load_sim
from skywave import bench_pipes


def fake_sim():
    return types.SimpleNamespace(stdin=io.BytesIO())


def test_fwd_ptt_token_styles():
    for line, want in [("PTT ON", b"a 1\n"), ("PTT OFF", b"a 0\n"),
                       ("PTT TRUE", b"a 1\n"), ("PTT FALSE", b"a 0\n"),
                       ("blah PTT ON blah", b"a 1\n")]:
        sim = fake_sim()
        bench_pipes.fwd_ptt(sim, "A", line)
        assert sim.stdin.getvalue() == want, line


def test_fwd_ptt_station_mapping_and_noise():
    sim = fake_sim()
    bench_pipes.fwd_ptt(sim, "B", "PTT ON")
    assert sim.stdin.getvalue() == b"b 1\n"
    sim = fake_sim()
    bench_pipes.fwd_ptt(sim, "A", "BITRATE (3) 1200 BPS")   # non-PTT line: no write
    assert sim.stdin.getvalue() == b""
    # tolerant of a missing pipe: no-ops, no exception
    bench_pipes.fwd_ptt(None, "A", "PTT ON")
    bench_pipes.fwd_ptt(types.SimpleNamespace(stdin=None), "A", "PTT ON")


def test_ptt_listener_parses_and_survives_garbage(monkeypatch):
    import sys
    cs = load_sim(SIM_HALF_DUPLEX=1, SIM_PTT=1)
    ptt = cs.PttState()
    monkeypatch.setattr(sys, "stdin", io.StringIO("a 1\nb 1\ngarbage\nx 1\na 0\n"))
    cs.ptt_listener(ptt)          # runs to EOF
    assert ptt.a is False         # last a-line was 'a 0'
    assert ptt.b is True
