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
   no matter how healthy it is. This adapter therefore applies the frozen
   per-rung thresholds in `scripts/swctrl_tau_v1.json` by default and records
   `tau` in every row. Set SWCTRL_FORCED_CHOICE=1 for the ungated
   word-error-rate number -- and then do not run the inherited selftest.

WHAT THE COLUMNS MEAN HERE:
  decoded       correct word delivered (above tau, when gated)
  false_decode  WRONG word delivered above tau. In stop-and-wait this is the
                unrecoverable failure -- a false ACK cannot be walked back --
                so it is the column that matters most, not a footnote.
  erasures      below tau: no delivery. Costs a turnaround, not correctness.

TAU IS PROVISIONAL. The shipped table was frozen for Phase-A scoring and is
NOT final for spec or registry purposes; it was calibrated on one traffic pool
at one geometry (SEG_CHIPS=168). Treat a tau-gated delivery curve as a
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
TAU_BASENAME = "swctrl_tau_v1.json"


def _tau_candidates(binary):
    """Where to look for the frozen tau table, in order.

    The install step COPIES this file into skywave's adapters/ directory, so
    anything resolved relative to __file__ stops working exactly when the
    documented workflow is followed -- which is how this was first written and
    how it broke. The binary is the reliable anchor: SWCTRL_VECTOR is required
    anyway, and it lives at <kit>/target/release/examples/swctrl_vector, so
    walking up from it finds the kit's scripts/ wherever the kit was unpacked.
    The __file__-relative path stays last so running from inside the kit,
    un-copied, still works.
    """
    out = []
    env = os.environ.get("SWCTRL_TAU_TABLE")
    if env:
        out.append(env)
    if binary:
        d = os.path.dirname(os.path.abspath(binary))
        for _ in range(5):
            d = os.path.dirname(d)
            if not d or d == "/":
                break
            out.append(os.path.join(d, "scripts", TAU_BASENAME))
    out.append(os.path.join(HERE, "..", "scripts", TAU_BASENAME))
    out.append(os.path.join(HERE, TAU_BASENAME))
    return out


class SwctrlVectorAdapter(VectorAdapter):
    name = "swctrl"

    def __init__(self, binary=None, tau_path=None, forced_choice=None):
        self.binary = binary or os.environ.get("SWCTRL_VECTOR") or ""
        if not self.binary or not os.path.isfile(self.binary):
            raise VectorContractError(
                "no `swctrl_vector` binary. Build it with:\n"
                "  cargo build --release --example swctrl_vector\n"
                "in the swctrl-kit checkout, then set SWCTRL_VECTOR to "
                "target/release/examples/swctrl_vector.")
        self.forced_choice = (forced_choice if forced_choice is not None
                              else bool(os.environ.get("SWCTRL_FORCED_CHOICE")))
        self.tau = {}
        self.tau_path = None
        tried = [tau_path] if tau_path else _tau_candidates(self.binary)
        for cand in tried:
            if cand and os.path.isfile(cand):
                with open(cand) as f:
                    self.tau = {k: v["tau_r"]
                                for k, v in json.load(f)["tau"].items()}
                self.tau_path = os.path.abspath(cand)
                break
        if self.tau_path is None and not self.forced_choice:
            raise VectorContractError(
                "no frozen tau table found. Looked in:\n  "
                + "\n  ".join(str(t) for t in tried)
                + "\nSet SWCTRL_TAU_TABLE to the kit's "
                  f"scripts/{TAU_BASENAME}, or set SWCTRL_FORCED_CHOICE=1 "
                  "deliberately. Gated is the default because a 5-ary forced "
                  "choice is right ~20% of the time on noise, so the choice "
                  "must be explicit rather than defaulted into.")
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
        """0.0 means forced choice. An unknown label under gating is an ERROR,
        not a silent fall-back to forced choice: the two produce curves several
        dB apart and nothing downstream would show which one you got."""
        if self.forced_choice:
            return 0.0
        if label not in self.tau:
            raise VectorContractError(
                f"no frozen tau for rung {label!r} (have: {sorted(self.tau)}). "
                "Add one or set SWCTRL_FORCED_CHOICE=1 deliberately.")
        return self.tau[label]

    # ---- contract --------------------------------------------------------

    def list_modes(self):
        if self._modes is not None:
            return self._modes
        rows = list(csv.DictReader(io.StringIO(self._run(["list"]))))
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
        out = self._run(["rx", "--in", os.path.abspath(vector_path),
                         "--sidecar", os.path.abspath(sidecar_path),
                         "--tau", str(tau)])
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
            "tau": f"{tau:.4f}",
            "gated": "no" if tau <= 0 else "yes",
            # WHICH table produced that tau. A gated curve is a measurement at
            # a specific threshold; a row that records the number but not its
            # source cannot be re-scored later.
            "tau_table": os.path.basename(self.tau_path) if self.tau_path else "",
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
