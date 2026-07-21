#!/usr/bin/env python3
"""Platform capability gates for skywave -- one place, so a Windows/macOS port is
a localized change rather than a scatter of sys.platform checks.

What is portable today:
  * The pure channel simulator + DSP (numpy/scipy) -- every platform.
  * The framed `sock` transport (SIM_TRANSPORT=sock) -- POSIX today: it uses
    AF_UNIX sockets, present on Linux and macOS. See has_af_unix() for Windows.

What is Linux-only today:
  * The real-hardware "alsa" rig (SIM_TRANSPORT=alsa, the default): four snd-aloop
    cards bridged by `arecord`/`aplay`. macOS/Windows have no ALSA. A native audio
    rig on those platforms supplies its own backend (CoreAudio/WASAPI + a virtual
    audio cable); until then non-Linux hosts run device-free over SIM_TRANSPORT=sock.

Windows notes for a future port (collected here so they are not rediscovered):
  * AF_UNIX: Windows 10+ supports it at the OS level, but CPython does NOT expose
    `socket.AF_UNIX` on Windows. The sock transport therefore needs a TCP-loopback
    mode before it runs on Windows -- has_af_unix() is the switch to branch on.
  * Process teardown: the harness uses POSIX `os.setsid`/`os.killpg` + `pkill`.
    Windows needs `CREATE_NEW_PROCESS_GROUP` + `taskkill`/psutil instead.
  * Audio: no `arecord`/`aplay`; a Windows rig would capture via WASAPI (e.g.
    sounddevice/ffmpeg dshow) over a VB-CABLE-style virtual device.
See docs/PORTABILITY.md for the full matrix.
"""
import socket
import sys


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_windows() -> bool:
    return sys.platform.startswith("win")


def has_af_unix() -> bool:
    """True if this Python exposes AF_UNIX (Linux/macOS yes; Windows CPython no).
    The `sock` transport binds AF_UNIX today; branch on this to add a Windows path."""
    return hasattr(socket, "AF_UNIX")


def alsa_rig_error():
    """An actionable error string if the ALSA snd-aloop rig cannot run on this host,
    or None when it can (Linux). Callers print it and exit rather than letting a bare
    `arecord`/`aplay` spawn die with a cryptic FileNotFoundError."""
    if is_linux():
        return None
    return ("the snd-aloop ALSA rig (SIM_TRANSPORT=alsa, arecord/aplay) is Linux-only; "
            f"on {sys.platform} run device-free with SIM_TRANSPORT=sock "
            "(add SIM_CLOCK=virt_time for a modem with a native socket audio backend). "
            "See docs/PORTABILITY.md.")
