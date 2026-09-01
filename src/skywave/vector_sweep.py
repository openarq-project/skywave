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
from skywave import vector_fm_channel as vfm
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
    # production | bench. Load-bearing: vector_report keeps non-production rows
    # off the primary frontier, so an ablation cannot outrank a shipping mode.
    "mode_class",
    # Drive-knob arm. Empty = the mode's built-in default. When set, `label`
    # carries it too (LABEL@clip=<g>) so the arm is a distinct series everywhere
    # -- resume dedup, frontier, floor table -- and `label_base` keeps the mode.
    "clip_gain", "label_base",
    # Amplitude normalization the adapter declared in the sidecar. A corpus that
    # silently mixed normalizations would be uncomparable and nothing else would
    # show it.
    "norm",
    # Width of the payload check that adjudicates this mode. Drives the
    # false_decode gate; empty means the adapter did not report one and the gate
    # falls back to zero tolerance for that mode.
    "crc_bits",
    # Number of candidate decodes a list decoder (e.g. CA-SCL) checks per
    # frame -- each is an independent shot at a false CRC pass, so the
    # false_decode gate's expectation is lam = n * list_size * 2**-crc_bits.
    # Empty/absent means 1 (a single-candidate decoder); fully backward
    # compatible with corpora that predate this column.
    "list_size",
    # FM stage (empty on HF rows). `preset` carries the FM fade string when
    # fm_port is set, so these say WHICH channel that preset was resolved on --
    # an FM row and an HF row with the same preset name are not the same cell.
    "fm_port", "fm_fade_kind", "fm_band", "fm_shadow", "fm_squelch",
    # Squelch/limiter provenance. fm_clipped_fraction is the honest witness for
    # a drive-policy arm: an arm that reports 0.0 there did not clip, so it
    # measured nothing and must not be scored as the clipping cell.
    "fm_squelch_tone_ms", "fm_deviation_headroom_db", "fm_clipped_fraction",
    "fm_deviation_ceiling",
    # Squelch ENGAGEMENT witness (vector_fm_channel.squelch_mute_stats): the
    # muted head per burst, measured. A squelch row with mean 0 ms is VOID.
    "fm_squelch_muted_frames", "fm_squelch_mean_mute_ms",
    "fm_squelch_tail_ms", "fm_squelch_block_ms",
    # Stage decomposition, when the adapter reports it: which STAGE lost the
    # frame. preamble_count >= sync_count >= decoded, so
    # (preamble_count - sync_count) is header loss and the rest is payload.
    # This is the distinction that separates "could not find it" from "found
    # it, could not read it", and one flag for both hid DART's real bottleneck
    # through two rounds of review. Empty for adapters that do not report it.
    "preamble_count", "header_fail",
    "codec_tx", "codec_rx",
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


def accumulate_extra(extra, k, v):
    """Fold one non-core adapter key into the per-cell extras. Numerics SUM
    across batches; non-numerics JOIN non-empty values with ';' — a plain
    overwrite let a later empty batch erase an earlier batch's forensic
    record (fd_detail), exactly the value the false-decode gate needs kept.
    """
    try:
        extra[k] = extra.get(k, 0) + int(v)
        return
    except (TypeError, ValueError):
        pass
    prev = extra.get(k)
    vs = str(v).strip()
    if isinstance(prev, str) and prev and vs:
        extra[k] = prev + ";" + vs
    elif vs or not isinstance(prev, str):
        extra[k] = vs


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1.0 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = (z / d) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, c - h), min(1.0, c + h))


