#!/usr/bin/env python3
"""ft8_campaign.py — unattended FT8 channel-estimate campaign over public KiwiSDRs.

    ft8_campaign.py record  --out DIR --kiwi host[:port][,host2] --bands 14074,7074 [--interval 600]
                            [--slots 2] [--duration 10800]
    ft8_campaign.py process --out DIR          # run ft8_chanest on every unprocessed capture
    ft8_campaign.py report  --out DIR          # per band × UTC hour tables + figure

Layout: DIR/raw/<kiwi>_<band>_<UTC>.wav (+ .json sidecar: kiwi, band_khz, t0_utc, fs, slots)
        DIR/est/<same>.jsonl (one row per decoded signal, ft8_chanest fields + sidecar fields)
        DIR/REPORT.md, DIR/campaign.png

Etiquette: one short connection per band per cycle (`slots`×15 s + 2 s), sequential, on one
receiver channel; the default cycle is 10 min ⇒ ~10 % duty on that channel. Failures (busy
receiver, network) are logged and the loop carries on — `record` is resumable, `process` skips
what it has done, so a crash or restart loses at most one cycle.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import subprocess
import sys
import time
import wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
KIWIREC = os.environ.get("KIWIRECORDER", os.path.expanduser("~/tools/kiwiclient/kiwirecorder.py"))


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def log(msg):
    print(f"[{utc_now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ----------------------------------------------------------------------- record --
def s_meter_dbm(host, port, band_khz, secs=17):
    """RSSI (dBm in the 3 kHz passband, receiver-calibrated), 1 s averages over one full FT8 slot.
    Returns (median, minimum): FT8 transmissions occupy 12.6 s of each 15 s slot, so the minimum
    1 s reading falls in the 2.4 s gap and is the passband NOISE power with no signal in it."""
    cmd = ["python3", KIWIREC, "-s", host, "-p", str(port), "-f", str(band_khz), "-m", "usb", "-L", "0", "-H", "3000",
           "--S-meter=0", "--sdt-sec=1", "--tlimit", str(secs)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=secs + 40).stdout
    except subprocess.TimeoutExpired:
        return None
    v = [float(l.split("RSSI:")[1]) for l in out.splitlines() if "RSSI:" in l]
    return (float(np.median(v)), float(np.min(v))) if len(v) >= 10 else (None, None)


def record_one(host, port, band_khz, out_dir, slots, pre_s=4.0, agc_gain=60):
    # pre_s: launch this long before the 15 s boundary. The stream starts 0.5–2.5 s after launch
    # (measured, varies per connection), so the boundary lands 1.5–3.5 s into the file and
    # `process` locates it from the decodes rather than assuming it.
    """Wait for a 15 s boundary, record slots×15 s (+pre-roll), return the wav path or None."""
    now = time.time()
    wait = 15 - (now % 15) - pre_s
    if wait < 0:
        wait += 15
    time.sleep(wait)
    t0 = utc_now()
    tag = f"{host.replace('.', '-')}_{band_khz}_{t0.strftime('%Y%m%dT%H%M%S')}"
    secs = int(slots * 15 + pre_s + 1)
    cmd = ["python3", KIWIREC, "-s", host, "-p", str(port), "-f", str(band_khz), "-m", "usb", "-L", "0", "-H", "3000",
           "--tlimit", str(secs), "--dir", out_dir, "--filename", tag, "-q"]
    if agc_gain is not None:
        cmd += ["-g", str(agc_gain)]          # AGC OFF: the recorded level then tracks the antenna
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=secs + 60)
    except subprocess.TimeoutExpired:
        log(f"  {host} {band_khz}: kiwirecorder timeout")
        return None
    wavs = glob.glob(os.path.join(out_dir, tag + "*.wav"))
    if not wavs or os.path.getsize(wavs[0]) < 100000:
        log(f"  {host} {band_khz}: no capture (rc {r.returncode}) {(r.stderr or r.stdout)[-160:].strip()}")
        for w in wavs:
            os.remove(w)
        return None
    path = os.path.join(out_dir, tag + ".wav")
    os.replace(wavs[0], path)
    with wave.open(path) as w:
        fs, n = w.getframerate(), w.getnframes()
    rssi, rssi_gap = s_meter_dbm(host, port, band_khz)   # absolute anchor right after the capture
    side = {"kiwi": f"{host}:{port}", "band_khz": band_khz, "t0_utc": t0.isoformat(), "fs": fs,
            "seconds": n / fs, "slots": slots, "pre_s": pre_s, "agc_gain": agc_gain,
            "rssi_dbm": rssi, "rssi_gap_dbm": rssi_gap}
    json.dump(side, open(path[:-4] + ".json", "w"))
    log(f"  {host} {band_khz}: {n / fs:.1f} s @ {fs} Hz, RSSI median {rssi} / slot-gap {rssi_gap} dBm")
    return path


def record(args):
    raw = os.path.join(args.out, "raw")
    os.makedirs(raw, exist_ok=True)
    kiwis = []
    for k in args.kiwi.split(","):
        host, _, port = k.partition(":")
        kiwis.append((host, int(port or 8073)))
    bands = [int(b) for b in args.bands.split(",")]
    t_end = time.time() + args.duration
    cycle = 0
    while time.time() < t_end:
        cycle += 1
        t_cycle = time.time()
        log(f"cycle {cycle}")
        for host, port in kiwis:
            for b in bands:
                record_one(host, port, b, raw, args.slots, agc_gain=None if args.agc else args.agc_gain)
        if args.process_each_cycle:
            process(args)
        sleep = args.interval - (time.time() - t_cycle)
        if sleep > 0 and time.time() + sleep < t_end:
            time.sleep(sleep)
        elif sleep > 0:
            break
    log("record done")


# ---------------------------------------------------------------------- process --
def slot_phase(x, fs, max_off_s):
    """Find the 15 s slot boundary in the file from the decodes: for a decode window starting at
    o, WSJT-X's dt is (boundary − o), so boundary = o + median(dt). Scan o in 1 s steps (jt9 -d 1,
    ~1 s each), take the o with most decodes. Returns boundary seconds, or None if nothing decodes."""
    import subprocess, tempfile
    from scipy.signal import resample_poly
    from math import gcd
    if fs != 12000:
        g = gcd(int(fs), 12000)
        x12 = resample_poly(x.astype(float), 12000 // g, int(fs) // g)
    else:
        x12 = x.astype(float)
    best = (0, None)
    with tempfile.TemporaryDirectory() as td:
        for o in range(0, int(max_off_s) + 1):
            seg = x12[o * 12000:(o + 15) * 12000]
            if len(seg) < 14 * 12000:
                break
            p = os.path.join(td, "w.wav")
            with wave.open(p, "wb") as ww:
                ww.setnchannels(1); ww.setsampwidth(2); ww.setframerate(12000)
                ww.writeframes(np.clip(seg, -32768, 32767).astype(np.int16).tobytes())
            out = subprocess.run(["jt9", "-8", "-d", "1", p], capture_output=True, text=True, cwd=td, timeout=60).stdout
            dts = [float(l.split()[2]) for l in out.splitlines() if " ~ " in l]
            if len(dts) > best[0]:
                best = (len(dts), o + float(np.median(dts)))
    return best[1]


def split_slots(path, side, slot_dir):
    with wave.open(path) as w:
        fs = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    b = slot_phase(x, fs, side["pre_s"] + 1)
    if b is None:
        log(f"  {os.path.basename(path)}: no decodes at any slot phase")
        return []
    while b < 0:                       # stream started after the boundary: first slot is partial
        b += 15.0
    side["slot_phase_s"] = b
    pre = int(round(b * fs))
    n15 = 15 * fs
    out = []
    for k in range((len(x) - pre) // n15):
        seg = x[pre + k * n15: pre + (k + 1) * n15]
        p = os.path.join(slot_dir, os.path.basename(path)[:-4] + f"_s{k}.wav")
        with wave.open(p, "wb") as ww:
            ww.setnchannels(1); ww.setsampwidth(2); ww.setframerate(fs); ww.writeframes(seg.tobytes())
        out.append((p, k))
    return out


def process(args):
    import ft8_chanest as F
    raw, est, slots = (os.path.join(args.out, d) for d in ("raw", "est", "slots"))
    os.makedirs(est, exist_ok=True); os.makedirs(slots, exist_ok=True)
    todo = [p for p in sorted(glob.glob(os.path.join(raw, "*.wav")))
            if not os.path.exists(os.path.join(est, os.path.basename(p)[:-4] + ".jsonl"))
            and os.path.exists(p[:-4] + ".json")]          # sidecar = capture complete
    for path in todo:
        side = json.load(open(path[:-4] + ".json"))
        t0 = dt.datetime.fromisoformat(side["t0_utc"])
        rows = []
        for sp, k in split_slots(path, side, slots):
            try:
                rr = F.run_file(sp)
            except Exception as e:                       # one bad slot must not stop the campaign
                log(f"  {os.path.basename(sp)}: estimator error {e!r}")
                continue
            slot_t = t0 + dt.timedelta(seconds=side["slot_phase_s"] + 15 * k)
            # absolute noise density: RSSI is the whole-passband power; the clean spectral floor is per
            # 6.25 Hz bin in the same (fixed-gain) units as the file's total passband power
            noise_dbm_hz = float("nan")
            if side.get("rssi_dbm") is not None and side.get("agc_gain") is not None and rr:
                with wave.open(sp) as w_:
                    xx = np.frombuffer(w_.readframes(w_.getnframes()), dtype=np.int16).astype(float) / 32768
                p_tot = float(np.mean(xx ** 2))                       # passband power, file units
                # floor per 6.25 Hz bin × (3000/6.25 bins) = noise power in the 3 kHz passband
                p_noise = rr[0]["floor_spectral_clean"] * (3000 / 6.25)
                # both are analytic-signal periodogram units vs real-signal mean square: |analytic|² = 2·real²
                noise_dbm_hz = side["rssi_dbm"] + 10 * np.log10(p_noise / (2 * p_tot)) - 10 * np.log10(3000)
            # preferred anchor: the slot-gap S-meter minimum IS the passband noise (no ratio needed)
            noise_gap_dbm_hz = (side["rssi_gap_dbm"] - 10 * np.log10(3000)) if side.get("rssi_gap_dbm") is not None else float("nan")
            for r in rr:
                r.pop("g_re", None); r.pop("g_im", None); r.pop("nbin", None); r.pop("amp_acf", None)
                r["noise_dbm_hz"] = noise_gap_dbm_hz if noise_gap_dbm_hz == noise_gap_dbm_hz else noise_dbm_hz
                r["noise_ratio_dbm_hz"] = noise_dbm_hz; r["noise_gap_dbm_hz"] = noise_gap_dbm_hz
                r["rssi_dbm"] = side.get("rssi_dbm"); r["agc_gain"] = side.get("agc_gain")
                r.update({"kiwi": side["kiwi"], "band_khz": side["band_khz"], "slot_utc": slot_t.isoformat(),
                          "utc_hour": slot_t.hour + slot_t.minute / 60})
                rows.append(r)
        with open(os.path.join(est, os.path.basename(path)[:-4] + ".jsonl"), "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        if not args.keep_slots:                          # intermediates are 1.6× the raw capture
            for pat in (os.path.basename(path)[:-4] + "_s*.wav",):
                for f_ in glob.glob(os.path.join(slots, pat)) + glob.glob(os.path.join(slots, "_rs12k", pat)):
                    os.remove(f_)
        json.dump(side, open(path[:-4] + ".json", "w"))          # records the measured slot phase
        log(f"  {os.path.basename(path)}: phase {side.get('slot_phase_s', float('nan')):.2f} s, "
            f"{len(rows)} signals, {sum(r['spread_reliable'] for r in rows)} reliable")


# ----------------------------------------------------------------------- report --
SURFACE, INK, INK2, INK3, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#8a8985", "#e6e5e1"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]


def q(v, p):
    return float(np.percentile(v, p)) if len(v) else float("nan")


def f1(x):
    return "–" if x != x else f"{x:.2f}"


def report(args):
    rows = []
    for p in glob.glob(os.path.join(args.out, "est", "*.jsonl")):
        rows += [json.loads(l) for l in open(p)]
    if not rows:
        print("no estimates yet"); return
    bands = sorted(set(r["band_khz"] for r in rows))
    L = [f"# FT8 channel-estimate campaign — {args.out}\n",
         f"{len(rows)} decoded signals, {sum(r['spread_reliable'] for r in rows)} with reliable spread "
         f"(per-bin SNR ≥ 16 dB); receivers {sorted(set(r['kiwi'] for r in rows))}; "
         f"{min(r['slot_utc'] for r in rows)} → {max(r['slot_utc'] for r in rows)} UTC.\n",
         "Per band × UTC hour. Spread = 2σ Doppler (Hz, reliable signals only); t_coh = amplitude ACF < 0.5 (s); "
         "fade p10 = 10th-percentile gain within a 12.6 s message (dB rel. RMS); SNR = spectral, after cancelling "
         "decoded signals; SINR gap = local empty-bin reading minus SNR (dB, negative = neighbours present); "
         "floor = noise density in dBm/Hz from the slot-gap S-meter minimum (fixed gain) or, for AGC captures, dB relative "
         "to the campaign median for that receiver — AGC-on floors rise as a band empties and mean nothing.\n"]
    med_floor = {}
    for k in set(r["kiwi"] for r in rows):
        v = [r["floor_spectral_clean"] for r in rows if r["kiwi"] == k]
        med_floor[k] = float(np.median(v))
    absf = any(r.get("noise_dbm_hz", float("nan")) == r.get("noise_dbm_hz", float("nan")) for r in rows)
    L.append("| band | UTC h | slots | decodes | reliable | spread p50 | spread p90 | t_coh p50 | fade p10 p50 | SNR p50 | SINR gap p50 | "
             + ("floor dBm/Hz |" if absf else "floor dB (AGC, relative) |"))
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for b in bands:
        for h in range(24):
            rr = [r for r in rows if r["band_khz"] == b and int(r["utc_hour"]) == h]
            if not rr:
                continue
            rel = [r for r in rr if r["spread_reliable"]]
            fl = [r["noise_dbm_hz"] if r.get("noise_dbm_hz", float("nan")) == r.get("noise_dbm_hz", float("nan"))
                  else 10 * np.log10(r["floor_spectral_clean"] / med_floor[r["kiwi"]]) for r in rr]
            L.append(f"| {b} | {h:02d} | {len(set(r['slot_utc'] for r in rr))} | {len(rr)} | {len(rel)} | "
                     f"{f1(q([r['doppler_2sigma_hz'] for r in rel], 50))} | {f1(q([r['doppler_2sigma_hz'] for r in rel], 90))} | "
                     f"{f1(q([r['t_coh_s'] for r in rel], 50))} | {f1(q([r['fade_p10_db'] for r in rr], 50))} | "
                     f"{f1(q([r['snr2500_spectral_db'] for r in rr], 50))} | "
                     f"{f1(q([r['snr2500_mean_db'] - r['snr2500_spectral_db'] for r in rr], 50))} | {f1(q(fl, 50))} |")
    open(os.path.join(args.out, "REPORT.md"), "w").write("\n".join(L) + "\n")
    # figure: per band, reliable spread and clean floor vs time
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(3, 1, figsize=(12, 9), facecolor=SURFACE, sharex=True)
    for ax in axes:
        ax.set_facecolor(SURFACE); ax.grid(True, color=GRID, linewidth=0.8); ax.set_axisbelow(True)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        ax.tick_params(colors=INK2, labelsize=8)
    t_ref = min(dt.datetime.fromisoformat(r["slot_utc"]) for r in rows)
    for i, b in enumerate(bands):
        rr = [r for r in rows if r["band_khz"] == b]
        rel = [r for r in rr if r["spread_reliable"]]
        th = [(dt.datetime.fromisoformat(r["slot_utc"]) - t_ref).total_seconds() / 3600 for r in rel]
        axes[0].scatter(th, [r["doppler_2sigma_hz"] for r in rel], s=8, color=SERIES[i], linewidths=0, label=f"{b} kHz")
        th2 = [(dt.datetime.fromisoformat(r["slot_utc"]) - t_ref).total_seconds() / 3600 for r in rr]
        axes[1].scatter(th2, [r["snr2500_spectral_db"] for r in rr], s=8, color=SERIES[i], linewidths=0, label=f"{b} kHz")
        slots_b = sorted(set(r["slot_utc"] for r in rr))
        fl_t = [(dt.datetime.fromisoformat(s) - t_ref).total_seconds() / 3600 for s in slots_b]
        def _fl(r):
            v = r.get("noise_dbm_hz", float("nan"))
            return v if v == v else 10 * np.log10(r["floor_spectral_clean"] / med_floor[r["kiwi"]])
        fl_v = [_fl(next(r for r in rr if r["slot_utc"] == s)) for s in slots_b]
        axes[2].plot(fl_t, fl_v, color=SERIES[i], linewidth=1.5, marker="o", markersize=3, label=f"{b} kHz")
    axes[0].set_ylabel("2σ Doppler spread, Hz"); axes[0].set_yscale("log"); axes[0].set_title("Reliable spread readings by band", fontsize=10, loc="left")
    axes[1].set_ylabel("SNR2500, dB"); axes[1].set_title("Per-signal SNR (spectral, after cancellation)", fontsize=10, loc="left")
    axes[2].set_ylabel("noise floor"); axes[2].set_title("Clean noise floor per slot (dBm/Hz with fixed gain; else dB rel. median, AGC)", fontsize=10, loc="left")
    axes[2].set_xlabel(f"hours since {t_ref.strftime('%Y-%m-%d %H:%M')} UTC")
    axes[0].legend(fontsize=8, frameon=False, labelcolor=INK2)
    fig.tight_layout(); fig.savefig(os.path.join(args.out, "campaign.png"), dpi=110, facecolor=SURFACE); plt.close(fig)
    print(f"report → {os.path.join(args.out, 'REPORT.md')} ({len(rows)} signals)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("record")
    p.add_argument("--out", required=True); p.add_argument("--kiwi", required=True)
    p.add_argument("--bands", default="14074,7074"); p.add_argument("--interval", type=float, default=600)
    p.add_argument("--slots", type=int, default=2); p.add_argument("--duration", type=float, default=10800)
    p.add_argument("--process-each-cycle", action="store_true"); p.add_argument("--keep-slots", action="store_true")
    p.add_argument("--agc-gain", type=int, default=60, help="fixed receiver gain (AGC off); 60 ≈ −26 dBFS on a busy band")
    p.add_argument("--agc", action="store_true", help="leave the receiver AGC on (noise-floor column then meaningless)")
    p = sub.add_parser("process"); p.add_argument("--out", required=True); p.add_argument("--keep-slots", action="store_true")
    p = sub.add_parser("report"); p.add_argument("--out", required=True)
    args = ap.parse_args()
    {"record": record, "process": process, "report": report}[args.cmd](args)


if __name__ == "__main__":
    main()
