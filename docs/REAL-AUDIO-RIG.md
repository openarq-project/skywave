# Running soundcard modems off Linux

On Linux, skywave drives real modems through four `snd-aloop` cards with
`arecord`/`aplay` (the "alsa" rig). That rig is Linux-only. This note is the
design analysis for running real modems on macOS and Windows: when you can avoid
a virtual audio device entirely, and, when you can't, which one to use.

The headline: **most modems do not actually need a virtual audio device.** A
modem that can read/write PCM over a pipe or socket can be bridged straight
through the channel simulator, exactly like the ALSA cable but with no driver.
Reserve virtual-audio-device work for the modems that truly only speak to a
soundcard.

## Two ways to carry modem audio

1. **Device-free transport** (preferred): the modem reads/writes raw PCM over a
   socket or named pipe; skywave bridges that through `channel_sim`. No audio
   driver, no reboot, works headless and in CI. This is what the `sock`
   transport already does for a socket-native modem.
2. **Virtual audio device**: the modem opens a "soundcard" that is actually a
   virtual loopback (BlackHole on macOS, VB-CABLE on Windows); skywave captures
   and plays that device. Needed only for a modem with no non-soundcard I/O path.

## Which modems need which (verified against each modem's own source)

| Modem | Device-free path it already has | Needs a virtual audio device off Linux? |
|---|---|---|
| **Armstrong** | `--audio sock` — AF_UNIX framed PCM | No (works on macOS today) |
| **Mercury** | `-x fifo` — raw s32le 8 kHz PCM over `-i`/`-o` named pipes; also `-x shm`, `-x null` | No — bridge the FIFOs |
| **FreeDATA** | `TESTMODE` exists but returns before `modulator.create_burst()` and queues raw *frames* — the PHY is bypassed, so there is no waveform to fade/noise | For real-PHY testing: yes, **or** drive its modulator/demodulator in-process |
| **ardopcf** | none — positional capture/playback device args only; `NOSOUND` is a disable switch, not a transport | **Yes** — genuinely soundcard-only |
| **VARA** | none — proprietary Windows soundcard (under Wine on Linux) | **Yes** (Windows/Wine) |

Notes:
- **Mercury** ships its own two-instance `fifo`-bridge integration tests and a
  loopback bench (`utils/loopsim/`), so the pipe path is a first-class, exercised
  feature, not a stub. Caveat: `fifo` uses POSIX `mkfifo`, so it is Linux/macOS
  only; Windows Mercury would need a TCP or Windows-named-pipe path.
- **FreeDATA**'s `TESTMODE` is an ARQ-logic harness (its headless CI uses it), not
  an audio channel. Its real modulated audio only flows through PortAudio. The
  clean device-free option is an adapter that drives FreeDATA's modulator +
  demodulator directly through skywave's in-process `Channel`, kept out-of-process
  to respect FreeDATA's GPL.
- **ardopcf** is the clearest case for a virtual device. A higher-leverage
  alternative is to contribute a `fifo`/pipe backend upstream (`pflarue/ardop`),
  which would make it device-free like Mercury.

## Virtual audio devices, when you do need one

### macOS (no fully CI-clean option today)

| Option | Cost / license | Multiple cables | Headless / CI | Verdict |
|---|---|---|---|---|
| **BlackHole** | free, **GPL-3.0** | channel-count variants (2/16/64ch); carving 4 independent endpoints out of one device is unresolved | no kext (user-space AudioServer plugin), but the installer **requires a reboot** — an open blocker on GitHub-hosted macOS runners (`actions/runner-images#11746`) | best for a **manual/self-hosted** Mac; not headless-CI-able today |
| **Rogue Amoeba Loopback** | paid | yes (nested devices) | unverified | most flexible on a dev workstation; paid, unproven headless |
| **Custom libASPL plugin** | MIT (cleanest license fit) | designed to taste | unverified (the "no reboot like BlackHole" claim did **not** survive verification) | cleanest long-term, but real engineering; don't assume it dodges the reboot/approval friction |
| **Soundflower** | free | — | — | effectively dead; superseded by BlackHole |

Capture/playback tooling (`sox`, `ffmpeg avfoundation`, `python-sounddevice`) all
still need a driver underneath — they replace `arecord`/`aplay`, not the loopback
device. Enterprise MDM can silently approve system extensions, but only on
User-Approved-MDM-enrolled Macs — not stock GitHub-hosted runners.

### Windows (one clearly good option)

| Option | Cost / license | Multiple cables | Headless / CI | Verdict |
|---|---|---|---|---|
| **VB-CABLE** | free donationware | base + A+B + C+D products (up to ~5 cables) | **proven silent `devcon.exe` install on `windows-latest`, no reboot** | best CI-ready free option on any platform; scaling to 4 endpoints via A+B/C+D is plausible but not CI-verified |
| **VAC** | commercial, license-tiered | yes | — | ruled out for a free/redistributable CI path |
| **Voicemeeter (Potato)** | donationware; paid VAIO extension | 8 I/O pairs (3 free) | scripted install but **reboot per install** | ruled out for ephemeral CI |

## Recommendation

1. **Extend the device-free transport first.** Mercury's `-x fifo` is a drop-in
   fit; add a fifo-bridge adapter (this repo, `adapters/mercury_fifo`) so Mercury
   joins Armstrong as a device-free modem on macOS. No driver, no CI friction.
2. **Factor the shared shape.** Armstrong (`sock`) and Mercury (`fifo`) are the
   same pattern: a device-free PCM cable through `channel_sim`. This is also where
   the Windows TCP variant of the `sock` transport slots in (see
   [PORTABILITY.md](PORTABILITY.md); `_platform.has_af_unix()` is the switch).
3. **Prefer upstream pipe backends over drivers** where a modem is open source
   (ardopcf): a contributed `fifo` backend beats maintaining a Mac audio rig.
4. **Defer the virtual-audio rig** until ardopcf / VARA / FreeDATA-real-PHY on
   macOS actually matter. When they do: **Windows + VB-CABLE first** (proven CI,
   VARA's native home), **macOS BlackHole on a manual/self-hosted box** second;
   a custom libASPL driver only if it becomes a maintained product need. Treat
   macOS real-audio testing as a workstation job, not a GitHub-hosted CI job.

## Open questions

- VARA's and FreeDATA's exact audio I/O were only partly established; confirm
  before committing to a driver for either.
- On macOS, how to carve four independent routable endpoints from CoreAudio
  (one multichannel device + aggregate/channel-map vs. several BlackHole
  variants) is unresolved.
- Whether the VB-CABLE A+B / C+D products install headlessly alongside the base
  cable in one CI job is unverified.

## Sources

Primary sources behind the modem-I/O and virtual-device findings:
`github.com/Rhizomatica/mercury` (fifo/shm/null backends, integration tests);
`github.com/pflarue/ardop` command-line options + backend source (soundcard-only);
`github.com/existentialaudio/blackhole` (GPL-3.0, no-kext, reboot) and
`actions/runner-images#11746` (CI reboot blocker); `github.com/gavv/libASPL`
(MIT); `github.com/AlekseyMartynov/action-vbcable-win` + `vb-audio.com/Cable`
(VB-CABLE silent CI install, product line); `vac.muzychenko.net` and the
Voicemeeter Potato manual (licensing/reboot). Claims were cross-checked and
adversarially verified; items that did not survive verification (e.g. a custom
libASPL plugin sharing BlackHole's exact no-reboot install path) are called out
as open above.
