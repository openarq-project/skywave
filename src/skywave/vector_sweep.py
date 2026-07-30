#!/usr/bin/env python3
"""Campaign driver for VectorAdapter mode characterization.

FER-vs-SNR sweeps over a modem's mode space, one way, no ARQ. Adapter-agnostic:
it drives whatever `skywave.adapters.vector_*` provides, so armstrong (8 kHz) and
modem73 (48 kHz) run through identical code.

  python3 -m skywave.vector_sweep --adapter armstrong --out out/arm.csv \\
      --presets off,good,moderate,poor --frames 150 --snr-lo -12 --snr-hi 30
  python3 -m skywave.vector_sweep --adapter modem73 --out out/m73.csv \\
      --select hull.txt --jobs 14 --allow-long

Design points that are not incidental:

* Clean vectors are encoded ONCE per (mode, batch) and reused across every SNR
  point and preset in that group. Re-encoding per cell is the largest waste
  available in this pipeline.
* Work is batched by AUDIO DURATION, not frame count. A 400-frame 27 s mode is
  3 hours of audio; a duration budget bounds peak scratch regardless of mode.
* Resumable: completed cells are read back from the output CSV and skipped.
* Scratch is registered and removed on SIGTERM/SIGINT, and stale dirs from a
  previous killed run are reaped at startup.
* Provenance per row (host, arch, adapter): float results are NOT bit-identical
  across architectures, so `arch` is a comparability key, not a nicety.
* No silent caps: anything skipped, dropped or failed is logged and counted.
"""
import argparse
import atexit
import csv
import glob
import importlib
import json
import math
import os
import platform
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

from skywave import vector_channel as vc
from skywave.vector_adapter import (load_sidecar, read_vector, validate_sidecar,
                                    write_vector)
from skywave.watterson import PRESETS

# Owner standing rule: no job over ~30 min on the interactive dev box -- it goes
# to a rented box or the Mac. The driver estimates its own runtime and refuses
# rather than leaving the rule to memory. --allow-long overrides.
DEV_BUDGET_S = 30 * 60
# Measured aggregate decode throughput, ~11x real-time per core. Skewed
# optimistic for modes with long frames.
PER_CORE_REALTIME = 11.0

FIELDS = [
    "adapter", "label", "family", "mode_id", "preset", "delay_ms", "doppler_hz",
    "filter_mode", "snr_db", "bw_hz", "sample_rate", "frames", "decoded",
    "fer", "fer_lo", "fer_hi", "goodput_bps", "payload_bytes", "air_s",
    "bandwidth_hz", "nominal_bps", "false_decode", "wrong_frame", "duplicates",
    "mean_snr_db", "mean_ber", "sync_count", "crc_errors",
    # Level stats travel with every row, not just in the mode list, because the
    # equal-PEP ranking is DERIVED from them post-hoc:
    #   floor_equalPEP = floor_equalAvgPower + (papr_db - papr_db(reference))
    # The channel stage sets sigma from the vector's own signal power, so a sweep
    # is inherently an equal-AVERAGE-POWER measurement, which favours high-PAPR
    # modes against a PEP-limited transmitter. Without papr_db on the row that
    # correction is unavailable and the campaign has to be re-run to get it.
    "rms_dbfs", "peak_dbfs", "papr_db",
    # Width of the payload check that adjudicates this mode. Drives the
    # false_decode gate; empty means the adapter did not report one and the gate
    # falls back to zero tolerance for that mode.
    "crc_bits",
    "extra_json", "batches", "seed_base", "cold", "host", "arch",
    # Content hash of the driver binary. host+arch pin the machine, not the
    # executable; a mid-campaign driver swap would otherwise look clean.
    "driver_id",
]

_print_lock = threading.Lock()
_scratch = set()
_scratch_lock = threading.Lock()


def log(msg):
    with _print_lock:
        print(msg, file=sys.stderr, flush=True)


def _reg(p):
    with _scratch_lock:
        _scratch.add(p)


def _rel(p):
    with _scratch_lock:
        _scratch.discard(p)
    shutil.rmtree(p, ignore_errors=True)


