"""FM port-profile DSP for skywave's FM port (the FM port design).

The two load-bearing FM audio port profiles:

  micspk    mic/speaker path: TX pre-emphasis (+6 dB/oct) + 300-3000 Hz voice
            bandpass (+ optional CTCSS tone summed AFTER the voice filter — the
            sub-300 Hz tone shares the modulator path); RX voice bandpass +
            de-emphasis (-6 dB/oct) + gated squelch (SquelchGate below).
  data9600  G3RUH flat path (varactor-direct TX, discriminator-tap RX): identity,
            no squelch. Kept as explicit classes so the harness banner and the
            per-direction state layout are uniform across profiles.

Same module discipline as watterson.py / rig_effects.py: pure DSP, fs is a
constructor parameter, NO env reads (channel_sim resolves SIM_FM_* and passes
numbers in), every class is stateful-across-blocks with a .process() taking the
deinterleaved mono float64 block. Nothing here is constructed unless an FM knob
is set, preserving the bit-exact-baseline discipline.

Filter choices:
- Pre-emphasis: first-difference differentiator, gain-normalized to unity at
  1 kHz. |H| ∝ sin(pi f/fs) ≈ f through 3 kHz at fs=48k (+6 dB/oct within
  0.1 dB across the voice band).
- De-emphasis: first-order leaky integrator (-6 dB/oct), corner default 75 Hz
  (well under the 300 Hz band edge so the 300-3000 slope is the TIA-603 nominal
  6 dB/oct), unity at 1 kHz. The sub-300 Hz boost this implies is bounded by
  the RX voice bandpass: CTCSS rejection comes from the HPF skirt, as in real
  mic/speaker radios (and is exactly why data-over-voice-path needs a channel simulator).
- Voice bandpass: Butterworth SOS 300-3000 Hz, stateful across blocks (no edge
  artifacts), same construction as channel_sim.RigBPF.
"""
import numpy as np

try:
    from scipy.signal import butter as _butter, sosfilt as _sosfilt
except ImportError:  # scipy only needed when an FM port profile is enabled
    _butter = _sosfilt = None

VOICE_LO_HZ = 300.0
VOICE_HI_HZ = 3000.0
_REF_HZ = 1000.0          # emphasis gain-normalization reference


def _need_scipy():
    if _butter is None:
        raise RuntimeError("SIM_FM_PORT=micspk needs scipy (pip install scipy)")


class _VoiceBPF:
    """Stateful 300-3000 Hz Butterworth bandpass (SOS), one per chain."""

    def __init__(self, fs, order):
        _need_scipy()
        self.sos = _butter(order, [VOICE_LO_HZ, VOICE_HI_HZ], btype="band",
                           fs=fs, output="sos")
        self.zi = np.zeros((self.sos.shape[0], 2))

    def process(self, mono):
        y, self.zi = _sosfilt(self.sos, mono, zi=self.zi)
        return y


class _PreEmphasis:
    """+6 dB/oct first-difference differentiator, unity gain at 1 kHz."""

    def __init__(self, fs):
        self.g = 1.0 / (2.0 * np.sin(np.pi * _REF_HZ / fs))
        self.x1 = 0.0

    def process(self, mono):
        y = np.empty_like(mono)
        y[0] = mono[0] - self.x1
        np.subtract(mono[1:], mono[:-1], out=y[1:])
        self.x1 = mono[-1]
        y *= self.g
        return y


class _DeEmphasis:
    """-6 dB/oct first-order leaky integrator, unity gain at 1 kHz."""

    def __init__(self, fs, corner_hz=75.0):
        self.a = float(np.exp(-2.0 * np.pi * corner_hz / fs))
        w = 2.0 * np.pi * _REF_HZ / fs
        self.b = float(abs(1.0 - self.a * np.exp(-1j * w)))
        self.y1 = 0.0

    def process(self, mono):
        # y[n] = a*y[n-1] + b*x[n] — sequential recurrence (scipy lfilter would
        # also do; kept dependency-light and stateful without zi bookkeeping).
        y = np.empty_like(mono)
        a, b, y1 = self.a, self.b, self.y1
        for i in range(len(mono)):
            y1 = a * y1 + b * mono[i]
            y[i] = y1
        self.y1 = y1
        return y


class _DeEmphasisFast:
    """Vectorized -6 dB/oct leaky integrator (scipy lfilter with carried state).
    Preferred implementation; _DeEmphasis is the reference recurrence."""

    def __init__(self, fs, corner_hz=75.0):
        from scipy.signal import lfilter as _lf
        self._lf = _lf
        a = float(np.exp(-2.0 * np.pi * corner_hz / fs))
        w = 2.0 * np.pi * _REF_HZ / fs
        b = float(abs(1.0 - a * np.exp(-1j * w)))
        self.b = [b]
        self.a = [1.0, -a]
        self.zi = np.zeros(1)

    def process(self, mono):
        y, self.zi = self._lf(self.b, self.a, mono, zi=self.zi)
        return y


