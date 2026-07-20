#!/usr/bin/env python3
"""Comparative sweep runner for the channel simulator.

Runs one modem across a list of cells (a JSON spec), driving the modem's adapter through
the shared half-duplex channel, and appends one CSV row per (cell, rep) incrementally so
partial progress survives a crash or kill. Modems share the transport (aloop cards / TNC
ports), so run one modem at a time.

The channel is held fixed across modems for a fair comparison: half-duplex + PTT, with
SNR (SIGMA) and fading (SIM_WATTERSON) swept and SEED varied per rep (independent noise
and fade realizations). An optional per-modem level calibration
(results/<modem>_txgain.txt) equalizes drive across modems; without it TXGAIN defaults to
1.0. The per-row snr3k comes from the sim's measured act_rms (NP_STATS), falling back to a
gain-scaled nominal.

Usage: sweep_runner.py <modem> <cells.json> <out.csv> [tag]
  modem: a key in the adapter registry — the built-ins (loopback, mercury) plus anything
  in <BENCH_ROOT>/adapters.json or the file named by $BENCH_ADAPTERS. A new project
  registers its modem there instead of editing this file (see MODEM-ADAPTER-CONTRACT.md).
  An unknown modem prints the list. BENCH_ROOT overrides the repository root (default:
  this file's dir).
"""
import os, sys, json, subprocess as sp, time, re, csv, math, signal
from rig_version import RIG_GEN
from results_schema import COLUMNS, RESULTS_SCHEMA, write_manifest

SWEEPDIR = os.path.dirname(os.path.abspath(__file__))
# Repository root: where per-run artifacts (logs, results, adapters.json) and the adapter
# scripts live. Derived from this file by default (no hardcoded absolute path); a
# relocated tree sets BENCH_ROOT. Previously this was a hardcoded absolute path.
BENCH_ROOT = os.path.abspath(os.environ.get("BENCH_ROOT") or SWEEPDIR)
LOGDIR  = os.path.join(SWEEPDIR, "logs")
os.makedirs(LOGDIR, exist_ok=True)


def harness_python(script):
    """Interpreter to launch an adapter under. An adapter that needs its own interpreter
    (a separate venv, say) sets $ADAPTER_PY; otherwise the plain python3."""
    return os.environ.get("ADAPTER_PY") or "python3"

# script = adapter to invoke; kill_pad = seconds added to the cell's internal
# timeout for the outer `timeout` process-kill; extra_env applied last. More modems are
# registered WITHOUT editing this file — see load_adapters() (drop an adapters.json beside
# the repository root, or point $BENCH_ADAPTERS at one). Collecting adapters is a project goal.
BUILTIN_ADAPTERS = {
    "loopback":  {"script": "example_loopback_adapter.py", "kill_pad": 30, "extra_env": {}},
    "mercury":   {"script": "mercury_adapter.py",   "kill_pad": 80, "extra_env": {}},
    "armstrong": {"script": "armstrong_adapter.py", "kill_pad": 90, "extra_env": {}},
    "ardop":     {"script": "ardop_adapter.py",     "kill_pad": 90, "extra_env": {}},
}