def _cleanup_all():
    with _scratch_lock:
        paths, _scratch_copy = list(_scratch), None
        _scratch.clear()
    for p in paths:
        shutil.rmtree(p, ignore_errors=True)


def _on_signal(signum, _frame):
    _cleanup_all()
    log(f"vector_sweep: signal {signum}, scratch cleaned; rerun to resume")
    os._exit(130)


def reap_stale(root, max_age_s=6 * 3600):
    n, now = 0, time.time()
    for p in glob.glob(os.path.join(root, "swvec-*")):
        try:
            if now - os.path.getmtime(p) > max_age_s:
                shutil.rmtree(p, ignore_errors=True)
                n += 1
        except OSError:
            continue
    return n


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1.0 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = (z / d) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, c - h), min(1.0, c + h))


def load_adapter(name):
    mod = importlib.import_module(f"skywave.adapters.vector_{name}")
    return mod.build()


def check_schema(path):
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return None
    with open(path) as f:
        have = f.readline().strip().split(",")
    if have == FIELDS:
        return None
    return (f"{path} was written with a different schema (missing "
            f"{[c for c in FIELDS if c not in have] or 'none'}, unexpected "
            f"{[c for c in have if c not in FIELDS] or 'none'}). Move it aside; "
            "appending would corrupt the file.")


def load_done(path):
    done = set()
    if not os.path.exists(path):
        return done
    with open(path) as f:
        for r in csv.DictReader(f):
            try:
                done.add((r["label"], r["preset"], round(float(r["snr_db"]), 3)))
            except (KeyError, ValueError):
                continue
    return done


