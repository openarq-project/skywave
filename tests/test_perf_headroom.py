"""Real-time headroom guard for the Python hot path.

The block budget is 21.3 ms (1024 frames @ 48 kHz). Measured on a development machine:
worst realistic config (HD + Rapp + poor fade + rig BPF both ends + 30 ms link
delay) p99 = 0.38 ms => 56x headroom; a slower target machine might be ~2x that. This test fails
if someone lands an accidentally-quadratic edit that eats the margin.
"""
import time

import numpy as np

from conftest import load_sim, make_link, feed, interleave


def test_worst_config_p99_headroom():
    from skywave import watterson
    from skywave import rig_effects as fxm
    from conftest import make_fx
    cs = load_sim(SIGMA=2000, SIM_HALF_DUPLEX=1, SIM_PA_P=2,
                  SIM_WATTERSON="poor", SIM_RIG_BPF="default",
                  SIM_LINK_DELAY_MS=30)
    fade = watterson.WattersonChannel(cs.FS, 2.0, 1.5, 60.0, 1234)
    band = cs._resolve_rig_band()
    fx = make_fx(alc=fxm.AlcOvershoot(cs.FS, 6.0, 10.0, nch=cs.NCH),
                 foff=fxm.FreqShift(cs.FS, 10.0),
                 skew=fxm.ClockSkew(cs.FS, 25.0),
                 agc=fxm.RxAgc(cs.FS),
                 imp=fxm.ImpulsiveNoise(2000.0, 6.0),
                 qrm=fxm.QrmGenerator(cs.FS, __import__("numpy").random.default_rng(7),
                                      2000.0, occupancy=0.5, sweep=True))
    link = make_link(cs, ptt=cs.PttState(), fade=fade,
                     rig_tx=cs.RigBPF(*band, cs.RIG_ORDER, cs.FS),
                     rig_rx=cs.RigBPF(*band, cs.RIG_ORDER, cs.FS),
                     fx=fx)
    n = np.arange(cs.BLOCK)
    x = interleave(cs, 12000 * np.sin(2 * np.pi * 1500 * n / cs.FS))
    times = []
    for i in range(300):
        t0 = time.perf_counter()
        feed(link, x)
        if i >= 30:
            times.append((time.perf_counter() - t0) * 1000.0)
    times.sort()
    p99 = times[int(len(times) * 0.99)]
    budget = 1000.0 * cs.BLOCK / cs.FS
    # generous 4x-under-budget bar: fails only on a real regression, not CI noise
    assert p99 < budget / 4.0, f"hot-path p99 {p99:.2f} ms vs budget {budget:.1f} ms"
