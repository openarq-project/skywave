#!/usr/bin/env python3
"""qrm_campaign.py — the public-KiwiSDR QRM pilot runner (pre-reg: openarq reviews/QRM-KIWI-PILOT-PREREG-2026-09-09.md).

One process per SITE, ONE receiver connection at a time (public receivers refuse a second connection
from the same IP — badp=5 — and one channel is the etiquette anyway). Hourly schedule:
  cycles 1-3 (15 min each): waterfall 5 min per band on 40/30/20 m via kiwi_wf_record.py
  cycle 4 (~15 min):        IQ round: 2 min fixed-gain IQ at each replay centre + S-meter anchor
                            (median + minimum of 1 s readings over 17 s) — waterfall coverage 45 min/h

    qrm_campaign.py record --site A --out DIR [--hours 168] [--dwell-min 5] [--alternate host:port]
    qrm_campaign.py record --host H --port P --station tag --out DIR ...      (ad-hoc site)

Stop rules (pre-reg §8): a receiver refusing a dwell is retried once after 30 s, then logged and
skipped; 3 consecutive fully-refused cycles ⇒ swap to --alternate if given (logged), else keep
trying and log ALERT; free disk < --min-free-gb ⇒ IQ tier halts, waterfall continues.
Every file has a sidecar; a file without one is not a capture.
"""
import argparse, collections, datetime as dt, glob, json, os, shutil, subprocess, sys, time, wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ft8_campaign import s_meter_dbm                          # noqa: E402
from kiwi_wf_record import kiwi_status                        # noqa: E402

KIWIREC = os.path.expanduser("~/tools/kiwiclient/kiwirecorder.py")
WFREC = os.path.join(HERE, "kiwi_wf_record.py")

SITES = {                                                     # pre-reg §2
    "A": ("162.199.177.108", 8073, "coventryOH"),
    "B": ("23197.proxy.kiwisdr.com", 8073, "northernneckVA"),
    "C": ("22091.proxy.kiwisdr.com", 8073, "youngsvilleNC"),
    "FL": ("22315.proxy.kiwisdr.com", 8073, "palmharborFL"),
    "NC2": ("ssi.proxy.kiwisdr.com", 8073, "bakersvilleNC"),
    "NC3": ("kiwisdr.itfais.com", 8073, "laurelspringsNC"),   # KT4RS Laurel Springs NC (EM96): passed qrm_site_gate 2026-09-21, C's alternate for the four-week run
}
BANDS = [                                                     # pre-reg §3: (name, wf centre kHz, zoom, IQ centres kHz)
    ("40m", 7097.0, 10, [7101.9, 7107.0]),
    ("30m", 10138.0, 10, [10146.4, 10133.0]),
    ("20m", 14095.0, 9, [14097.0, 14108.0]),
]
LOG = [None]


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def log(msg):
    line = f"{utc_now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    if LOG[0]:
        with open(LOG[0], "a") as f:
            f.write(line + "\n")


def free_gb(path):
    return shutil.disk_usage(path).free / 1e9


def wf_dwell(host, port, station, band, minutes, out):
    """One waterfall dwell as a subprocess. Returns 'ok' | 'busy' | 'fail'."""
    name, centre, zoom, _ = band
    cmd = [sys.executable, WFREC, "-s", host, "-p", str(port), "-f", str(centre), "-z", str(zoom),
           "--minutes", str(minutes), "--out", out, "--station", station]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=minutes * 60 + 90)
    except subprocess.TimeoutExpired:
        log(f"  wf {station} {name}: TIMEOUT (killed)")
        return "fail"
    if r.returncode == 0:
        log(f"  wf {station} {name}: {r.stdout.strip().splitlines()[-1] if r.stdout.strip() else 'ok'}")
        return "ok"
    if r.returncode == 3:
        log(f"  wf {station} {name}: BUSY {r.stderr.strip()[-120:]}")
        return "busy"
    log(f"  wf {station} {name}: FAIL rc={r.returncode} {(r.stderr or r.stdout)[-200:].strip()}")
    return "fail"