def batches_for(air_s, frames, budget_s):
    per = max(1, int(budget_s // max(air_s, 1e-6)))
    out, left = [], frames
    while left > 0:
        n = min(per, left)
        out.append(n)
        left -= n
    return out


CORE_KEYS = {"frames", "decoded", "false_decode", "wrong_frame", "duplicates",
             "mean_snr_db", "mean_ber", "sync_count", "crc_errors"}


def do_mode(adapter, mode, presets, snrs, args, done, writer, wlock, stats):
    label = mode["label"]
    air = float(mode["air_s"])
    wanted = [(p, s) for p in presets for s in snrs
              if (label, p, round(s, 3)) not in done]
    if not wanted:
        stats["skipped"] += len(presets) * len(snrs)
        return

    bw = args.bw if args.bw > 0 else float(mode.get("bandwidth_hz") or 2500)
    batch_list = batches_for(air, args.frames, args.batch_seconds)
    acc = {}
    scratch = tempfile.mkdtemp(prefix="swvec-", dir=args.scratch)
    _reg(scratch)
    try:
        for bi, nframes in enumerate(batch_list):
            seed = args.seed + bi * 7919
            vec_path, side_path = adapter.encode(label, nframes, seed, scratch,
                                                 gap_ms=args.gap_ms)
            side = load_sidecar(side_path)
            vec = read_vector(vec_path)
            validate_sidecar(side, vector_len=vec.size)
            fs = int(side["sample_rate"])
            S = vc.clean_signal_power(vec, side)

            for preset in presets:
                faded, _ = vc.apply_fade(vec, side, preset, seed + 101,
                                         args.filter)
                for snr in snrs:
                    if (label, preset, round(snr, 3)) in done:
                        continue
                    sigma = vc.sigma_for(S, fs, bw, snr)
                    npath = os.path.join(scratch, "noisy.f32")
                    write_vector(npath, vc.add_awgn(faded, sigma, seed + 202))
                    try:
                        r = adapter.decode(npath, side_path, cold=args.cold)
                    except Exception as e:                       # noqa: BLE001
                        log(f"  ! {label} {preset} {snr:+.1f}: {type(e).__name__}: {e}")
                        stats["failed"] += 1
                        continue
                    a = acc.setdefault((preset, snr), dict(
                        frames=0, decoded=0, false_decode=0, wrong_frame=0,
                        duplicates=0, sync_count=0, crc_errors=0,
                        snr_sum=0.0, ber_sum=0.0, n=0, extra={}))
                    a["frames"] += int(r.get("frames", nframes))
                    for k in ("decoded", "false_decode", "wrong_frame",
                              "duplicates", "sync_count", "crc_errors"):
                        a[k] += int(r.get(k, 0))
                    d = int(r.get("decoded", 0))
                    if d:
                        a["snr_sum"] += float(r.get("mean_snr_db", 0.0)) * d
                        a["ber_sum"] += max(0.0, float(r.get("mean_ber", 0.0))) * d
                        a["n"] += d
                    for k, v in r.items():
                        if k not in CORE_KEYS:
                            try:
                                a["extra"][k] = a["extra"].get(k, 0) + int(v)
                            except (TypeError, ValueError):
                                a["extra"][k] = v
                    os.remove(npath)
            for p in (vec_path, side_path):
                if os.path.exists(p):
                    os.remove(p)
    finally:
        _rel(scratch)

    host, arch = socket.gethostname(), platform.machine()
    try:
        driver_id = adapter.provenance() or ""
    except OSError:
        # An unreadable/absent binary is a provenance gap, not a reason to lose
        # the cell -- the empty value is reported as a caveat by vector_report.
        driver_id = ""
    for (preset, snr), a in sorted(acc.items(), key=lambda kv: (kv[0][0], -kv[0][1])):
        n, k = a["frames"], a["decoded"]
        fer = 1.0 - k / n if n else 1.0
        lo, hi = wilson(n - k, n)
        spec = PRESETS.get(preset)
        row = {
            "adapter": adapter.name, "label": label,
            "family": mode.get("family", ""), "mode_id": mode.get("mode_id", ""),
            "preset": preset,
            "delay_ms": spec[0] if spec else "", "doppler_hz": spec[1] if spec else "",
            "filter_mode": args.filter if spec else "",
            "snr_db": f"{snr:.2f}", "bw_hz": f"{bw:.0f}",
            "sample_rate": mode.get("sample_rate", ""),
            "frames": n, "decoded": k, "fer": f"{fer:.6f}",
            "fer_lo": f"{lo:.6f}", "fer_hi": f"{hi:.6f}",
            "goodput_bps": f"{mode['payload_bytes'] * 8.0 * (1 - fer) / air:.2f}",
            "payload_bytes": mode["payload_bytes"], "air_s": f"{air:.4f}",
            "bandwidth_hz": mode.get("bandwidth_hz", ""),
            "nominal_bps": f"{float(mode.get('nominal_bps', 0)):.2f}",
            "false_decode": a["false_decode"], "wrong_frame": a["wrong_frame"],
            "duplicates": a["duplicates"], "sync_count": a["sync_count"],
            "crc_errors": a["crc_errors"],
            "mean_snr_db": f"{a['snr_sum'] / a['n']:.2f}" if a["n"] else "",
            "mean_ber": f"{a['ber_sum'] / a['n']:.5f}" if a["n"] else "",
            "rms_dbfs": mode.get("rms_dbfs", ""),
            "peak_dbfs": mode.get("peak_dbfs", ""),
            "papr_db": mode.get("papr_db", ""),
            "crc_bits": mode.get("crc_bits") if mode.get("crc_bits") else "",
            "extra_json": json.dumps(a["extra"], separators=(";", ":")),
            "batches": len(batch_list), "seed_base": args.seed,
            "cold": 1 if args.cold else 0, "host": host, "arch": arch,
            "driver_id": driver_id,
        }
        with wlock:
            writer.writerow(row)
        stats["cells"] += 1
    log(f"  = {label}: {len(acc)} cell(s) over {len(batch_list)} batch(es)")


def frange(lo, hi, step):
    out, x = [], hi
    while x >= lo - 1e-9:
        out.append(round(x, 4))
        x -= step
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--adapter", required=True,
                    help="modem73 | armstrong | <any skywave.adapters.vector_X>")
    ap.add_argument("--out", required=True)
    ap.add_argument("--select", default="",
                    help="file of mode labels, one per line (default: all)")
    ap.add_argument("--presets", default="off")
    ap.add_argument("--frames", type=int, default=100)
    ap.add_argument("--snr-lo", type=float, default=-15.0)
    ap.add_argument("--snr-hi", type=float, default=24.0)
    ap.add_argument("--snr-step", type=float, default=2.0)
    ap.add_argument("--bw", type=float, default=2500.0,
                    help="reference noise bandwidth Hz; 0 = each mode's own")
    ap.add_argument("--gap-ms", type=int, default=300)
    ap.add_argument("--filter", default="milstd")
    ap.add_argument("--seed", type=int, default=4242)
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    ap.add_argument("--batch-seconds", type=float, default=300.0)
    ap.add_argument("--scratch", default="out")
    ap.add_argument("--cold", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--allow-long", action="store_true")
    a = ap.parse_args(argv)

    adapter = load_adapter(a.adapter)
    modes = adapter.list_modes()
    if a.select:
        with open(a.select) as f:
            keep = {l.strip() for l in f if l.strip() and not l.startswith("#")}
        missing = keep - {m["label"] for m in modes}
        if missing:
            log(f"vector_sweep: {len(missing)} selected label(s) unknown to "
                f"{a.adapter}: {sorted(missing)[:5]}")
        modes = [m for m in modes if m["label"] in keep]

    dropped = 0
    if a.limit and a.limit < len(modes):
        dropped = len(modes) - a.limit
        modes = modes[:a.limit]

    presets = [p.strip() for p in a.presets.split(",") if p.strip()]
    snrs = frange(a.snr_lo, a.snr_hi, a.snr_step)

    err = check_schema(a.out)
    if err:
        log("vector_sweep: " + err)
        return 2
    done = load_done(a.out)

    os.makedirs(a.scratch, exist_ok=True)
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    atexit.register(_cleanup_all)
    if (n := reap_stale(a.scratch)):
        log(f"vector_sweep: reaped {n} stale scratch dir(s)")

    ncells = len(modes) * len(presets) * len(snrs)
    todo_audio = sum(float(m["air_s"]) * a.frames for m in modes
                     for p in presets for s in snrs
                     if (m["label"], p, round(s, 3)) not in done)
    est = todo_audio / (PER_CORE_REALTIME * max(a.jobs, 1))
    log(f"vector_sweep: adapter={adapter.name} | {len(modes)} modes x "
        f"{len(presets)} presets x {len(snrs)} SNR = {ncells} cells")
    log(f"vector_sweep: {a.frames} frames/cell, {a.jobs} jobs, "
        f"host {socket.gethostname()} ({platform.machine()})")
    log(f"vector_sweep: {todo_audio / 3600:.1f} h audio -> ~{est / 60:.0f} min wall")
    if len(done):
        log(f"vector_sweep: resuming, {len(done)} cell(s) already present")
    if dropped:
        log(f"vector_sweep: DROPPED {dropped} mode(s) via --limit")
    if est > DEV_BUDGET_S and not a.allow_long:
        log(f"vector_sweep: REFUSING -- est {est / 60:.0f} min exceeds the "
            f"{DEV_BUDGET_S // 60} min interactive-box limit. Owner rule: "
            "anything over 30 min belongs on a rented box or the Mac, not dev. "
            "Cut scope or pass --allow-long.")
        return 2

    new = not os.path.exists(a.out) or os.path.getsize(a.out) == 0
    stats = dict(cells=0, skipped=0, failed=0)
    wlock = threading.Lock()
    with open(a.out, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        with ThreadPoolExecutor(max_workers=a.jobs) as ex:
            futs = {ex.submit(do_mode, adapter, m, presets, snrs, a, done, w,
                              wlock, stats): m["label"] for m in modes}
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as e:                       # noqa: BLE001
                    log(f"  ! {futs[fut]}: {type(e).__name__}: {e}")
                    stats["failed"] += 1

    log(f"vector_sweep: wrote {stats['cells']} cell(s), skipped "
        f"{stats['skipped']}, {stats['failed']} failure(s) -> {a.out}")
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
