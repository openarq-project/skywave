# Transport design: sockets and the virtual clock

## 1. Overview

skywave's channel simulator was originally built around a real ALSA
loopback audio cable: two stations write and read 48 kHz PCM through
virtual sound cards, and the simulator inserts fading, noise, filtering,
and delay in between. That works, and it stays available as a transport,
but it forces every test session to run in real time: an hour of channel
time costs an hour of wall-clock time, and every session needs its own
sound-card resources, which limits how many sessions can run at once on
one machine.

This document describes two building blocks that remove that constraint
without changing what is being measured:

- a framed transport over Unix-domain sockets, a drop-in replacement for
  the ALSA cable that carries the same audio blocks and adds an in-band
  push-to-talk (PTT) signal, and
- a block-lockstep virtual clock, which runs the simulator and both
  stations as a discrete-event simulation stepping one audio block at a
  time, as fast as the CPU allows, rather than pacing that step to a
  wall-clock timer.

Together they let a session run compute-bound instead of wall-clock-bound,
using the same waveform, noise, and fading arithmetic as the real-time
transport. The channel model itself (fading, noise, filtering) is
documented separately; this document is scoped to how audio and time move
between stations.

## 2. Why this works: sample rate is a ratio, not a speed

Every physical quantity the simulator models is defined per sample, not
per wall-clock second: bandwidth is cycles per N samples, signal-to-noise
ratio is a per-sample noise power, Doppler spread is tap decorrelation
measured over a sample count, and frequency offset, clock drift,
filtering, and link delay are all sample-indexed already in the channel
model's implementation. Computing 48000 samples in 50 milliseconds of wall
time produces the exact same second of simulated channel as computing them
in one second of wall time: bit-identical waveform, noise, and fade
realization either way.

Wall time enters the simulator in exactly three places, and the design
replaces all three:

1. Audio pacing. A real loopback cable's kernel timer delivers samples at
   48 kHz real time, no faster, no slower. (This is also why naively
   accelerating the system clock underneath the real-time rig does not
   work: the pacing lives in the kernel's audio subsystem, invisible to
   any userspace clock trick.)
2. OS clock reads inside the station under test, wherever its code asks
   the operating system what time it is right now.
3. Wall-latency signaling for out-of-band control, such as relaying a
   push-to-talk edge as a text line over a side channel to the simulator.

## 3. The socket transport

### 3.1 Wire format

The socket transport carries the same audio blocks as the ALSA cable,
each frame preceded by a four-byte, little-endian length prefix, over one
Unix-domain socket per station:

```
sim -> station:  { seq: u64, virtual_now_ms: u64, n: u16, samples: n*channels i16 }
station -> sim:  { seq: u64, ptt: u8,             n: u16, samples: n*channels i16 }
```

The cable stays 48 kHz, 16-bit signed PCM, at whatever channel count the
session is configured for, matching the ALSA cable's format exactly. The
socket transport is a substitution for the physical and kernel audio path,
not a simplification of it: dropping to a lower sample rate or fewer
channels to save CPU is deliberately out of scope, because that would
change what is being measured, not just how the audio is carried.

The `ptt` field carries the sending station's push-to-talk state in-band,
one value per block. A reserved value (255) means "not provided," used
during a transitional mode where PTT keeps arriving on the older side
channel while audio moves over sockets; ordinarily the header field is
authoritative on its own.

### 3.2 Why sockets

A Unix-domain socket per station means no shared kernel audio devices, no
fixed port numbers, and no global OS resources to clean up between runs.
Each session gets its own socket directory and its own set of ports, so
many sessions can run in parallel on the same machine without colliding
with each other, which a shared set of ALSA loopback cards cannot offer
past a handful of instances.

## 4. Block lockstep and the virtual clock

### 4.1 The tick

Time is a shared sample counter owned by the simulator, which acts as the
clock master. Both stations advance one audio block per tick, in
lockstep, as fast as the CPU allows. The simulator's existing block size,
1024 frames (21.33 ms of signal time at 48 kHz), is also the event
granularity of this discrete-event simulation; the simulator was already
block-granular before virtual time was introduced, so the change is
deleting the mandatory wall-clock wait between blocks, not restructuring
how blocks are processed.

Per tick, the simulator:

1. Sends each station its receive block, framed with a sequence number
   and the current virtual time.