def load_adapters(root=None, extra_path=None, builtin=None):
    """Adapter registry: built-ins merged with an EXTERNAL registry so a project can
    register a modem WITHOUT editing this file (B2, 2026-07-19).

    Precedence low->high: BUILTIN_ADAPTERS, <root>/adapters.json, $BENCH_ADAPTERS
    (or `extra_path`). Each external entry needs a "script"; "kill_pad" (90) and
    "extra_env" ({}) default. A relative "script" is resolved against the registry
    file's own directory (so a project ships its adapter alongside its registry),
    while a bare built-in name stays relative to BENCH_ROOT at launch (cwd)."""
    root = root or BENCH_ROOT
    merged = {k: dict(v) for k, v in (builtin or BUILTIN_ADAPTERS).items()}
    for path in (os.path.join(root, "adapters.json"),
                 extra_path or os.environ.get("BENCH_ADAPTERS")):
        if not path or not os.path.exists(path):
            continue
        try:
            reg = json.load(open(path))
        except ValueError as e:
            raise SystemExit(f"sweep_runner: bad adapter registry {path}: {e}")
        base = os.path.dirname(os.path.abspath(path))
        for name, entry in reg.items():
            e = dict(entry)
            if "script" not in e:
                raise SystemExit(f"sweep_runner: adapter '{name}' in {path} missing 'script'")
            if not os.path.isabs(e["script"]):
                cand = os.path.join(base, e["script"])
                if os.path.exists(cand):
                    e["script"] = cand           # ship-alongside-registry resolution
            e.setdefault("kill_pad", 90)
            e.setdefault("extra_env", {})
            merged[name] = e
    return merged


def resolve_adapter(modem, adapters=None):
    """Look up a modem's adapter config or exit with the list of known modems."""
    adapters = adapters if adapters is not None else ADAPTERS
    if modem not in adapters:
        raise SystemExit(
            f"sweep_runner: unknown modem '{modem}'. Known: {', '.join(sorted(adapters))}. "
            f"Register it in {os.path.join(BENCH_ROOT, 'adapters.json')} or via $BENCH_ADAPTERS.")
    return adapters[modem]


ADAPTERS = load_adapters()

# Processes to sweep between cells so a wedged transport can't poison the next. CAUTION:
# these run via `pkill -9 -f` so each pattern MUST NOT be a substring of THIS driver's own
# cmdline ("python3 sweep_runner.py <modem> <spec> <csv> <tag>") or the driver self-kills.
# Match the transport processes only; an adapter cleans its own modem processes via its
# preclean_patterns() (see ModemAdapter).
KILL_PATS = ["channel_sim.py", "arecord -D plughw", "aplay -D plughw"]

RES_BYTES = re.compile(r"(\d+)\s*/\s*(\d+)\s*B")
RES_IN    = re.compile(r"in\s+([\d.]+)s")
RES_INTACT= re.compile(r"intact=(\w+)")
RES_GP    = re.compile(r"goodput=([\d.]+)")
RES_PEAK  = re.compile(r"peak_bitrate=(\d+)")
RES_SN    = re.compile(r"SN_med=(-?[\d.]+)")


def snr3k_nominal(sigma, gain=1.0):
    """Fallback label: assumes the codec2-family 8198-LSB active RMS scaled by
    the drive gain. Only exact for calibrated codec2-family rows; measured
    act_rms (below) is authoritative when available."""
    s = float(sigma)
    return round(9.0 + 20 * math.log10(8198.0 * float(gain) / s), 1) if s > 0 else 99.0


def snr3k_measured(act_rms, sigma):
    s = float(sigma)
    if s <= 0:
        return 99.0
    if act_rms <= 0:
        return None
    return round(9.0 + 20 * math.log10(act_rms / s), 1)


def modem_txgain(modem):
    """Optional equal-drive calibration: a per-modem TXGAIN from
    results/<modem>_txgain.txt so modems are compared at matched drive. A missing file
    means no calibration -> 1.0. EQUAL_GAIN=1 forces 1.0 regardless."""
    if os.environ.get("EQUAL_GAIN", "0").strip() == "1":
        return "1.0"
    path = os.path.join(SWEEPDIR, "results", f"{modem}_txgain.txt")
    if not os.path.exists(path):
        return "1.0"
    return open(path).read().strip()


def read_np_stats(prefix):
    """Signal stats from the data-heavy direction (larger active-sample count)."""
    best = {}
    for sfx in (".11", ".22"):
        try:
            d = json.load(open(prefix + sfx))
        except (OSError, ValueError):
            continue
        if d.get("n", 0) * d.get("duty", 0.0) > best.get("n", 0) * best.get("duty", 0.0):
            best = d
    return best