def iq_capture(host, port, station, band, centre, secs, out, gain=60):
    """2-min fixed-gain IQ capture (±6 kHz) + S-meter anchor + sidecar. Returns True on success."""
    name = band[0]
    t0 = utc_now()
    tag = f"{station}_{name}_iq{int(centre * 10)}_{t0.strftime('%Y%m%dT%H%M%S')}"
    cmd = [sys.executable, KIWIREC, "-s", host, "-p", str(port), "-f", str(centre), "-m", "iq", "-L", "-6000", "-H", "6000",
           "-g", str(gain), "--tlimit", str(secs), "--dir", out, "--filename", tag, "-q",
           "--busy-retries", "1", "--busy-timeout", "5"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=secs + 90)
    except subprocess.TimeoutExpired:
        log(f"  iq {station} {name} {centre}: TIMEOUT")
        return False
    wavs = glob.glob(os.path.join(out, tag + "*.wav"))
    if not wavs or os.path.getsize(wavs[0]) < 100000:
        log(f"  iq {station} {name} {centre}: no capture rc={r.returncode} {(r.stderr or r.stdout)[-160:].strip()}")
        for w in wavs:
            os.remove(w)
        return False
    path = os.path.join(out, tag + ".wav")
    os.replace(wavs[0], path)
    with wave.open(path) as w:
        fs, n, ch = w.getframerate(), w.getnframes(), w.getnchannels()
    time.sleep(8)
    sm = s_meter_dbm(host, port, centre, secs=17) or (None, None)
    status = kiwi_status(host, port)
    side = {"host": f"{host}:{port}", "station": station, "band": name, "centre_khz": centre, "mode": "iq",
            "passband_hz": [-6000, 6000], "fs": fs, "channels": ch, "seconds": n / fs, "start_utc": t0.isoformat(),
            "agc_gain": gain, "rssi_dbm": sm[0], "rssi_min_dbm": sm[1],
            "rssi_note": "S-meter over the 3 kHz USB passband at centre_khz, 1 s readings over 17 s; min ≈ noise",
            "sm_cal": status.get("sm_cal"), "wf_cal": status.get("wf_cal"), "receiver": status}
    json.dump(side, open(path[:-4] + ".json", "w"), indent=1)
    log(f"  iq {station} {name} {centre}: {n / fs:.0f} s @ {fs} Hz, rssi {sm[0]} / min {sm[1]} dBm")
    return True


def iq_round(a, host, port, station, out):
    """One IQ round on the SAME connection slot (public receivers may refuse a second connection from one IP):
    every replay centre, 2 min each + S-meter anchor. Returns the number of captures landed."""
    if free_gb(out) < a.min_free_gb:
        log(f"  iq {station}: HALT free disk {free_gb(out):.1f} GB < {a.min_free_gb} (round skipped)")
        return 0
    got = 0
    for band in BANDS:
        for centre in band[3]:
            ok = iq_capture(host, port, station, band, centre, a.iq_secs, out)
            if not ok:
                time.sleep(30)
                ok = iq_capture(host, port, station, band, centre, a.iq_secs, out)   # single retry
            got += bool(ok)
            time.sleep(3)
    return got


def record(a):
    if a.site:
        host, port, station = SITES[a.site]
    else:
        host, port, station = a.host, a.port, a.station
    os.makedirs(a.out, exist_ok=True)
    LOG[0] = os.path.join(a.out, "record.log")
    alt = None
    if a.alternate:
        h, _, p = a.alternate.partition(":")
        alt = (h, int(p or 8073), SITES.get(a.alternate, (None, None, f"alt_{h.split('.')[0]}"))[2])
        if a.alternate in SITES:
            alt = SITES[a.alternate]
    log(f"record: site {station} {host}:{port} → {a.out}; {a.hours} h, dwell {a.dwell_min} min, "
        f"iq {a.iq_secs} s hourly; status {kiwi_status(host, port)}")
    t_end = time.time() + a.hours * 3600
    refused_cycles = 0
    cycle = 0
    while time.time() < t_end:
        cycle += 1
        t_cycle = time.time()
        if not a.no_iq and cycle % a.iq_every == 0:           # hourly IQ round takes one cycle's slot
            n = iq_round(a, host, port, station, a.out)
            log(f"cycle {cycle}: IQ round, {n}/{sum(len(b[3]) for b in BANDS)} captures landed")
            if n == 0:
                time.sleep(60)
            continue
        got = 0
        for band in BANDS:
            if time.time() >= t_end:
                break
            time.sleep(a.gap_s)                               # the receiver refuses an IMMEDIATE reconnection (badp=5)
            res = wf_dwell(host, port, station, band, a.dwell_min, a.out)
            if res == "busy":
                time.sleep(30)
                res = wf_dwell(host, port, station, band, a.dwell_min, a.out)
            if res == "ok":
                got += 1
            elif res == "fail":
                time.sleep(20)
        if got == 0:
            refused_cycles += 1
            log(f"cycle {cycle}: NO dwell landed ({refused_cycles} consecutive)")
            if refused_cycles >= 3:
                if alt:
                    log(f"ALERT: swapping {station} {host}:{port} → alternate {alt[2]} {alt[0]}:{alt[1]}")
                    host, port, station = alt
                    alt = None
                else:
                    log(f"ALERT: {station} refused {refused_cycles} consecutive cycles (no alternate given)")
            time.sleep(max(0, 60 - (time.time() - t_cycle)))
        else:
            refused_cycles = 0
        if cycle % 4 == 0:
            log(f"cycle {cycle}: free {free_gb(a.out):.1f} GB, wf files {len(glob.glob(os.path.join(a.out, '*.wf.npy')))}, "
                f"iq files {len(glob.glob(os.path.join(a.out, '*iq*.wav')))}")
    log("record: done")


