# VectorAdapter contract

The Device-Under-Test contract for PHY **mode** characterization. Companion to
`MODEM-ADAPTER-CONTRACT.md`, not a replacement for it.

## Why a second adapter family

`ModemAdapter` is a **link-level** contract — `start_stations` → `wait_ready` →
`link_connect` → `transfer` → `teardown_stations`. It measures a modem as a
system: ARQ, turnaround, CSMA, goodput over a real link.

That is the wrong instrument for characterizing modes. An ARQ layer's ACKs ride
the data mode, so at low SNR the control path fails at roughly the same SNR the
data path does and the measured cliff is a composite of two failures with no way
to attribute it. Retransmission integrates over channel draws, making the result
a function of the retry policy rather than the mode. CSMA and turnaround airtime
get charged to the mode's goodput. A transfer deadline turns "slow" into "failed."
None of those are properties of a mode.

Mode characterization is **one-way**: per-mode frame-decode probability vs SNR,
one frame per trial, zero retries. Goodput, floors and ladder design all derive
from that curve analytically:

    goodput_bps = payload_bytes * 8 * (1 - FER) / air_s

So this is a **frame codec** contract: no stations, no connection, no PTT, no
wall-clock timing. The two families share exactly one thing — `watterson.py` —
and nothing else.

A useful side effect: with no retransmission there is no diversity to be
optimistic about, so independent block fading per frame is not an approximation
here, it is the correct model. The caveat that qualifies ARQ-level fading results
does not apply.

## The contract is files, not an API

An adapter is any program that can write a sample vector plus a JSON sidecar and
read them back. That keeps adapters language-agnostic — no FFI, no in-process
interface to satisfy:

| Adapter | Implementation | Rate |
|---|---|---|
| `vector_modem73` | wraps a C++ binary (`modevector`) | 48 kHz |
| `vector_armstrong` | wraps `cargo --example vector` (Rust) | 8 kHz |

**Vector**: raw `float32`, mono, little-endian, nominally ±1.0. Sample rate is
declared in the sidecar, never assumed — the two shipped adapters differ by 6×.
An i16-domain modem normalizes by 32768 on write and back on read, which makes a
no-channel round trip bit-exact.

**Sidecar**: flat JSON. Required fields:

| Field | Meaning |
|---|---|
| `label` | canonical mode name, CSV-safe (no commas) |
| `sample_rate` | Hz |
| `payload_bytes` | bytes delivered per successfully decoded frame |
| `frames` | frame count in the vector |
| `seed` | payload seed; the decoder regenerates expected bytes |
| `frame_offsets` | sample index of each frame start, ascending |
| `frame_lengths` | sample count of each frame |

Recommended and consumed when present: `mode_id`, `bandwidth_hz`, `air_s`,
`gap_samples`, `flush_samples`, `lead_samples`, `rms_dbfs`, `peak_dbfs`,
`papr_db`, `norm`.

`validate_sidecar()` is called by the sweep on **every** encode, so a broken
adapter fails on its first cell rather than producing a plausible-looking
campaign. It rejects missing fields, count mismatches, non-ascending or
overlapping frames, and a last frame that runs past the end of the vector.

## Hooks

```python
class MyVectorAdapter(VectorAdapter):
    name = "mymodem"

    def list_modes(self):     ...   # [{label, payload_bytes, sample_rate, air_s, ...}]
    def encode(self, label, frames, seed, outdir, **kw):  ...  # -> (vector, sidecar)
    def decode(self, vector, sidecar, cold=False):        ...  # -> outcome dict
```

`list_modes()` should **measure** `air_s` and the level stats from real encoder
output, not derive them from a formula. A formula cannot see per-mode clipping or
TX filtering, and the level figures are what decide the power-normalization
policy for a cross-modem comparison. Measured example: modem73's OFDM sits at
−9.1 dBFS RMS with 9.1 dB PAPR while its MFSK is −5.0/3.0, so equal-average-power
and equal-PEP comparisons of the same pair differ by ~10 dB.

`decode()` must return at least `frames`, `decoded`, `false_decode`. Any other
key is carried through to the CSV's `extra_json` column, so modem-specific
counters survive without a schema change.

- **`decoded`** counts frames whose delivered payload matched its expected bytes
  exactly. Attribution is content-addressed because a decoder does not say which
  frame it decoded: match the delivered bytes against every expected payload.
- **`false_decode`** counts deliveries that passed the modem's own CRC but
  matched *no* expected frame. This is the metric a link-level test can never
  see, and it is what catches a blind mode-detection ladder mislabelling a frame.
- **`cold=True`** means each frame gets a receiver with no memory of the previous
  one. Implement it by **constructing a fresh decoder per frame**, not by calling
  a reset method — reset frequently leaves last-good-mode state behind, which
  lets a warm decoder ride a previous frame's mode through a damaged header and
  inflates the decode rate exactly at the knee being measured.

## Payload generation is part of the contract

Payloads are deterministic in `(seed, frame_idx)` so the decoder regenerates the
expected bytes instead of the sidecar carrying them. An adapter in another
language must reproduce `skywave.vector_adapter.gen_payload` **exactly**:

- bytes 0..4 = frame index, big-endian (when `payload_bytes >= 4`). In the clear
  deliberately: a frame that passes CRC but lands on the wrong index is then
  diagnosable rather than just "mismatch".