def between_cell_cleanup():
    for pat in KILL_PATS:
        sp.run(["pkill", "-9", "-f", pat], stderr=sp.DEVNULL, stdout=sp.DEVNULL)
    time.sleep(1.5)


def run_cell(modem, cell, rep, writer, fcsv, tag):
    cfg = resolve_adapter(modem)
    sigma = cell["sigma"]; watt = cell.get("watterson", "off")
    payload = cell.get("payload", 4096); tmo = cell.get("timeout", 120)
    env = dict(os.environ)
    env["SIM_HALF_DUPLEX"] = "1"; env["SIM_PTT"] = "1"
    # Instant T/R turnaround by default (a fair baseline). A non-zero deaf window only
    # modems with a turnaround gate survive, so it is held at 0 here; override via env
    # (SIM_TR_UNKEY_MS) for a T/R-penalty study.
    env["SIM_TR_UNKEY_MS"] = os.environ.get("SIM_TR_UNKEY_MS", "0")
    env["SIGMA"] = str(sigma)
    env["SIM_WATTERSON"] = watt
    # Optional custom fade: an explicit delay+doppler pair overrides the named preset
    # (channel_sim takes its custom path when BOTH are set), for delay-spread sweeps.
    # Backward-compatible: only applied when the cell carries the fields.
    if "fade_delay_ms" in cell:
        env["SIM_FADE_DELAY_MS"] = str(cell["fade_delay_ms"])
    if "fade_doppler_hz" in cell:
        env["SIM_FADE_DOPPLER_HZ"] = str(cell["fade_doppler_hz"])
    env["SEED"] = str(1234 + rep * 7)
    # equal-PEP drive unless the launcher pinned TXGAIN itself (campaign_pep
    # does) or EQUAL_GAIN=1 requests the historical baseline.
    if "TXGAIN" not in os.environ:
        env["TXGAIN"] = modem_txgain(modem)
    base = f"{tag}_{modem}_s{sigma}_{watt}_p{payload}_r{rep}"
    log = os.path.join(LOGDIR, base + ".log")
    # Signal stats always on: the measured act_rms feeds the per-row snr3k.
    npstats = (os.path.join(os.environ["NP_STATS_DIR"], f"{modem}_s{sigma}_r{rep}")
               if os.environ.get("NP_STATS_DIR")
               else os.path.join(LOGDIR, base + ".npstats"))
    env["NP_STATS"] = npstats
    env.update(cfg["extra_env"])
    kill = int(tmo) + cfg["kill_pad"]
    attempts = 2  # one extra try, consumed ONLY on a connect-type failure (Mercury HD race)
    for att in range(attempts):
        t0 = time.time()
        with open(log, "wb") as lf:
            p = sp.run(["timeout", str(kill), harness_python(cfg["script"]), "-u",
                        cfg["script"], str(payload), str(tmo)],
                       cwd=BENCH_ROOT, env=env, stdout=lf, stderr=sp.STDOUT)
        el = round(time.time() - t0, 1)
        txt = open(log, errors="replace").read()
        got = tot = 0; dt = el; intact = "false"; gp = 0.0; peak = 0; sn = -99.0
        mres = re.search(r"\bRESULT\b", txt)
        if mres:
            seg = txt[mres.start():mres.start() + 400]
            mb = RES_BYTES.search(seg)
            if mb: got, tot = int(mb.group(1)), int(mb.group(2))
            mi = RES_IN.search(seg);     dt = float(mi.group(1)) if mi else el
            mt = RES_INTACT.search(seg); intact = mt.group(1) if mt else "false"
            mg = RES_GP.search(seg);     gp = float(mg.group(1)) if mg else 0.0
            mp = RES_PEAK.search(seg);   peak = int(mp.group(1)) if mp else 0
            ms = RES_SN.search(seg);     sn = float(ms.group(1)) if ms else -99.0
            if got >= (tot or payload) and intact.lower() in ("true", "1"):
                status = "ok"
            elif got > 0:
                status = "partial"
            else:
                status = "fail"
        else:
            tot = payload
            status = "timeout" if p.returncode == 124 else (
                "fail_connect" if ("no CONNECT" in txt or "not listening" in txt
                                   or "not up" in txt or "NOCONN" in txt) else "fail")
        # A real partial/timeout at low SNR is a valid data point — only a bare connect
        # failure (no carrier acquired at all) is treated as a transient worth one retry.
        if status != "fail_connect" or att == attempts - 1:
            break
        print(f"    (connect-fail; retry {att + 1}/{attempts - 1})", flush=True)
        between_cell_cleanup()
    if att > 0 and status != "fail_connect":
        status += "+retry"
    stats = read_np_stats(npstats)
    act_rms = round(float(stats.get("act_rms", 0.0)), 1)
    gain = env.get("TXGAIN", "1.0")
    snr = snr3k_measured(act_rms, sigma)
    if snr is None:
        snr = snr3k_nominal(sigma, gain)
    row = {"modem": modem, "tag": tag, "sigma": sigma, "snr3k": snr,
           "act_rms": act_rms, "txgain": gain,
           "watterson": watt, "payload": payload, "rep": rep,
           "got": got, "total": tot or payload, "intact": intact,
           "goodput": round(gp, 2), "peak_bps": peak, "sn_med": sn,
           "elapsed": el, "status": status, "rc": p.returncode,
           "log": os.path.basename(log), "rig_gen": RIG_GEN}
    writer.writerow(row); fcsv.flush()
    print(f"[{modem}] s={sigma}({row['snr3k']}dB) {watt} p={payload} r{rep}: "
          f"{status:12} gp={gp:6.1f} B/s  ({el:.0f}s)", flush=True)
    return row


