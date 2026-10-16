# wa-call-recorder

Record WhatsApp voice/video calls **locally on Windows** — both sides of the
conversation, one WAV file, nothing uploaded, no cloud service involved.

```
Recording → recordings/call_20261016_132900.wav
  remote/speakers loopback: Speaker (Realtek(R) Audio) [Loopback]  (48000 Hz, 2 ch)
  local/mic:                Microphone Array (AMD Audio Device)  (48000 Hz, 2 ch)
  duration: 60 s  (Ctrl+C stops early)
  [  12.0s]  remote     4.6 MB  |  mic      4.2 MB
```

## Why WhatsApp (and the other person) never "knows"

WhatsApp calls are end-to-end encrypted; there is no server copy to pull and no
recording API. This tool doesn't touch WhatsApp at all — it taps the audio at
the **operating-system layer of your own machine**, after the decrypted audio
reaches your speakers:

* **remote side** — WASAPI loopback of the output device the call plays through
  (whatever WhatsApp sends to your speakers/headphones, you hear, and it's captured)
* **your side** — your microphone

WhatsApp has no mechanism to detect or be notified about local OS audio
capture, so nothing is shown to your contact and no packet leaves your machine.
That "invisibility" is a fact about the software, not about the law — see
**Consent & legality** below before you use it on anyone.

## Setup

Windows 10/11, Python 3.9+:

**Easiest:** double-click **`run_recorder.bat`** — it installs the dependencies
if needed, lists your devices, waits for you to start the call, then records and
prints the file it saved.

**Or from a terminal:**

```
pip install -r requirements.txt
python wa_call_recorder.py devices
python wa_call_recorder.py selftest          # prove the capture path works
python wa_call_recorder.py record            # Ctrl+C to stop
python wa_call_recorder.py record --duration 60 -o mycall.wav
```

**How to stop:** press **Ctrl+C** in the window — that's what writes the final
mixed WAV. Ctrl+Break works too. Closing the console window or killing the
process can lose the recording; if that happens the stray `*.tmp.wav` tracks are
automatically cleaned up the next time you record.

Useful flags:

| Flag | Meaning |
|---|---|
| `--loopback N` | capture a specific output endpoint (index from `devices`) |
| `--mic N` | capture a specific microphone (index from `devices`) |
| `--no-mic` | record the remote side only |
| `--keep-raw` | also keep `file.speakers.wav` + `file.mic.wav` as separate tracks for your own editor/DAW (this tool won't re-mix them for you) |
| `--spk-gain G` / `--mic-gain G` | rebalance the two sides **while recording** (default 1.0). These apply at record time only — there is no re-mix command, so decide them before you stop |
| `--duration S` | auto-stop after N seconds (default: run until Ctrl+C) |

### Salvaging an interrupted recording

```
python wa_call_recorder.py remix track-a.wav track-b.wav -o call.wav
python wa_call_recorder.py remix mycall.wav.spk.tmp.wav mycall.wav.mic.tmp.wav \
    -o mycall.wav --mic-gain 3
```

`remix` takes the two kept tracks **in any order** (it identifies them by
filename) and applies the gains *after the fact* — the one thing the gain flags
on `record` cannot do. This also reads tracks whose header was never finalized,
so it recovers `.tmp.wav` files from a hard-killed run.

Output defaults to `recordings/call_<timestamp>.wav` next to the script.

## Before a real call: run the self-test

```
python wa_call_recorder.py selftest
```

It plays a 1 kHz tone through the output device calls use, records it back
through the loopback, and then analyses what actually arrived — so you learn
the answer *before* the call, not after. You hear the beep; the numbers prove
it:

```
  captured 2.69 s at 48000 Hz
  silence baseline   -58.6 dB
  1 kHz tone band     42.4 dB  (+101.0 dB over silence)
  tone vs broadband  8497.3x

PASS — the loopback captured the test tone, so the remote
       side of a call will be recorded.
```

Why go to the trouble: "the byte counter was moving" does **not** mean audio
was recorded. A muted or idle endpoint delivers silence at full data rate —
that is exactly how one 107-second call came back empty. The self-test is the
only thing that catches it in advance.

The analysis is deliberately hard to fool: it measures the tone's own narrow
band before and during the beep (a quiet 1 kHz tone still passes on the jump),
and compares it against an unrelated band, so loud broadband noise can't fake a
pass. Exit code is 0 only if the test passed, so you can script it.

It checks the microphone too — the beep acoustically, or a 3-second "say
something" recheck when you're on headphones. `--no-mic` skips that, and
`-o keep.wav` saves the captured tone so you can listen to it yourself.

If you would rather test by hand: record 10 s while playing YouTube, check the
`remote` counter moves, and listen to the file.

## Tips & known limits

* **One output endpoint at a time.** The loopback records *everything* played
  on the selected output — mute notifications, close music apps, etc.
* **Match the device.** If WhatsApp plays through the default output, record
  the loopback marked `*default output*` in `devices`. If you route WhatsApp's
  speaker elsewhere during a call, pass that device's `--loopback` index.
* **Bluetooth caveats.** BT headphone endpoints only deliver loopback data
  while something is actively playing — during a live call WhatsApp keeps the
  endpoint active, so capture works, but the meter may sit at `0 B` between
  calls. Wired speakers/headsets are more predictable.
* **Microphone access.** If the mic meter stays at `0 B` while you speak,
  check Windows Settings → Privacy → Microphone ("Let desktop apps access").
* **Very long calls** are fine: both tracks are written to disk as they're
  captured, then mixed at the end (the mix loads one track in memory).
* **Interrupted runs are recovered, not deleted.** If a run is killed before it
  can mix (window closed, process killed), its raw tracks are left behind. The
  next `record` mixes them into `<name>_recovered.wav` instead of throwing real
  audio away, and prints where it put it.

## A call that arrives on your phone

**This tool runs only on Windows** — there is no version that runs *on* the
phone, and no Android/iOS app can capture the remote side of a WhatsApp call.
WhatsApp's call audio never becomes readable to another app: Android permits
call-audio capture only for a privileged pre-installed app holding
`CAPTURE_AUDIO_OUTPUT`, and Apple gives ReplayKit/CallKit no hook into another
app's call audio.

So don't fight the phone — move the call to the PC. Best first:

1. **Answer it in WhatsApp on this PC (recommended).** Your phone stays the
   primary device; the PC is just a *linked device*, and it can make and
   receive voice/video calls on its own — the phone doesn't even need to be
   online. Link it once (WhatsApp → *Linked devices*), then take the call in
   the Windows app (or WhatsApp Web, which now supports calls in current
   Chrome/Edge/Safari/Firefox) and let this tool record it. The only route
   that captures both sides cleanly.
2. **Speakerphone + this PC as the recorder.** Put the call on speaker and run
   `record --no-mic` on the PC beside it. Works on any phone with no software
   trickery, but quality is room-acoustic, it picks up room noise, and you
   lose the remote side the moment you put headphones on.
3. **Hardware tap.** A wired headset splitter or call-recording adapter
   between the phone and your headset captures both sides electrically, with
   no app involved. The only dependable phone-side option.
4. **Android call-recorder apps — don't rely on them.** Cube ACR (with its
   "App Connector") is the only notable third-party route for VoIP audio;
   reliability is device-specific, and reports show newer Android privacy
   changes breaking it. Test it before a call you care about.
5. **iPhone's built-in recording isn't it.** iOS 18.1's call recording covers
   *Phone-app and FaceTime audio* calls only, and plays an audio notice to both
   participants, so the other person is told. WhatsApp calls aren't covered.

WhatsApp has no recording feature of its own on any platform — which is why
anything offering "cloud WhatsApp call recording" is a scam or malware.

## Consent & legality (read once)

Recording conversations you take part in is lawful in *some* jurisdictions
("one-party consent" — e.g. India in general, most US states for personal
calls) and a crime in others ("all-party consent" — e.g. several US states,
much of the EU). Where each party is located can matter. WhatsApp's Terms of
Service don't prohibit local recording of your own calls, but privacy,
criminal and evidence law where *you and the other person* are located
governs — this file is not legal advice. Check your jurisdiction before
recording someone without telling them, and keep recordings private.

Also: because WhatsApp calls are end-to-end encrypted, *no legitimate service
can "record WhatsApp calls from the cloud"* — only endpoint capture like this
tool can. Anything promising cloud-side WhatsApp call recording is a scam or
malware.

## How it's built

Single-file CLI (`wa_call_recorder.py`, ~1000 lines) on
[PyAudioWPatch](https://pypi.org/project/PyAudioWPatch/) (PortAudio fork with
WASAPI loopback) + NumPy:

* two reader threads capture the remote loopback and the mic to temporary WAVs
  (streaming to disk, so long calls don't grow in RAM)
* loopback read blocks when a BT endpoint is idle → `abort_stream()` unblocks
  it on stop
* mic is auto-opened at the loopback's sample rate when supported (else the
  mic track is linear-resampled before mixing)
* blockwise mix with gain flags and int16 clipping protection
* `selftest` plays a 1 kHz tone on the endpoint's paired output and proves the
  loopback produced it: Hann-windowed FFT band energy before the beep vs during
  it, with a 3–3.4 kHz control band so broadband noise can't pass
