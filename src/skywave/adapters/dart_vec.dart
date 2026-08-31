// dart_vec.dart -- VectorAdapter shim for HTCommander's DART modem.
//
// Driven by skywave/adapters/vector_dart.py. Three commands: `modes`,
// `encode`, `decode`; everything crosses the boundary as JSON on stdout plus
// raw float32/byte files, so the Python side owns all scoring.
//
// This file is skywave's. The DART modem itself is NOT vendored here: the
// Python adapter copies HTCommander's `src/lib/hamlib/dart_*.dart` into a
// scratch build directory next to this entrypoint at run time, so the modem
// under test is always the user's own checkout at its own commit, and skywave
// carries no third-party snapshot to drift. Those files import only dart:math
// and dart:typed_data -- no Flutter, no Bluetooth -- which is what makes this
// possible at all.
//
// WHY PAYLOADS COME IN FROM PYTHON. The VectorAdapter contract lets a decoder
// regenerate expected payloads from (seed, frame_idx) via its documented
// xorshift32. Reimplementing that here would add a second source of truth that
// can silently diverge -- a mismatch would look like a channel failure. Instead
// Python writes the payload bytes, this shim encodes exactly those, and on
// decode hands back what it recovered. All matching, and therefore all of
// `decoded` / `wrong_frame` / `false_decode`, is adjudicated in one place.
//
// WHY DECODE IS WINDOWED, NOT SLICED AT THE OFFSET. Each frame is handed to
// DART's own `decode()` over a window that starts before the frame does, so
// the modem's real chirp correlator has to FIND the burst. Slicing exactly at
// the sidecar offset would hand it perfect timing and quietly delete
// acquisition from the measurement -- and acquisition is the part of DART with
// known defects (a real-valued correlator whose peak scales as |cos phi|).
// `sync_count` reports how many windows the correlator locked in at all, so a
// detection failure is distinguishable from a decode failure.
//
// CRC GATING. `DartDecodeResult.payload` is populated whether or not the CRC
// passed, and DART's own link layer forgets to check it on control frames.
// This shim reports `crcOk` per frame and delivers bytes only when it is true,
// so `false_decode` measures the waveform rather than that bug.

import 'dart:convert';
import 'dart:io';
import 'dart:math' as math;
import 'dart:typed_data';

import 'dart_constellation.dart';
import 'dart_modem.dart';
import 'dart_ofdm.dart';

const int kSampleRate = 32000;

/// Modes in ladder order. Mode F sits at the bottom: it is a different
/// waveform, not a lower rung of the same one.
const List<DartMode> kModes = [
  DartMode.modeF,
  DartMode.mode0,
  DartMode.mode1,
  DartMode.mode2,
  DartMode.mode3,
  DartMode.mode4,
  DartMode.mode5,
];

String modeKey(DartMode m) => m == DartMode.modeF ? 'F' : '${m.index}';

DartMode? modeFromKey(String k) {
  for (final m in kModes) {
    if (modeKey(m) == k) return m;
  }
  return null;
}

// --------------------------------------------------------------- utilities

Int16List _f64ToPcm(Float64List x, int from, int to) {
  final n = to - from;
  final out = Int16List(n);
  for (int i = 0; i < n; i++) {
    final v = (x[from + i] * 32768.0).round();
    out[i] = v > 32767 ? 32767 : (v < -32768 ? -32768 : v);
  }
  return out;
}

void _writeF32(String path, Float64List x) {
  final bd = ByteData(x.length * 4);
  for (int i = 0; i < x.length; i++) {
    bd.setFloat32(i * 4, x[i], Endian.little);
  }
  File(path).writeAsBytesSync(bd.buffer.asUint8List());
}

Float64List _readF32(String path) {
  final bytes = File(path).readAsBytesSync();
  final bd = ByteData.sublistView(bytes);
  final n = bytes.length ~/ 4;
  final out = Float64List(n);
  for (int i = 0; i < n; i++) {
    out[i] = bd.getFloat32(i * 4, Endian.little);
  }
  return out;
}

double _rmsDbfs(Int16List pcm) {
  double s = 0;
  for (final v in pcm) {
    final d = v / 32768.0;
    s += d * d;
  }
  final rms = math.sqrt(s / math.max(pcm.length, 1));
  return rms > 0 ? 20 * math.log(rms) / math.ln10 : -120.0;
}

double _peakDbfs(Int16List pcm) {
  double p = 0;
  for (final v in pcm) {
    final d = (v / 32768.0).abs();
    if (d > p) p = d;
  }
  return p > 0 ? 20 * math.log(p) / math.ln10 : -120.0;
}

