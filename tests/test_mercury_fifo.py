"""The device-free Mercury fifo bridge (adapters/mercury_fifo._FifoBridge).

The full adapter needs the Mercury binary, but the bridge itself -- fifo open
handshake, s32le<->s16 conversion, hfchan filtering, and 8 kHz RX pacing -- is
exercised here with a Python stand-in for the modem, so it runs in CI and on
macOS with no Mercury and no soundcard.
"""
import os
import sys
import time

import numpy as np
import pytest

from skywave.modem_adapter import AdapterConfig
from skywave.adapters.mercury_fifo import _FifoBridge, FS, PACE_BLOCK

HFCHAN = [sys.executable, "-u", "-m", "skywave.hfchan"]


def test_hfchan_args_map_config():
    cfg = AdapterConfig(sigma="150", txgain="1.3", watterson="poor", seed=1000)
    args = _FifoBridge({}, cfg, ["hfchan"])._hfchan_args(1011)
    assert args[args.index("--Fs") + 1] == str(FS)          # 8 kHz, no resample
    assert args[args.index("--block") + 1] == str(PACE_BLOCK)
    assert args[args.index("--seed") + 1] == "1011"
    assert args[args.index("--gain") + 1] == "1.3"          # <- txgain
    assert args[args.index("--sigma") + 1] == "150"         # <- channel_sim-native noise scale
    assert args[args.index("--fade") + 1] == "poor"         # <- watterson


def test_hfchan_args_clean_channel_omits_sigma_and_fade():
    cfg = AdapterConfig(sigma="0", txgain="1.0", watterson="off", seed=1)
    args = _FifoBridge({}, cfg, ["hfchan"])._hfchan_args(12)
    assert "--sigma" not in args        # clean -> hfchan's own --No default
    assert "--fade" not in args


def _open_write_nonblock(path, deadline):
    """O_WRONLY|O_NONBLOCK on a fifo raises ENXIO until the reader (the bridge)
    opens; retry until it does. Mirrors what a real Mercury does."""
    while time.monotonic() < deadline:
        try:
            return os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            time.sleep(0.02)
    raise TimeoutError(f"no reader on {path}")


def test_fifo_bridge_carries_audio_clean(sock_dir):
    """A tone written to A's TX fifo emerges, filtered but with real energy, on
    B's RX fifo -- i.e. the forward direction actually carries modulated audio
    through hfchan with no soundcard. Clean channel so delivery is deterministic."""
    fifos = {k: os.path.join(sock_dir, f"{k}.s32le.fifo")
             for k in ("a_rx", "a_tx", "b_rx", "b_tx")}
    for p in fifos.values():
        os.mkfifo(p, 0o600)

    cfg = AdapterConfig(sigma="0", txgain="1.0", watterson="off", seed=7)
    bridge = _FifoBridge(fifos, cfg, HFCHAN)
    bridge.start()

    deadline = time.monotonic() + 15.0
    a_tx = _open_write_nonblock(fifos["a_tx"], deadline)          # we are station A's TX
    b_rx = os.open(fifos["b_rx"], os.O_RDONLY | os.O_NONBLOCK)    # we are station B's RX
    try:
        # 0.4 s of a 1500 Hz tone in the SSB passband, as s32le (bridge uses >>16).
        n = int(0.4 * FS)
        t = np.arange(n)
        tone = (8000.0 * np.sin(2 * np.pi * 1500.0 * t / FS)).astype(np.int32) << 16
        payload = tone.astype("<i4").tobytes()
        mv = memoryview(payload)
        while mv and time.monotonic() < deadline:               # non-blocking drip in
            try:
                mv = mv[os.write(a_tx, mv):]
            except BlockingIOError:
                time.sleep(0.005)

        got = bytearray()
        want = n * 4                                            # bytes we hope to see back
        while len(got) < want and time.monotonic() < deadline:
            try:
                chunk = os.read(b_rx, 65536)
            except BlockingIOError:
                chunk = b""
            if chunk:
                got += chunk
            else:
                time.sleep(0.01)
    finally:
        os.close(a_tx)
        os.close(b_rx)
        bridge.stop()

    rx = np.frombuffer(bytes(got[: len(got) & ~3]), dtype="<i4")
    assert rx.size >= n // 2, f"only {rx.size} samples returned (want ~{n})"
    # int32 payload lives in the high word; measure energy on the s16-scale signal.
    s16 = (rx >> 16).astype(np.float64)
    assert np.sqrt(np.mean(s16 * s16)) > 100.0, "delivered audio is ~silent"
