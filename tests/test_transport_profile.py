"""Tests for the declarative harness-transport profile.

Covers the parse/validate surface, the SIM_* env mapping, setdefault precedence (explicit
env wins over the profile), and that the three shipped example profiles are valid. The
load-bearing test is END-TO-END: channel_sim brought up over the SOCKET transport selected
PURELY by SIM_TRANSPORT_PROFILE (no SIM_TRANSPORT env, no ALSA devices) delivers samples
byte-identical to the Link.process ground truth -- proving the portable, aloop-free path
works through the profile alone.

Run:  cd skywave && python3 -m pytest tests/test_transport_profile.py -q
"""
import os
import socket
import subprocess as sp
import sys
import time

import numpy as np
import pytest

from conftest import REPO_ROOT, load_sim, make_link, feed, tone_block
from skywave import sock_frames
from skywave import transport_profile as tp

TRANSPORTS = os.path.join(REPO_ROOT, "transports")


def _write(tmp_path, text, name="t.toml"):
    p = tmp_path / name
    p.write_text(text)
    return str(p)


# ------------------------------------------------------------------ validate/map

def test_unknown_section_and_key_rejected(tmp_path):
    with pytest.raises(SystemExit):
        tp.load_profile(_write(tmp_path, "[bogus]\nx = 1\n"))
    with pytest.raises(SystemExit):
        tp.load_profile(_write(tmp_path, "[transport]\nnope = 1\n"))


def test_enum_and_cross_field_validation(tmp_path):
    with pytest.raises(SystemExit):
        tp.load_profile(_write(tmp_path, '[transport]\nkind = "usb"\n'))
    with pytest.raises(SystemExit):
        tp.load_profile(_write(tmp_path, '[transport]\nclock = "ntp"\n'))
    # clock=virt_time with an explicit non-sock kind is a contradiction -> reject
    with pytest.raises(SystemExit):
        tp.load_profile(_write(tmp_path, '[transport]\nkind = "alsa"\nclock = "virt_time"\n'))
    # clock=virt_time with kind unset is fine (env may supply kind=sock later)
    tp.load_profile(_write(tmp_path, '[transport]\nclock = "virt_time"\n'))


def test_to_sim_env_maps_and_formats(tmp_path):
    prof = tp.load_profile(_write(
        tmp_path,
        '[transport]\nkind = "sock"\nclock = "real_time"\nsock_dir = "/tmp/x"\n'
        "sock_buf = 32768\naccept_s = 12\nmax_virtual_s = 900\nshim = true\n"))
    env = tp.to_sim_env(prof)
    assert env["SIM_TRANSPORT"] == "sock"
    assert env["SIM_CLOCK"] == "real_time"
    assert env["SIM_SOCK_DIR"] == "/tmp/x"
    assert env["SIM_SOCK_BUF"] == "32768"
    assert env["SIM_SOCK_ACCEPT_S"] == "12"      # int-valued float rendered clean
    assert env["SIM_MAX_VIRTUAL_S"] == "900"
    assert env["SIM_SOCK_SHIM"] == "1"


def test_shim_false_maps_to_zero(tmp_path):
    prof = tp.load_profile(_write(tmp_path, "[transport]\nshim = false\n"))
    assert tp.to_sim_env(prof)["SIM_SOCK_SHIM"] == "0"


# ------------------------------------------------------------------ apply_to_environ

def test_apply_setdefault_env_wins(tmp_path):
    prof = _write(tmp_path, '[meta]\nname = "p"\n[transport]\nkind = "sock"\nclock = "real_time"\n')
    env = {"SIM_TRANSPORT_PROFILE": prof, "SIM_TRANSPORT": "alsa"}   # explicit env preset
    name = tp.apply_to_environ(env)
    assert name == "p"
    assert env["SIM_TRANSPORT"] == "alsa"        # profile did NOT override the preset
    assert env["SIM_CLOCK"] == "real_time"            # unset key filled from the profile


def test_apply_noop_when_unset():
    env = {"SIM_TRANSPORT": "alsa"}
    assert tp.apply_to_environ(env) is None
    assert env == {"SIM_TRANSPORT": "alsa"}


# ------------------------------------------------------------------ shipped examples

@pytest.mark.parametrize("name,kind,clock", [
    ("alsa-native.toml", "alsa", "real_time"),
    ("sock-real_time.toml", "sock", "real_time"),
    ("sock-virt_time.toml", "sock", "virt_time"),
])
def test_shipped_profiles_valid(name, kind, clock):
    env = tp.to_sim_env(tp.load_profile(os.path.join(TRANSPORTS, name)))
    assert env["SIM_TRANSPORT"] == kind
    assert env["SIM_CLOCK"] == clock


# ------------------------------------------- end-to-end: sock selected via profile only

def test_channel_sim_over_sockets_via_profile(tmp_path):
    """channel_sim as a real process, sock transport selected ONLY by
    SIM_TRANSPORT_PROFILE (SIM_TRANSPORT absent from env), no ALSA: B's delivered frames
    are byte-identical to the Link.process ground truth."""
    cs = load_sim(SIGMA=150, SEED=555)
    blocks = [tone_block(cs, block_index=i) for i in range(4)]
    ref = make_link(cs, seed=555 + 11)
    want = [feed(ref, b) for b in blocks]

    env = dict(os.environ)
    env.update({"SIM_TRANSPORT_PROFILE": os.path.join(TRANSPORTS, "sock-real_time.toml"),
                "SIM_SOCK_DIR": str(tmp_path), "SIM_SOCK_ACCEPT_S": "10",
                "SIGMA": "150", "SEED": "555", "TXGAIN": "1.0",
                "SIM_NCH": "2", "SIM_BLOCK": "1024"})
    # prove the PROFILE selects sock: no explicit transport env, and clear leftovers
    for k in ("SIM_TRANSPORT", "SIM_CLOCK", "NP_STATS", "SIM_TXDUMP", "SIM_KEYLOG",
              "SIM_HALF_DUPLEX", "SIM_PTT", "SIM_SOCK_SHIM"):
        env.pop(k, None)
    import skywave
    sim = sp.Popen([sys.executable, "-u", "-m", "skywave.channel_sim"],
                   env=skywave.child_env(env), cwd=REPO_ROOT, stderr=sp.PIPE)
    try:
        def connect(name):
            path = os.path.join(str(tmp_path), name)
            deadline = time.monotonic() + 10.0
            while True:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    s.connect(path)
                    return s
                except (FileNotFoundError, ConnectionRefusedError):
                    s.close()
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.05)

        sa, sb = connect("a.sock"), connect("b.sock")
        rxb = sb.makefile("rb")
        out = bytearray(cs.NBYTES)
        got = []
        for i, b in enumerate(blocks):
            sa.sendall(sock_frames.pack_station(i, sock_frames.PTT_UNKNOWN,
                                                cs.BLOCK, b.tobytes()))
            hdr = sock_frames.recv_into(rxb, sock_frames.HDR_SIM, memoryview(out))
            assert hdr is not None and hdr[2] == cs.BLOCK
            got.append(np.frombuffer(bytes(out), dtype="<i2"))
        sa.close(); sb.close()
        for i, (w, g) in enumerate(zip(want, got)):
            assert np.array_equal(w, g), f"block {i} diverged (profile-selected sock)"
    finally:
        sim.terminate()
        try:
            sim.wait(timeout=5.0)
        except sp.TimeoutExpired:
            sim.kill()
            sim.wait()
