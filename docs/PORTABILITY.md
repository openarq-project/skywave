# Portability

skywave runs on Linux and macOS today. Windows is not supported yet; the gaps are
small and catalogued below.

The project is two layers, and they port differently:

- The **channel simulator and DSP** are pure numpy/scipy and run anywhere Python
  runs.
- The **transports** that carry audio between two modems and the channel are where
  the operating system shows through.

## Platform support matrix

| Capability | Linux | macOS | Windows |
|---|:---:|:---:|:---:|
| Channel sim, DSP, `hfchan`, in-process `Channel`, the test suite | yes | yes | expected (untested) |
| `sock` transport (device-free, `SIM_TRANSPORT=sock`) | yes | yes | no (needs a TCP mode, see below) |
| `alsa` rig (real snd-aloop loopback, the default) | yes | no | no |
| Real-modem adapter over `sock` (e.g. `armstrong --audio sock`) | yes | yes | not yet |
| Real-modem adapters over the ALSA rig (mercury, ardop, ...) | yes | no | no |

The ALSA rig is the only Linux-only piece of the core. Off Linux the harness fails
with a clear message pointing at the `sock` transport rather than a cryptic
`arecord`/`aplay` error; the gate lives in one place, `skywave/_platform.py`.

To run a *real modem* off Linux, prefer its own device-free PCM path (Armstrong
`--audio sock`, Mercury `-x fifo`) bridged through the channel sim; a virtual
audio device (BlackHole, VB-CABLE) is only needed for a modem that speaks nothing
but a soundcard. The full analysis and per-modem breakdown is in
[REAL-AUDIO-RIG.md](REAL-AUDIO-RIG.md).

## Running on macOS

Requirements: Python 3.11+ (the system `python3` on current macOS is 3.9, too old).
[uv](https://docs.astral.sh/uv/) is the easiest way to get one:

```
brew install uv
uv python install 3.13
uv venv
uv pip install -e ".[test]"
uv run python -m pytest tests/ -q          # the full suite
```

Use the **device-free `sock` transport** for anything that needs a live modem, and
`SIM_CLOCK=virt_time` for a modem that has a native socket audio backend (a
deterministic, block-lockstep virtual clock; goodput is then wall-referenced rather
than on-air seconds). Example: two Armstrong stations transferring through the
channel with no audio hardware at all:

```
export ARMSTRONG_BIN=/path/to/armstrong-hf
SIM_TRANSPORT=sock SIM_CLOCK=virt_time SIGMA=0 \
  python -m skywave.adapters.armstrong 4096 90
```

Two macOS-specific things are worth knowing:

- **AF_UNIX path length.** macOS caps a unix-socket path (`sun_path`) at 104 bytes
  (Linux allows 108). channel_sim defaults `SIM_SOCK_DIR` to `/tmp/simsock-<pid>`,
  which is safe; if you point it at a deep directory you get a clear "socket path
  too long" error instead of a raw `OSError`. Keep `SIM_SOCK_DIR` short. Tests that
  bind sockets use the `sock_dir` fixture, which stays under a short root for the
  same reason.
- **virt_time is CPU-speed sensitive.** In `virt_time` the modem's own timers run on
  the virtual clock, which a fast host advances quickly in wall-clock terms. A long
  *wall-clock* idle in a harness or adapter can therefore race past a modem's
  keepalive/inactivity timeout and drop the link before data flows. The Armstrong
  adapter keeps its post-connect settle brief in virt_time for exactly this reason
  (a 2 s settle dropped the link on an M-series Mac, which steps virtual time about
  9x faster than the Linux benches it was tuned on); override with `ARM_SETTLE_S`
  if you ever need a longer settle on an unusually slow virt_time host. When you add
  a new adapter, prefer keeping idle windows short over adding wall-clock sleeps.

## Windows: what a port needs

Nothing structural blocks Windows; three concrete items do. They are collected in
`skywave/_platform.py` so they are not rediscovered:

1. **AF_UNIX.** Windows 10+ supports AF_UNIX at the OS level, but CPython does not
   expose `socket.AF_UNIX` on Windows. The `sock` transport binds AF_UNIX today, so
   it needs a **TCP-loopback mode** (bind `127.0.0.1:<port>` instead of a `.sock`
   path) before it runs on Windows. `_platform.has_af_unix()` is the switch to
   branch on; the wire format (`sock_frames.py`) is transport-agnostic and does not
   change.
2. **Process teardown.** The harness spawns children as a POSIX session leader
   (`os.setsid`) and tears the group down with `os.killpg` and `pkill -f`. Windows
   has none of these; it needs `CREATE_NEW_PROCESS_GROUP` at spawn and
   `taskkill`/psutil (or a Job object) at teardown. This is isolated to
   `bench_pipes.py`, `modem_adapter.py`, and each adapter's preclean/teardown.
3. **Audio.** There is no `arecord`/`aplay`. A real-hardware Windows rig would
   capture and play through WASAPI (for example via `sounddevice` or ffmpeg's
   `dshow`) across a VB-CABLE-style virtual audio device, the Windows analogue of
   snd-aloop. The device-free `sock` transport avoids this entirely once item 1 is
   done, so a modem with a native socket backend can be benchmarked on Windows
   without any virtual-audio driver.

A pragmatic Windows bring-up order: TCP `sock` mode (item 1) and teardown (item 2)
first, which unlocks socket-backend modems with no audio driver; a native WASAPI
rig (item 3) only if a Windows-only, audio-only modem needs it.