def main():
    modem, spec, out = sys.argv[1], sys.argv[2], sys.argv[3]
    tag = sys.argv[4] if len(sys.argv) > 4 else "sw"
    resolve_adapter(modem)          # fail fast with the known-modem list on a typo
    cells = json.load(open(spec))
    # Column order is owned by results_schema (the versioned corpus contract, B4) so a
    # rename can't silently desync the writer from downstream readers.
    cols = COLUMNS
    # size==0, not just non-existence: a caller that pre-touches/truncates `out`
    # (e.g. crossmodem_launch.sh's smoke gate does `: > "$SMOKE_CSV"`) leaves a
    # 0-byte file that os.path.exists() sees as "already there", so the header
    # never gets written and every downstream DictReader silently sees zero rows.
    new = not os.path.exists(out) or os.path.getsize(out) == 0
    fcsv = open(out, "a", newline="")
    writer = csv.DictWriter(fcsv, fieldnames=cols)
    if new:
        writer.writeheader(); fcsv.flush()
    n = sum(c.get("reps", 1) for c in cells)
    # Drop the versioned schema + provenance sidecar next to the corpus. Idempotent,
    # so a resumed run refreshes it. External consumers read_manifest()/read_corpus() it.
    write_manifest(out, schema=RESULTS_SCHEMA, modem=modem, tag=tag,
                   spec=os.path.basename(spec), cells=len(cells), runs=n,
                   bench_root=BENCH_ROOT, rig_gen=RIG_GEN)
    print(f"=== {modem}: {len(cells)} cells, {n} runs -> {out} (tag={tag}) ===", flush=True)
    between_cell_cleanup()
    i = 0
    for c in cells:
        for rep in range(c.get("reps", 1)):
            i += 1
            print(f"--- run {i}/{n} ---", flush=True)
            try:
                run_cell(modem, c, rep, writer, fcsv, tag)
            except Exception as e:
                print(f"[{modem}] cell EXCEPTION: {e}", flush=True)
            between_cell_cleanup()
    fcsv.close()
    print(f"=== {modem} DONE ===", flush=True)


if __name__ == "__main__":
    sys.exit(main())
