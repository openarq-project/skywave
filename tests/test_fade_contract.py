"""Cross-module contract: sweep_runner.fade_resolved must agree with the fade
channel_sim actually builds.

sweep_runner names every corpus row with the fade it believes ran. It cannot ask the
sim -- the sim is a subprocess it launches -- so it MIRRORS channel_sim's resolution
ladder: SIM_FADE_SCHEDULE beats an explicit delay+doppler pair beats the named
SIM_WATTERSON preset. A mirror can drift, and every fade-provenance bug in this
harness has been exactly that drift:

  * the pair was applied but never named, so faded rows looked unfaded;
  * the descriptor was rebuilt with a "/" in it, so the log basename became a path;
  * the pair gate tested KEY PRESENCE where the sim tests a non-empty VALUE;
  * the whole thing was resolved before the cell `env` landed, so a scheduled fade --
    reachable ONLY through that env -- recorded "off".

Each was a one-rung reimplementation that stopped matching. This pins the two together:
it drives the REAL resolution (build_channel_effects, the same builder main() uses) and
asserts fade_resolved reaches the same verdict -- same rung of the ladder, same
delay/doppler numbers. Reorder the ladder, change a gate, or rename a rung on either
side and this fails.

Scope note: both sides read watterson.PRESETS, so this does NOT catch a preset being
retuned (they move together, which is correct). What it catches is the LADDER and its
GATES -- which is where every bug so far has lived.

Run:  cd skywave && python3 -m pytest tests/test_fade_contract.py -q
"""
import os
import re

import pytest

from conftest import load_sim
from skywave import sweep_runner

# Keep the generated fade sequences tiny: this test cares about which rung was taken,
# not about the fade waveform, and the default 1200 s costs real seconds to synthesize.
FAST = {"SIM_FADE_DUR_S": "2"}


def _parse_sim(desc):
    """(rung, delay_ms, doppler_hz) from channel_sim's banner desc -- the sim's own
    account of what it built: "fade=off" | "fade=schedule[...]xf=1s" |
    "fade=custom(3.5ms/0.8Hz)" | "fade=poor(2.0ms/1.0Hz)"."""
    if desc == "fade=off":
        return "off", "", ""
    if desc.startswith("fade=schedule["):
        return "schedule", "", ""
    m = re.match(r"fade=(\S+?)\(([\d.]+)ms/([\d.]+)Hz\)$", desc)
    assert m, f"unparsed channel_sim fade desc: {desc!r}"
    return ("custom" if m.group(1) == "custom" else "preset",
            float(m.group(2)), float(m.group(3)))


def _parse_runner(resolved):
    """(rung, delay_ms, doppler_hz) from what sweep_runner would write to the row."""
    name, delay, doppler = resolved
    if name.startswith("sched_"):
        return "schedule", "", ""
    if name.startswith("custom_"):
        return "custom", delay, doppler
    if name == "off":
        return "off", "", ""
    return "preset", delay, doppler


def _both(env):
    """Resolve ONE env through both paths. load_sim scrubs every SIM_ key and installs
    exactly this env, so the runner's mirror reads precisely what the sim read."""
    cs = load_sim(**{**FAST, **env})
    eff = cs.build_channel_effects()
    assert not isinstance(eff, int), f"channel_sim rejected the config: {env}"
    return _parse_sim(eff.fade_desc), _parse_runner(
        sweep_runner.fade_resolved(dict(os.environ)))


LADDER = [
    pytest.param({}, id="off"),
    pytest.param({"SIM_WATTERSON": "poor"}, id="preset-poor"),
    pytest.param({"SIM_WATTERSON": "nvis"}, id="preset-nvis"),
    pytest.param({"SIM_WATTERSON": "flutter"}, id="preset-flutter"),
    pytest.param({"SIM_WATTERSON": "nvis-disturbed"}, id="preset-hyphenated"),
    pytest.param({"SIM_FADE_DELAY_MS": "3.5", "SIM_FADE_DOPPLER_HZ": "0.8"},
                 id="custom-pair"),
    # the rung that started all this: the pair silently wins, and SIM_WATTERSON's value
    # is irrelevant -- a row named from it claims a fade that never ran
    pytest.param({"SIM_WATTERSON": "poor", "SIM_FADE_DELAY_MS": "3.5",
                  "SIM_FADE_DOPPLER_HZ": "0.8"}, id="pair-beats-preset"),
    # half a pair is NOT a custom fade: the sim falls through to the preset
    pytest.param({"SIM_WATTERSON": "poor", "SIM_FADE_DELAY_MS": "3.5"},
                 id="half-pair-keeps-preset"),
    pytest.param({"SIM_WATTERSON": "poor", "SIM_FADE_DOPPLER_HZ": "0.8"},
                 id="other-half-pair-keeps-preset"),
    # "" is channel_config's unset sentinel: present but falsy, so the preset runs
    pytest.param({"SIM_WATTERSON": "poor", "SIM_FADE_DELAY_MS": "",
                  "SIM_FADE_DOPPLER_HZ": ""}, id="empty-pair-keeps-preset"),
    pytest.param({"SIM_FADE_SCHEDULE": "good:1,poor:0"}, id="schedule"),
    # top rung: the schedule wins over BOTH lower ones
    pytest.param({"SIM_WATTERSON": "poor", "SIM_FADE_DELAY_MS": "3.5",
                  "SIM_FADE_DOPPLER_HZ": "0.8", "SIM_FADE_SCHEDULE": "good:1,poor:0"},
                 id="schedule-beats-everything"),
]


@pytest.mark.parametrize("env", LADDER)
def test_runner_mirrors_sim_fade_resolution(env):
    sim, runner = _both(env)
    assert runner == sim, (
        f"sweep_runner would name this row {runner}, channel_sim built {sim} -- "
        f"the mirror has drifted from the ladder for env {env}")


def test_the_guard_would_catch_a_drifted_mirror(monkeypatch):
    """The guard above only earns its keep if a wrong mirror actually fails it. Swap in
    the pre-fix behaviour (name the row from SIM_WATTERSON alone) and confirm it does."""
    monkeypatch.setattr(sweep_runner, "fade_resolved",
                        lambda env: (env.get("SIM_WATTERSON", "off"), "", ""))
    with pytest.raises(AssertionError, match="mirror has drifted"):
        test_runner_mirrors_sim_fade_resolution(
            {"SIM_WATTERSON": "poor", "SIM_FADE_DELAY_MS": "3.5",
             "SIM_FADE_DOPPLER_HZ": "0.8"})
