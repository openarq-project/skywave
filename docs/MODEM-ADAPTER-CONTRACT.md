# Modem-adapter contract for the skywave harness

*Reference implementation: `skywave/modem_adapter.py` (base class),
`skywave/adapters/example.py` (runnable template), `tests/test_modem_adapter.py`
(contract tests).*

This is the Device-Under-Test contract: what a modem adapter receives from the harness,
what it must do, and what it must emit. An adapter that honors it plugs into the whole
existing stack — the shared half-duplex `channel_sim`, the transports, `sweep_runner`'s
campaign loop, and the A/B drivers — by implementing four hooks. It is how another modem
project runs its modem on this bench without adopting anything else.

The contract distills what a modem adapter has to do in common. The base emits the exact
stdout line `sweep_runner` parses, so an adapter written on it is a drop-in for the
campaign loop.

## 1. Input

Invoked as `<adapter> [payload_bytes] [timeout_s]`, configured through the environment.
`AdapterConfig.from_env()` parses all of it once into a dataclass — the single documented
input surface (grow *that*, not ad-hoc `os.environ.get` calls in adapter code).

| Source | Key | Meaning | Default |
|---|---|---|---|
| argv[0] | `payload_bytes` | bytes to transfer A→B | 4096 |
| argv[1] | `timeout_s` | transfer deadline (excludes connect) | 120 |
| env | `SIGMA` | AWGN std, int16 LSBs (0 = clean); passed through to `channel_sim` | 0 |
| env | `TXGAIN` | equal-PEP drive (`results/<modem>_txgain.txt`) | 1.0 |
| env | `SEED` | RNG seed; the framework varies it per rep | 1234 |
| env | `NP_STATS` | signal-stats sidecar prefix `channel_sim` writes (feeds per-row snr3k) | "" |
| env | `SIM_HALF_DUPLEX` | 1 = HD keying in the channel | 0 |
| env | `SIM_PTT` | 1 = gate the channel on **real** host PTT (relay each modem's PTT line) | 0 |
| env | `SIM_WATTERSON` | fade preset name (`off`, `poor`, `nvis`, ...) | off |
| env | `SIM_*` | any other channel knob — passed through untouched to `channel_sim` | — |
| env | `<MODEM>_BIN` | adapter's own binary path override (e.g. `MERCURY_BIN`) | — |

## 2. Lifecycle

`ModemAdapter.run()` is the template method. `[hook]` = a method the adapter implements.

```
  preclean_patterns()  ── pkill stale procs (MUST NOT match own cmdline: self-kill trap)
  launch_channel()     ── shared channel_sim via bench_pipes (override→no-op if persistent)
  [start_stations]     ── launch the two modem instances wired to the transport
  [wait_ready]         ── poll both control endpoints until up  ── fail → NOCONN, exit 1
  [link_connect]       ── native ARQ handshake (+retries); relay PTT via on_line()
                          fail → NOCONN, exit 1
  make_payload()       ── incompressible, seed-deterministic bytes
  [transfer]           ── send A→B, read until len(payload) or deadline; pump on_line()
  emit result          ── RESULT: line  +  RESULT_JSON line
  teardown_stations() / teardown_channel()   (always, in finally)
```

## 3. Hooks

**Required** (abstract): `start_stations`, `wait_ready`, `link_connect`, `transfer`.

**Optional** (sensible defaults): `preclean_patterns` (→ `[]`), `launch_channel` (→ shared
`bench_pipes.launch_channel_sim`; override to no-op for VARA-style persistent channels),
`teardown_stations` (→ SIGTERM every launched station), `scan_telemetry` (→ pull
bitrate/SN into `self.modes`/`self.snrs`), `make_payload` (→ seeded incompressible).

Call `self.on_line(station, line)` for **every** control line read from a station: it
relays PTT to the channel (`bench_pipes.fwd_ptt`) and scans telemetry. `station` is
`"A"` or `"B"` (A = answerer, B = caller/sender, matching every adapter in the tree).

## 4. Output

**Framework-compatible line** — `sweep_runner.run_cell()` finds `RESULT`, then within 400
chars applies these regexes (do not reorder/rename the tokens casually):

```
RESULT: <got>/<total> B in <secs>s intact=<bool> goodput=<B/s> B/s | peak_bitrate=<bps>bps | SN_med=<dB>
        └RES_BYTES┘        └RES_IN┘ └─RES_INTACT─┘ └───RES_GP───┘      └──RES_PEAK──┘        └RES_SN┘
```

`AdapterResult.result_line()` produces exactly this. Classification (sweep_runner):
`got≥total and intact` → **ok**; `got>0` → **partial**; else → **fail**. A connect failure
prints the **`NOCONN`** token (via `fail_connect()`) and writes no RESULT → **fail_connect**
(sweep_runner retries it once).

**Structured forward contract** — `RESULT_JSON {…}`, schema `modem-adapter-result/1`:
`{schema, got, total, seconds, intact, goodput, peak_bitrate, sn_med}`. Downstream tooling
should prefer this; the RESULT line stays for the current parser.

**Exit code**: `0` intact delivery · `2` partial/failed transfer · `1` connect failure.

## 5. Adding your modem

1. `class MyAdapter(ModemAdapter):` with `name` and the four required hooks. Copy
   `skywave/adapters/example.py` and replace each fake hook with the real thing
   (subprocess launch, control-protocol connect, native handshake, data transfer). See
   `skywave/adapters/mercury.py` for a concrete external-modem shape (a TCP TNC protocol).
2. `if __name__ == "__main__": sys.exit(run_adapter(MyAdapter))`.
3. Register it **without editing the framework**: drop an `adapters.json` in the
   repository root (or point `$BENCH_ADAPTERS` at one) with
   `{"mymodem": {"script": "my_arq_pipe.py", "kill_pad": N, "extra_env": {...}}}`. A
   relative `script` resolves against the registry file's own directory, so you ship the
   adapter alongside its registry. Then `skywave-sweep mymodem <spec.json> <out.csv>`
   drives it; an unknown name prints the known list. `BENCH_ROOT` sets the artifact/cwd root.
4. Optionally calibrate its drive to equal PEP: `skywave-sweep --calibrate-pep <mymodem>`
   writes `results/<mymodem>_txgain.txt`, applied automatically in every cross-modem
   campaign so all modems transmit at a matched peak power. See docs/EQUAL-PEP.md.

Nothing else changes: the channel, transports, A/B drivers, and scoring are all reused.

## 6. References

- `skywave/modem_adapter.py` — base class + `AdapterConfig`/`AdapterResult`.
- `skywave/adapters/example.py` — runnable, hardware-free reference/template.
- `skywave/sweep_runner.py` — the campaign loop (input env, `RES_*` parsers, classification).
- `skywave/bench_pipes.py` — `launch_channel_sim()` + `fwd_ptt()` (the channel/PTT seam).
- `skywave/adapters/mercury.py` — a real external-modem adapter (Mercury over a TCP TNC).
