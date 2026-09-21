#!/usr/bin/env python3
"""qrm_site_gate.py — the DAY-0 instrument-validity gate for a public-KiwiSDR occupancy site (2026-09-21).

    qrm_site_gate.py SITE_LABEL HOST PORT [OUT_DIR]      (run from the skywave tools dir, next to kiwi_wf_record.py)

One-minute waterfall dwell on each campaign band (7097 z10, 10138 z10, 14095 z9) and on the 27.5 MHz dead band, then the
S-meter noise anchor (min 1 s RSSI over 17 s in a 3 kHz USB passband) next to each; qrm_occupancy.file_floor per file.
PASS = (1) single-FFT exponential noise bins on EVERY row: shape (median - p25) 3.6-4.2 dB and time-sd 5.2-6.2 dB — a
compressed shape (3.0 / 4.0) means the receiver is delivering a processed waterfall (kiwirecorder's default interp 13 =
drop + CIC compensation does this on some KiwiSDR 2 units; kiwi_wf_record.py now requests interp 3); (2) the waterfall
floor TRACKS the antenna noise: the per-row delta (S-meter - waterfall) is constant across the four rows to +-2 dB,
including the dead band, so there is no path floor; (3) record that constant delta — it is the receiver's
S-meter-vs-waterfall calibration offset (0 on KiwiSDR 1 units, -3...-8 dB on KiwiSDR 2 units with sm_cal -16) and does
not affect INR statistics, which are relative to the waterfall's own floor. Site C's pilot data failed (1) and (2).
"""
import sys, os, glob, json, time, subprocess, numpy as np
sys.path.insert(0, os.path.expanduser('~/tools/skywave/tools'))
from ft8_campaign import s_meter_dbm
from kiwi_wf_record import kiwi_status
import qrm_occupancy as occ
site, host, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
out = os.path.join(sys.argv[4] if len(sys.argv) > 4 else 'site-gate', site); os.makedirs(out, exist_ok=True)
WF = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'kiwi_wf_record.py')   # defaults to --interp 3
print(site, 'status', {k: v for k, v in kiwi_status(host, port).items() if k in ('wf_cal', 'sm_cal', 'sw_version', 'users', 'snr', 'adc_ov')}, flush=True)
rows = []
for centre, zoom in [(7097.0, 10), (10138.0, 10), (14095.0, 9), (27500.0, 10)]:
    time.sleep(10)
    r = subprocess.run([sys.executable, WF, '-s', host, '-p', str(port), '-f', str(centre), '-z', str(zoom), '--minutes', '1',
                        '--out', out, '--station', site], capture_output=True, text=True, timeout=200)
    fs = sorted(glob.glob(os.path.join(out, f'{site}_{int(centre)}_z{zoom}_*.wf.npy')))
    if not fs:
        print(site, centre, zoom, 'NO WF rc', r.returncode, (r.stderr or r.stdout)[-200:], flush=True); continue
    side, dbm, ts, f_khz, rbw = occ.load(fs[-1])
    ff = occ.file_floor(dbm); fp, shape = occ.frame_floor(dbm)
    lin = 10 ** (dbm / 10); quiet = lin[:, 2:]
    sd_db = float(np.nanmedian(np.nanstd(dbm[:, 2:], axis=0)))
    rows.append((centre, zoom, len(ts), ff, float(np.nanmedian(fp)), float(np.nanmedian(shape)), sd_db))
    print(f'{site} wf {centre} z{zoom}: {len(ts)} frames, file floor {ff:.1f} dBm/Hz, frame-p25 floor {np.nanmedian(fp):.1f}, shape med-p25 {np.nanmedian(shape):.1f} dB, time-sd {sd_db:.2f} dB', flush=True)
sm = {}
for centre in (7101.9, 10146.4, 14097.0, 27500.0):
    time.sleep(10)
    v = s_meter_dbm(host, port, centre, secs=17)
    sm[centre] = v
    print(f'{site} S-meter {centre}: {v} -> min {v[1] - 10*np.log10(3000):.1f} dBm/Hz' if v else f'{site} S-meter {centre}: none', flush=True)
print('\n| site | centre | zoom | wf file floor dBm/Hz | S-meter min dBm/Hz | Δ (S − wf) dB | shape dB | time-sd dB |')
for centre, zoom, n, ff, fp, shape, sd in rows:
    key = {7097.0: 7101.9, 10138.0: 10146.4, 14095.0: 14097.0, 27500.0: 27500.0}[centre]
    v = sm.get(key)
    smh = v[1] - 10 * np.log10(3000) if v else float('nan')
    print(f'| {site} | {centre:.0f} | {zoom} | {ff:.1f} | {smh:.1f} | {smh - ff:+.1f} | {shape:.1f} | {sd:.2f} |')
