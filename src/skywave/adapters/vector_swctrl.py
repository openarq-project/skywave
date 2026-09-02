#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""VectorAdapter for SW-CTRL -- drives swctrl-kit's `swctrl_vector` example.

SW-CTRL is a stop-and-wait CONTROL carrier, not a data mode: a burst transmits
one of five keyed sequences (ACK / ACK-UP / ACK-DOWN / NACK / POLL), so it
carries log2(5) ~= 2.32 bits. Everything below follows from that.

INSTALL: drop this file into skywave's `src/skywave/adapters/` as
`vector_swctrl.py`, then `--adapter swctrl`. It only uses `VectorAdapter` and
`VectorContractError`, so it tracks the same contract as the shipped adapters.

    export SWCTRL_VECTOR=/path/to/swctrl-kit/target/release/examples/swctrl_vector
    python3 -m skywave.vector_sweep --adapter swctrl --out out/swctrl.csv \\
        --presets off,good,moderate,poor --frames 200 \\
        --snr-lo -26 --snr-hi 6 --snr-step 2 --bw 3000 --allow-long

TWO DELIBERATE DEVIATIONS FROM THE CONTRACT, both forced by a 5-symbol
alphabet, both stated here rather than discovered later:

1. ATTRIBUTION IS POSITIONAL, NOT CONTENT-ADDRESSED. The contract matches
   delivered bytes against every expected payload, which is right when payloads
   are long and unique. `payload_bytes` here is 1 and it holds a word index in
   0..4, so a WRONG word collides with some other frame's expected payload
   about 20% of the time by chance. Content addressing would score those as
   correct decodes of a different frame. Frames sit at known sidecar offsets
   and each is detected in its own armed window, so position is the honest key.

2. THE DEFAULT IS TAU-GATED, NOT FORCED CHOICE. A 5-ary forced choice is right
   ~20% of the time on pure noise, so the inherited `selftest()`'s negative leg
   ("deep noise decodes NOTHING") cannot pass against a forced-choice detector
   no matter how healthy it is. This adapter therefore passes `--tau frozen`
   by default: the instrument gates at armstrong's PRODUCTION per-rung freeze
   (`ctrl::rx::frozen_tau`, T0..T3) and reports the number it used, which is
   recorded as `tau` / `tau_table` in every row. Set SWCTRL_FORCED_CHOICE=1
   for the ungated word-error-rate number -- and then do not run the
   inherited selftest. SWCTRL_TAU_TABLE=<json> gates at an explicit table
   instead (recorded by basename).

   WHY THE TABLE IS NO LONGER SEARCHED FOR (2026-09-01): this adapter used to
   walk up from the binary looking for `scripts/swctrl_tau_v1.json` -- the
   Phase-A table for the PRE-fold statistic (T1 tau 3.211). The in-tree
   instrument runs the production DPDI fold, whose pure-noise mean at T1 is
   about 5.3, so that gate passed 44 of 50 noise windows. The FM-CTRL step-1
   pilot ran that way and was filed as "tau = 0". A gate the adapter finds by
   path is a gate nobody chose; the production value is the only one that
   means anything against the production statistic.

WHAT THE COLUMNS MEAN HERE:
  decoded       correct word delivered (above tau, when gated)
  false_decode  WRONG word delivered above tau. In stop-and-wait this is the
                unrecoverable failure -- a false ACK cannot be walked back --
                so it is the column that matters most, not a footnote.
  erasures      below tau: no delivery. Costs a turnaround, not correctness.

