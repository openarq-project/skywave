#!/usr/bin/env python3
"""VectorAdapter for armstrong -- drives `phy`'s `vector` example.

armstrong's PHY runs at 8 kHz with i16 samples (modem73's is 48 kHz float), which
is why the contract carries `sample_rate` in the sidecar instead of assuming one.
The Rust side normalizes i16 to +-1.0 on write and back on read, so a no-channel
round trip is bit-exact.

NOT A REPLACEMENT for armstrong's in-tree `codec2_mode_floor_bench`. That bench is
self-contained Rust with no Python dependency and keeps working where skywave
cannot run at all -- GitHub runners, bare containers, any box without
numpy/scipy. It remains the CI-runnable instrument. This adapter exists for the
cross-modem case: armstrong modes measured beside modem73/codec2 modes on ONE
channel generation with ONE SNR convention.

The two instruments therefore measure the same quantity by different routes, which
is a comparability hazard if left implicit. Keep it explicit: the in-tree bench
reports SNR3000 via `phy::channel::Watterson`; this path applies skywave's
`vector_channel` at whatever `--bw` the campaign states, and every sweep row
records `bw_hz`. Never quote a floor from one against a floor from the other
without stating which produced it.

Env:
  ARMSTRONG_SRC     armstrong checkout (default ~/tools/armstrong)
  ARMSTRONG_VECTOR  path to a prebuilt `vector` binary; skips cargo entirely
                    (use this on a bench box where you shipped the binary)
"""
import csv
import io
import os
import subprocess

from skywave.vector_adapter import VectorAdapter, VectorContractError

DEFAULT_SRC = os.path.expanduser("~/tools/armstrong")