- remaining bytes = xorshift32, seeded `x = seed ^ (0x9E3779B9 * (frame_idx+1))`,
  `x = 0x1234567` if that is zero, then `x ^= x<<13; x ^= x>>17; x ^= x<<5` per
  byte, taking the low 8 bits.

A divergence here presents as a total decode failure, which is a loud and
therefore safe failure mode.

## The selftest is a gate, not a courtesy

Every adapter inherits `selftest()`, and it must pass before the adapter is
trusted with a campaign: a clean round trip decodes **every** frame, and a
deep-noise round trip decodes **none**. The negative leg is what makes the
positive leg mean anything — a harness that only ever reports 0% is
indistinguishable from a broken harness.

Default fail level is −30 dB. Do not raise it casually: −6 dB in 2500 Hz is
+1 dB inside a 500 Hz mode's own band, which such a mode decodes perfectly. The
noise-bandwidth confound bites the selftest first — it did exactly that during
development, and the harness was right while the test was wrong.

## SNR convention

Shared by every adapter, or nothing is comparable:

    SNR_B = S / (N0 * B),  N0 = 2*sigma^2/fs
      =>   sigma^2 = S*fs / (2*B*10^(SNR/10))

`S` is the mean square over the sidecar's frame regions of the **clean** vector,
measured **before** fading. That ordering is load-bearing: measuring `S` post-fade
makes the noise level a function of the particular fade draw, so the same nominal
SNR would mean a different thing in every cell. Gaps and lead-in silence are
excluded, or `S` would depend on how much silence the encoder inserted.

Occupied bandwidth varies enormously across modes (250 Hz to 2400 Hz among
modem73's alone). On a fixed reference axis the narrow modes collect a real
advantage — correct for "what do I pick at this SNR?", wrong for "which waveform
is more efficient". Publish the operator axis as primary and carry Eb/N0
alongside. `--bw 0` uses each mode's own bandwidth instead of a fixed reference.

## Independent block fading

Each frame must see an independent fade draw or N frames do not give N
independent trials and the Wilson interval on FER is a lie.

The obvious implementation — a fresh `WattersonChannel` per frame — is **wrong**.
`hf_gain` is `1/sqrt(var(p1)+var(p2))` computed from the realization's own
samples, so a short low-Doppler realization normalizes that draw back to unit
average power: a frame that landed in a deep fade gets scaled back up, deleting
the very event the sweep exists to measure.

`vector_channel` therefore builds **one long realization** and slices it at
strides of `3/doppler` seconds. For a Gaussian Doppler spectrum the envelope
autocorrelation is `exp(-pi^2 d^2 tau^2 / 2)`, so a 3/d stride leaves correlation
~e⁻⁴⁴. Frame *i* is faded at virtual fade-time `i*stride` with the Hilbert
history zeroed — exact, because the signal preceding every frame really is
silence.

Verified on 200 frames of a 1.64 s mode: mean power within 0.15 dB of the clean
vector (the `hf_gain` doctrine survives the slicing) and lag-1 correlation ≤0.066
(draws are independent). The spread narrows from `good` to `poor` because at 1 Hz
a short frame spans several fades and averages over them, while at 0.1 Hz the
whole frame rides one Rayleigh draw — **CCIR good is the hardest class for short
frames**, not a gentle warm-up cell.

## Running a campaign

```sh
python3 -m skywave.vector_sweep --adapter armstrong --out out/arm.csv \
    --presets off,good,moderate,poor --frames 150 \
    --snr-lo -12 --snr-hi 30 --snr-step 2 --jobs 14 --bw 3000 --allow-long
```

The driver encodes clean vectors once per (mode, batch) and reuses them across
every SNR point and preset; batches by audio duration rather than frame count so
peak scratch is bounded regardless of mode length; is resumable from its own CSV;
cleans scratch on SIGTERM and reaps stale dirs at startup; records
`host`/`arch`/`adapter` per row; and refuses to start if its own runtime estimate
exceeds 30 minutes without `--allow-long` (owner rule: long jobs do not run on an
interactive box).

Selecting which modes to sweep deserves care. A cheap screen on the easy channel
prunes exactly the candidates that exist for the hard one — modem73's 12 ROBUST
modes scored **0 of 12** on the AWGN Pareto hull, so "deep-sweep the hull" would
have deleted the whole fading-designed family before fading was measured. The
selection rule is:

    selection = screen_hull
              ∪ every candidate whose design intent is a regime the screen
                did not exercise
              − candidates the screen proves cannot work at all

## Relationship to armstrong's in-tree bench

armstrong keeps `crates/phy/tests/codec2_mode_floor_bench.rs` as a **parallel**
implementation, deliberately. It is self-contained Rust with no Python
dependency, so it keeps working where skywave cannot run at all — GitHub runners,
bare containers, any box without numpy/scipy — and it stays the CI-runnable
instrument.

The two measure the same quantity by different routes, which is a comparability
hazard if left implicit. The in-tree bench reports SNR3000 via
`phy::channel::Watterson`; this path applies `vector_channel` at whatever `--bw`
the campaign states, and every sweep row records `bw_hz`. Never quote a floor
from one against a floor from the other without saying which produced it.