/// Occupied bandwidth, ITU-style: the band remaining after 0.5% of the total
/// spectral energy is excluded from each edge (the 99% occupied bandwidth).
/// MEASURED from real encoder output, per the contract's insistence that
/// geometry figures not come from a formula -- and here that matters more than
/// usual. DART's design doc states its frequency content is "strictly within
/// 400-2600 Hz" with "zero energy outside this range", which is true of a
/// single OFDM symbol (inactive bins are zeroed) but NOT of the emitted
/// waveform: symbols are concatenated with a 4-sample cyclic prefix and no
/// windowing, so every symbol boundary is a discontinuity and the transitions
/// splatter. Measured on a mode-2 frame: 93.7% of energy in 400-2600 Hz, 2.7%
/// below 400, 2.9% in 2600-4000, 0.7% above 4000 -- and the splatter is in the
/// BODY (3.5% above 2600 Hz) not the preamble (0.58%). A formula would have
/// reported 2200 Hz and hidden all of it.
double _occupiedBwHz(Int16List pcm, {double frac = 0.99}) {
  int n = 1;
  while (n < pcm.length && n < 32768) {
    n <<= 1;
  }
  n = math.min(n, 32768);
  final re = List<Complex>.filled(n, const Complex(0, 0));
  // Window only the occupied span, not the zero-padded tail: tapering across
  // padding would distort the very edges being measured.
  final int m = math.min(pcm.length, n);
  for (int i = 0; i < m; i++) {
    final w = m > 1 ? 0.5 * (1 - math.cos(2 * math.pi * i / (m - 1))) : 1.0;
    re[i] = Complex(pcm[i] / 32768.0 * w, 0.0);
  }
  final spec = DartOfdm.fftPublic(re, inverse: false);
  final half = n ~/ 2;
  double total = 0;
  final psd = Float64List(half);
  for (int k = 0; k < half; k++) {
    psd[k] = spec[k].magnitudeSquared;
    total += psd[k];
  }
  if (total <= 0) return 0.0;
  final double edge = total * (1.0 - frac) / 2.0;
  double acc = 0;
  int lo = 0, hi = half - 1;
  while (lo < hi && acc + psd[lo] < edge) {
    acc += psd[lo++];
  }
  acc = 0;
  while (hi > lo && acc + psd[hi] < edge) {
    acc += psd[hi--];
  }
  return (hi - lo + 1) * kSampleRate / n;
}

// ------------------------------------------------------------------ modes

Map<String, dynamic> _describe(DartMode mode, int payloadBytes) {
  final modem = DartModem();
  final params = DartModeParams.fromMode(mode);
  final payload = Uint8List(payloadBytes); // shape only; levels are payload-blind
  final pcm = modem.encode(payload: payload, mode: mode, seqNum: 0);
  final airS = pcm.length / kSampleRate;
  final rms = _rmsDbfs(pcm);
  final peak = _peakDbfs(pcm);
  return {
    'label': 'dart_m${modeKey(mode)}_b$payloadBytes',
    'label_base': 'dart_m${modeKey(mode)}_b$payloadBytes',
    'mode_key': modeKey(mode),
    'mode_id': mode.index,
    // Within-modem class, not the adapter name: the SC-FDMA ladder and the
    // constant-envelope fallback are different families and the frontier
    // census is per-family.
    'family': params.isFsk ? 'cpfsk' : 'ofdm',
    'payload_bytes': payloadBytes,
    'sample_rate': kSampleRate,
    'air_s': airS,
    'rms_dbfs': rms,
    'peak_dbfs': peak,
    'papr_db': peak - rms,
    'bandwidth_hz': _occupiedBwHz(pcm),
    // The check that ADJUDICATES the payload is the CRC-32 appended before
    // FEC. The 16-bit header CRC is a narrower, earlier check and is
    // deliberately not reported here (contract: report the payload check).
    'crc_bits': 32,
    'list_size': 1,
    // Net rate at THIS payload size -- the honest figure. DART's own mode
    // table quotes asymptotic rates only reached at maximum frame size.
    'nominal_bps': payloadBytes * 8.0 / airS,
    'description': params.description,
  };
}

// ----------------------------------------------------------------- encode