TAU IS PROVISIONAL. The production freeze carries its own caveat (calibrated
at a narrow search span; see `frozen_tau`'s doc in ctrl/src/rx.rs) and is NOT
final for spec or registry purposes. Treat a tau-gated delivery curve as a
measurement AT THAT TAU, and report the forced-choice curve beside it -- the
two differ by several dB and the gap is a real design cost, not noise.
"""
import csv
import io
import json
import os
import subprocess

from skywave.vector_adapter import VectorAdapter, VectorContractError

HERE = os.path.dirname(os.path.abspath(__file__))

# What the instrument is told when no explicit table is given: gate at
# armstrong's production per-rung freeze. The instrument resolves the number
# from the sidecar's rung and echoes it back in the summary row.
TAU_FROZEN = "frozen"
TAU_FROZEN_TABLE = "frozen_tau(ctrl::rx)"


class SwctrlVectorAdapter(VectorAdapter):
    name = "swctrl"

    def __init__(self, binary=None, tau_path=None, forced_choice=None):
        self.binary = binary or os.environ.get("SWCTRL_VECTOR") or ""
        if not self.binary or not os.path.isfile(self.binary):
            raise VectorContractError(
                "no `swctrl_vector` binary. Build it with:\n"
                "  cargo build --release -p ctrl --example swctrl_vector\n"
                "in the armstrong checkout, then set SWCTRL_VECTOR to "
                "target/release/examples/swctrl_vector.")
        self.forced_choice = (forced_choice if forced_choice is not None
                              else bool(os.environ.get("SWCTRL_FORCED_CHOICE")))
        # An explicit table is the ONLY way a JSON file reaches the gate.
        self.tau = {}
        self.tau_path = None
        explicit = tau_path or os.environ.get("SWCTRL_TAU_TABLE")
        if explicit and not self.forced_choice:
            if not os.path.isfile(explicit):
                raise VectorContractError(f"SWCTRL_TAU_TABLE not found: {explicit}")
            with open(explicit) as f:
                self.tau = {k: v["tau_r"] for k, v in json.load(f)["tau"].items()}
            self.tau_path = os.path.abspath(explicit)
        self._modes = None

    def _run(self, args):
        p = subprocess.run([self.binary] + args, capture_output=True, text=True)
        if p.returncode != 0:
            raise VectorContractError(
                f"swctrl_vector {' '.join(args[:2])} failed rc={p.returncode}: "
                f"{p.stderr.strip()[-400:]}")
        return p.stdout

    def provenance(self):
        """First 12 hex of the driver's md5. host+arch pin the machine but not
        the binary, and a campaign that swapped drivers mid-run would otherwise
        look clean."""
        import hashlib
        h = hashlib.md5()
        with open(self.binary, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()[:12]

    def tau_for(self, label):
        """What to pass as `--tau`: 0.0 = forced choice, a number from an
        explicit table, or `frozen` (the instrument resolves the production
        value itself). An unknown label under an explicit table is an ERROR,
        not a silent fall-back to forced choice: the two produce curves several
        dB apart and nothing downstream would show which one you got. Under
        `frozen` the instrument applies the same rule (P32/T4/T5 refuse)."""
        if self.forced_choice:
            return 0.0
        if self.tau_path is None:
            return TAU_FROZEN
        if label not in self.tau:
            raise VectorContractError(
                f"no tau for rung {label!r} in {self.tau_path} (have: "
                f"{sorted(self.tau)}). Add one or set SWCTRL_FORCED_CHOICE=1 "
                "deliberately.")
        return self.tau[label]

    def tau_table_name(self):
        if self.forced_choice:
            return ""
        if self.tau_path is None:
            return TAU_FROZEN_TABLE
        return os.path.basename(self.tau_path)

    # ---- contract --------------------------------------------------------

    def list_modes(self):
        if self._modes is not None:
            return self._modes
        # SWCTRL_EXTRA_MODES=C1082,C1518: free-length chip words (the F4
        # head, T2 + declared attack) the sweep must know about, or a
        # --select label for them is skipped silently.
        args = ["list"]
        extra = os.environ.get("SWCTRL_EXTRA_MODES", "").strip()
        if extra:
            args += ["--extra", extra]
        rows = list(csv.DictReader(io.StringIO(self._run(args))))
        out = []
        for r in rows:
            out.append({
                "label": r["label"],
                "mode_id": int(r["mode_id"]),
                "family": r["family"],
                # Every rung is experimental: SW-CTRL has NO registry codepoint.
                # Nothing here may be quoted as a shipping mode.
                "mode_class": r["mode_class"],
                # There is no CRC anywhere in this mode. Identity is checked by
                # the keyed alphabet BEFORE any decode exists, and the residual
                # false-accept exposure is priced by tau, not by a checksum.
                # Reporting a crc_bits here would be a fiction the scorers
                # would then average.
                "crc_bits": 0,
                "payload_bytes": int(r["payload_bytes"]),
                "bandwidth_hz": int(r["bandwidth_hz"]),
                "sample_rate": int(r["sample_rate"]),
                "air_s": float(r["air_s"]),
                # log2(5) / air_s. This is the honest information rate of a
                # state-enumeration carrier -- NOT a payload throughput, and
                # not comparable to a data mode's bps without saying so.
                "nominal_bps": float(r["nominal_bps"]),
                "rms_dbfs": float(r["rms_dbfs"]),
                "peak_dbfs": float(r["peak_dbfs"]),
                "papr_db": float(r["papr_db"]),
                "chips": int(r["chips"]),
                "segs": int(r["segs"]),
                "modulation": r["modulation"],
                "alphabet": r["alphabet"],
            })
        self._modes = out
        return out

    def encode(self, label, frames, seed, outdir, gap_ms=300, flush_ms=1500,
               key=0xC0DE, **kw):
        """`gap_ms` is a REQUEST. The receiver's noise-floor reference is drawn
        from the burst-free region after each burst, so the silence has a hard
        minimum set by the rung's template length; the driver raises a too-small
        gap to that minimum and records the actual value in `gap_samples`."""
        os.makedirs(outdir, exist_ok=True)
        vec = os.path.abspath(os.path.join(outdir, "clean.f32"))
        side = os.path.abspath(os.path.join(outdir, "clean.json"))
        self._run(["tx", "--mode", label, "-o", vec, "--sidecar", side,
                   "--frames", str(frames), "--seed", str(seed),
                   "--key", str(key), "--gap-ms", str(gap_ms)])
        return vec, side

    def decode(self, vector_path, sidecar_path, cold=False):
        with open(sidecar_path) as f:
            label = json.load(f)["label"]
        tau = self.tau_for(label)
        args = ["rx", "--in", os.path.abspath(vector_path),
                "--sidecar", os.path.abspath(sidecar_path),
                "--tau", str(tau),
                # Per-frame outcomes beside the vector, for forensics
                # (squelch phase, t per frame). The batch scratch keeps them
                # when the sweep is run with --scratch.
                "--per-frame", os.path.join(os.path.dirname(
                    os.path.abspath(vector_path)), "frames.csv")]
        # Foreign-key leg: arm the detector for a key the words were NOT
        # generated under. Rows carry tx_key/rx_key so the leg is visible.
        rx_key = os.environ.get("SWCTRL_RX_KEY")
        if rx_key:
            args += ["--key", str(int(rx_key, 0))]
        out = self._run(args)
        rows = list(csv.DictReader(io.StringIO(out)))
        if not rows:
            raise VectorContractError("swctrl_vector rx produced no row")
        r = rows[-1]

        def i(k):
            try:
                return int(r[k])
            except (KeyError, ValueError):
                return 0

        def fl(k, d=0.0):
            try:
                return float(r[k])
            except (KeyError, ValueError):
                return d

        return {
            "frames": i("frames"),
            "decoded": i("decoded"),
            "false_decode": i("false_decode"),
            "erasures": i("erasures"),
            # STRINGS, deliberately. The sweep's `accumulate_extra` does
            # int(v) on every non-core key and SUMS it across batches: right
            # for counts, destructive for anything else. As ints these came
            # out as tau 5.333 -> 5 and a mean-of-means summed over batches.
            # Non-numeric values take the join-with-';' path instead, which
            # preserves precision and makes a multi-batch cell show its parts
            # rather than a silently wrong single number. `s_offset_db` in the
            # sweep itself uses the same trick.
            # The number the INSTRUMENT used (it resolves `frozen` itself and
            # echoes the value), never the request.
            "tau": f'{fl("tau"):.4f}',
            "gated": "no" if fl("tau") <= 0 else "yes",
            # WHICH table produced that tau. A gated curve is a measurement at
            # a specific threshold; a row that records the number but not its
            # source cannot be re-scored later. `tau_source` is the
            # instrument's own word for it (forced / explicit / frozen_tau).
            "tau_table": self.tau_table_name(),
            "tau_source": r.get("tau_source", ""),
            # HEX, deliberately: the sweep's accumulate_extra int()s any
            # numeric-looking value and SUMS it across batches, which would
            # turn key 49374 into 98748 on a two-batch cell. Hex fails int()
            # and takes the join-with-';' path instead.
            "tx_key": f"0x{int(r['tx_key']):04x}" if r.get("tx_key") else "",
            "rx_key": f"0x{int(r['rx_key']):04x}" if r.get("rx_key") else "",
            # Raw detector outcomes (ints, summed across batches): frames the
            # detector returned SOMETHING for, and frames it returned nothing
            # for (also counted in erasures). Under a gate, a null-leg row
            # needs the two apart.
            "detections": i("detections"),
            "no_detection": i("no_detection"),
            # Frames whose floor estimate was ZERO, so t = +inf and the gate
            # could not erase them. That happens when the trailing burst-free
            # reference region is muted -- a squelched FM gap is exactly that
            # -- and a gated row with t_infinite > 0 delivered every such
            # frame ungated. VOID under any gated bar.
            "t_infinite": i("t_infinite"),
            "void": ("t_infinite_under_gate"
                     if fl("tau") > 0 and i("t_infinite") > 0 else ""),
            # Floor-normalised detection statistic and the winner/runner-up
            # ratio: the soft evidence a tau policy is built from. Carried so a
            # re-scoring at a different tau does not need a re-run.
            "mean_t": f'{fl("mean_t"):.4f}',
            "mean_margin": f'{fl("mean_margin"):.4f}',
            # A CONSTANT position bias is a group delay in the channel tool,
            # not jitter. hfchan's fading path carries ~128 samples its AWGN
            # path does not; if this column is large and constant, widen the
            # search window before believing any FER on the row.
            "mean_pos_err": f'{fl("mean_pos_err"):.2f}',
            "mean_pos_bias": f'{fl("mean_pos_bias"):.2f}',
            # The receiver is constructed per invocation, so every run is
            # already cold; the flag is accepted and reported, never silently
            # ignored.
            "always_cold": "yes",
        }


def build():
    return SwctrlVectorAdapter()
