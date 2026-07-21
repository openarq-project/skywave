#!/usr/bin/env python3
"""channel -- the in-process Channel object.

The in-process library API: run audio through the HF/VHF channel simulator IN-PROCESS from a
typed ChannelConfig, instead of spawning channel_sim as a subprocess and piping over
ALSA/sockets.

    from skywave.channel_config import ChannelConfig
    from skywave.channel import Channel
    ch = Channel(ChannelConfig(sigma=200, watterson="poor"))
    rx_block = ch.process(tx_block)          # one block, NSAMP int16 in -> NSAMP int16 out

Channel drives ONE direction (A->B) of the simulator's real `Link` -- the same class the
subprocess uses -- built through the SAME `build_channel_effects()` the subprocess calls,
so every configured effect (fade, rig BPF, ALC, FOFF, AGC, impulsive noise, QRM) is
actually applied. There is no reduced-fidelity path: a config that asks for a fade gets a
fade.

IMPLEMENTATION NOTE / LIMITATION. channel_sim reads its config from module globals at
import, and the `Link` DSP reads those globals at run time. So Channel applies the config
by (re)loading channel_sim with `cfg.to_env()` in the process environment. Consequences,
documented honestly:
  * Constructing a Channel MUTATES process-global state (os.environ + the channel_sim
    module). It is therefore NOT reentrant and NOT thread-safe: only ONE Channel config is
    live per process at a time -- constructing a second Channel reloads the module and the
    first Channel's Link then reads the new globals. Construct, use, discard.
  * FM ports, the transport/clock layer, and the fine second-order sub-knobs are not part
    of ChannelConfig (v1); they take channel_sim's defaults here.
Removing the reload (Link reading a config object directly) is a follow-on decoupling
for later.
"""
import importlib
import os
import threading

from skywave import channel_sim as _cs

# env keys channel_sim reads that are NOT SIM_*-prefixed (cleared on (re)configure so a
# stale value can't leak into the reloaded module) -- mirrors the harness reload contract.
_PASSTHROUGH = ("SIGMA", "TXGAIN", "SEED", "NP_STATS", "SIM_TXDUMP", "SIM_KEYLOG")


class ChannelConfigError(Exception):
    """The config is invalid for the channel (channel_sim rejected it; details on stderr)."""


class _FakeProc:
    """Stand-in TX source: Channel drives Link.process() directly (never Link.run()), so
    the source stream is unused -- matches the rig_tests make_link technique."""
    stdout = None


def _apply_env(env):
    for k in list(os.environ):
        if k.startswith("SIM_") or k in _PASSTHROUGH:
            del os.environ[k]
    os.environ.update({k: str(v) for k, v in env.items()})


class Channel:
    """One direction (A->B) of the channel simulator, configured in-process from a
    ChannelConfig. `process(block)` runs one NSAMP-int16 interleaved block through it."""

    def __init__(self, cfg):
        self.cfg = cfg
        _apply_env(cfg.to_env())
        try:
            cs = importlib.reload(_cs)           # module globals now reflect cfg
        except SystemExit as e:                  # e.g. an invalid ALC preset (import-time)
            raise ChannelConfigError(f"channel_sim rejected the config at load: {e}")
        self._cs = cs
        eff = cs.build_channel_effects()         # the SAME builder the subprocess uses
        if isinstance(eff, int):                 # config error (already printed to stderr)
            raise ChannelConfigError(f"invalid channel config (channel_sim exit {eff}); "
                                     "see stderr")
        # Build the A->B Link exactly as channel_sim.main() does for that direction
        # (seed SEED+11, gain GAIN_A, sigma SIGMA_AB, the _ab effect objects).
        self._link = cs.Link(
            "A->B", _FakeProc(), 0, cs.SEED + 11, "", threading.Event(),
            "a", "b", cs.Keys(), None, eff.fade_ab, cs.LINK_DELAY_SAMP,
            eff.rig_ab_tx, eff.rig_ab_rx, eff.fx_ab, eff.sql_ab,
            cs.GAIN_A, cs.SIGMA_AB)
        self._link.noise_lpf = eff.noise_lpf_ab
        self.nch = cs.NCH
        self.block = cs.BLOCK
        self.nsamp = cs.NSAMP                    # samples per process() block (NCH*BLOCK)
        self.fs = cs.FS

    def process(self, block_int16):
        """Run one interleaved int16 block (length == self.nsamp) through the channel;
        returns a new int16 array of the same length. Mutating input in place is avoided
        (the return is a copy)."""
        if len(block_int16) != self.nsamp:
            raise ValueError(f"block must be {self.nsamp} int16 samples "
                             f"(NCH*BLOCK), got {len(block_int16)}")
        self._link.xin[:] = block_int16
        self._link.process()
        return self._link.xout.copy()