class FmPortTx:
    """Transmit-side port shaping. micspk: pre-emphasis -> voice BPF -> +CTCSS
    (tone injected after the voice filter — it shares the modulator path but not
    the voice band). data9600: identity."""

    def __init__(self, fs, profile, order=6, ctcss_hz=0.0, ctcss_amp=0.0):
        self.profile = profile
        self.fs = fs
        if profile == "micspk":
            _need_scipy()
            self.pre = _PreEmphasis(fs)
            self.bpf = _VoiceBPF(fs, order)
        elif profile != "data9600":
            raise ValueError(f"unknown FM port profile '{profile}'")
        self.ctcss_hz = float(ctcss_hz)
        self.ctcss_amp = float(ctcss_amp)
        self._phase = 0.0                     # CTCSS phase, continuous across blocks

    def process(self, mono):
        if self.profile == "micspk":
            mono = self.bpf.process(self.pre.process(mono))
        if self.ctcss_hz > 0.0 and self.ctcss_amp > 0.0:
            n = len(mono)
            ph = self._phase + 2.0 * np.pi * self.ctcss_hz / self.fs * np.arange(n)
            mono = mono + self.ctcss_amp * np.sin(ph)
            self._phase = float((ph[-1] + 2.0 * np.pi * self.ctcss_hz / self.fs)
                                % (2.0 * np.pi))
        return mono


class FmPortRx:
    """Receive-side port shaping. micspk: voice BPF -> de-emphasis (squelch is a
    separate SquelchGate stage, applied after this). data9600: identity."""

    def __init__(self, fs, profile, order=6, deemph_corner_hz=75.0):
        self.profile = profile
        if profile == "micspk":
            _need_scipy()
            self.bpf = _VoiceBPF(fs, order)
            self.de = _DeEmphasisFast(fs, deemph_corner_hz)
        elif profile != "data9600":
            raise ValueError(f"unknown FM port profile '{profile}'")

    def process(self, mono):
        if self.profile == "micspk":
            mono = self.de.process(self.bpf.process(mono))
        return mono


class SquelchGate:
    """Time-gated RX audio mute (the FM port design's port-profile table):
    models carrier-squelch attack (open_ms after carrier up; +tone_ms CTCSS
    decode when a tone is configured), the muted idle channel, and the optional
    closing 'squelch crash' tail burst (tail_ms of seeded noise at tail_amp
    after carrier drop, then hard mute).

    Timing is block-quantized exactly like the harness T/R model. The carrier
    input is a per-block bool from the harness (the transmitter's delayed rf_up
    — PTT keys the carrier regardless of audio content); when the harness can't
    supply it (full-duplex/VOX-less runs) pass None and a block-RMS energy
    detect (thresh, int16 units) stands in.

    State machine: CLOSED -> (carrier) ATTACK[wait blocks, muted] -> OPEN
    (audio passes) -> (carrier drop) TAIL[tail blocks, noise burst] -> CLOSED.
    Carrier drop during ATTACK aborts to CLOSED with no tail (squelch never
    opened); carrier return during TAIL re-opens immediately (it never closed).
    """

    def __init__(self, fs, block, open_ms=30.0, tone_ms=0.0, tail_ms=0.0,
                 tail_amp=2000.0, thresh=800.0, seed=0):
        block_ms = 1000.0 * block / fs
        self.wait_blocks = int(round((open_ms + tone_ms) / block_ms))
        self.tail_blocks = int(round(tail_ms / block_ms))
        self.tail_amp = float(tail_amp)
        self.thresh = float(thresh)
        self.rng = np.random.default_rng(seed + 900)   # dedicated stream
        self.up_blocks = 0        # consecutive carrier-present blocks
        self.open = False
        self.tail_left = 0

    def process(self, mono, carrier=None):
        if carrier is None:
            carrier = float(np.sqrt(np.mean(mono * mono))) > self.thresh
        if carrier:
            self.up_blocks += 1
            if self.tail_left > 0:            # re-open out of the tail
                self.tail_left = 0
                self.open = True
            elif not self.open:
                self.open = self.up_blocks > self.wait_blocks
            if self.open:
                return mono
            return np.zeros_like(mono)        # attack window: still muted
        # no carrier
        self.up_blocks = 0
        if self.open:
            self.open = False
            self.tail_left = self.tail_blocks
        if self.tail_left > 0:                # closing burst, then mute
            self.tail_left -= 1
            return self.rng.standard_normal(len(mono)) * self.tail_amp
        return np.zeros_like(mono)