def check(a):
    """Smoke check 1 (pre-reg §6): each IQ capture's S-meter noise anchor (min 1 s RSSI over 3 kHz → dBm/Hz) vs the
    nearest-in-time waterfall floor on the same band from qrm_occupancy's frames.csv; prints the deltas."""
    import csv
    fr = collections.defaultdict(list)
    for r in csv.DictReader(open(os.path.join(a.score, "frames.csv"))):
        fr[(r["site"], int(r["band_khz"]))].append((dt.datetime.fromisoformat(r["utc"]).timestamp(), float(r["floor_dbm_hz"])))
    band_of = {b[0]: int(b[1]) for b in BANDS}
    print("| site | band | IQ centre | IQ start UTC | S-meter min dBm → dBm/Hz | wf floor dBm/Hz (Δt min) | Δ dB |\n|---|---|---|---|---|---|---|")
    for p in sorted(glob.glob(os.path.join(a.out, "*iq*.json"))):
        s = json.load(open(p))
        if s.get("rssi_min_dbm") is None:
            print(f"| {s['station']} | {s['band']} | {s['centre_khz']} | {s['start_utc'][11:19]} | none | | |"); continue
        n_hz = s["rssi_min_dbm"] - 10 * np.log10(3000)
        t = dt.datetime.fromisoformat(s["start_utc"]).timestamp()
        v = fr.get((s["station"], band_of[s["band"]]), [])
        if not v:
            print(f"| {s['station']} | {s['band']} | {s['centre_khz']} | {s['start_utc'][11:19]} | {n_hz:.1f} | no waterfall | |"); continue
        # median floor of the nearest 60 frames
        v.sort(key=lambda x: abs(x[0] - t)); near = v[:60]
        f = float(np.median([x[1] for x in near])); dtm = (near[0][0] - t) / 60
        print(f"| {s['station']} | {s['band']} | {s['centre_khz']} | {s['start_utc'][11:19]} | {s['rssi_min_dbm']:.1f} → {n_hz:.1f} | {f:.1f} ({dtm:+.0f}) | {n_hz - f:+.1f} |")
    # noise SHAPE per site x band (frames.csv noise_shape_db = per-frame median - p25 of the bins): 3.8-4.0 dB = single-FFT
    # exponential bins (valid); ~3.0 dB = a processed waterfall (the kiwirecorder interp-13 CIC-compensation defect on some
    # KiwiSDR 2 units, 2026-09-21) — the gate statistic, quoted in the heartbeat by name
    shp = collections.defaultdict(list)
    for r in csv.DictReader(open(os.path.join(a.score, "frames.csv"))):
        shp[(r["site"], int(r["band_khz"]))].append(float(r["noise_shape_db"]))
    print("\n| site | band kHz | frames | noise shape median−p25 dB (3.8–4.0 valid; ≤ 3.2 = processed waterfall ⇒ escalate) |\n|---|---|---|---|")
    for (site, band), v in sorted(shp.items()):
        print(f"| {site} | {band} | {len(v)} | {np.median(v):.1f} |")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check"); c.add_argument("--out", required=True); c.add_argument("--score", required=True)
    r = sub.add_parser("record")
    r.add_argument("--site", choices=sorted(SITES)); r.add_argument("--host"); r.add_argument("--port", type=int, default=8073)
    r.add_argument("--station", default="kiwi"); r.add_argument("--out", required=True)
    r.add_argument("--hours", type=float, default=168); r.add_argument("--dwell-min", type=float, default=5)
    r.add_argument("--iq-secs", type=int, default=120); r.add_argument("--no-iq", action="store_true")
    r.add_argument("--iq-every", type=int, default=4, help="every Nth cycle is the IQ round (4 ⇒ hourly at 15-min cycles)")
    r.add_argument("--gap-s", type=float, default=10, help="pause between consecutive connections to one receiver")
    r.add_argument("--min-free-gb", type=float, default=10); r.add_argument("--alternate", help="site key or host[:port]")
    a = ap.parse_args()
    if a.cmd == "record":
        if not a.site and not a.host:
            ap.error("--site or --host")
        record(a)
    elif a.cmd == "check":
        check(a)


if __name__ == "__main__":
    main()
