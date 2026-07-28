#!/usr/bin/env python3
"""Modem73Adapter -- a modem73 adapter on the ModemAdapter base class.

Drives modem73 (https://github.com/RFnexus/modem73) through skywave on the same
4-card ALSA loopback rig mercury/ardop/armstrong use. modem73 is a DATAGRAM KISS
TNC -- it has no native ARQ or connected mode (reliability is the application's
job, per its design) -- so this adapter supplies the application layer itself: a
small selective-repeat protocol over the KISS data plane, with sequence-numbered
data frames B->A and ACK-bitmap frames A->B. The ACKs ride the actual RF channel
(they pay real half-duplex airtime through the sim), so the measured goodput is
honestly comparable to modems with built-in ARQ.

Plumbing quirks this adapter absorbs:
  * miniaudio prefers the PulseAudio backend, which cannot see the raw snd-aloop
    cards; a bogus PULSE_SERVER forces the ALSA fallback.
  * modem73 selects audio devices by miniaudio enumeration INDEX; the adapter
    resolves the named PCMs in modem73_aloop.conf via `modem73 --list-audio`.
  * CSMA's level-based busy detection would lock out TX at bench noise levels
    (SIGMA 2000 is already above the -30 dB threshold), so the adapter switches
    both stations to sync-only carrier sense (csma_sync_only).
  * PTT: modem73's control port does not push PTT events; under SIM_PTT=1 the
    adapter polls get_status and synthesizes PTT ON/OFF lines for the relay.

Config (env, on top of the standard contract):
  MODEM73_BIN          modem73 binary (default: "modem73" on PATH)
  MODEM73_MODULATION   OFDM modulation for the CLI: BPSK..QAM256 (default QPSK)
  MODEM73_RATE         OFDM code rate: 1/2, 2/3, 3/4, 5/6, 1/4 (default 1/2)
  MODEM73_FRAME        frame size: short | normal | long (default normal)
  MODEM73_SET_CONFIG   raw JSON merged into a set_config on both stations, for
                       anything the CLI can't reach, e.g. the ROBUST/MFSK modes:
                       '{"modem_type":2,"robust_mode":1}'

Set MODEM73_BIN to the binary, then run it via the harness as the `modem73` modem:

  skywave-sweep modem73 spec.json out.csv
"""
import json
import os
import re
import select
import socket
import struct
import subprocess as sp
import time

from skywave.modem_adapter import ModemAdapter, run_adapter

# ---------------- KISS framing (pure helpers, unit-tested) ----------------
FEND, FESC, TFEND, TFESC = 0xC0, 0xDB, 0xDC, 0xDD


def kiss_encode(data: bytes, port: int = 0) -> bytes:
    """One KISS data frame: FEND, cmd byte, escaped payload, FEND."""
    out = bytearray([FEND, (port << 4) | 0x00])
    for b in data:
        if b == FEND:
            out += bytes([FESC, TFEND])
        elif b == FESC:
            out += bytes([FESC, TFESC])
        else:
            out.append(b)
    out.append(FEND)
    return bytes(out)


class KissDecoder:
    """Incremental KISS parser: feed() bytes, get back completed frame payloads
    (the data frames only; command byte stripped)."""

    def __init__(self):
        self.buf = bytearray()
        self.in_frame = False
        self.esc = False

    def feed(self, data: bytes):
        frames = []
        for b in data:
            if b == FEND:
                if self.in_frame and len(self.buf) >= 1:
                    if self.buf[0] & 0x0F == 0x00:      # data frame
                        frames.append(bytes(self.buf[1:]))
                self.buf.clear()
                self.in_frame = True
                self.esc = False
                continue
            if not self.in_frame:
                continue
            if self.esc:
                self.buf.append(FEND if b == TFEND else FESC if b == TFESC else b)
                self.esc = False
            elif b == FESC:
                self.esc = True
            else:
                self.buf.append(b)
        return frames


# ---------------- SKYW/1 selective-repeat framing (pure helpers) ----------------
# Data  : 'D' + seq(3B BE) + chunk bytes
# Poll  : 'Q' + round(2B BE) + total_chunks(3B BE)     sender asks receiver to ACK
# Ack   : 'A' + base(4B BE) + bitmap  (all seq < base received; bit i = base+i)
# Probe : 'P' + magic(4B) -> reply 'R' + magic(4B)     link_connect handshake