def _codec(adapter, spec):
    """Turn an adapter-interpreted codec spec into a runner for apply_codec.

    Only the adapter knows which codec its modem actually crosses, so the
    framework stays codec-agnostic and asks. An adapter that does not implement
    `codec_runner` fails loudly here rather than silently ignoring the flag --
    a campaign that thought it had a codec in the loop and did not would be
    unattributable afterwards.
    """
    fn = getattr(adapter, "codec_runner", None)
    if fn is None:
        raise SystemExit(
            f"vector_sweep: adapter '{adapter.name}' has no codec_runner(), so "
            f"--codec-tx/--codec-rx cannot be honoured (spec {spec!r})")
    return fn(spec)


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
            # The resume key must name the ARM, not just the cell: port, gate,
            # squelch and limiter all change the row, and a key on
            # (label, preset, snr) alone made a second arm appended to the same
            # --out skip every cell as "done" -- a corpus that looks complete
            # and is missing a whole leg (FM-CTRL pre-reg review, 2026-09-01).
            try:
                tt = json.loads(r.get("extra_json") or "{}").get("tau_table", "")
            except ValueError:
                tt = ""
            try:
                done.add((r["label"], r["preset"], round(float(r["snr_db"]), 3),
                          r.get("fm_port", ""), r.get("fm_squelch", ""),
                          r.get("fm_squelch_tone_ms", ""),
                          r.get("fm_deviation_headroom_db", ""), str(tt)))
            except (KeyError, ValueError):
                continue
    return done


def arm_fields(args, adapter=None):
    """The arm-identifying row fields, in the exact string form the CSV
    carries them (csv writes str(v)), so `done_key` matches `load_done`."""
    fm = bool(args.fm_port)
    sq = str((1 if args.fm_squelch else 0) if fm else "")
    tone = str(args.fm_squelch_tone_ms if fm and args.fm_squelch else "")
    hr = str(args.fm_deviation_headroom_db
             if fm and args.fm_deviation_headroom_db is not None else "")
    tt = ""
    if adapter is not None and hasattr(adapter, "tau_table_name"):
        tt = str(adapter.tau_table_name())
    return (args.fm_port or "", sq, tone, hr, tt)


def done_key(args, adapter, label, preset, snr):
    return (label, preset, round(snr, 3)) + arm_fields(args, adapter)


