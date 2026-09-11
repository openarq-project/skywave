#!/usr/bin/env python3
"""kiwi_wf_record.py — record a KiwiSDR waterfall (1024 dBm bins per frame) to a compact file.

The occupancy tier of the QRM campaign: one frame per second over a whole digital sub-band,
receiver-calibrated dBm per bin (kiwirecorder's convention: byte − 255 + wf_cal), ~3.7 MB/h.

    kiwi_wf_record.py -s host [-p 8073] -f 7097 -z 10 --minutes 60 --out DIR [--station tag]

Writes DIR/<tag>_<band>_z<zoom>_<UTC>.wf.npy (uint8 frames × 1024) + .t.npy (per-frame epoch s)
+ .json sidecar (host, centre kHz, zoom, span/rbw, start UTC, and the receiver's /status page:
wf_cal, sm_cal, antenna, snr, users). Zoom 10 = 29.3 kHz span, 28.6 Hz/bin; zoom 9 = 58.6 kHz,
57 Hz/bin. Exit 3 = receiver busy / no frames (the campaign runner retries once).
"""
import argparse, datetime as dt, json, os, sys, time, urllib.request
import numpy as np

sys.path.insert(0, os.path.expanduser("~/tools/kiwiclient"))
import kiwirecorder as kr                                     # noqa: E402


class WfDump:
    frames, times = [], []


def _dump(self, seq, samples):                                # replaces the PNG/log handler
    WfDump.frames.append(np.frombuffer(bytes(samples), dtype=np.uint8).copy())
    WfDump.times.append(time.time())


def kiwi_status(host, port, timeout=10):
    """The receiver's /status page as a dict (wf_cal, sm_cal, antenna, snr, users, ...)."""
    try:
        txt = urllib.request.urlopen(f"http://{host}:{port}/status", timeout=timeout).read().decode("utf-8", "replace")
    except Exception as e:                                    # noqa: BLE001
        return {"error": str(e)[:120]}
    d = {}
    for line in txt.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip()
    keep = ("name", "sw_version", "antenna", "snr", "users", "users_max", "wf_cal", "sm_cal", "gps", "grid", "loc",
            "ant_connected", "adc_ov", "freq_offset")
    return {k: d[k] for k in keep if k in d}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-s", "--server-host", required=True); ap.add_argument("-p", "--server-port", type=int, default=8073)
    ap.add_argument("-f", "--freq", type=float, required=True, help="centre, kHz")
    ap.add_argument("-z", "--zoom", type=int, default=10); ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--out", required=True); ap.add_argument("--station", default="kiwi")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    status = kiwi_status(a.server_host, a.server_port)
    wf_cal = int(status.get("wf_cal", -13))
    t0 = dt.datetime.now(dt.timezone.utc)
    tag = f"{a.station}_{int(a.freq)}_z{a.zoom}_{t0.strftime('%Y%m%dT%H%M%S')}"
    argv = ["kiwirecorder.py", "-s", a.server_host, "-p", str(a.server_port), "--wf", "-f", str(a.freq), "-z", str(a.zoom),
            "--tlimit", str(int(a.minutes * 60)), "--speed", "1", "--station", a.station, "--log=warn",
            "--busy-retries", "1", "--busy-timeout", "5"]
    sys.argv = argv
    kr.KiwiWaterfallRecorder._process_waterfall_samples = _dump   # method patch (the class name is used in super())
    try:
        kr.main()
    except SystemExit:
        pass
    if not WfDump.frames:
        print(f"{tag}: no frames (busy or unreachable) users={status.get('users')}/{status.get('users_max')}", file=sys.stderr)
        sys.exit(3)
    fr = np.stack(WfDump.frames); ts = np.array(WfDump.times)
    span_khz = 30000.0 / (2 ** a.zoom)
    np.save(os.path.join(a.out, tag + ".wf.npy"), fr); np.save(os.path.join(a.out, tag + ".t.npy"), ts)
    json.dump({"host": f"{a.server_host}:{a.server_port}", "station": a.station, "centre_khz": a.freq, "zoom": a.zoom,
               "span_khz": span_khz, "rbw_hz": 1000 * span_khz / 1024, "start_utc": t0.isoformat(),
               "frames": int(fr.shape[0]), "seconds": float(ts[-1] - ts[0]) if len(ts) > 1 else 0.0,
               "dbm_offset": -255, "wf_cal": wf_cal, "receiver": status,
               "note": "dBm/bin = byte + dbm_offset + wf_cal; first two bins are the DC notch"},
              open(os.path.join(a.out, tag + ".json"), "w"), indent=1)
    print(f"{tag}: {fr.shape[0]} frames over {ts[-1] - ts[0]:.0f} s, {fr.shape[1]} bins, rbw {1000 * span_khz / 1024:.1f} Hz, wf_cal {wf_cal}")


if __name__ == "__main__":
    main()