def chunk_payload(payload: bytes, mtu: int):
    """Split into data-frame payloads. Chunk data size = mtu - 4 (1B type + 3B seq)."""
    size = mtu - 4
    if size <= 0:
        raise ValueError(f"mtu {mtu} too small")
    return [payload[i:i + size] for i in range(0, len(payload), size)]


def make_data(seq: int, chunk: bytes) -> bytes:
    return b"D" + seq.to_bytes(3, "big") + chunk


def make_poll(rnd: int, total: int) -> bytes:
    return b"Q" + (rnd & 0xFFFF).to_bytes(2, "big") + total.to_bytes(3, "big")


def make_ack(received, total: int, max_len: int) -> bytes:
    """base = first missing seq; bitmap covers [base, total), truncated to fit."""
    base = 0
    while base < total and base in received:
        base += 1
    nbits = max(0, total - base)
    nbytes = min((nbits + 7) // 8, max_len - 5)
    bm = bytearray(nbytes)
    for i in range(nbytes * 8):
        if base + i in received:
            bm[i // 8] |= 0x80 >> (i % 8)
    return b"A" + struct.pack(">I", base) + bytes(bm)


def parse_ack(frame: bytes):
    """-> (base, set of acked seqs >= base covered by the bitmap)."""
    base = struct.unpack(">I", frame[1:5])[0]
    acked = set()
    bm = frame[5:]
    for i in range(len(bm) * 8):
        if bm[i // 8] & (0x80 >> (i % 8)):
            acked.add(base + i)
    return base, acked


def parse_list_audio(text: str):
    """Parse `modem73 --list-audio` into ({input name: idx}, {output name: idx})."""
    inputs, outputs = {}, {}
    cur = None
    for line in text.splitlines():
        if re.match(r"\s*Input", line):
            cur = inputs
            continue
        if re.match(r"\s*Output", line):
            cur = outputs
            continue
        m = re.match(r"\s*(\d+) - (.+?)\s*$", line)
        if m and cur is not None:
            cur.setdefault(m.group(2), int(m.group(1)))
    return inputs, outputs


# ---------------- control-port client ----------------
class ControlClient:
    """modem73 JSON control port: 4-byte BE length prefix + JSON. Responses and
    broadcast events interleave on one socket; request() returns the first
    non-event message and hands events to the callback."""

    def __init__(self, host, port, on_event=None):
        self.sock = socket.create_connection((host, port), timeout=5)
        self.sock.setblocking(False)
        self.buf = b""
        self.on_event = on_event
        self.pending = []                # non-event messages not yet claimed

    def fileno(self):
        return self.sock.fileno()

    def _pump_buf(self):
        while len(self.buf) >= 4:
            n = struct.unpack(">I", self.buf[:4])[0]
            if len(self.buf) < 4 + n:
                return
            raw, self.buf = self.buf[4:4 + n], self.buf[4 + n:]
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if isinstance(msg, dict) and "event" in msg:
                if self.on_event:
                    self.on_event(msg)
            else:
                self.pending.append(msg)

    def read_available(self):
        """Drain the socket without blocking; dispatch events."""
        while True:
            try:
                d = self.sock.recv(65536)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            if not d:
                return
            self.buf += d
            self._pump_buf()

    def request(self, obj: dict, timeout: float = 5.0):
        b = json.dumps(obj).encode()
        self.sock.sendall(struct.pack(">I", len(b)) + b)
        end = time.time() + timeout
        while time.time() < end:
            if self.pending:
                return self.pending.pop(0)
            select.select([self.sock], [], [], 0.2)
            self.read_available()
        return None

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class Modem73Adapter(ModemAdapter):
    name = "modem73"
    # A = receiver (data sink, sends ACKs), B = sender -- matching mercury's
    # B-caller/sender convention. KISS port, control port per station.
    A_KISS, A_CTL = 8460, 8461
    B_KISS, B_CTL = 8470, 8471
    ready_timeout_s = 20.0
    connect_timeout_s = 120.0

    def __init__(self, cfg):
        super().__init__(cfg)
        self.bin = os.environ.get("MODEM73_BIN", "").strip() or "modem73"
        self.modulation = os.environ.get("MODEM73_MODULATION", "").strip() or "QPSK"
        self.rate = os.environ.get("MODEM73_RATE", "").strip() or "1/2"
        self.frame = os.environ.get("MODEM73_FRAME", "").strip() or "normal"
        self.extra_cfg = os.environ.get("MODEM73_SET_CONFIG", "").strip()
        self.conf = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "modem73_aloop.conf")
        self.kissA = self.kissB = None            # KISS TCP sockets
        self.ctlA = self.ctlB = None              # ControlClient per station
        self.decA = KissDecoder()
        self.decB = KissDecoder()
        self.mtu = 510                            # payload_size - 2; refreshed at connect
        self.frame_dur = 3.0                      # airtime of one frame; parsed from log
        self._ptt_state = {"A": False, "B": False}
        self._ptt_next_poll = 0.0

    # ---- hooks ----
    def preclean_patterns(self):
        # Scoped to our control ports so they can never match a user's real
        # modem73 session (or this adapter's own cmdline). The arecord/aplay
        # patterns catch orphans left by a PRIOR run's abnormal exit (a NOCONN, an
        # external timeout kill) that teardown_stations() never got to run for --
        # without this, one bad cell silently fail_connects every cell after it for
        # the rest of the campaign (the devices stay exclusively held).
        return [f"modem73 .*--control-port {self.A_CTL}",
                f"modem73 .*--control-port {self.B_CTL}",
                "arecord -D plughw:[2-5]",
                "aplay -D plughw:[2-5]"]

    def _station_env(self):
        # Bogus PULSE_SERVER defeats miniaudio's Pulse backend (it cannot see the
        # aloop cards) so it falls back to ALSA; ALSA_CONFIG_PATH supplies the
        # named loopback endpoints that --list-audio index resolution finds.
        return dict(os.environ,
                    PULSE_SERVER="unix:/nonexistent-skywave-bench",
                    ALSA_CONFIG_PATH=self.conf)

    def _resolve_devices(self):
        out = sp.run([self.bin, "--list-audio"], env=self._station_env(),
                     capture_output=True, text=True, timeout=30)
        inputs, outputs = parse_list_audio(out.stdout + out.stderr)
        try:
            return {"A": (inputs["M73_RXA"], outputs["M73_TXA"]),
                    "B": (inputs["M73_RXB"], outputs["M73_TXB"])}
        except KeyError as e:
            raise SystemExit(f"modem73 --list-audio did not enumerate {e}; "
                             f"is the 4-card snd-aloop rig loaded?")

    def start_stations(self):
        dev = self._resolve_devices()
        self._launch("W1SKA", self.A_KISS, self.A_CTL, dev["A"], "/tmp/m73A.log")
        self._launch("W2SKB", self.B_KISS, self.B_CTL, dev["B"], "/tmp/m73B.log")

    def _launch(self, call, kiss_port, ctl_port, dev, log):
        rx_idx, tx_idx = dev
        p = sp.Popen([self.bin, "--headless", "--ptt", "none",
                      "--bind", "127.0.0.1", "-p", str(kiss_port),
                      "--control-port", str(ctl_port),
                      "--input-device", str(rx_idx), "--output-device", str(tx_idx),
                      "-c", call, "-m", self.modulation, "-r", self.rate,
                      f"--{self.frame}", "--csma-burst", "4"],
                     env=self._station_env(),
                     stdout=open(log, "wb"), stderr=sp.STDOUT)
        self._stations.append(p)

    def wait_ready(self, deadline):
        for port in (self.A_KISS, self.A_CTL, self.B_KISS, self.B_CTL):
            if not self._wait_listen(port, deadline):
                return False
        return True

    def _wait_listen(self, port, deadline):
        while time.time() < deadline:
            if any(p.poll() is not None for p in self._stations):
                return False          # a station died at startup: fail loudly
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                return True
            except OSError:
                time.sleep(0.3)
        return False

    def link_connect(self, deadline):
        self.ctlA = ControlClient("127.0.0.1", self.A_CTL,
                                  on_event=lambda m: self._on_event("A", m))
        self.ctlB = ControlClient("127.0.0.1", self.B_CTL,
                                  on_event=lambda m: self._on_event("B", m))
        self.kissA = socket.create_connection(("127.0.0.1", self.A_KISS))
        self.kissA.setblocking(False)
        self.kissB = socket.create_connection(("127.0.0.1", self.B_KISS))
        self.kissB.setblocking(False)

        # Level-based CSMA busy detection locks out TX at bench noise levels;
        # sync-only keeps the turnaround behavior on decodable carriers only.
        setcfg = {"csma_sync_only": 1}
        if self.extra_cfg:
            setcfg.update(json.loads(self.extra_cfg))
        for st, ctl in (("A", self.ctlA), ("B", self.ctlB)):
            r = ctl.request(dict(setcfg, cmd="set_config"))
            if not (r and r.get("ok")):
                print(f"  ({st}: set_config {setcfg} -> {r})", flush=True)
        gc = self.ctlB.request({"cmd": "get_config"}) or {}
        payload_size = int(gc.get("payload_size") or 512)
        self.mtu = payload_size - 2       # PHY payload carries a 2-byte length prefix
        self.frame_dur = self._parse_frame_dur("/tmp/m73B.log")
        # Nominal PHY bitrate of the configured mode, as the telemetry peak.
        bps = self._parse_bitrate("/tmp/m73B.log")
        if bps:
            self.on_line("B", f"BITRATE (0) {bps} BPS")
        print(f"  (mtu={self.mtu} B, frame_dur={self.frame_dur:.2f}s, "
              f"mode={gc.get('modulation')} {gc.get('code_rate')})", flush=True)

        # Probe handshake: B sends 'P', A auto-replies 'R' (in _handle_rx_frame),
        # so one round trip proves both directions of the cable decode.
        magic = os.urandom(4)
        self._probe_ok = False
        self._probe_magic = magic
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            self.kissB.sendall(kiss_encode(b"P" + magic))
            print(f"  (probe {attempt} ->)", flush=True)
            self._await_reply(lambda: self._probe_ok, deadline)
            if self._probe_ok:
                # The shared TNC-adapter acquisition marker: sweep_runner's
                # CONNECTED_RE reads connected/time_to_connect off this line
                # (modem73 has no TNC CONNECTED text of its own -- the SKYW/1
                # probe round trip IS its acquisition event).
                print(f"  <- B: CONNECTED (SKYW/1 probe {attempt})", flush=True)
                return True
        return False

    def _b_tx_idle(self):
        """True once B's TX queue is empty and it is off the air."""
        r = self.ctlB.request({"cmd": "get_status"}, timeout=1.0)
        if not r:
            return False
        return int(r.get("tx_queue") or 0) == 0 and r.get("channel_state") != "tx"

    def _await_reply(self, have_reply, deadline):
        """Pump until `have_reply()`: first wait out B's own TX (queue drain is
        airtime, not response latency), then give A a reply window of one frame
        airtime plus CSMA turnaround slack."""
        drain_cap = time.time() + 300.0   # backstop: a dead status port can't pin us
        while time.time() < min(deadline, drain_cap):
            if have_reply():
                return True
            self._pump(0.3)
            if self._b_tx_idle():
                break
        end = min(deadline, time.time() + 2 * self.frame_dur + 4.0)
        while time.time() < end:
            if have_reply():
                return True
            self._pump(0.3)
        return have_reply()

    def transfer(self, payload, deadline):
        chunks = chunk_payload(payload, self.mtu)
        total = len(chunks)
        self._rx_chunks = {}              # receiver-side store: seq -> bytes
        self._rx_total = total
        self._last_ack = None
        unacked = set(range(total))
        rnd = 0
        print(f"sending {len(payload)} B as {total} chunks (mtu {self.mtu}) ...",
              flush=True)
        while unacked and time.time() < deadline:
            rnd += 1
            for seq in sorted(unacked):
                self.kissB.sendall(kiss_encode(make_data(seq, chunks[seq])))
            for poll_try in range(1, 4):
                self._last_ack = None
                self.kissB.sendall(kiss_encode(make_poll(rnd, total)))
                # Drain-aware wait: airtime for the queued frames, then a real
                # ACK window (one frame airtime + CSMA turnaround slack).
                self._await_reply(lambda: self._last_ack is not None, deadline)
                if self._last_ack is not None:
                    base, acked = self._last_ack
                    unacked = {s for s in unacked if s >= base and s not in acked}
                    print(f"  round {rnd}: ack base={base} -> "
                          f"{total - len(unacked)}/{total} chunks", flush=True)
                    break
                if time.time() >= deadline:
                    break
                print(f"  round {rnd}: no ack (poll {poll_try}/3)", flush=True)
        return b"".join(self._rx_chunks[s] for s in sorted(self._rx_chunks))

    # ---- receive plane ----
    def _pump(self, timeout):
        """One select cycle over both KISS sockets and both control sockets;
        also drives the PTT status poll when SIM_PTT is on."""
        socks = [self.kissA, self.kissB, self.ctlA, self.ctlB]
        r, _, _ = select.select(socks, [], [], timeout)
        for s in r:
            if s is self.ctlA or s is self.ctlB:
                s.read_available()
                continue
            try:
                d = s.recv(65536)
            except (BlockingIOError, OSError):
                continue
            if not d:
                continue
            dec = self.decA if s is self.kissA else self.decB
            st = "A" if s is self.kissA else "B"
            for frame in dec.feed(d):
                self._handle_rx_frame(st, frame)
        if self.cfg.ptt:
            self._poll_ptt()

    def _handle_rx_frame(self, station, frame):
        if not frame:
            return
        t = frame[:1]
        if station == "A":                     # receiver side
            if t == b"D" and len(frame) > 4:
                seq = int.from_bytes(frame[1:4], "big")
                self._rx_chunks[seq] = frame[4:]
            elif t == b"Q" and len(frame) >= 6:
                ack = make_ack(set(self._rx_chunks), self._rx_total, self.mtu)
                self.kissA.sendall(kiss_encode(ack))
            elif t == b"P":
                self.kissA.sendall(kiss_encode(b"R" + frame[1:5]))
        else:                                  # sender side hears ACKs / probe replies
            if t == b"A" and len(frame) >= 5:
                self._last_ack = parse_ack(frame)
            elif t == b"R" and frame[1:5] == getattr(self, "_probe_magic", None):
                self._probe_ok = True

    def _on_event(self, station, msg):
        if msg.get("event") == "rx_frame":
            snr = msg.get("snr")
            if snr is not None:
                self.on_line(station, f"SN {float(snr):.1f}")

    def _poll_ptt(self):
        """modem73 pushes no PTT events; poll get_status and synthesize the
        PTT ON/OFF lines the channel relay expects. Best-effort (~5 Hz)."""
        now = time.time()
        if now < self._ptt_next_poll:
            return
        self._ptt_next_poll = now + 0.2
        for st, ctl in (("A", self.ctlA), ("B", self.ctlB)):
            r = ctl.request({"cmd": "get_status"}, timeout=0.5)
            if not r:
                continue
            on = bool(r.get("ptt_on"))
            if on != self._ptt_state[st]:
                self._ptt_state[st] = on
                self.on_line(st, "PTT ON" if on else "PTT OFF")

    def scan_telemetry(self, station, line):
        m = re.search(r"BITRATE \(\d+\) (\d+) BPS", line)
        if m:
            self.modes.append(int(m.group(1)))
        s = re.search(r"\bSN (-?[0-9.]+)", line)
        if s:
            self.snrs.append(float(s.group(1)))

    # ---- station-log parsers (the control port exposes neither airtime nor bps) ----
    @staticmethod
    def _parse_frame_dur(log):
        try:
            with open(log, errors="replace") as f:
                durs = re.findall(r"duration: ([0-9.]+)s", f.read())
            return float(durs[-1]) if durs else 3.0
        except OSError:
            return 3.0

    @staticmethod
    def _parse_bitrate(log):
        try:
            with open(log, errors="replace") as f:
                rates = re.findall(r"bitrate: ([0-9.]+)kb/s", f.read())
            return int(float(rates[-1]) * 1000) if rates else 0
        except OSError:
            return 0

    def teardown_stations(self):
        for c in (self.ctlA, self.ctlB):
            if c is not None:
                c.close()
        for s in (self.kissA, self.kissB):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        super().teardown_stations()      # SIGTERM the modem73 processes
        # modem73's miniaudio ALSA backend shells out to arecord/aplay for the four
        # aloop endpoints; a station that dies abnormally (a NOCONN, an external
        # timeout kill) can orphan these, leaving the device exclusively held for
        # every subsequent run -- a whole campaign silently fail_connects from one
        # bad cell onward. Scoped to this rig's fixed cards (modem73_aloop.conf: TXA
        # card2, RXA card3, TXB card4, RXB card5), same pattern ardop.py uses.
        for pat in ["arecord -D plughw:[2-5]", "aplay -D plughw:[2-5]"]:
            sp.run(["pkill", "-9", "-f", pat], stdout=sp.DEVNULL, stderr=sp.DEVNULL)


if __name__ == "__main__":
    import sys
    sys.exit(run_adapter(Modem73Adapter))
