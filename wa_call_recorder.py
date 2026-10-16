#!/usr/bin/env python3
"""wa-call-recorder — record a WhatsApp (or any) call locally on Windows.

Captures two tracks at once, entirely on this machine:
  * remote side — WASAPI loopback of the output device the call plays through
  * local side  — your microphone

and mixes them into one timestamped WAV. Nothing is uploaded, WhatsApp is
never contacted, and no recording indicator exists for local OS capture.

Usage:
  python wa_call_recorder.py devices
  python wa_call_recorder.py selftest                     # prove it works
  python wa_call_recorder.py record                       # Ctrl+C to stop
  python wa_call_recorder.py record --duration 60 -o call.wav
  python wa_call_recorder.py record --loopback 16 --mic 1 --keep-raw
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import os
import signal
import struct
import sys
import tempfile
import threading
import time
import wave

import numpy as np
import pyaudiowpatch as pya

CHUNK = 1024            # frames per PortAudio read (~21 ms @ 48 kHz)
MIX_BLOCK = 4096        # frames per mixing block
STATUS_INTERVAL = 0.5   # seconds between status updates
REMOTE_WARN_AFTER = 5.0 # seconds with no remote audio before warning
JOIN_TIMEOUT = 5.0      # seconds to wait for reader threads to finish


def _out(msg: str) -> None:
    print(msg, flush=True)


def _scan_data_chunk(path: str):
    """Return (byte offset, declared size) of a RIFF/WAVE file's data chunk."""
    with open(path, "rb") as f:
        head = f.read(12)
        if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            raise ValueError(f"not a WAV file: {path}")
        pos = 12
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                raise ValueError(f"no data chunk in {path}")
            cid = hdr[:4]
            size = struct.unpack("<I", hdr[4:8])[0]
            if cid == b"data":
                return pos + 8, size
            pos += 8 + size + (size & 1)
            f.seek(pos)


def read_track(path: str):
    """Read one track as (stereo int16 array, sample rate).

    Frames are counted from the real file length rather than the header: the
    capture threads use `writeframesraw`, so a run killed before it could close
    its file leaves a stale frame count behind — while every sample after it is
    still perfectly good. Counting from the file size recovers that audio.
    """
    with wave.open(path, "rb") as w:
        rate, channels, width = (w.getframerate(), w.getnchannels(),
                                 w.getsampwidth())
    if width != 2:
        raise ValueError(f"{path}: only 16-bit PCM is supported")
    offset, _declared = _scan_data_chunk(path)
    frame_bytes = 2 * max(1, channels)
    frames = max(0, os.path.getsize(path) - offset) // frame_bytes
    with open(path, "rb") as f:
        f.seek(offset)
        raw = f.read(frames * frame_bytes)
    return _stereo(raw, channels), rate


def _resample_stereo(a, src_rate: int, dst_rate: int):
    """Linear-resample a stereo block in memory (rates can differ per device)."""
    if src_rate == dst_rate or a.shape[0] == 0:
        return a
    n = a.shape[0]
    n_out = max(1, int(round(n * dst_rate / src_rate)))
    pos = np.clip(np.arange(n_out, dtype=np.float64) * src_rate / dst_rate,
                  0.0, n - 1.0)
    src_t = np.arange(n, dtype=np.float64)
    out = np.empty((n_out, 2), dtype=np.float32)
    for c in range(2):
        out[:, c] = np.interp(pos, src_t, a[:, c].astype(np.float32))
    return out


def mix_arrays(spk, spk_rate: int, mic, mic_rate: int,
               spk_gain: float, mic_gain: float):
    """Sum the two tracks with gains, padding the shorter one with silence."""
    mic = _resample_stereo(mic, mic_rate, spk_rate)
    n = max(spk.shape[0], mic.shape[0])
    if n == 0:
        return np.zeros((0, 2), dtype=np.int16)
    mix = (_pad(spk, n).astype(np.float32) * float(spk_gain)
           + _pad(mic, n).astype(np.float32) * float(mic_gain))
    np.clip(mix, -32768.0, 32767.0, out=mix)
    return mix.astype(np.int16)


def write_stereo(path: str, arr, rate: int) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(arr.tobytes())


