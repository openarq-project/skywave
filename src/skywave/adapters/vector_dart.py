#!/usr/bin/env python3
"""VectorAdapter for HTCommander's DART modem (Apache-2.0, source-available).

DART is a DFT-spread OFDM (SC-FDMA) modem carrying data through an FM
handheld's audio path -- 9 subcarriers in 500-2500 Hz at 32 kHz, LDPC N=648 at
four rates, BPSK through 16QAM, plus a constant-envelope 4-CPFSK fallback
("Mode F"). It is the closest thing in the wild to armstrong-fm's `micspk`
problem, which is why it is worth having on the bench. Review, findings and
caveats: `FM-EXTERNAL-REVIEW-DART-2026-08-30.md` in the bakeoff repo.

NOT VENDORED. This adapter drives the user's own HTCommander checkout: it
copies `src/lib/hamlib/dart_*.dart` into a scratch build directory alongside
skywave's own `dart_vec.dart` entrypoint and runs them with the Dart SDK. So
the modem under test is always a real commit of the real project, skywave
carries no third-party snapshot to drift, and `provenance()` reports that
commit. The copy works at all only because those files import nothing but
`dart:math` and `dart:typed_data` -- no Flutter, no Bluetooth.

    export DART_SRC=/path/to/HTCommander/src/lib/hamlib   # or the repo root
    export DART_SDK=/path/to/dart-sdk                     # or have `dart` on PATH
    skywave-vector-sweep --adapter dart --fm-port micspk ...

WHAT THIS MEASURES, AND WHAT IT DOES NOT. The vector path characterizes PHY
modes: acquisition, demod and FEC, frame by frame. It does not exercise ARQ or
rate adaptation, and for DART that is not a limitation but an accurate scope --
`DartLink` (its windows, ACK/NACK and rate ladder) is referenced only from
DART's own tests and is not wired to its radio path, so there is no shipped ARQ
to measure. See the review, finding F8.

CHANNEL CAVEAT, LOUDLY. DART's own over-the-air numbers come from a path with
TWO Bluetooth SBC hops around the FM link. This bench has none. Cells run here
characterize the WAVEFORM on skywave's FM channel; they are not a reproduction
of DART's field results and must not be quoted as one.

PAYLOAD SIZE IS PART OF THE MODE. Labels are `dart_m<key>_b<bytes>` because
DART's rate is dominated by fixed per-frame overhead -- a 297 ms header plus
un-punctured LDPC block padding -- so "mode 2" has no single throughput. At
64 B the ladder's own rate distinctions partly vanish (m1/m2 and m4/m5 share an
airtime, because both land on the same number of 648-bit blocks). Set
`DART_PAYLOAD_BYTES` to sweep that axis; it is the most informative knob here.

CRC GATING. The Dart shim returns payload bytes only on a CRC-32 pass, and all
matching happens on this side, so `decoded` / `wrong_frame` / `false_decode`
are adjudicated in one place against payloads this module generated.
"""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile

from skywave.vector_adapter import (VectorAdapter, VectorContractError,
                                    gen_payload, save_sidecar)

HERE = os.path.dirname(os.path.abspath(__file__))
ENTRYPOINT = os.path.join(HERE, "dart_vec.dart")
#: The DART sources this adapter needs. Anything else in hamlib/ is not copied.
HAMLIB_FILES = (
    "dart_constellation.dart", "dart_fsk.dart", "dart_ldpc.dart",
    "dart_link.dart", "dart_modem.dart", "dart_ofdm.dart",
    "dart_packet_info.dart", "dart_preamble.dart",
)
DEFAULT_PAYLOAD_BYTES = 64


def _find_hamlib(root):
    """Accept either the hamlib dir itself or an HTCommander checkout root."""
    cands = [root,
             os.path.join(root, "src", "lib", "hamlib"),
             os.path.join(root, "lib", "hamlib")]
    for c in cands:
        if all(os.path.isfile(os.path.join(c, f)) for f in HAMLIB_FILES):
            return c
    return None


def _find_dart():
    sdk = os.environ.get("DART_SDK")
    if sdk:
        exe = os.path.join(sdk, "bin", "dart")
        if os.path.isfile(exe):
            return exe
        if os.path.isfile(sdk):
            return sdk
    return shutil.which("dart")