2. Waits for each station to feed the block through its receive path, run
   its own logic with that virtual time as the current time, and reply
   with its transmit block (silence if it isn't keyed) and its current
   PTT state.
3. Barriers on both replies before advancing. Neither station is allowed
   to compute ahead of the other.
4. Applies the channel's transform chain unchanged: gain, clipping,
   fading, link delay, half-duplex/PTT gating, filtering, noise, and
   whatever other channel effects are configured (see the channel model
   document for those).
5. Increments the block counter and loops immediately, with no sleep.

### 4.2 The barrier

The barrier is the property a real audio cable provided for free: a
station cannot get ahead of its peer, because both are paced by the same
kernel clock. Removing the wall clock means that guarantee has to be
rebuilt explicitly, so the simulator waits for both stations' replies
before it advances the shared counter. Without it, a faster station could
race ahead of a slower one, and the simulated channel would no longer
correspond to a single consistent slice of time on both ends.

### 4.3 In-band PTT

Carrying push-to-talk state in the block header, rather than relaying it
as a separately-timed control message, makes key edges sample-exact and
deterministic, and removes a whole class of relay-latency bugs that a
wall-clock side channel is prone to: since the PTT bit rides in the same
frame as the audio it describes, there is no window where the two can
arrive out of order or be delayed relative to each other.

### 4.4 Injecting virtual time into a station under test

For a station to participate, its own clock reads have to be redirected
to the shared virtual time instead of the OS clock. In practice this only
works cleanly if the station's protocol and session logic already treat
"the current time" as a value handed to them by the caller, rather than a
value they fetch from the operating system themselves. A design where the
protocol state machine is a pure function of its inputs, including time,
needs no changes at all to run on virtual time; only the code path that
feeds it time needs a clock-source swap.

Virtual time is computed simply as an epoch plus the number of samples
consumed, converted to milliseconds at the fixed sample rate. Because a
tick corresponds to one full audio block, and a station's own scheduling
may want a finer polling interval than that, a tick can be subdivided
into several sub-ticks with interpolated virtual time in between, so
internal polling behavior keeps its usual cadence relative to virtual
time even though it is being driven by block-sized deliveries.

One example of a
station built to speak this transport: a transport thread reads one
framed receive block per iteration, feeds it into the same processing
path an ALSA-backed station would use, and replies with the produced
transmit block and current PTT state. Any implementation willing to
accept an injected clock and speak the framed socket protocol can
participate the same way; nothing about the transport is specific to one
implementation.

Because virtualizing a station's clock requires cooperation from that
station's own code, this transport only works between stations that have
been built or adapted to accept an injected clock. An off-the-shelf
implementation that only knows how to read the system clock, and was
never adapted, can still be exercised, but only over the real-time
transport; it cannot take part in a lockstep virtual-time session.

## 5. The fidelity knob: pipeline latency

A real audio pipeline (sound-card buffers, the kernel audio subsystem,
capture and playback daemons) carries a measurable amount of latency in
every turnaround, typically on the order of 100 or more milliseconds
round trip, and a protocol's turnaround-related timing constants (minimum
post-unkey delays, half-duplex send gates, retry budgets) get tuned
against that real latency. The socket transport has essentially none of
that latency inherent to it, so left alone it would make every turnaround
look faster than it really is, and would silently detune anything
measured against turnaround timing.

To keep results faithful to real timing, the simulator inserts a fixed
per-direction delay, sized in whole blocks, calibrated by measuring the
real pipeline once: send an impulse from one station's transmit path and
timestamp its arrival at the other station's receive path, in both
directions, on real hardware. In one such measurement the two directions
came out close together, roughly 144 ms and 145 ms, about a millisecond
apart, consistently detected across repeated probes. That measured figure
becomes the delay the simulator inserts on the virtual transport.

Getting this figure wrong diverges every turnaround-economics measurement
this transport produces, so it is the first thing to check if a session
run under the virtual transport disagrees with the same session on real
hardware. As a validation, comparing turnaround counts and turnaround
durations (measured from a protocol's own internal timers, not wall-clock
log timestamps) between a calibrated virtual-transport run and a
real-hardware run on a matching session showed close agreement:
turnaround counts matched exactly, and turnaround duration medians
differed by under one percent, comfortably inside the tolerance used to
accept the calibration.

## 6. What's preserved, what diverges

Preserved bit-for-bit relative to the real-time transport: all channel
DSP, signal-to-noise ratio, fade realizations, filtering and offset
effects, frame airtimes, and a protocol's own timer semantics (a
3-second timeout is still 144000 samples, regardless of how fast the
block loop runs in wall time).

What diverges, and why this transport is treated as an exploration tool
rather than a final-word one:

1. CPU contention vanishes. Each station gets effectively unlimited
   compute per block, an idealization of infinitely fast CPU. That makes
   results host-independent and portable across machines, unlike the
   real-time transport, where a slow host can measurably shift results
   (one open modem implementation's noise floor was observed to shift by
   roughly two signal-to-noise steps purely from a change of host
   machine). The trade-off is that CPU-starvation failure modes become
   invisible under lockstep; those stay the province of the real-time
   transport.
2. Scheduling races collapse. On a clean channel with no added noise,
   throughput measured across identical seeds on real hardware showed a
   spread of roughly 80 to 91 bytes per second, essentially pure
   operating-system scheduling noise. Under lockstep that spread mostly
   disappears, down to a few tenths of a percent, because a run becomes a
   near-pure function of the binary, seeds, and configuration rather than
   of scheduling luck. That makes it a sharper tool for isolated A/B
   comparisons, at the cost of a less realistic spread; use it with that
   trade-off in mind.