def recover_stale_temps(directory: str, keep: set, prefix: str = "call_") -> int:
    """Rebuild interrupted recordings left as orphaned *.tmp.wav track pairs.

    A run killed before it could mix leaves its raw tracks behind with stale
    headers. Deleting those would throw away real audio, so mix them into a
    playable file instead and report where it went.
    """
    try:
        entries = os.listdir(directory)
    except OSError:
        return 0
    groups: dict = {}
    for name in entries:
        if not (name.startswith(prefix) and ".tmp.wav" in name.lower()):
            continue
        path = os.path.join(directory, name)
        if os.path.abspath(path) in keep:
            continue
        groups.setdefault(name.split(".wav")[0], []).append(path)

    recovered = 0
    for root, paths in sorted(groups.items()):
        spk = next((p for p in paths if "spk" in os.path.basename(p).lower()), None)
        mic = next((p for p in paths if "mic" in os.path.basename(p).lower()), None)
        out = os.path.join(directory, f"{root}_recovered.wav")
        try:
            if spk and mic:
                s, sr = read_track(spk)
                m, mr = read_track(mic)
                if s.shape[0] == 0 and m.shape[0] == 0:
                    raise ValueError("both tracks are empty")
                arr, rate = mix_arrays(s, sr, m, mr, 1.0, 1.0), sr
            else:
                src = spk or mic
                arr, rate = read_track(src)
                if arr.shape[0] == 0:
                    raise ValueError("track is empty")
            n = 1
            while os.path.exists(out):
                n += 1
                out = os.path.join(directory, f"{root}_recovered_{n}.wav")
            write_stereo(out, arr, rate)
        except Exception as e:  # noqa: BLE001
            _out(f"note: could not recover {root}: {e}")
            continue
        for p in paths:
            try:
                os.remove(p)
            except OSError:
                pass
        recovered += 1
        _out(f"recovered an interrupted recording → {out}")
    return recovered


# --------------------------------------------------------------------------- devices


def cmd_devices(_args) -> int:
    with pya.PyAudio() as p:
        try:
            default_in = p.get_default_input_device_info()["index"]
        except Exception:
            default_in = None
        try:
            default_lb = p.get_default_wasapi_loopback()["index"]
        except Exception:
            default_lb = None

        print("Microphones (use with --mic):")
        found = False
        for i in range(p.get_device_count()):
            d = p.get_device_info_by_index(i)
            if d.get("maxInputChannels", 0) and not d.get("isLoopbackDevice"):
                found = True
                mark = "   *default*" if i == default_in else ""
                print(f"  [{i}] {d['name']}  {int(d['defaultSampleRate'])} Hz{mark}")
        if not found:
            print("  (none)")

        print("\nLoopback outputs (use with --loopback):")
        lbs = list(p.get_loopback_device_info_generator())
        if not lbs:
            print("  (none)")
        for d in lbs:
            mark = "   *default output*" if d["index"] == default_lb else ""
            print(f"  [{d['index']}] {d['name']}  {int(d['defaultSampleRate'])} Hz{mark}")

        print("\nTip: record the loopback of the device WhatsApp Desktop plays")
        print("     through (usually the one marked *default output*).")
    return 0


def pick_loopback(p, want=None):
    lbs = list(p.get_loopback_device_info_generator())
    if not lbs:
        raise SystemExit("error: no WASAPI loopback devices found")
    if want is not None:
        for d in lbs:
            if d["index"] == want:
                return d
        raise SystemExit(
            f"error: device [{want}] is not a loopback device — "
            "run `python wa_call_recorder.py devices` for valid indexes")
    try:
        return p.get_default_wasapi_loopback()
    except Exception:
        return lbs[0]


def pick_mic(p, want=None):
    mics = []
    for i in range(p.get_device_count()):
        d = p.get_device_info_by_index(i)
        if d.get("maxInputChannels", 0) and not d.get("isLoopbackDevice"):
            mics.append(d)
    if not mics:
        raise SystemExit("error: no microphone found")
    if want is not None:
        for d in mics:
            if d["index"] == want:
                return d
        raise SystemExit(
            f"error: device [{want}] is not a microphone — "
            "run `python wa_call_recorder.py devices` for valid indexes")
    try:
        return p.get_default_input_device_info()
    except Exception:
        return mics[0]


# --------------------------------------------------------------------------- capture


