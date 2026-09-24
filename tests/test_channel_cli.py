"""skywave-channel's command line (channel_cli.py + sim_knobs.py).

Every channel_sim environment variable is also a flag by one naming rule; the
table of settings must equal what channel_sim actually reads; a flag beats
the environment, which beats a profile; both launch paths (the console
script and `python -m skywave.channel_sim`) take flags; shipped profiles
resolve by bare name.
"""
import importlib
import os
import re
import subprocess as sp
import sys

import pytest

import skywave
from conftest import REPO_ROOT, load_sim

from skywave import channel_cli, channel_profile, transport_profile
from skywave.sim_knobs import GROUPS, KNOBS

SIM_SRC = os.path.join(REPO_ROOT, "src", "skywave", "channel_sim.py")
# Read only to hard-error on (the retired QRM knobs), so they are not settings.
RETIRED = {"SIM_QRM_CW_LAMBDA", "SIM_QRM_CW_SNR_DB", "SIM_QRM_SWEEP_SNR_DB"}


def test_knob_table_is_what_channel_sim_reads():
    src = open(SIM_SRC).read()
    read = set(re.findall(r'environ(?:\.get)?\(?\[?"([A-Z0-9_]+)"', src))
    read |= set(re.findall(r'setdefault\("([A-Z0-9_]+)"', src))
    read |= {"SIM_PROFILE", "SIM_TRANSPORT_PROFILE"}    # read by the profile loaders
    table = [k[0] for k in KNOBS]
    assert len(table) == len(set(table)), "a setting is listed twice"
    assert set(table) == read - RETIRED, (
        f"missing from sim_knobs.py: {sorted(read - RETIRED - set(table))}; "
        f"listed but not read: {sorted(set(table) - read)}")
    for env, group, kind, metavar, text in KNOBS:
        assert group in GROUPS and kind in ("flag", "value"), env
        assert text and len(text) <= 95, env


def test_flag_names_follow_the_rule_and_are_unique():
    assert channel_cli.flag_name("SIM_WATTERSON") == "--watterson"
    assert channel_cli.flag_name("SIM_FADE_SCHEDULE") == "--fade-schedule"
    assert channel_cli.flag_name("SIGMA") == "--sigma"
    assert channel_cli.flag_name("NP_STATS") == "--np-stats"
    names = [channel_cli.flag_name(k[0]) for k in KNOBS]
    names += [a for al in channel_cli.ALIASES.values() for a in al]
    assert len(names) == len(set(names)), "two settings map to one flag"


def test_flags_write_the_environment_and_beat_it(monkeypatch):
    monkeypatch.setenv("SIGMA", "5")
    monkeypatch.setenv("SIM_BLOCK", "512")
    for k in ("SIM_WATTERSON", "SIM_HALF_DUPLEX", "SIM_PTT", "SIM_RX_PAD_DB"):
        monkeypatch.delenv(k, raising=False)
    channel_cli.apply_argv(["--sigma", "300", "--fade", "poor", "--half-duplex",
                            "--no-ptt", "--rx-pad-db", "-12"])
    assert os.environ["SIGMA"] == "300"                 # flag beats the variable
    assert os.environ["SIM_WATTERSON"] == "poor"        # the --fade alias
    assert os.environ["SIM_HALF_DUPLEX"] == "1"
    assert os.environ["SIM_PTT"] == "0"
    assert os.environ["SIM_RX_PAD_DB"] == "-12"         # a negative value
    assert os.environ["SIM_BLOCK"] == "512"             # not given: untouched


@pytest.mark.parametrize("argv", [["--sigmaa", "1"], ["--sig", "1"], ["stray"]])
def test_unknown_or_abbreviated_flags_are_rejected(argv):
    with pytest.raises(SystemExit) as e:
        channel_cli.apply_argv(argv)
    assert e.value.code == 2


def test_help_lists_every_group_and_flag(capsys):
    with pytest.raises(SystemExit) as e:
        channel_cli.apply_argv(["--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    for g in GROUPS:
        assert g in out
    for env, *_ in KNOBS:
        assert channel_cli.flag_name(env) in out, env


def test_precedence_flag_over_env_over_profile(monkeypatch):
    try:
        cs = load_sim()                      # a clean SIM_* environment
        monkeypatch.setenv("SIGMA", "999")
        monkeypatch.setenv("SIM_RX_PAD_DB", "-6")
        channel_cli.apply_argv(["--profile", "poor", "--sigma", "123"])
        cs = importlib.reload(cs)
        assert cs.SIGMA == 123.0             # flag > environment > profile (4000)
        assert cs.RX_PAD_DB == -6.0          # environment > profile (-12)
        assert cs.WATTERSON == "poor"        # from the profile, found by bare name
    finally:
        load_sim()


@pytest.mark.parametrize("launch", ["module", "console"])
def test_both_launch_paths_take_flags(launch, sock_dir):
    load_sim()
    flags = ["--listen", "127.0.0.1:18999", "--transport", "alsa",
             "--sock-dir", sock_dir]
    if launch == "module":
        cmd = [sys.executable, "-m", "skywave.channel_sim", *flags]
    else:
        cmd = [sys.executable, "-c",
               "import sys; from skywave.channel_cli import main; sys.exit(main())",
               *flags]
    env = {k: v for k, v in os.environ.items() if not k.startswith("SIM_")}
    r = sp.run(cmd, env=skywave.child_env(env), cwd=REPO_ROOT, stdin=sp.DEVNULL,
               capture_output=True, timeout=30)
    # Only reachable if both flags landed: --listen chose the TCP server and
    # --transport alsa then conflicts with it.
    assert r.returncode == 2, r.stderr
    assert b"SIM_LISTEN needs SIM_TRANSPORT=sock" in r.stderr


def test_no_flags_is_unchanged(monkeypatch):
    before = dict(os.environ)
    channel_cli.apply_argv([])
    assert dict(os.environ) == before


def test_shipped_profiles_resolve_by_bare_name(tmp_path, monkeypatch):
    assert channel_profile.load_profile("poor")["meta"]["name"] == "poor"
    assert transport_profile.load_profile("sock-virt_time")["transport"]["kind"] == "sock"
    with pytest.raises(SystemExit, match="clean, poor"):
        channel_profile.load_profile("nosuch")
    # a file of that name in the working directory wins over the shipped one
    (tmp_path / "poor").write_text('[meta]\nname = "mine"\n')
    monkeypatch.chdir(tmp_path)
    assert channel_profile.load_profile("poor")["meta"]["name"] == "mine"
