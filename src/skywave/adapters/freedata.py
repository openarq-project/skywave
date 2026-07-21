#!/usr/bin/env python3
"""FreedataAdapter -- a FreeDATA adapter on the ModemAdapter base class.

Drives two FreeDATA server instances (https://github.com/DJ2LS/FreeDATA) through
skywave's shared half-duplex channel_sim and measures a real raw-ARQ transfer with the
SAME semantics as every other adapter: incompressible payload, exact byte-for-byte intact
check, and a goodput clock over the transfer. A self-contained port of a hand-written
FreeDATA raw-ARQ driver (server launch + config helpers included) onto the ModemAdapter
contract.

HOW FreeDATA differs from the TCP-TNC adapters (mercury/vara):
  * TRANSPORT is REST + websocket, not a socket TNC. The transfer is one
    POST /modem/send_arq_raw; completion + per-burst progress + PTT arrive on the
    /events websocket. `link_connect` therefore has no handshake -- it just starts the
    /events listeners (which are ALSO the only PTT path, so they must be live before the
    modem can key) and lets the audio stacks settle; the POST lives in `transfer`.
  * The MONO cable: FreeDATA opens the raw aloop hw device at mono and cannot adapt the
    channel count, so `launch_channel` forces SIM_NCH=1.
  * ITS OWN INTERPRETER: the server needs FreeDATA's venv (fastapi/uvicorn/...) and this
    adapter needs websocket-client. Point $ADAPTER_PY at that venv's python (sweep_runner's
    adapter_argv launches the adapter under it); the server launch inherits it (or set
    $FREEDATA_PY). $FREEDATA_DIR locates the FreeDATA checkout (default ~/tools/FreeDATA).

PARTIAL BYTES: the raw-ARQ path exposes only a COUNT for a non-completing run (the actual
payload bytes arrive only with the terminal event). That count is surfaced as zero-padding
so the base records got=received_bytes / intact=False -- the payload is never partially
reconstructable, so intact is False by construction, exactly as for the other adapters'
short partials.

Not runnable on a dev box (needs a FreeDATA checkout + its venv + ALSA aloop).
Run:  ADAPTER_PY=<freedata-venv-python> skywave-sweep freedata spec.json out.csv
"""
import base64
import json
import os
import socket
import subprocess as sp
import sys
import threading
import time
import urllib.request

from skywave.modem_adapter import ModemAdapter, run_adapter
from skywave import bench_pipes

FREEDATA_DIR = os.path.expanduser(os.environ.get("FREEDATA_DIR", "~/tools/FreeDATA"))

# ALSA aloop device names for the 4-card map (override via env for another wiring). The
# config stores a CRC of the name, matching FreeDATA's own device identification.
A_OUT_DEV = os.environ.get("FD_A_OUT", "Loopback: PCM (hw:2,1)")
A_IN_DEV = os.environ.get("FD_A_IN", "Loopback: PCM (hw:3,1)")
B_OUT_DEV = os.environ.get("FD_B_OUT", "Loopback: PCM (hw:4,0)")
B_IN_DEV = os.environ.get("FD_B_IN", "Loopback: PCM (hw:5,0)")


def _get_crc_16(data: bytes) -> str:
    """CRC-16-CCITT-FALSE -- matches helpers.get_crc_16 in FreeDATA."""
    crc = 0xFFFF
    polynomial = 0x1021
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ polynomial) if (crc & 0x8000) else (crc << 1)
            crc &= 0xFFFF
    return crc.to_bytes(2, byteorder="big").hex()


def _audio_crc(name: str, hostapi: int = 0) -> str:
    return _get_crc_16(f"{name}.{hostapi}".encode("utf-8"))