class Reader(threading.Thread):
    """Reads one capture device into a WAV file until told to stop."""

    def __init__(self, pa, label, device_index, rate, channels, path):
        super().__init__(daemon=True, name=f"rec-{label}")
        self.pa = pa
        self.label = label
        self.device_index = device_index
        self.rate = rate
        self.channels = channels
        self.path = path
        self.stop_event = threading.Event()
        self.bytes_written = 0
        self.error = None
        self._stream = None

    def run(self):
        try:
            stream = self.pa.open(
                format=pya.paInt16,
                channels=self.channels,
                rate=self.rate,
                input=True,
                input_device_index=self.device_index,
                frames_per_buffer=CHUNK,
            )
        except Exception as e:  # noqa: BLE001
            self.error = f"could not open device: {e!r}"
            return
        self._stream = stream
        try:
            with wave.open(self.path, "wb") as wf:
                wf.setnchannels(self.channels)
                wf.setsampwidth(2)
                wf.setframerate(self.rate)
                while not self.stop_event.is_set():
                    # blocks while the endpoint is idle (e.g. Bluetooth
                    # headphones asleep); stop_now()'s abort_stream()
                    # unblocks it. When audio is playing on the endpoint
                    # (i.e. during a call) data flows continuously.
                    data = stream.read(CHUNK, exception_on_overflow=False)
                    if not data:
                        break
                    wf.writeframesraw(data)
                    self.bytes_written += len(data)
        except Exception as e:  # noqa: BLE001
            if not self.stop_event.is_set():
                self.error = f"capture failed: {e!r}"
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def stop_now(self):
        self.stop_event.set()
        if self._stream is not None:
            try:
                self._stream.abort_stream()
            except Exception:
                pass


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def track_frames(path: str) -> int:
    try:
        with wave.open(path, "rb") as w:
            return w.getnframes()
    except Exception:
        return 0


# --------------------------------------------------------------------------- mixing


def _stereo(data: bytes, channels: int) -> np.ndarray:
    if not data:
        return np.zeros((0, 2), dtype=np.int16)
    a = np.frombuffer(data, dtype=np.int16)
    if a.size % channels:
        a = a[: a.size - a.size % channels]
    a = a.reshape(-1, channels)
    if channels == 1:
        a = np.repeat(a, 2, axis=1)
    elif channels > 2:
        a = a[:, :2]
    return a


def _pad(a: np.ndarray, n: int) -> np.ndarray:
    if a.shape[0] >= n:
        return a[:n]
    return np.concatenate(
        [a, np.zeros((n - a.shape[0], 2), dtype=np.int16)], axis=0)


