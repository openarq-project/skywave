#!/usr/bin/env python3
"""VectorAdapter for modem73 -- wraps the `modevector` C++ driver.

modem73's PHY classes are header-only and operate on in-memory float buffers, so
`modevector` links them directly: no ALSA, no CSMA, no PTT, no real time. Its
sidecar is already contract-compliant, so this adapter is a thin process wrapper.

Env:
  MODEVECTOR_BIN   path to the modevector binary
                   (default: ~/tools/m73-modevector/modevector)

Mode labels are modevector's own spec strings:
  ofdm:QPSK:1/2:normal | ofdm:0x1B | rdm:RDM-600 | mfsk:MFSK-16
The raw-byte OFDM form reaches modes modem73's own CLI cannot express.
"""
import csv
import io
import os
import subprocess

from skywave.vector_adapter import VectorAdapter, VectorContractError

DEFAULT_BIN = os.path.expanduser("~/tools/m73-modevector/modevector")

#: Payload CRC width per modem73 family. This differs BY FAMILY, which is exactly
#: why a false_decode gate cannot be zero-tolerance: an mfsk mode admits ~1 in
#: 65,536 of the corrupt frames reaching its check, so one false accept in a
#: campaign-sized corpus is arithmetic, while the same event on ofdm/rdm is a
#: ~1-in-24,000 surprise. Sites (modem73 @ 4d350b0):
#:   mfsk  CRC-16 CCITT  phy/mfsk_modem.hh:100, checked at :860
#:   ofdm  CRC-32        modem.hh:236 (tx) / :1659 (rx), `crc1`
#:   rdm   CRC-32        phy/robust_modem.hh:62, 317
#: OFDM also carries a CRC-16 (`crc0`) but it guards the 56-bit meta/mode
#: descriptor at modem.hh:468/780, not the payload, so it is not the gate here.
CRC_BITS_BY_FAMILY = {"mfsk": 16, "ofdm": 32, "rdm": 32}


class Modem73VectorAdapter(VectorAdapter):
    name = "modem73"

    def __init__(self, binary=None):
        self.bin = binary or os.environ.get("MODEVECTOR_BIN") or DEFAULT_BIN
        if not os.path.isfile(self.bin):
            raise VectorContractError(
                f"modevector binary not found at {self.bin}; build it "
                "(`make` in m73-modevector) or set MODEVECTOR_BIN")
        self._modes = None

    # ---- helpers ---------------------------------------------------------

    def _run(self, args):
        p = subprocess.run([self.bin] + args + ["--quiet"],
                           capture_output=True, text=True)
        if p.returncode != 0:
            raise VectorContractError(
                f"modevector {' '.join(args[:2])} failed rc={p.returncode}: "
                f"{p.stderr.strip()[-400:]}")
        return p.stdout

    def provenance(self):
        """First 12 hex of the driver's md5 -- host+arch pin the machine but not
        the binary, and a campaign that swapped drivers mid-run would otherwise
        look clean."""
        import hashlib
        h = hashlib.md5()
        with open(self.bin, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()[:12]

    # ---- contract --------------------------------------------------------

    def list_modes(self):
        if self._modes is not None:
            return self._modes
        # `list` encodes one real frame per mode and MEASURES air time and
        # level, rather than deriving them from a formula -- a formula cannot
        # see the RDM encoder's soft clipping or its TX bandpass.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "modes.csv")
            self._run(["list", "-o", path])
            with open(path) as f:
                rows = list(csv.DictReader(f))
        out = []
        for r in rows:
            out.append({
                "label": r["label"],
                "mode_id": r["mode_id"],
                "family": r["family"],
                "crc_bits": CRC_BITS_BY_FAMILY.get(r["family"]),
                "payload_bytes": int(r["payload_bytes"]),
                "bandwidth_hz": int(r["bandwidth_hz"]),
                "sample_rate": 48000,
                "air_s": float(r["air_s"]),
                "nominal_bps": float(r["nominal_bps"]),
                "rms_dbfs": float(r["rms_dbfs"]),
                "peak_dbfs": float(r["peak_dbfs"]),
                "papr_db": float(r["papr_db"]),
                "frame": r["frame"],
                "code_rate": r["code_rate"],
                "modulation": r["modulation"],
            })
        self._modes = out
        return out

    def encode(self, label, frames, seed, outdir, norm="rms", level_dbfs=-20.0,
               gap_ms=300, flush_ms=1500, **kw):
        os.makedirs(outdir, exist_ok=True)
        vec = os.path.join(outdir, "clean.f32")
        side = os.path.join(outdir, "clean.json")
        self._run(["tx", "--mode", label, "-o", vec, "--sidecar", side,
                   "--frames", str(frames), "--seed", str(seed),
                   "--norm", norm, "--level", str(level_dbfs),
                   "--gap-ms", str(gap_ms), "--flush-ms", str(flush_ms)])
        return vec, side

    def decode(self, vector_path, sidecar_path, cold=False):
        args = ["rx", "--in", vector_path, "--sidecar", sidecar_path]
        if cold:
            args.append("--cold")
        out = self._run(args)
        rows = list(csv.DictReader(io.StringIO(out)))
        if not rows:
            raise VectorContractError("modevector rx produced no summary row")
        r = rows[-1]

        def i(k):
            try:
                return int(r[k])
            except (KeyError, ValueError):
                return 0

        def f(k, d=0.0):
            try:
                return float(r[k])
            except (KeyError, ValueError):
                return d

        return {
            "frames": i("frames"),
            "decoded": i("decoded"),
            "false_decode": i("false_decode"),
            "wrong_frame": i("wrong_frame"),
            "duplicates": i("duplicates"),
            "mean_snr_db": f("mean_snr_db"),
            "mean_ber": f("mean_ber", -1.0),
            "sync_count": i("sync_count"),
            "crc_errors": i("crc_errors"),
            # modem73-specific, carried through to the CSV:
            "sticky_syncs": i("sticky_syncs"),
            "false_locks": i("false_locks"),
        }


def build():
    return Modem73VectorAdapter()