def batches_for(air_s, frames, budget_s):
    per = max(1, int(budget_s // max(air_s, 1e-6)))
    out, left = [], frames
    while left > 0:
        n = min(per, left)
        out.append(n)
        left -= n
    return out


CORE_KEYS = {"frames", "decoded", "false_decode", "wrong_frame", "duplicates",
             "mean_snr_db", "mean_ber", "sync_count", "crc_errors",
             "preamble_count", "header_fail"}


def do_mode(adapter, mode, presets, snrs, args, done, writer, wlock, stats):
    label = mode["label"]
    air = float(mode["air_s"])
    wanted = [(p, s) for p in presets for s in snrs
              if done_key(args, adapter, label, p, s) not in done]
    if not wanted:
        stats["skipped"] += len(presets) * len(snrs)
        return

    bw = args.bw if args.bw > 0 else float(mode.get("bandwidth_hz") or 2500)
    batch_list = batches_for(air, args.frames, args.batch_seconds)
    acc = {}
    scratch = tempfile.mkdtemp(prefix="swvec-", dir=args.scratch)
    _reg(scratch)
    s_offset_db = 0.0  # set per batch below; 0.0 if every batch failed early
    # Same reason as s_offset_db: set per batch, and the row is emitted even
    # when every batch failed early, so these must exist before the try.
    clip_frac, clip_ceiling = "", ""
    mute_frames, mute_ms = "", ""
    try:
        for bi, nframes in enumerate(batch_list):
            seed = args.seed + bi * 7919
            vec_path, side_path = adapter.encode(
                mode.get("label_base", label), nframes, seed, scratch,
                gap_ms=args.gap_ms, env=mode.get("_env"))
            side = load_sidecar(side_path)
            vec = read_vector(vec_path)
            validate_sidecar(side, vector_len=vec.size)
            fs = int(side["sample_rate"])
            # FM: the TX port shapes the signal BEFORE it reaches the channel,
            # so S -- and therefore every labeled SNR -- must be measured on the
            # port-shaped vector. Measuring it pre-port would carry a mode-shape
            # constant, the same error the Option-A ruling removed on HF.
            # See vector_fm_channel's docstring.
            base = vec
            if args.codec_tx:
                base, _ = vfm.apply_codec(base, fs, _codec(adapter, args.codec_tx))
            if args.fm_port:
                base, _ = vfm.apply_tx_port(base, fs, args.fm_port,
                                            args.fm_order, args.fm_ctcss_hz,
                                            args.fm_ctcss_amp)
                # TX deviation limiting sits AFTER pre-emphasis and BEFORE S,
                # matching vector_fm_channel.run()'s order exactly: a real TX
                # limits what emphasis already peaked, and S must be measured
                # on what actually goes out or every labelled SNR carries the
                # limiter's loss as a hidden constant. Hoisted out of the
                # preset/SNR loops because it depends on neither.
                if args.fm_deviation_headroom_db is not None:
                    base, cf, ceil = vfm.apply_deviation_limit(
                        base, side, fs, args.fm_deviation_headroom_db,
                        args.fm_deviation_kind)
                    clip_frac, clip_ceiling = f"{cf:.6f}", f"{ceil:.4f}"
            S = vc.clean_signal_power(base, side)
            # Option-A transition: record the old-S/new-S conversion offset so
            # pre-convention floors remain convertible (the report prints it).
            s_legacy = vc.legacy_signal_power(base, side)
            s_offset_db = (10.0 * math.log10(S / s_legacy)
                           if S > 0 and s_legacy > 0 else 0.0)

            for preset in presets:
                fm_spec = None
                if args.fm_port:
                    fm_spec = vfm.resolve_fade(preset, args.fm_band)
                    faded, _, ngain = vfm.apply_fm_fade(
                        base, side, fm_spec, seed + 101,
                        args.fm_shadow_sigma_db, args.fm_shadow_tau_s)
                else:
                    faded, _ = vc.apply_fade(vec, side, preset, seed + 101,
                                             args.filter)
                    ngain = None
                for snr in snrs:
                    if done_key(args, adapter, label, preset, snr) in done:
                        continue
                    sigma = vc.sigma_for(S, fs, bw, snr)
                    npath = os.path.join(scratch, "noisy.f32")
                    # Headroom guard (Option-A bundle): joint SNR-invariant
                    # scale-down so deep-SNR noise no longer clips at the
                    # driver's f32->i16 conversion (~0.3-0.5 dB pessimistic
                    # below ~-8 dB SNR3000 before this).
                    if args.fm_port:
                        # Link-path order: noise enters BEFORE the RX port, so
                        # de-emphasis shapes the noise too. The RX port cannot
                        # be hoisted out of the SNR loop for that reason.
                        noisy = vfm.add_awgn_shaped(
                            faded, sigma, seed + 202,
                            ngain if fm_spec and fm_spec[0] == "ionosnc" else None)
                        noisy, _ = vfm.apply_rx_port(noisy, fs, args.fm_port,
                                                     args.fm_order)
                        if args.fm_squelch:
                            pre_sq = noisy
                            noisy = vfm.apply_squelch(
                                noisy, side, fs, args.fm_squelch_open_ms,
                                args.fm_squelch_tone_ms,
                                tail_ms=args.fm_squelch_tail_ms,
                                seed=seed + 303,
                                carrier=args.fm_squelch_carrier,
                                block_ms=args.fm_squelch_block_ms)
                            ms = vfm.squelch_mute_stats(pre_sq, noisy, side, fs)
                            mute_frames = str(ms["muted_frames"])
                            mute_ms = f'{ms["mean_mute_ms"]:.1f}'
                    else:
                        # NOT an `elif` on codec_rx: this else belongs to
                        # `if args.fm_port` above and is the HF path's noise
                        # step. It was briefly stolen by the codec block
                        # (52bfc62), which silently discarded the ENTIRE FM RX
                        # chain -- rx port AND squelch -- on every campaign
                        # that did not pass --codec-rx. Caught because a
                        # squelch arm scored bit-identical to squelch-off.
                        noisy = vc.add_awgn(faded, sigma, seed + 202)
                    if args.codec_rx:
                        # LAST: the radio encodes what it demodulated. Applies
                        # ON TOP of whichever chain ran above, never instead
                        # of one.
                        noisy, _ = vfm.apply_codec(
                            noisy, fs, _codec(adapter, args.codec_rx))
                    write_vector(npath, vc.apply_headroom(noisy))
                    try:
                        r = adapter.decode(npath, side_path, cold=args.cold)
                    except Exception as e:                       # noqa: BLE001
                        log(f"  ! {label} {preset} {snr:+.1f}: {type(e).__name__}: {e}")
                        stats["failed"] += 1
                        continue
                    a = acc.setdefault((preset, snr), dict(
                        frames=0, decoded=0, false_decode=0, wrong_frame=0,
                        duplicates=0, sync_count=0, crc_errors=0,
                        preamble_count=0, header_fail=0,
                        snr_sum=0.0, ber_sum=0.0, n=0, extra={}))
                    a["frames"] += int(r.get("frames", nframes))
                    for k in ("decoded", "false_decode", "wrong_frame",
                              "duplicates", "sync_count", "crc_errors",
                              "preamble_count", "header_fail"):
                        a[k] += int(r.get(k, 0))
                    d = int(r.get("decoded", 0))
                    if d:
                        a["snr_sum"] += float(r.get("mean_snr_db", 0.0)) * d
                        a["ber_sum"] += max(0.0, float(r.get("mean_ber", 0.0))) * d
                        a["n"] += d
                    for k, v in r.items():
                        if k not in CORE_KEYS:
                            accumulate_extra(a["extra"], k, v)
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
        fm_spec = vfm.resolve_fade(preset, args.fm_band) if args.fm_port else None
        row = {
            "adapter": adapter.name, "label": label,
            "family": mode.get("family", ""), "mode_id": mode.get("mode_id", ""),
            "preset": preset,
            # Tier-A FM is a FLAT fade -- there is deliberately no tapped
            # delay line in the FM bench -- so delay_ms stays empty on FM rows
            # and doppler_hz comes from the resolved FM spec.
            "delay_ms": "" if args.fm_port else (spec[0] if spec else ""),
            "doppler_hz": (fm_spec[1] if fm_spec else "") if args.fm_port
                          else (spec[1] if spec else ""),
            "filter_mode": "" if args.fm_port else (args.filter if spec else ""),
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
            "preamble_count": a["preamble_count"] or "",
            "header_fail": a["header_fail"] or "",
            "mean_snr_db": f"{a['snr_sum'] / a['n']:.2f}" if a["n"] else "",
            "mean_ber": f"{a['ber_sum'] / a['n']:.5f}" if a["n"] else "",
            "mode_class": mode.get("mode_class", "production"),
            "clip_gain": mode.get("clip_gain", ""),
            "label_base": mode.get("label_base", label),
            "norm": side.get("norm", "") if isinstance(side, dict) else "",
            "rms_dbfs": mode.get("rms_dbfs", ""),
            "peak_dbfs": mode.get("peak_dbfs", ""),
            "papr_db": mode.get("papr_db", ""),
            "crc_bits": mode.get("crc_bits") if mode.get("crc_bits") else "",
            "list_size": mode.get("list_size") if mode.get("list_size") else "",
            "fm_port": args.fm_port,
            "fm_fade_kind": (fm_spec[0] if fm_spec else "off") if args.fm_port else "",
            "fm_band": args.fm_band if (args.fm_port and fm_spec) else "",
            "fm_shadow": (f"{args.fm_shadow_sigma_db:g}:{args.fm_shadow_tau_s:g}"
                          if args.fm_port and args.fm_shadow_sigma_db > 0 else ""),
            "fm_squelch": (1 if args.fm_squelch else 0) if args.fm_port else "",
            "fm_squelch_tone_ms": (args.fm_squelch_tone_ms
                                   if args.fm_port and args.fm_squelch else ""),
            "fm_deviation_headroom_db": (args.fm_deviation_headroom_db
                                         if args.fm_port and
                                         args.fm_deviation_headroom_db
                                         is not None else ""),
            "fm_clipped_fraction": clip_frac,
            "fm_deviation_ceiling": clip_ceiling,
            "fm_squelch_muted_frames": mute_frames,
            "fm_squelch_mean_mute_ms": mute_ms,
            "fm_squelch_tail_ms": (args.fm_squelch_tail_ms
                                   if args.fm_port and args.fm_squelch else ""),
            "fm_squelch_block_ms": (args.fm_squelch_block_ms
                                    if args.fm_port and args.fm_squelch else ""),
            "codec_tx": args.codec_tx, "codec_rx": args.codec_rx,
            "extra_json": json.dumps(
                {**a["extra"], "s_offset_db": f"{s_offset_db:.3f}"},
                separators=(",", ":")),
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
    # --- FM stage (vector_fm_channel). Setting --fm-port switches the channel
    # from Watterson+AWGN to port + Tier-A FM fade + AWGN, and reinterprets
    # --presets as FM fade specs (off | fixed | pedestrian | mobile-urban |
    # mobile-highway | ionos:<d>:<r> | ionosnc:<d>:<r>:<sn0> | rayleigh:<fD> |
    # rice:<fD>[:<K>] | static).
    ap.add_argument("--fm-port", default="", choices=("",) + vfm.PORTS,
                    help="enable the FM stage on this port profile")
    ap.add_argument("--fm-band", default="2m", choices=list(vfm.fm_channel.BANDS))
    ap.add_argument("--fm-order", type=int, default=6)
    ap.add_argument("--fm-ctcss-hz", type=float, default=0.0)
    ap.add_argument("--fm-ctcss-amp", type=float, default=0.0)
    ap.add_argument("--fm-shadow-sigma-db", type=float, default=0.0)
    ap.add_argument("--fm-shadow-tau-s", type=float, default=0.0)
    ap.add_argument("--fm-squelch", action="store_true")
    ap.add_argument("--fm-squelch-open-ms", type=float, default=30.0)
    # CTCSS tone-squelch adds decode time on TOP of carrier detect, and it
    # costs that time SIMPLEX -- there need not be a repeater in the path.
    # Without this the sweep could only ever model carrier squelch.
    ap.add_argument("--fm-squelch-tail-ms", type=float, default=0.0,
                    help="squelch HANG time after carrier drop (ms): a noise "
                         "tail, then mute. 0 = mute immediately. The "
                         "ctrl detector draws its floor from the trailing "
                         "burst-free region, so this decides whether a gated "
                         "row can be scored at all (t_infinite witness).")
    ap.add_argument("--fm-squelch-block-ms", type=float,
                    default=vfm.SQUELCH_BLOCK_MS,
                    help="squelch state-machine block (ms); attack time is "
                         "quantized to it. 32 = DART's 1024 @ 32 kHz.")
    ap.add_argument("--fm-squelch-tone-ms", type=float, default=0.0,
                    help="additional CTCSS tone-decode delay before the "
                         "squelch opens (TIA-603 class: 80-200 ms). Applies "
                         "on top of --fm-squelch-open-ms.")
    # TX deviation limiting. Parameterised by HEADROOM above the signal's own
    # RMS rather than an absolute level, so the knob means the same thing
    # across modes: below a mode's PAPR it clips, above it does not, and the
    # difference IS the PAPR penalty.
    ap.add_argument("--fm-deviation-headroom-db", type=float, default=None,
                    help="TX deviation ceiling above signal RMS, in dB. "
                         "Omitted = no limiting. Every row reports "
                         "clipped_fraction; an arm reporting 0.0 there "
                         "measured nothing.")
    ap.add_argument("--fm-deviation-kind", default="hard",
                    choices=("hard", "soft"))
    ap.add_argument("--fm-squelch-carrier", default="frames",
                    choices=("frames", "energy"))
    # Codec-in-the-loop. The runner is ADAPTER-supplied (only the adapter knows
    # which codec its modem crosses), so these take an adapter-interpreted spec
    # rather than a command line. DART: "bitpool=40,alloc=snr" app->radio and
    # "bitpool=18,alloc=loudness" radio->app -- the two directions are not
    # symmetric and the return path is the one the app cannot change.
    ap.add_argument("--codec-tx", default="",
                    help="adapter codec spec for the transmit hop")
    ap.add_argument("--codec-rx", default="",
                    help="adapter codec spec for the receive hop")
    ap.add_argument("--seed", type=int, default=4242)
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    ap.add_argument("--batch-seconds", type=float, default=300.0)
    ap.add_argument("--scratch", default="out")
    ap.add_argument("--clip-env", default="",
                    help="name of the adapter's drive-knob env var, e.g. "
                         "ARM_QAM64W_CLIPGAIN. Requires --clip-gains.")
    ap.add_argument("--clip-gains", default="",
                    help="comma-separated values for --clip-env. Each becomes a "
                         "separate arm labelled LABEL@clip=<v>. Include the "
                         "mode's DEFAULT as a control by also sweeping without "
                         "this flag, or by naming the built-in value.")
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

    # Drive-knob arms. Each value becomes its own series with its own label, so
    # every downstream consumer (resume dedup, frontier, floor table) treats the
    # arms as distinct modes without needing to know what a clip gain is.
    gains = [g.strip() for g in a.clip_gains.split(",") if g.strip()]
    if gains and not a.clip_env:
        log("vector_sweep: --clip-gains needs --clip-env")
        return 2
    if a.clip_env and not gains:
        log("vector_sweep: --clip-env needs --clip-gains")
        return 2
    if gains:
        armed = []
        for g in gains:
            # Re-measure under the arm: a drive knob CHANGES the waveform, and
            # papr_db is exactly what the equal-PEP derivation keys on, so
            # carrying the default mode's crest into a clipped arm would corrupt
            # it. The driver measures, so both arms use one convention.
            try:
                relevel = {m["label"]: m
                           for m in adapter.list_modes(env={a.clip_env: g})}
            except TypeError:
                relevel = {}
                log(f"vector_sweep: WARNING {a.adapter} cannot re-measure levels "
                    f"per arm; papr_db/rms_dbfs will describe the DEFAULT drive, "
                    f"not clip={g}. Do not derive equal-PEP floors from it.")
            for m in modes:
                arm = dict(m)
                arm.update({k: v for k, v in relevel.get(m["label"], {}).items()
                            if k in ("rms_dbfs", "peak_dbfs", "papr_db")})
                arm["label_base"] = m["label"]
                arm["label"] = f"{m['label']}@clip={g}"
                arm["clip_gain"] = g
                arm["_env"] = {a.clip_env: g}
                armed.append(arm)
        log(f"vector_sweep: clip axis {a.clip_env} = {gains} -> "
            f"{len(modes)} mode(s) x {len(gains)} arm(s) = {len(armed)} series")
        modes = armed

    presets = [p.strip() for p in a.presets.split(",") if p.strip()]
    snrs = frange(a.snr_lo, a.snr_hi, a.snr_step)

    if a.fm_port:
        # Resolve every fade spec BEFORE any cell runs: a typo must fail at
        # startup, not produce a campaign missing one arm. Same reasoning as
        # validate_sidecar checking on the first cell.
        for p in presets:
            try:
                vfm.resolve_fade(p, a.fm_band)
            except (ValueError, SystemExit) as e:
                log(f"vector_sweep: bad --presets entry '{p}' for the FM "
                    f"stage: {e}")
                return 2
        if a.fm_squelch and a.fm_port != "micspk":
            log("vector_sweep: --fm-squelch is a micspk stage "
                "(data9600 is the squelchless discriminator tap)")
            return 2
        log(f"vector_sweep: FM stage on port={a.fm_port} band={a.fm_band}"
            + (f" shadow={a.fm_shadow_sigma_db:g}dB/{a.fm_shadow_tau_s:g}s"
               if a.fm_shadow_sigma_db > 0 else "")
            + (" squelch" if a.fm_squelch else ""))

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
                     if done_key(a, adapter, m["label"], p, s) not in done)
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