def resample_track(src: str, dst: str, src_rate: int, dst_rate: int) -> None:
    """Whole-file linear resample of one track (fallback when rates differ)."""
    with wave.open(src, "rb") as w:
        channels = w.getnchannels()
        n = w.getnframes()
        raw = w.readframes(n)
    if n == 0:
        # just create an empty stereo wav at dst_rate
        with wave.open(dst, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(dst_rate)
        return
    a = _stereo(raw, channels).astype(np.float32)
    n_out = max(1, int(round(n * dst_rate / src_rate)))
    pos = np.clip(np.arange(n_out, dtype=np.float64) * src_rate / dst_rate,
                  0.0, n - 1.0)
    src_t = np.arange(n, dtype=np.float64)
    with wave.open(dst, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(dst_rate)
        for start in range(0, n_out, MIX_BLOCK):
            sl = slice(start, min(start + MIX_BLOCK, n_out))
            out = np.empty((sl.stop - sl.start, 2), dtype=np.float32)
            for c in range(2):
                out[:, c] = np.interp(pos[sl], src_t, a[:, c])
            w.writeframesraw(np.clip(out, -32768.0, 32767.0).astype(np.int16).tobytes())


def mix_tracks(spk_path, spk_rate, spk_ch,
               mic_path, mic_rate, mic_ch,
               out_path, spk_gain, mic_gain) -> None:
    """Mix the remote (speakers loopback) track and the mic track into out_path."""
    resampled = None
    if mic_path is not None and mic_rate != spk_rate and track_frames(mic_path) > 0:
        resampled = out_path + ".resample.tmp.wav"
        resample_track(mic_path, resampled, mic_rate, spk_rate)
        mic_path, mic_rate = resampled, spk_rate
    try:
        with wave.open(spk_path, "rb") as s, \
                wave.open(mic_path, "rb") as m, \
                wave.open(out_path, "wb") as o:
            o.setnchannels(2)
            o.setsampwidth(2)
            o.setframerate(spk_rate)
            while True:
                ds = s.readframes(MIX_BLOCK)
                dm = m.readframes(MIX_BLOCK)
                if not ds and not dm:
                    break
                a = _stereo(ds, spk_ch)
                b = _stereo(dm, mic_ch)
                n = max(a.shape[0], b.shape[0])
                if n == 0:
                    continue
                mix = (_pad(a, n).astype(np.float32) * float(spk_gain)
                       + _pad(b, n).astype(np.float32) * float(mic_gain))
                np.clip(mix, -32768.0, 32767.0, out=mix)
                o.writeframesraw(mix.astype(np.int16).tobytes())
    finally:
        if resampled:
            try:
                os.remove(resampled)
            except OSError:
                pass


# --------------------------------------------------------------------------- record


def cmd_record(args) -> int:
    pa = pya.PyAudio()
    spk = mic_r = None
    out_path = args.output
    spk_tmp = mic_tmp = None
    rc = 0
    try:
        lb = pick_loopback(pa, args.loopback)
        mic = None if args.no_mic else pick_mic(pa, args.mic)

        lb_rate = int(lb["defaultSampleRate"])
        lb_ch = max(1, int(lb.get("maxInputChannels") or 2))
        if mic is None:
            mic_ch, mic_rate = 0, 0
        else:
            mic_ch = min(2, max(1, int(mic.get("maxInputChannels") or 1)))
            mic_rate = int(mic["defaultSampleRate"])
            if mic_rate != lb_rate:
                try:
                    if pa.is_format_supported(lb_rate, input_device=mic["index"],
                                              input_channels=mic_ch,
                                              input_format=pya.paInt16):
                        mic_rate = lb_rate
                except Exception:
                    pass  # keep native rate; resampled at mix time

        if not out_path:
            out_dir = os.path.join(os.getcwd(), "recordings")
            os.makedirs(out_dir, exist_ok=True)
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            out_path = os.path.join(out_dir, f"call_{stamp}.wav")
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        spk_tmp = out_path + ".spk.tmp.wav"
        mic_tmp = None if mic is None else out_path + ".mic.tmp.wav"

        # rebuild anything a previous run was killed before it could mix
        recovered = recover_stale_temps(
            os.path.dirname(os.path.abspath(out_path)),
            keep={os.path.abspath(p) for p in (spk_tmp, mic_tmp) if p})
        if recovered:
            _out(f"  (use `remix` on any *recovered.wav to rebalance it)")

        _out(f"Recording → {out_path}")
        _out(f"  remote/speakers loopback: {lb['name']}  ({lb_rate} Hz, {lb_ch} ch)")
        if mic is None:
            _out("  local/mic:                (disabled — remote side only)")
        else:
            _out(f"  local/mic:                {mic['name']}  ({mic_rate} Hz, {mic_ch} ch)")
        _out(f"  duration: {args.duration:g} s  (Ctrl+C stops early)"
             if args.duration else "  press Ctrl+C to stop")

        spk = Reader(pa, "speakers", lb["index"], lb_rate, lb_ch, spk_tmp)
        if mic is not None:
            mic_r = Reader(pa, "mic", mic["index"], mic_rate, mic_ch, mic_tmp)
        spk.start()
        if mic_r is not None:
            mic_r.start()

        stop = threading.Event()

        def _request_stop(*_a):
            stop.set()

        # SIGINT (Ctrl+C) is the documented stop; also honour SIGTERM/SIGBREAK so
        # a polite kill or Ctrl+Break still finalizes the WAV instead of leaving
        # orphaned .tmp.wav tracks behind.
        stoppable = [signal.SIGINT]
        for name in ("SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is not None and sig not in stoppable:
                stoppable.append(sig)
        previous_handlers = []
        for sig in stoppable:
            try:
                previous_handlers.append((sig, signal.signal(sig, _request_stop)))
            except (ValueError, OSError):
                pass
        start = time.monotonic()
        mic_bytes = 0
        try:
            while not stop.is_set():
                elapsed = time.monotonic() - start
                if args.duration and elapsed >= args.duration:
                    break
                if mic_r is not None:
                    mic_bytes = mic_r.bytes_written
                line = (f"\r  [{elapsed:6.1f}s]  remote "
                        f"{_fmt_bytes(spk.bytes_written):>9}  |  mic "
                        f"{_fmt_bytes(mic_bytes):>9}  ")
                if (elapsed > REMOTE_WARN_AFTER and spk.bytes_written == 0
                        and spk.error is None):
                    line += " (no remote audio yet — check output device)"
                sys.stdout.write(line)
                sys.stdout.flush()
                if not spk.is_alive() and not (mic_r and mic_r.is_alive()):
                    break
                time.sleep(STATUS_INTERVAL)
        finally:
            for sig, previous_handler in previous_handlers:
                try:
                    signal.signal(sig, previous_handler)
                except (ValueError, OSError):
                    pass
        sys.stdout.write("\n")

        spk.stop_now()
        if mic_r is not None:
            mic_r.stop_now()
        spk.join(timeout=JOIN_TIMEOUT)
        if mic_r is not None:
            mic_r.join(timeout=JOIN_TIMEOUT)
        for r in (spk, mic_r):
            if r is not None:
                if r.is_alive():
                    _out(f"warning: {r.label} reader did not stop cleanly")
                if r.error:
                    _out(f"warning: {r.label}: {r.error}")

        spk_frames = track_frames(spk_tmp)
        mic_frames = 0 if mic_r is None else track_frames(mic_tmp)
        if spk_frames == 0 and mic_frames == 0:
            _out("error: nothing was captured")
            rc = 1
            return rc

        if mic_r is None:
            # remote-only recording: keep it as-is (rename)
            try:
                os.remove(out_path)
            except OSError:
                pass
            os.replace(spk_tmp, out_path)
            spk_tmp = None
        else:
            mix_tracks(spk_tmp, lb_rate, lb_ch, mic_tmp, mic_rate, mic_ch,
                       out_path, args.spk_gain, args.mic_gain)

        if os.path.exists(out_path):
            _out(f"\nSaved {out_path}  "
                 f"({max(spk_frames / lb_rate if spk_frames else 0.0, mic_frames / mic_rate if mic_frames else 0.0):.1f} s)")
            if spk_frames == 0:
                _out("warning: remote side was silent — WhatsApp is probably")
                _out("         playing through a different output device; pick")
                _out("         a matching loopback with --loopback (see `devices`)")
            if mic_r is not None and mic_frames == 0:
                _out("warning: microphone track is empty")
        return rc
    finally:
        for r in (spk, mic_r):
            if r is not None:
                r.stop_now()
        for tmp, suffix in ((spk_tmp, ".speakers.wav"), (mic_tmp, ".mic.wav")):
            if tmp and os.path.exists(tmp):
                if args.keep_raw:
                    try:
                        os.replace(tmp, out_path + suffix)
                    except OSError:
                        pass
                else:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
        pa.terminate()


def cmd_remix(args) -> int:
    """Mix two kept tracks into a fresh recording.

    This is how you salvage an interrupted run: the .tmp.wav tracks it left
    behind still hold the audio, and the gains here are applied after the fact.
    """
    a, b = args.tracks
    # the tracks are interchangeable; use the filenames to tell them apart
    if "mic" in os.path.basename(a).lower() and \
            "mic" not in os.path.basename(b).lower():
        a, b = b, a
    for p in (a, b):
        if not os.path.exists(p):
            _out(f"error: no such file: {p}")
            return 1
    try:
        spk, spk_rate = read_track(a)
        mic, mic_rate = read_track(b)
    except Exception as e:  # noqa: BLE001
        _out(f"error: {e}")
        return 1
    _out(f"  remote/speakers: {a}  "
         f"({spk.shape[0] / spk_rate:.1f} s, {spk_rate} Hz)")
    _out(f"  local/mic:       {b}  "
         f"({mic.shape[0] / mic_rate:.1f} s, {mic_rate} Hz)")
    if max(spk.shape[0], mic.shape[0]) == 0:
        _out("error: both tracks are empty")
        return 1
    mixed = mix_arrays(spk, spk_rate, mic, mic_rate, args.spk_gain, args.mic_gain)
    out_dir = os.path.dirname(os.path.abspath(args.output))
    os.makedirs(out_dir, exist_ok=True)
    write_stereo(args.output, mixed, spk_rate)
    _out(f"Saved {args.output}  ({mixed.shape[0] / spk_rate:.1f} s)")
    return 0


# --------------------------------------------------------------------------- selftest

SELFTEST_TONE_HZ = 1000.0        # tone we play and then look for
SELFTEST_TONE_SEC = 1.4          # how long it plays
SELFTEST_LEAD_SEC = 0.4          # silence captured before it starts
SELFTEST_TAIL_SEC = 0.6          # capture kept after it ends
SELFTEST_TONE_FROM = SELFTEST_LEAD_SEC + 0.2   # earliest the tone can appear
ANALYSIS_FRAME = 4096            # samples per analysis window (~85 ms @ 48 kHz)
TONE_BAND = (900.0, 1100.0)      # the tone must show up in this band
NOISE_BAND = (3000.0, 3400.0)    # control band the tone must NOT fill
SPEAK_TEST_SEC = 3.0             # microphone-only recheck


def _db(x: float) -> float:
    return 20.0 * math.log10(max(float(x), 1e-9))


def _tone_samples(rate: int, freq: float, seconds: float, amp: float = 0.35):
    """One sine burst as int16, faded at both ends so it doesn't click."""
    n = max(1, int(rate * seconds))
    t = np.arange(n, dtype=np.float64) / rate
    x = np.sin(2.0 * np.pi * freq * t) * amp
    fade = min(int(0.01 * rate), n // 2)
    if fade:
        ramp = np.linspace(0.0, 1.0, fade)
        x[:fade] *= ramp
        x[-fade:] *= ramp[::-1]
    return np.clip(x * 32767.0, -32768.0, 32767.0).astype(np.int16)


def _paired_output_index(p, lb):
    """The output endpoint a loopback device mirrors, so we play on the right one."""
    name = str(lb.get("name", ""))
    base = name[:-len(" [Loopback]")] if name.endswith(" [Loopback]") else name
    for i in range(p.get_device_count()):
        d = p.get_device_info_by_index(i)
        if d.get("isLoopbackDevice") or not d.get("maxOutputChannels", 0):
            continue
        if str(d.get("name")) == base:
            return i, str(d.get("name")), int(d.get("maxOutputChannels") or 2)
    try:
        d = p.get_default_output_device_info()
        return d["index"], str(d.get("name")), int(d.get("maxOutputChannels") or 2)
    except Exception:  # noqa: BLE001
        return None, None, 0


def _spectrum_series(mono, rate: int, lo: float, hi: float):
    """Per-window peak magnitude in [lo, hi] Hz, window RMS, and window times."""
    if mono.shape[0] < ANALYSIS_FRAME:
        return None
    window = np.hanning(ANALYSIS_FRAME).astype(np.float32)
    freqs = np.fft.rfftfreq(ANALYSIS_FRAME, 1.0 / rate)
    sel = (freqs >= lo) & (freqs <= hi)
    if not sel.any():
        return None
    step = ANALYSIS_FRAME // 2
    count = 1 + (mono.shape[0] - ANALYSIS_FRAME) // step
    band = np.empty(count, dtype=np.float32)
    rms = np.empty(count, dtype=np.float32)
    times = np.empty(count, dtype=np.float32)
    for k in range(count):
        i = k * step
        seg = mono[i:i + ANALYSIS_FRAME].astype(np.float32) / 32768.0
        rms[k] = math.sqrt(float(np.mean(seg * seg)))
        band[k] = float(np.abs(np.fft.rfft(seg * window))[sel].max())
        times[k] = (i + ANALYSIS_FRAME / 2.0) / rate
    return band, rms, times


def analyse_capture(path: str) -> dict:
    """Describe what a captured track contains, for the tone self-test.

    Measures the level in the tone's own narrow band before and during the
    beep, and compares it against an unrelated band so broadband noise can't
    masquerade as the tone.
    """
    arr, rate = read_track(path)
    if arr.shape[0] < ANALYSIS_FRAME:
        raise ValueError("too few frames captured to analyse")
    best = None
    for ch in range(int(arr.shape[1])):
        series = _spectrum_series(arr[:, ch], rate, *TONE_BAND)
        if series is None:
            continue
        band, rms, times = series
        ctrl = _spectrum_series(arr[:, ch], rate, *NOISE_BAND)
        ctrl_band = ctrl[0] if ctrl is not None else np.zeros_like(band)
        if best is None or float(band.max()) > best[0]:
            best = (float(band.max()), band, rms, times, ctrl_band)
    if best is None:
        raise ValueError("no analysable frames in the capture")
    _, band, rms, times, ctrl = best

    quiet = times < SELFTEST_TONE_FROM
    loud = ~quiet
    if not loud.any():
        loud = np.ones_like(times, dtype=bool)
    # With no pre-tone frames (an endpoint that stays idle until something
    # plays) fall back to the quietest frames rather than the whole file.
    base = (float(np.percentile(band[quiet], 90)) if quiet.any()
            else float(np.percentile(band, 10)))
    peak = float(np.percentile(band[loud], 95))
    ctrl_peak = float(np.percentile(ctrl[loud], 95))
    return {
        "rate": rate,
        "seconds": arr.shape[0] / rate,
        "base_db": _db(base),
        "peak_db": _db(peak),
        "jump_db": _db(peak) - _db(base),
        "rms": float(np.percentile(rms[loud], 95)),
        "specificity": (peak / ctrl_peak) if ctrl_peak > 0 else float("inf"),
    }


def _tone_is_clear(m: dict) -> bool:
    """Unambiguous either by a big jump over the baseline or sheer level —
    but only if the energy sits in the tone's band and not across the spectrum,
    so loud broadband noise can't be mistaken for the beep."""
    return (m["specificity"] >= 3.0 and m["rms"] >= 0.002
            and (m["rms"] >= 0.02 or m["jump_db"] >= 15.0))


def _play_tone(pa, device_index, channels, rate):
    """Play the test tone on one output endpoint; returns None or an error."""
    stream = None
    try:
        channels = min(2, max(1, int(channels)))
        stream = pa.open(format=pya.paInt16, channels=channels, rate=rate,
                         output=True, output_device_index=device_index,
                         frames_per_buffer=CHUNK)
        tone = _tone_samples(rate, SELFTEST_TONE_HZ, SELFTEST_TONE_SEC)
        if channels > 1:
            tone = np.repeat(tone.reshape(-1, 1), channels, axis=1).ravel()
        stream.write(tone.tobytes())
        return None
    except Exception as e:  # noqa: BLE001
        return repr(e)
    finally:
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass


def cmd_selftest(args) -> int:
    """Play a known tone and prove this machine actually captured it.

    The point is to fail *before* the call rather than after: bytes flowing is
    not the same as audio recorded — a muted or idle endpoint happily delivers
    silence at full data rate, which is exactly how a 107 s call came back
    empty once.
    """
    pa = pya.PyAudio()
    spk_r = mic_r = None
    stem = os.path.join(tempfile.gettempdir(), f"wa_selftest_{os.getpid()}")
    tmp_spk, tmp_mic, tmp_speak = (f"{stem}.spk.wav", f"{stem}.mic.wav",
                                   f"{stem}.speak.wav")
    temps = [tmp_spk, tmp_mic, tmp_speak]
    rc = 1
    try:
        lb = pick_loopback(pa, args.loopback)
        lb_rate = int(lb["defaultSampleRate"])
        lb_ch = max(1, int(lb.get("maxInputChannels") or 2))
        out_i, out_name, out_ch = _paired_output_index(pa, lb)

        _out("Self-test: play a tone, then prove this machine captured it")
        _out(f"  capture loopback: {lb['name']}  ({lb_rate} Hz, {lb_ch} ch)")
        if out_i is None:
            _out("  play the tone on: (no output endpoint found)")
            _out("\nFAIL — there is no output device to play the test tone on.")
            return 1
        _out(f"  play the tone on: {out_name}  [{out_i}]")
        _out(f"  tone: {SELFTEST_TONE_HZ:.0f} Hz for {SELFTEST_TONE_SEC:g} s "
             "— you should hear a beep")

        mic_info = None if args.no_mic else pick_mic(pa, args.mic)
        mic_rate = mic_ch = 0
        if mic_info is not None:
            mic_rate = int(mic_info["defaultSampleRate"])
            mic_ch = min(2, max(1, int(mic_info.get("maxInputChannels") or 1)))

        spk_r = Reader(pa, "selftest", lb["index"], lb_rate, lb_ch, tmp_spk)
        spk_r.start()
        if mic_info is not None:
            mic_r = Reader(pa, "mic", mic_info["index"], mic_rate, mic_ch,
                           tmp_mic)
            mic_r.start()

        time.sleep(SELFTEST_LEAD_SEC)
        play_errors: list = []

        def _run_tone():
            err = _play_tone(pa, out_i, out_ch, lb_rate)
            if err:
                play_errors.append(err)

        player = threading.Thread(target=_run_tone, daemon=True,
                                  name="selftest-tone")
        player.start()
        player.join(timeout=SELFTEST_TONE_SEC + 5.0)
        time.sleep(SELFTEST_TAIL_SEC)

        spk_r.stop_now()
        if mic_r is not None:
            mic_r.stop_now()
        spk_r.join(timeout=JOIN_TIMEOUT)
        if mic_r is not None:
            mic_r.join(timeout=JOIN_TIMEOUT)
        for r in (spk_r, mic_r):
            if r is not None and r.error:
                _out(f"warning: {r.label}: {r.error}")
        if play_errors:
            _out(f"warning: could not play the tone: {play_errors[0]}")

        # ---- remote side: the loopback verdict that decides everything
        try:
            res = analyse_capture(tmp_spk)
        except Exception as e:  # noqa: BLE001
            _out(f"\nFAIL — could not analyse the captured audio: {e}")
            return 1
        remote_ok = _tone_is_clear(res)
        _out("")
        _out(f"  captured {res['seconds']:.2f} s at {res['rate']} Hz")
        _out(f"  silence baseline   {res['base_db']:7.1f} dB")
        _out(f"  1 kHz tone band    {res['peak_db']:7.1f} dB  "
             f"(+{res['jump_db']:.1f} dB over silence)")
        _out(f"  tone vs broadband  {res['specificity']:7.1f}x")
        if remote_ok:
            _out("\nPASS — the loopback captured the test tone, so the remote")
            _out("       side of a call will be recorded.")
        else:
            _out("\nFAIL — the test tone did not reach the captured output.")
            if res["specificity"] < 3.0:
                _out("       What arrived looked like broadband noise, not a tone.")
            _out("       Check that this is the output calls play through, that it")
            _out("       is not muted, and that its volume is up.")

        # ---- local side: did the mic capture anything at all?
        mic_ok = None
        if mic_info is None:
            _out("\n  (microphone check skipped)")
        else:
            _out(f"\n  microphone: {mic_info['name']}")
            try:
                heard = _tone_is_clear(analyse_capture(tmp_mic))
            except Exception:  # noqa: BLE001
                heard = False
            if heard:
                mic_ok = True
                _out("  the mic also heard the beep — both capture paths work.")
            else:
                _out("  the mic did not hear the beep (normal with headphones);")
                _out(f"  recording {SPEAK_TEST_SEC:g} s to verify it anyway.")
                _out("  >>> say something out loud now <<<")
                speak = Reader(pa, "mic2", mic_info["index"], mic_rate, mic_ch,
                               tmp_speak)
                speak.start()
                end = time.monotonic() + SPEAK_TEST_SEC
                while time.monotonic() < end:
                    sys.stdout.write(
                        f"\r  recording {end - time.monotonic():4.1f}s ... ")
                    sys.stdout.flush()
                    time.sleep(0.2)
                sys.stdout.write("\r" + " " * 32 + "\r")
                speak.stop_now()
                speak.join(timeout=JOIN_TIMEOUT)
                peak = 0
                try:
                    marr, _ = read_track(tmp_speak)
                    peak = int(np.abs(marr).max()) if marr.shape[0] else 0
                except Exception:  # noqa: BLE001
                    peak = 0
                if peak <= 2:
                    mic_ok = False
                    _out("  FAIL — the microphone delivered pure silence. The")
                    _out("         usual causes are Windows Settings → Privacy →")
                    _out("         Microphone (\"Let desktop apps access\"), or a")
                    _out("         hardware mute key/switch on the headset.")
                else:
                    mic_ok = True
                    _out(f"  mic peak {peak} of 32767 — the mic path works.")
                    if peak < 400:
                        _out("  (quiet — speak up, or raise the input level)")

        rc = 0 if (remote_ok and mic_ok is not False) else 1
        _out("")
        _out("All good — ready to record a real call." if rc == 0 else
             "Fix the item(s) above before you rely on a recording.")

        if args.output:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(args.output)),
                            exist_ok=True)
                os.replace(tmp_spk, args.output)
                temps.remove(tmp_spk)
                _out(f"kept the captured test audio at {args.output}")
            except OSError as e:
                _out(f"note: could not keep the capture: {e}")
        return rc
    finally:
        for r in (spk_r, mic_r):
            if r is not None:
                r.stop_now()
        for path in temps:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        pa.terminate()


# --------------------------------------------------------------------------- cli


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="wa_call_recorder.py",
        description="Record a WhatsApp (or any) call locally on Windows: "
                    "output loopback (remote side) + microphone (you).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("devices", help="list microphones and loopback outputs")

    st = sub.add_parser("selftest", help="play a test tone and prove the "
                                         "loopback captured it")
    st.add_argument("--loopback", type=int, default=None,
                    help="loopback device index, see `devices`")
    st.add_argument("--mic", type=int, default=None,
                    help="microphone device index, see `devices`")
    st.add_argument("--no-mic", action="store_true",
                    help="skip the microphone check")
    st.add_argument("-o", "--output", default=None,
                    help="keep the captured test audio at this path")

    rec = sub.add_parser("record", help="start recording a call")
    rec.add_argument("-o", "--output", help="output WAV path "
                                            "(default: recordings/call_<timestamp>.wav)")
    rec.add_argument("--duration", type=float, default=None,
                     help="stop after N seconds (default: run until Ctrl+C)")
    rec.add_argument("--loopback", type=int, default=None,
                     help="loopback device index, see `devices`")
    rec.add_argument("--mic", type=int, default=None,
                     help="microphone device index, see `devices`")
    rec.add_argument("--spk-gain", type=float, default=1.0,
                     help="gain for the remote side (default 1.0)")
    rec.add_argument("--mic-gain", type=float, default=1.0,
                     help="gain for your side (default 1.0)")
    rec.add_argument("--keep-raw", action="store_true",
                     help="also keep the separate .speakers.wav / .mic.wav tracks")
    rec.add_argument("--no-mic", action="store_true",
                     help="record only the remote side (no microphone)")

    rem = sub.add_parser("remix", help="mix two kept tracks into a recording "
                                        "(salvage an interrupted run)")
    rem.add_argument("tracks", nargs=2, metavar=("TRACK_A", "TRACK_B"),
                     help="the two .speakers.wav / .mic.wav tracks, in any "
                          "order (the .tmp.wav files work too)")
    rem.add_argument("-o", "--output", required=True, help="output WAV path")
    rem.add_argument("--spk-gain", type=float, default=1.0,
                     help="gain for the remote side (default 1.0)")
    rem.add_argument("--mic-gain", type=float, default=1.0,
                     help="gain for your side (default 1.0)")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "devices":
        return cmd_devices(args)
    if args.cmd == "selftest":
        return cmd_selftest(args)
    if args.cmd == "remix":
        return cmd_remix(args)
    return cmd_record(args)


if __name__ == "__main__":
    sys.exit(main())