class DartVectorAdapter(VectorAdapter):
    name = "dart"

    def __init__(self, src=None, dart=None, payload_bytes=None):
        root = src or os.environ.get("DART_SRC") or ""
        self.hamlib = _find_hamlib(root) if root else None
        if not self.hamlib:
            raise VectorContractError(
                "DART sources not found. Set DART_SRC to an HTCommander "
                "checkout (or directly to its src/lib/hamlib), e.g. "
                "DART_SRC=~/src/HTCommander. Looked for: "
                + ", ".join(HAMLIB_FILES[:3]) + ", ...")
        self.dart = dart or _find_dart()
        if not self.dart:
            raise VectorContractError(
                "Dart SDK not found. Put `dart` on PATH or set DART_SDK to an "
                "SDK directory. The DART sources are pure Dart -- the Flutter "
                "SDK is not needed.")
        self.payload_bytes = int(
            payload_bytes or os.environ.get("DART_PAYLOAD_BYTES")
            or DEFAULT_PAYLOAD_BYTES)
        if self.payload_bytes <= 0:
            raise VectorContractError("DART_PAYLOAD_BYTES must be > 0")
        self._modes = None
        self._build = None

    # ---- build -----------------------------------------------------------

    def _builddir(self):
        """Assemble (once) a Flutter-free Dart package: the hamlib sources plus
        skywave's entrypoint. Kept for the adapter's lifetime so the sweep pays
        the copy once, not per cell."""
        if self._build:
            return self._build
        d = tempfile.mkdtemp(prefix="skyw-dart-")
        os.makedirs(os.path.join(d, "bin"), exist_ok=True)
        for f in HAMLIB_FILES:
            shutil.copy2(os.path.join(self.hamlib, f), os.path.join(d, "bin", f))
        shutil.copy2(ENTRYPOINT, os.path.join(d, "bin", "dart_vec.dart"))
        with open(os.path.join(d, "pubspec.yaml"), "w") as f:
            f.write('name: dart_vec\nenvironment:\n  sdk: ">=3.0.0 <4.0.0"\n')
        self._build = d
        return d

    def _run(self, args, timeout=1800):
        d = self._builddir()
        cmd = [self.dart, "run", "bin/dart_vec.dart"] + args
        p = subprocess.run(cmd, cwd=d, capture_output=True, text=True,
                           timeout=timeout)
        if p.returncode != 0:
            raise VectorContractError(
                f"dart_vec {args[0]} failed rc={p.returncode}: "
                f"{(p.stderr or p.stdout).strip()[-500:]}")
        try:
            return json.loads(p.stdout)
        except json.JSONDecodeError as e:
            raise VectorContractError(
                f"dart_vec {args[0]} did not produce JSON ({e}); "
                f"stdout head: {p.stdout[:200]!r}") from None

    def provenance(self):
        """HTCommander commit if the source tree is a git checkout, else a hash
        of the sources themselves. Either way a campaign that swapped modem
        versions mid-run is detectable, which host+arch alone would not catch."""
        try:
            p = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"],
                               cwd=self.hamlib, capture_output=True, text=True,
                               timeout=30)
            if p.returncode == 0 and p.stdout.strip():
                return "git:" + p.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        h = hashlib.md5()
        for f in HAMLIB_FILES:
            with open(os.path.join(self.hamlib, f), "rb") as fh:
                h.update(fh.read())
        return "md5:" + h.hexdigest()[:12]

    # ---- contract --------------------------------------------------------

    def list_modes(self):
        if self._modes is None:
            out = self._run(["modes", "--payload-bytes",
                             str(self.payload_bytes)])
            self._modes = out["modes"]
        return self._modes

    def _mode_key(self, label):
        for m in self.list_modes():
            if m["label"] == label:
                return m["mode_key"], int(m["payload_bytes"])
        raise VectorContractError(f"unknown DART mode label {label!r}")

    def encode(self, label, frames, seed, outdir, gap_ms=300, **kw):
        key, pb = self._mode_key(label)
        os.makedirs(outdir, exist_ok=True)
        vec = os.path.join(outdir, "clean.f32")
        side = os.path.join(outdir, "clean.json")
        pl = os.path.join(outdir, "payloads.bin")
        # Contract payloads, generated HERE so there is exactly one source of
        # truth for what the decoder should have recovered.
        with open(pl, "wb") as f:
            for i in range(frames):
                f.write(gen_payload(seed, i, pb))
        meta = self._run(["encode", "--mode", key, "--payload-bytes", str(pb),
                          "--frames", str(frames), "--gap-ms", str(gap_ms),
                          "--payloads", pl, "--out", vec])
        sidecar = {
            "label": label,
            "sample_rate": meta["sample_rate"],
            "payload_bytes": pb,
            "frames": meta["frames"],
            "seed": seed,
            "frame_offsets": meta["frame_offsets"],
            "frame_lengths": meta["frame_lengths"],
            "gap_samples": meta["gap_samples"],
            "norm": "peak0.8",   # DartOfdm.toPcm normalizes each burst to 0.8 FS
        }
        save_sidecar(side, sidecar)
        return vec, side

    def decode(self, vector_path, sidecar_path, cold=False):
        with open(sidecar_path) as f:
            side = json.load(f)
        n = int(side["frames"])
        pb = int(side["payload_bytes"])
        seed = int(side["seed"])
        expected = [gen_payload(seed, i, pb) for i in range(n)]
        by_bytes = {}
        for i, e in enumerate(expected):
            by_bytes.setdefault(e, i)

        args = ["decode", "--in", vector_path, "--meta", sidecar_path]
        if cold:
            args += ["--cold", "1"]
        out = self._run(args)

        decoded = wrong_frame = false_decode = crc_errors = duplicates = 0
        snr_sum = evm_sum = corr_sum = drift_sum = 0.0
        nq = ndrift = 0
        seen = set()
        import base64
        for r in out["results"]:
            if not r.get("sync"):
                continue
            if r.get("snr_db") is not None:
                snr_sum += float(r["snr_db"])
                evm_sum += float(r.get("evm_percent") or 0.0)
                corr_sum += float(r.get("preamble_corr") or 0.0)
                nq += 1
            if r.get("phase_drift_deg") is not None:
                drift_sum += float(r["phase_drift_deg"])
                ndrift += 1
            if not r.get("crc_ok"):
                crc_errors += 1
                continue
            got = base64.b64decode(r["payload"])
            idx = r["frame"]
            if idx < n and got == expected[idx]:
                if idx in seen:
                    duplicates += 1
                else:
                    seen.add(idx)
                    decoded += 1
            elif got in by_bytes:
                # Passed CRC and IS a real payload, but not this frame's -- the
                # blind-mode-ladder failure the contract wants separated out.
                wrong_frame += 1
            else:
                false_decode += 1

        res = {
            "frames": n,
            "decoded": decoded,
            "false_decode": false_decode,
            "wrong_frame": wrong_frame,
            "duplicates": duplicates,
            "crc_errors": crc_errors,
            "sync_count": int(out.get("sync_count", 0)),
        }
        if nq:
            res["mean_snr_db"] = snr_sum / nq      # a CORE key: weighted properly
        # Non-core telemetry travels as integer SUM + COUNT, never as a mean.
        # Two reasons, both silent if ignored:
        #   1. vector_sweep.accumulate_extra folds extras with int(v), so any
        #      fractional value (a 0.89 correlation, a 0.7 deg drift) becomes 0
        #      and the column looks measured-and-zero rather than lost.
        #   2. It SUMS across batches, and a cell can be several batches, so a
        #      per-batch mean would be added to another per-batch mean.
        # Sum and count both compose correctly under that fold; divide at
        # analysis time: corr = qual_corr_sum_x1000 / (1000 * qual_n).
        if nq:
            res["qual_n"] = nq
            res["qual_evm_sum_x100"] = int(round(evm_sum * 100))
            res["qual_corr_sum_x1000"] = int(round(corr_sum * 1000))
        if ndrift:
            # DART reports this as carrier phase noise; the review (F5) notes it
            # is numerically what an uncorrected ~1 Hz offset would produce and
            # that its CFO estimator is a stub. Carried so a cell can look.
            res["drift_n"] = ndrift
            res["drift_sum_mdeg"] = int(round(drift_sum * 1000))
        return res

    def __del__(self):
        if getattr(self, "_build", None):
            shutil.rmtree(self._build, ignore_errors=True)


def build():
    return DartVectorAdapter()