void _cmdEncode(Map<String, String> a) {
  final mode = modeFromKey(a['mode']!);
  if (mode == null) {
    stderr.writeln('dart_vec: unknown mode ${a['mode']}');
    exit(2);
  }
  final payloadBytes = int.parse(a['payload-bytes']!);
  final frames = int.parse(a['frames']!);
  final gapMs = double.parse(a['gap-ms'] ?? '300');
  final blob = File(a['payloads']!).readAsBytesSync();
  if (blob.length < frames * payloadBytes) {
    stderr.writeln('dart_vec: payload file holds ${blob.length} bytes, '
        'need ${frames * payloadBytes}');
    exit(2);
  }

  final gap = (gapMs * kSampleRate / 1000).round();
  final modem = DartModem();
  final bursts = <Int16List>[];
  for (int i = 0; i < frames; i++) {
    final p = Uint8List.sublistView(blob, i * payloadBytes, (i + 1) * payloadBytes);
    bursts.add(modem.encode(
        payload: Uint8List.fromList(p), mode: mode, seqNum: i & 0xFF));
  }
  int total = gap;
  for (final b in bursts) {
    total += b.length + gap;
  }
  final vec = Float64List(total);
  final offsets = <int>[];
  final lengths = <int>[];
  int at = gap;
  for (final b in bursts) {
    for (int i = 0; i < b.length; i++) {
      vec[at + i] = b[i] / 32768.0;
    }
    offsets.add(at);
    lengths.add(b.length);
    at += b.length + gap;
  }
  _writeF32(a['out']!, vec);
  stdout.writeln(jsonEncode({
    'frames': frames,
    'sample_rate': kSampleRate,
    'payload_bytes': payloadBytes,
    'frame_offsets': offsets,
    'frame_lengths': lengths,
    'vector_len': total,
    'gap_samples': gap,
  }));
}

// ----------------------------------------------------------------- decode

void _cmdDecode(Map<String, String> a) {
  final meta = jsonDecode(File(a['meta']!).readAsStringSync())
      as Map<String, dynamic>;
  final offsets = (meta['frame_offsets'] as List).cast<int>();
  final lengths = (meta['frame_lengths'] as List).cast<int>();
  final gap = meta['gap_samples'] as int;
  final cold = (a['cold'] ?? '0') == '1';
  final vec = _readF32(a['in']!);

  // Search window: start a little before the frame so the correlator has to
  // acquire, and run a little past its end so the last OFDM symbol and the
  // codec/filter tail are inside the buffer.
  final lead = math.min(gap, (0.15 * kSampleRate).round());
  final trail = math.min(gap, (0.10 * kSampleRate).round());

  DartModem? warm;
  final out = <Map<String, dynamic>>[];
  int syncCount = 0;
  for (int i = 0; i < offsets.length; i++) {
    final from = math.max(0, offsets[i] - lead);
    final to = math.min(vec.length, offsets[i] + lengths[i] + trail);
    final pcm = _f64ToPcm(vec, from, to);
    // cold: a receiver with NO memory of the previous frame. Constructed
    // fresh, never reset -- a reset can leave last-good state behind.
    final modem = cold ? DartModem() : (warm ??= DartModem());
    DartDecodeResult? r;
    try {
      r = modem.decode(pcm);
    } catch (_) {
      r = null;
    }
    if (r == null) {
      out.add({'frame': i, 'sync': false});
      continue;
    }
    syncCount++;
    out.add({
      'frame': i,
      'sync': true,
      'crc_ok': r.crcOk,
      'mode_index': r.header.modeIndex,
      'seq': r.header.seqNum,
      // Bytes are delivered ONLY on a CRC pass; see the header note.
      'payload': r.crcOk ? base64Encode(r.payload) : null,
      'ldpc_corrections': r.ldpcCorrections,
      'evm_percent': r.quality.evmPercent,
      'snr_db': r.quality.snrDb,
      'preamble_corr': r.quality.preambleCorrelation,
      'phase_drift_deg': r.phaseDriftDeg,
    });
  }
  stdout.writeln(jsonEncode({
    'frames': offsets.length,
    'sync_count': syncCount,
    'results': out,
  }));
}

// ------------------------------------------------------------------- main

Map<String, String> _parse(List<String> argv) {
  final m = <String, String>{};
  for (int i = 0; i < argv.length; i++) {
    if (!argv[i].startsWith('--')) continue;
    final k = argv[i].substring(2);
    if (i + 1 < argv.length && !argv[i + 1].startsWith('--')) {
      m[k] = argv[++i];
    } else {
      m[k] = '1';
    }
  }
  return m;
}

void main(List<String> argv) {
  if (argv.isEmpty) {
    stderr.writeln('usage: dart_vec <modes|encode|decode> [--flags]');
    exit(2);
  }
  final cmd = argv.first;
  final a = _parse(argv.sublist(1));
  switch (cmd) {
    case 'modes':
      final pb = int.parse(a['payload-bytes'] ?? '64');
      stdout.writeln(jsonEncode(
          {'modes': [for (final m in kModes) _describe(m, pb)]}));
      break;
    case 'encode':
      _cmdEncode(a);
      break;
    case 'decode':
      _cmdDecode(a);
      break;
    default:
      stderr.writeln('dart_vec: unknown command $cmd');
      exit(2);
  }
}
