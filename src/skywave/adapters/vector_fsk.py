#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""VectorAdapter for armstrong's restored 4-FSK modem -- drives the
`fsk_vector` example in `crates/fsk` (the FM family's B2.1/B2.4 instrument).

    export FSK_VECTOR=~/tools/armstrong/target/release/examples/fsk_vector
    python3 -m skywave.vector_sweep --adapter fsk --fm-port micspk \\
        --fm-squelch --fm-squelch-block-ms 4 --fm-squelch-open-ms 30 \\
        --fm-squelch-tone-ms 220 --presets off --frames 100 ...

A burst is [preamble P ms][UW 16 dibits][payload N bytes as UNCODED dibits].
This adapter measures the HEAD, not a data mode: `decoded` = UW found within
the search window AND every payload dibit right; `sync_count` = UW found;
`mean_ber` = payload dibit error rate; `false_decode` is always 0 (payload is
compared to the contract generator, so a wrong-offset sync shows as a synced-
but-not-decoded frame, i.e. `crc_errors`). No FEC, no CRC: `crc_bits` = 0.

Env:
  FSK_VECTOR       path to the binary
  FSK_UW_KEY       TX unique-word key (0 = the v0 constant UW; default 0)
  FSK_RX_UW_KEY    if set, the RECEIVER's UW key -- the foreign-key arm
  FSK_PAYLOAD_BYTES payload size (default 40)
  FSK_SYNC_MIN     matched-symbol threshold of 32 (default 28)
"""
import csv
import io
import json
import os
import subprocess

from skywave.vector_adapter import VectorAdapter, VectorContractError

DEFAULT_BIN = os.path.expanduser("~/tools/armstrong/target/release/examples/fsk_vector")


class FskVectorAdapter(VectorAdapter):
    name = "fsk"

    def __init__(self, binary=None):
        self.binary = binary or os.environ.get("FSK_VECTOR") or DEFAULT_BIN
        if not os.path.isfile(self.binary):
            raise VectorContractError(
                f"no `fsk_vector` binary at {self.binary}. Build it with:\n"
                "  cargo build --release -p fsk --example fsk_vector\n"
                "in the armstrong checkout, then set FSK_VECTOR.")
        self.payload_bytes = int(os.environ.get("FSK_PAYLOAD_BYTES", "40"))
        self.uw_key = int(os.environ.get("FSK_UW_KEY", "0"), 0)
        self.rx_uw_key = os.environ.get("FSK_RX_UW_KEY")
        self.sync_min = int(os.environ.get("FSK_SYNC_MIN", "28"))
        self._modes = None

    def _run(self, args):
        p = subprocess.run([self.binary] + args, capture_output=True, text=True)
        if p.returncode != 0:
            raise VectorContractError(
                f"fsk_vector {' '.join(args[:2])} failed rc={p.returncode}: "
                f"{p.stderr.strip()[-400:]}")
        return p.stdout

    def provenance(self):
        import hashlib
        h = hashlib.md5()
        with open(self.binary, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()[:12]

    def list_modes(self):
        if self._modes is not None:
            return self._modes
        rows = list(csv.DictReader(io.StringIO(
            self._run(["list", "--payload-bytes", str(self.payload_bytes)]))))
        self._modes = [{
            "label": r["label"], "mode_id": int(r["mode_id"]), "family": "fsk",
            "mode_class": "experimental", "crc_bits": 0,
            "payload_bytes": int(r["payload_bytes"]),
            "bandwidth_hz": int(r["bandwidth_hz"]), "sample_rate": int(r["sample_rate"]),
            "air_s": float(r["air_s"]), "nominal_bps": float(r["nominal_bps"]),
            "rms_dbfs": float(r["rms_dbfs"]), "peak_dbfs": float(r["peak_dbfs"]),
            "papr_db": float(r["papr_db"]), "preamble_ms": int(r["preamble_ms"]),
            "modulation": r["modulation"],
        } for r in rows]
        return self._modes

    def encode(self, label, frames, seed, outdir, gap_ms=300, flush_ms=1500, **kw):
        os.makedirs(outdir, exist_ok=True)
        vec = os.path.abspath(os.path.join(outdir, "clean.f32"))
        side = os.path.abspath(os.path.join(outdir, "clean.json"))
        self._run(["tx", "--mode", label, "-o", vec, "--sidecar", side,
                   "--frames", str(frames), "--seed", str(seed),
                   "--uw-key", str(self.uw_key),
                   "--payload-bytes", str(self.payload_bytes),
                   "--gap-ms", str(gap_ms)])
        return vec, side

    def decode(self, vector_path, sidecar_path, cold=False):
        args = ["rx", "--in", os.path.abspath(vector_path),
                "--sidecar", os.path.abspath(sidecar_path),
                "--sync-min", str(self.sync_min)]
        if self.rx_uw_key is not None:
            args += ["--uw-key", str(int(self.rx_uw_key, 0))]
        rows = list(csv.DictReader(io.StringIO(self._run(args))))
        if not rows:
            raise VectorContractError("fsk_vector rx produced no row")
        r = rows[-1]

        def i(k):
            try:
                return int(r[k])
            except (KeyError, ValueError):
                return 0

        def fl(k):
            try:
                return float(r[k])
            except (KeyError, ValueError):
                return 0.0

        return {
            "frames": i("frames"), "decoded": i("decoded"),
            "false_decode": 0, "sync_count": i("sync_count"),
            "crc_errors": i("crc_errors"),
            # The sweep weights mean_ber by decoded frames; decoded frames
            # have zero dibit errors by definition here, so the honest head
            # number is the ALL-frames dibit error rate, carried as a string
            # extra (erased/unsynced frames count as fully wrong).
            "mean_ber": fl("mean_ber"),
            "dibit_err_all": f'{fl("mean_ber"):.4f}',
            # strings: the sweep int()s and SUMS numeric extras across batches
            "mean_pos_err": f'{fl("mean_pos_err"):.2f}',
            "mean_pos_bias": f'{fl("mean_pos_bias"):.2f}',
            "mean_matched": f'{fl("mean_matched"):.2f}',
            "uw_key": f"0x{i('uw_key'):04x}", "rx_uw_key": f"0x{i('rx_uw_key'):04x}",
            "preamble_ms": f"p{i('preamble_ms')}",
        }


def build():
    return FskVectorAdapter()