class ArmstrongVectorAdapter(VectorAdapter):
    name = "armstrong"

    def __init__(self, src=None, binary=None, bench=None):
        # Bench ablations (0xBExx: -NOCLIP, -NS3, -P08) are OFF by default: they
        # must never wander into a production sweep by accident. Opt in with
        # ARMSTRONG_VECTOR_BENCH=1, and note the binary also has to be built
        # `--features bench-modes` or the driver refuses with an explicit message.
        self.bench = (bench if bench is not None
                      else bool(os.environ.get("ARMSTRONG_VECTOR_BENCH")))
        self.src = src or os.environ.get("ARMSTRONG_SRC") or DEFAULT_SRC
        self.binary = binary or os.environ.get("ARMSTRONG_VECTOR") or ""
        if not self.binary:
            built = os.path.join(self.src, "target", "release", "examples", "vector")
            if os.path.isfile(built):
                self.binary = built
        if not self.binary:
            raise VectorContractError(
                "no armstrong `vector` binary. Build it with:\n"
                f"  cargo build -p phy --release --example vector\n"
                f"in {self.src}, or set ARMSTRONG_VECTOR to a prebuilt copy.")
        self._modes = None

    def _run(self, args, env=None):
        """`env` overlays the child environment. Modes expose bench drive knobs
        that way (e.g. ARM_QAM64W_CLIPGAIN), so a clip arm is a different child
        environment rather than a different binary or mode id."""
        child = None
        if env:
            child = dict(os.environ)
            child.update({k: str(v) for k, v in env.items()})
        p = subprocess.run([self.binary] + args, capture_output=True, text=True,
                           cwd=self.src, env=child)
        if p.returncode != 0:
            raise VectorContractError(
                f"armstrong vector {' '.join(args[:2])} failed "
                f"rc={p.returncode}: {p.stderr.strip()[-400:]}")
        return p.stdout

    def provenance(self):
        """First 12 hex of the driver's md5 -- host+arch pin the machine but not
        the binary, and a campaign that swapped drivers mid-run would otherwise
        look clean."""
        import hashlib
        h = hashlib.md5()
        with open(self.binary, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()[:12]

    # ---- contract --------------------------------------------------------

    def list_modes(self, env=None):
        """`env` re-measures under a drive-knob arm (e.g. ARM_QAM64W_CLIPGAIN).

        The DRIVER does the measuring in both cases, deliberately. Recomputing
        levels on this side produced a 1.1 dB disagreement: `list` reports RMS
        over the mode's nominal air_s window, while the modulated audio is
        shorter than that, so two defensible conventions gave two different
        numbers for the same waveform. One convention per corpus.
        """
        if env is None and self._modes is not None:
            return self._modes
        argv = ["list", "--bench"] if self.bench else ["list"]
        rows = list(csv.DictReader(io.StringIO(self._run(argv, env=env))))
        out = []
        for r in rows:
            out.append({
                "label": r["label"],
                "mode_id": int(r["mode_id"]),
                # The driver's own class (datac / qam / plh / freedata). NOT the
                # adapter name -- `adapter` already carries that, and the
                # frontier census is per-family, so collapsing armstrong's four
                # classes into one label would delete that analysis outright.
                "family": r["family"],
                # production | bench. The scorer keeps bench ablations off the
                # production frontier, so an ablation cannot outrank a shipping
                # mode and then be quoted as one.
                "mode_class": r.get("mode_class", "production"),
                # Every mode in armstrong's registry is codec2-backed and its
                # payload is adjudicated by the codec2 raw-data CRC-16/CCITT
                # (`CODEC2_FCS`, phy/src/mode_registry.rs), with
                # `wire_must_add_fcs()` false throughout -- so 16 is the width
                # that gates the payload on this instrument.
                #
                # This is the MODEM-LAYER check only, and deliberately so: it is
                # what a vector sweep exercises. armstrong applies further checks
                # elsewhere (a PLH head CRC-6, a 6-bit FASTTAG tag, and in a
                # control-block session a per-block CRC-16 plus an end-to-end
                # CRC-32 over the application byte stream). None of those are in
                # this path -- the receiver here is told the mode -- so none of
                # them belong in this number.
                "crc_bits": 16,
                "payload_bytes": int(r["payload_bytes"]),
                "bandwidth_hz": int(r["bandwidth_hz"]),
                "sample_rate": 8000,
                "air_s": float(r["air_s"]),
                "nominal_bps": float(r["nominal_bps"]),
                "rms_dbfs": float(r["rms_dbfs"]),
                "peak_dbfs": float(r["peak_dbfs"]),
                "papr_db": float(r["papr_db"]),
                "frame": r["frame"],
                "code_rate": r["code_rate"],
                "modulation": r["modulation"],
            })
        if env is None:
            self._modes = out
        return out

    def encode(self, label, frames, seed, outdir, gap_ms=300, flush_ms=1500,
               env=None, **kw):
        os.makedirs(outdir, exist_ok=True)
        vec = os.path.abspath(os.path.join(outdir, "clean.f32"))
        side = os.path.abspath(os.path.join(outdir, "clean.json"))
        self._run(["tx", "--mode", label, "-o", vec, "--sidecar", side,
                   "--frames", str(frames), "--seed", str(seed),
                   "--gap-ms", str(gap_ms), "--flush-ms", str(flush_ms)],
                  env=env)
        return vec, side

    def decode(self, vector_path, sidecar_path, cold=False):
        # The Rust driver has no warm/cold distinction: phy::modem::rx is called
        # fresh per invocation, so every run is already cold. Accepting the flag
        # and ignoring it keeps the contract uniform; report it so nobody reads
        # a `cold=1` column here as meaning something it does not.
        out = self._run(["rx", "--in", os.path.abspath(vector_path),
                         "--sidecar", os.path.abspath(sidecar_path)])
        rows = list(csv.DictReader(io.StringIO(out)))
        if not rows:
            raise VectorContractError("armstrong vector rx produced no row")
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
            # Real failed-CRC count as of armstrong's crc_eval_stash — the
            # false-accept exposure term the E1 coarse gate lacked (it read
            # a hardcoded 0 and degenerated to zero-tolerance).
            "crc_errors": i("crc_errors"),
            # Total CRC evaluations (fails + passes) and up-to-4 forensic
            # records of false decodes ("len=..:first4=.."; '' when none).
            # Non-core keys: they travel via the sweep's extra_json column,
            # which vector_report's gate forensics already read.
            "crc_evals": i("crc_evals"),
            "fd_detail": r.get("fd_detail", "") or "",
            "always_cold": 1,
        }


def build():
    return ArmstrongVectorAdapter()
