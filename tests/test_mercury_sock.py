"""MercurySockAdapter + SIM_FS -- Mercury's virtual-clock -x sock path.

Covers the pieces that make the device-free, wall-clock-free Mercury bench
correct WITHOUT needing a Mercury binary (so it runs in CI and on macOS):
the SIM_FS knob re-deriving the cable and physics at 8 kHz, the adapter's
channel/station launch mapping (lockstep sock transport at 8 kHz mono,
MERCURY_AUDIO_SOCK per station), and the registry entry. The lockstep
barrier/virtual-clock behavior itself is covered by test_virtual_lockstep.py;
the wire layout by mercury's tests/audioio/test_sock_wire.c on the C side and
test_sock_transport.py here.
"""
import os

import numpy as np
import pytest

from conftest import load_sim, make_link, feed, tone_block


# ---- SIM_FS knob (channel_sim) ----

def test_sim_fs_default_unchanged():
    cs = load_sim()
    assert cs.FS == 48000


def test_sim_fs_8k_rederives_cable_constants():
    cs = load_sim(SIM_FS=8000, SIM_LINK_DELAY_MS=3)
    assert cs.FS == 8000
    # 3 ms of link delay at 8 kHz is 24 samples (not the 144 of 48 kHz)
    assert cs.LINK_DELAY_SAMP == 24
    # virtual block time: 1024 samples at 8 kHz = 128 ms
    assert abs(cs.BLOCK_MS - 128.0) < 1e-9


def test_sim_fs_8k_rig_bpf_stable():
    """The rig BPF (150-2900 Hz) re-derives at FS=8000: stable, passband flat,
    stopband attenuated. At 48 kHz the same passband sits at normalized
    frequencies 6x lower, so this catches a filter design that only works
    at the soundcard rate."""
    cs = load_sim(SIM_FS=8000, SIM_NCH=1, SIM_RIG_BPF="data")
    b = cs.RigBPF(150, 2900, 4, cs.FS)
    rng = np.random.default_rng(7)
    y = b.process(rng.normal(0, 3000, cs.FS * 2))
    assert np.all(np.isfinite(y))
    f = np.fft.rfftfreq(len(y), 1 / cs.FS)
    P = np.abs(np.fft.rfft(y)) ** 2
    pb = P[(f > 500) & (f < 2500)].mean()
    sb = P[(f < 50) | (f > 3600)].mean()
    assert 10 * np.log10(pb / sb) > 30.0


def test_sim_fs_8k_link_passes_tone():
    """A full Link at 8 kHz mono carries a mid-band tone with sane gain."""
    cs = load_sim(SIM_FS=8000, SIM_NCH=1)
    link = make_link(cs)
    x = tone_block(cs, amp=8000.0, freq=1000.0)
    y = feed(link, x)
    assert np.all(np.isfinite(y))
    # steady-state RMS within 3 dB of the input (clean channel, unity gain)
    rin = np.sqrt(np.mean(x[cs.BLOCK // 2:].astype(np.float64) ** 2))
    rout = np.sqrt(np.mean(y[cs.BLOCK // 2:].astype(np.float64) ** 2))
    assert abs(20 * np.log10(rout / rin)) < 3.0


# ---- adapter launch mapping (no Mercury binary needed) ----

def _mk_adapter(monkeypatch, tmp_sock_dir):
    from skywave.modem_adapter import AdapterConfig
    from skywave.adapters import mercury_sock
    monkeypatch.setenv("SIM_SOCK_DIR", tmp_sock_dir)
    cfg = AdapterConfig.from_env(argv=["512", "60"], env=dict(os.environ))
    return mercury_sock.MercurySockAdapter(cfg)


def test_adapter_channel_env(monkeypatch, sock_dir):
    ad = _mk_adapter(monkeypatch, sock_dir)
    captured = {}

    def fake_launch(extra_env=None):
        captured.update(extra_env or {})
        return object()

    from skywave import bench_pipes
    monkeypatch.setattr(bench_pipes, "launch_channel_sim", fake_launch)
    ad.launch_channel()
    assert captured["SIM_TRANSPORT"] == "sock"
    assert captured["SIM_CLOCK"] == "virt_time"
    assert captured["SIM_FS"] == "8000"      # mercury native rate, forced
    assert captured["SIM_NCH"] == "1"        # mono cable, forced
    assert captured["SIM_BLOCK"] == "160"    # 20 ms key-edge granularity
    assert captured["SIM_SOCK_DIR"] == sock_dir


def test_adapter_station_launch(monkeypatch, sock_dir):
    ad = _mk_adapter(monkeypatch, sock_dir)
    calls = []

    class FakeProc:
        pass

    def fake_popen(argv, env=None, **kw):
        calls.append((argv, env))
        return FakeProc()

    from skywave.adapters import mercury_sock
    monkeypatch.setattr(mercury_sock.sp, "Popen", fake_popen)
    ad.start_stations()
    assert len(calls) == 2
    for (argv, env), station, port in zip(calls, ("a", "b"), (8300, 8310)):
        assert argv[1:3] == ["-x", "sock"]
        assert str(port) in argv
        assert env["MERCURY_AUDIO_SOCK"] == os.path.join(sock_dir, f"{station}.sock")
    assert len(ad._stations) == 2


def test_adapter_registered():
    from skywave import sweep_runner
    entry = sweep_runner.BUILTIN_ADAPTERS["mercury_sock"]
    assert entry["module"] == "skywave.adapters.mercury_sock"
    import importlib
    mod = importlib.import_module(entry["module"])
    assert mod.MercurySockAdapter.name == "mercury_sock"