def _write_config(path, mycall, modemport, cmd_port, data_port, input_crc, output_crc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    content = f"""\
[NETWORK]
modemaddress = 127.0.0.1
modemport = {modemport}

[STATION]
mycall = {mycall}
mygrid = JN48ea
myssid = 0
ssid_list = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]
enable_explorer = False
respond_to_cq = True
enable_callsign_blacklist = False
callsign_blacklist = []

[AUDIO]
input_device = {input_crc}
output_device = {output_crc}
rx_audio_level = 0
tx_audio_level = 0
rx_auto_audio_level = True
tx_auto_audio_level = False

[RIGCTLD]
ip = 127.0.0.1
port = 4532
path =
command =
arguments =
enable_vfo = False

[FLRIG]
ip = 127.0.0.1
port = 12345

[RADIO]
control = disabled
model_id = 1001
serial_port = NONE
serial_speed = 38400
data_bits = 8
stop_bits = 1
serial_handshake = ignore
ptt_port = NONE
ptt_mode = TX
ptt_type = NONE
serial_dcd = NONE
serial_dtr = OFF
serial_rts = OFF

[MODEM]
enable_morse_identifier = False
tx_delay = 50
maximum_bandwidth = 2438

[SOCKET_INTERFACE]
enable = True
host = 127.0.0.1
cmd_port = {cmd_port}
data_port = {data_port}

[MESSAGES]
enable_auto_repeat = True

[QSO_LOGGING]
enable_adif_udp = False
adif_udp_host = 127.0.0.1
adif_udp_port = 2237
enable_adif_wavelog = False
adif_wavelog_host = http://localhost
adif_wavelog_api_key = API-KEY

[GUI]
auto_run_browser = False
distance_unit = km

[EXP]
enable_ring_buffer = False
enable_vhf = False
enable_groupchat = False
"""
    with open(path, "w") as f:
        f.write(content)


class FreedataAdapter(ModemAdapter):
    name = "freedata"
    A_REST, B_REST = 5000, 5001          # modemport = REST + /events websocket
    A_CMD, A_DAT = 9000, 9001            # socket interface (config only; raw path unused)
    B_CMD, B_DAT = 9002, 9003
    ACALL, BCALL = "FD1ABC", "FD2XYZ"    # A = answerer/receiver, B = dxcall target
    DXCALL_B = "FD2XYZ-0"
    CFG_A, LOG_A = "/tmp/freedata_a/config.ini", "/tmp/freedata_a/server.log"
    CFG_B, LOG_B = "/tmp/freedata_b/config.ini", "/tmp/freedata_b/server.log"
    ready_timeout_s = 40.0
    connect_timeout_s = 60.0

    def __init__(self, cfg):
        super().__init__(cfg)
        # rx_bytes: last progress count; rx_b64: terminal payload; done: terminal seen.
        self.state = {"stop": False, "post_accept": None,
                      "done": None, "t_end": None, "rx_b64": None, "rx_bytes": 0}

    # ---- hooks ----
    def preclean_patterns(self):
        # "freedata_server" matches the server processes' `-c` launch string, NOT this
        # adapter's cmdline (python -m skywave.adapters.freedata) -- no self-kill.
        return ["freedata_server", "arecord -D plughw", "aplay -D plughw", "noise_pipe"]

    def launch_channel(self):
        # Mono cable: FreeDATA opens the raw aloop hw device at mono and cannot adapt the
        # channel count (PortAudio "Invalid number of channels").
        self._sim = bench_pipes.launch_channel_sim(extra_env={"SIM_NCH": "1"})

    def start_stations(self):
        a_out, a_in = _audio_crc(A_OUT_DEV), _audio_crc(A_IN_DEV)
        b_out, b_in = _audio_crc(B_OUT_DEV), _audio_crc(B_IN_DEV)
        _write_config(self.CFG_A, self.ACALL, self.A_REST, self.A_CMD, self.A_DAT, a_in, a_out)
        _write_config(self.CFG_B, self.BCALL, self.B_REST, self.B_CMD, self.B_DAT, b_in, b_out)
        self._launch_server(self.CFG_A, self.LOG_A)
        self._launch_server(self.CFG_B, self.LOG_B)

    def _launch_server(self, config_path, log_path):
        # FREEDATA_DATABASE isolates each instance's message store next to its config;
        # without it both servers share one db and a receiver poll can false-positive.
        db_path = os.path.join(os.path.dirname(config_path), "messages.db")
        env = dict(os.environ, FREEDATA_CONFIG=config_path, PYTHONPATH=FREEDATA_DIR,
                   FREEDATA_DATABASE=db_path)
        launch_script = (f"import sys; sys.path.insert(0, {FREEDATA_DIR!r}); "
                         "from freedata_server.server import main; main()")
        # The server needs FreeDATA's venv (fastapi/uvicorn/...). Inherit the adapter's
        # interpreter (set via $ADAPTER_PY to the FreeDATA venv), or override with $FREEDATA_PY.
        fd_py = os.environ.get("FREEDATA_PY") or sys.executable
        p = sp.Popen([fd_py, "-c", launch_script], env=env,
                     stdout=open(log_path, "wb"), stderr=sp.STDOUT, cwd=FREEDATA_DIR)
        self._stations.append(p)

    def wait_ready(self, deadline):
        ok = (self._wait_listen(self.A_REST, deadline)
              and self._wait_listen(self.B_REST, deadline))
        if not ok:
            self._dump_logs()
        return ok

    def _wait_listen(self, port, deadline):
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                return True
            except OSError:
                time.sleep(0.4)
        return False

    def link_connect(self, deadline):
        # Raw-ARQ has no handshake distinct from the POST. Start the /events listeners
        # (PTT relay + progress/terminal folding) BEFORE the modem can key -- they are the
        # ONLY PTT path -- then let the audio stacks settle. The POST happens in transfer().
        for port, st in ((self.A_REST, "A"), (self.B_REST, "B")):
            threading.Thread(target=self._ws_events, args=(port, st), daemon=True).start()
        time.sleep(5.0)
        return True

    def transfer(self, payload, deadline):
        body = json.dumps({"dxcall": self.DXCALL_B, "type": "raw",
                           "data": base64.b64encode(payload).decode()}).encode()
        t_accept = None
        for attempt in range(1, 8):
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.A_REST}/modem/send_arq_raw",
                data=body, headers={"Content-Type": "application/json"}, method="POST")
            try:
                urllib.request.urlopen(req, timeout=10).read()
                t_accept = time.time()
                self.state["post_accept"] = t_accept
                print(f"POST send_arq_raw ({len(payload)} B) accepted (attempt {attempt})", flush=True)
                break
            except Exception as e:
                print(f"  POST attempt {attempt} failed: {e}", flush=True)
                time.sleep(3)
        if t_accept is None:
            print("FAIL: POST never accepted", flush=True)
            self._dump_logs()
            return b""
        while self.state["done"] is None and time.time() < deadline:
            time.sleep(0.2)
        if self.state["done"] and self.state["rx_b64"]:
            try:
                return base64.b64decode(self.state["rx_b64"])
            except Exception:
                return b""
        # Non-completion: the IRS reported a partial COUNT via progress events, but the
        # actual bytes arrive only with the terminal event. Surface the count as
        # zero-padding so the base records got=received_bytes / intact=False (the payload
        # is never partially reconstructable -- intact False by construction).
        return b"\x00" * self.state["rx_bytes"]

    def teardown_stations(self):
        self.state["stop"] = True
        super().teardown_stations()      # SIGTERM the two FreeDATA servers
        time.sleep(1)
        for pat in ["freedata_server", "arecord -D plughw", "aplay -D plughw", "noise_pipe"]:
            sp.run(["pkill", "-9", "-f", pat], stdout=sp.DEVNULL, stderr=sp.DEVNULL)

    # ---- helpers ----
    def _ws_events(self, port, station):
        # websocket-client, provided by the FreeDATA venv ($ADAPTER_PY). Lazy-imported so
        # this module loads without it (py_compile / registry tests don't need it).
        import websocket
        is_b = (station == "B")
        while not self.state["stop"]:
            try:
                ws = websocket.create_connection(f"ws://127.0.0.1:{port}/events", timeout=5)
            except Exception:
                time.sleep(0.5)
                continue
            ws.settimeout(1.0)
            while not self.state["stop"]:
                try:
                    msg = ws.recv()
                except Exception:
                    continue
                if not msg:
                    continue
                try:
                    ev = json.loads(msg)
                except Exception:
                    continue
                if "ptt" in ev:
                    self.on_line(station, "PTT ON" if bool(ev["ptt"]) else "PTT OFF")
                if is_b:
                    self._apply_inbound(ev)
            try:
                ws.close()
            except Exception:
                pass

    def _apply_inbound(self, ev):
        """Fold one B-side /events message into self.state. FreeDATA broadcasts progress
        (received_bytes/total_bytes, no `success`) and finished (success/data, no
        received_bytes) under the SAME `arq-transfer-inbound` key -- told apart by key
        presence. Reading received_bytes off progress events is what makes a non-completing
        run report the bytes it moved instead of 0."""
        if ev.get("type") != "arq":
            return
        tr = ev.get("arq-transfer-inbound")
        if not isinstance(tr, dict):
            return
        if "received_bytes" in tr:               # progress event (last value wins)
            try:
                self.state["rx_bytes"] = int(tr["received_bytes"])
            except (TypeError, ValueError):
                pass
        if tr.get("success") is True:            # terminal event
            self.state["t_end"] = time.time()
            self.state["rx_b64"] = tr.get("data")
            self.state["done"] = True

    def _dump_logs(self):
        for label, path in [("A", self.LOG_A), ("B", self.LOG_B)]:
            print(f"\n--- {label} log (last 20 lines) ---", flush=True)
            try:
                sp.run(["tail", "-20", path])
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(run_adapter(FreedataAdapter))
