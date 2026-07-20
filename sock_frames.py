"""Framed unix-socket audio transport for skywave.

Wire format (little-endian), one length-prefixed frame per audio block:

    u32 length                    # bytes AFTER this field (header + samples)
    sim -> station:  u64 seq | u64 virtual_now_ms | u16 n | n*NCH i16 samples
    station -> sim:  u64 seq | u8 ptt             | u16 n | n*NCH i16 samples

`n` is frames per channel (the sim's SIM_BLOCK); the sample payload is
interleaved int16 at the cable's SIM_NCH channels, exactly the bytes the aloop
cable carries. `ptt` makes key edges block-exact and deterministic:
0 = unkeyed, 1 = keyed, PTT_UNKNOWN (255) = not provided by this station (the
stage-1 sock<->ALSA shim cannot see the modem's PTT; the sim's stdin PTT relay
governs, unchanged). In stage 1 (wall clock retained) `virtual_now_ms` carries
wall milliseconds; the shim ignores it. Stage 2 makes it the lockstep clock.

Shared by channel_sim.py (SIM_TRANSPORT=sock), sock_alsa_shim.py, a future
native socket-backend station, and tests/test_sock_transport.py (which pins the
byte layout as a golden — changing this format is a wire change).
"""
import struct

LEN = struct.Struct("<I")
HDR_SIM = struct.Struct("<QQH")   # sim -> station: seq, virtual_now_ms, n
HDR_STA = struct.Struct("<QBH")   # station -> sim: seq, ptt, n
PTT_UNKNOWN = 255


def pack_sim(seq, virtual_now_ms, nframes, payload):
    """One sim->station frame as bytes (payload = interleaved int16 bytes)."""
    return (LEN.pack(HDR_SIM.size + len(payload))
            + HDR_SIM.pack(seq, virtual_now_ms, nframes) + bytes(payload))


def pack_station(seq, ptt, nframes, payload):
    """One station->sim frame as bytes."""
    return (LEN.pack(HDR_STA.size + len(payload))
            + HDR_STA.pack(seq, ptt, nframes) + bytes(payload))


def _read_exact(f, n):
    """Read exactly n bytes from file-like f; None on clean EOF at a frame
    boundary; raises EOFError on EOF mid-frame (a torn frame is a protocol
    error, not a normal shutdown)."""
    buf = bytearray(n)
    mv = memoryview(buf)
    got = 0
    while got < n:
        k = f.readinto(mv[got:])
        if not k:
            if got == 0:
                return None
            raise EOFError(f"EOF mid-frame ({got}/{n} bytes)")
        got += k
    return buf


def recv_into(f, hdr, payload_mv):
    """Read one length-prefixed frame from file-like f (e.g. sock.makefile('rb')).

    Header fields are returned as hdr's unpack tuple; the sample payload is read
    into payload_mv, whose length fixes the expected frame size (fixed-block
    transport: every frame must carry exactly len(payload_mv) sample bytes).
    Returns None on clean EOF at a frame boundary; raises ValueError on a
    size-mismatched frame, EOFError on a torn frame.
    """
    head = _read_exact(f, LEN.size)
    if head is None:
        return None
    (length,) = LEN.unpack(head)
    if length != hdr.size + len(payload_mv):
        raise ValueError(f"frame length {length} != header {hdr.size} "
                         f"+ payload {len(payload_mv)}")
    fields = hdr.unpack(_read_exact(f, hdr.size))
    got = 0
    while got < len(payload_mv):
        k = f.readinto(payload_mv[got:])
        if not k:
            raise EOFError(f"EOF mid-payload ({got}/{len(payload_mv)} bytes)")
        got += k
    return fields
