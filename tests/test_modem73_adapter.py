"""Unit tests for the modem73 adapter's pure logic: KISS framing, the SKYW/1
selective-repeat framing, --list-audio parsing, and telemetry scanning. No
modem73 binary, sockets, or ALSA involved."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from skywave.adapters.modem73 import (            # noqa: E402
    FEND, FESC, TFEND, TFESC,
    KissDecoder, kiss_encode, chunk_payload, make_data, make_poll,
    make_ack, parse_ack, parse_list_audio, Modem73Adapter,
)
from skywave.modem_adapter import AdapterConfig   # noqa: E402


# ---------------- KISS framing ----------------
def test_kiss_roundtrip_plain():
    d = KissDecoder()
    assert d.feed(kiss_encode(b"hello")) == [b"hello"]


def test_kiss_roundtrip_escapes():
    payload = bytes([FEND, FESC, TFEND, TFESC, 0x00, 0xFF]) * 3
    d = KissDecoder()
    assert d.feed(kiss_encode(payload)) == [payload]


def test_kiss_encode_has_no_bare_fend_inside():
    enc = kiss_encode(bytes([FEND]) * 5)
    assert enc[0] == FEND and enc[-1] == FEND
    assert FEND not in enc[1:-1]


def test_kiss_decoder_split_across_feeds():
    frame = kiss_encode(b"split-me-" + bytes([FESC, FEND]))
    d = KissDecoder()
    out = []
    for i in range(len(frame)):
        out += d.feed(frame[i:i + 1])
    assert out == [b"split-me-" + bytes([FESC, FEND])]


def test_kiss_decoder_multiple_frames_one_feed():
    d = KissDecoder()
    blob = kiss_encode(b"one") + kiss_encode(b"two") + kiss_encode(b"three")
    assert d.feed(blob) == [b"one", b"two", b"three"]


def test_kiss_decoder_ignores_non_data_frames():
    d = KissDecoder()
    txdelay = bytes([FEND, 0x01, 42, FEND])       # KISS cmd 1 = TXDELAY
    assert d.feed(txdelay + kiss_encode(b"data")) == [b"data"]


def test_kiss_decoder_ignores_interframe_garbage():
    d = KissDecoder()
    assert d.feed(b"\x01\x02\x03" + kiss_encode(b"x")) == [b"x"]


# ---------------- SKYW/1 framing ----------------
def test_chunk_payload_sizes_and_reassembly():
    payload = bytes(range(256)) * 10               # 2560 B
    chunks = chunk_payload(payload, mtu=510)
    assert all(len(c) <= 506 for c in chunks)
    assert b"".join(chunks) == payload


def test_chunk_payload_rejects_tiny_mtu():
    with pytest.raises(ValueError):
        chunk_payload(b"x", mtu=4)


def test_data_frame_fits_mtu():
    chunks = chunk_payload(b"z" * 5000, mtu=510)
    assert all(len(make_data(i, c)) <= 510 for i, c in enumerate(chunks))


def test_ack_roundtrip_all_received():
    total = 40
    ack = make_ack(set(range(total)), total, max_len=510)
    base, acked = parse_ack(ack)
    assert base == total                           # everything below base is done


def test_ack_roundtrip_with_holes():
    total = 40
    received = set(range(total)) - {3, 17, 33}
    base, acked = parse_ack(make_ack(received, total, max_len=510))
    assert base == 3
    covered = {s for s in range(base, total) if s in acked}
    assert covered == {s for s in received if s >= base}
    # sender-side update: everything < base plus the acked bits are done
    unacked = {s for s in range(total) if s >= base and s not in acked}
    assert unacked == {3, 17, 33}


def test_ack_bitmap_truncates_to_mtu():
    total = 100_000                                # bitmap would be 12.5 kB untruncated
    ack = make_ack(set(), total, max_len=170)      # narrow ROBUST-mode MTU
    assert len(ack) <= 170
    base, acked = parse_ack(ack)
    assert base == 0 and acked == set()


def test_ack_empty_receiver():
    base, acked = parse_ack(make_ack(set(), 10, max_len=510))
    assert base == 0 and acked == set()


def test_poll_frame_shape():
    f = make_poll(7, 12345)
    assert f[:1] == b"Q" and len(f) == 6
    assert int.from_bytes(f[3:6], "big") == 12345


# ---------------- --list-audio parsing ----------------
LIST_AUDIO = """\
MODEM73 build Jul 28 2026
Input  0devices:
  default - System Default
  0 - Discard all samples (playback) or generate zero samples (capture)
  1 - M73_TXA
  2 - M73_RXA
  3 - M73_TXB
  4 - M73_RXB
  17 - Loopback, Loopback PCM

Output devices:
  default - System Default
  0 - Discard all samples (playback) or generate zero samples (capture)
  1 - M73_TXA
  2 - M73_RXA
  3 - M73_TXB
  4 - M73_RXB
"""


def test_parse_list_audio_sections():
    inputs, outputs = parse_list_audio(LIST_AUDIO)
    assert inputs["M73_RXA"] == 2 and inputs["M73_RXB"] == 4
    assert outputs["M73_TXA"] == 1 and outputs["M73_TXB"] == 3


def test_parse_list_audio_empty():
    assert parse_list_audio("") == ({}, {})


# ---------------- adapter-level pure behavior ----------------
def _adapter():
    return Modem73Adapter(AdapterConfig.from_env(argv=["1024", "10"], env={}))


def test_scan_telemetry_collects_sn_and_bitrate():
    a = _adapter()
    a.scan_telemetry("A", "SN 12.5")
    a.scan_telemetry("A", "SN -3.0")
    a.scan_telemetry("B", "BITRATE (0) 1577 BPS")
    assert a.snrs == [12.5, -3.0]
    assert a.modes == [1577]


def test_receiver_answers_probe_and_poll(monkeypatch):
    """Feed the receive plane directly: A stores data chunks, answers Q with an
    ACK, and the sender side accepts that ACK."""
    a = _adapter()
    sent = []

    class FakeSock:
        def sendall(self, b):
            sent.append(b)
    a.kissA = FakeSock()
    a.mtu = 510
    a._rx_chunks = {}
    a._rx_total = 3
    a._handle_rx_frame("A", make_data(0, b"aa"))
    a._handle_rx_frame("A", make_data(2, b"cc"))
    a._handle_rx_frame("A", make_poll(1, 3))
    assert len(sent) == 1
    dec = KissDecoder()
    (ack,) = dec.feed(sent[0])
    a._handle_rx_frame("B", ack)
    base, acked = a._last_ack
    unacked = {s for s in range(3) if s >= base and s not in acked}
    assert unacked == {1}


def test_preclean_patterns_do_not_match_own_cmdline():
    a = _adapter()
    import re as _re
    own = "python3 -m skywave.adapters.modem73 4096 120"
    for pat in a.preclean_patterns():
        assert not _re.search(pat, own)