3. Latency is modeled, not inherited, as covered in Section 5.

## 7. Determinism and repeatability

Because the virtual clock is derived purely from a sample count, two runs
with the same binary, seeds, and configuration produce byte-identical
session records when the entire choreography around the session, not
just the simulated audio, also runs in virtual time.

If instead the harness driving a session issues its commands measured in
wall time, layered on top of an otherwise-virtual session, a small
residual appears: which virtual tick a command lands on can vary slightly
from run to run, since it depends on how much real CPU time the harness
itself happened to take. That can shift a station's early channel-quality
estimate by a small amount from run to run, without changing any decision
the protocol makes (which mode to use, whether a threshold is crossed,
and so on stayed identical across the runs where this was checked).
Closing that last residual requires the driving harness itself to
checkpoint its choreography to virtual-time boundaries rather than to its
own wall clock; short of that, the practical effect is a small amount of
extra spread, still on the order of a fraction of a percent, rather than
any change in outcome.

## 8. Performance characteristics

The compute-bound speedup this transport buys is not uniform across
sessions. For a session that is mostly idle, silence-heavy, or so noisy
that a station's receive path never opens, wall time compresses
substantially, since there is nothing for either station to compute; one
measurement of a deliberately noise-saturated, non-completing session
showed roughly a 1.65x speedup. For a dense, busy session the speedup is
much smaller, roughly 1.0 to 1.1x in one comparison, because demodulation
is the critical-path cost and already runs close to real time on ordinary
hardware; removing the wall-clock wait between blocks does not remove
that cost.

The lever that reliably pays off is running many sessions in parallel
rather than compressing any one session. Because the socket transport
gives each session its own sockets and ports instead of sharing global
audio hardware, and because the virtual clock makes results
host-independent, many sessions can run concurrently on one ordinary
multi-core machine with results matching serial execution almost exactly
(goodput agreeing with the serial baseline to within a fraction of a
percent in one comparison), while several sessions run at once complete
in a fraction of the wall time the same number of sequential runs would
take. Combining per-session compression with that parallelism, the net
throughput gain observed at the whole-machine level was on the order of
five to thirteen times what the same machine achieves running sessions
one at a time over the real-time transport.

## 9. Known limitations and open considerations

- Sub-block PTT edge timing. A real push-to-talk edge can land at any
  instant; carrying it in a per-block header quantizes it to the block
  boundary (about 21 ms). That is coarser than continuous audio, but it
  is still finer and less jittery than a wall-latency side-channel relay,
  so it is a net improvement over the real-time transport's own PTT path.
  If a scenario turns out to be sensitive to sub-block edge timing, the
  fix is to add a sample-offset field to the header rather than shrinking
  the block size.
- Hidden timers. Only the main scheduling loop's clock is virtualized by
  design; any other timer a station's runtime uses internally needs to be
  audited for whether it shapes observable behavior. One that does needs
  to be routed through the injected clock as well; diagnostic-only timers
  (logging timestamps, throttling) are fine left on the real wall clock,
  since they do not affect what is being measured.
- Barrier robustness. If a station's process or socket dies mid-session,
  the simulator has to detect the closed connection and abort the run
  with a clear marker, rather than hang forever waiting at the barrier
  for a reply that will never come.
- Native DSP code. Pure signal-processing code, as opposed to protocol or
  session logic, is generally free of wall-clock dependence by nature,
  but that assumption is worth confirming per codec or library rather
  than taken for granted; a determinism check across repeated runs is a
  reasonable way to catch a hidden dependency if one exists.
- Scope. Virtualizing the clock requires cooperation from both stations
  under test, so this transport is inherently limited to implementations
  that accept an injected clock. An off-the-shelf implementation that
  only reads its own system clock can still be exercised over the
  real-time transport, and a cross-implementation comparison involving
  such a station should stay on the real-time transport, since only one
  side of the conversation would actually be running on virtual time.

## 10. Non-goals

- Not a replacement for real-hardware validation (Section 6). Results
  that drive a real decision should be confirmed with a small number of
  matching real-hardware runs before being treated as final.
- No cloud or GPU requirement. Ordinary multi-core hardware covers the
  common case; because determinism makes results portable across
  machines, scaling out to more hardware is a purely additive option, not
  a necessity, if more throughput is ever wanted.
- No simplification of the audio path. The socket transport intentionally
  keeps full sample rate, full channel count, and the same resampling and
  filtering path as the real-time transport; none of that gets dropped to
  save CPU, because doing so would change what is being measured rather
  than just its clock source.

## 11. See also

The channel model itself, fading, noise, filtering, and the rest of the
transform chain applied each tick, is documented separately; this
document covers only how audio and time move between stations.
