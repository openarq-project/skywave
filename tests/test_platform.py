"""Platform capability gates (skywave._platform).

The ALSA snd-aloop rig is Linux-only; off Linux the harness must fail with an
actionable message pointing at the device-free sock transport, not a cryptic
`arecord`/`aplay` FileNotFoundError. These tests fake the platform so they run
(and mean the same thing) on every OS.
"""
from skywave import _platform


def test_alsa_rig_error_is_none_on_linux(monkeypatch):
    monkeypatch.setattr(_platform.sys, "platform", "linux")
    assert _platform.alsa_rig_error() is None


def test_alsa_rig_error_actionable_off_linux(monkeypatch):
    for plat in ("darwin", "win32"):
        monkeypatch.setattr(_platform.sys, "platform", plat)
        msg = _platform.alsa_rig_error()
        assert msg, f"expected an error on {plat}"
        # names the cause and the portable alternative
        assert "Linux-only" in msg
        assert "SIM_TRANSPORT=sock" in msg


def test_os_predicates_are_mutually_exclusive(monkeypatch):
    for plat, want in (("linux", "is_linux"), ("darwin", "is_macos"), ("win32", "is_windows")):
        monkeypatch.setattr(_platform.sys, "platform", plat)
        got = [n for n in ("is_linux", "is_macos", "is_windows")
               if getattr(_platform, n)()]
        assert got == [want], f"{plat} -> {got}"
