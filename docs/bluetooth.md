# Bluetooth speakers — design (not yet implemented)

## Goal

Pair a Bluetooth speaker with the Tonearm host once, star it, and have it play whenever it is switched on while a record is playing. It should otherwise behave like any other speaker in the UI: toggle, volume, star.

## Approach

Tonearm does not talk to Bluetooth. OwnTone already outputs to PulseAudio sinks, and a connected Bluetooth speaker appears as one, so the speaker shows up in OwnTone's output list and Tonearm picks it up with no special casing. The work is in two parts:

1. host setup, so a paired speaker becomes an OwnTone output;
2. a small Tonearm change, so a starred speaker that appears mid-record joins the playback.

## Part 1: host setup

Target: Raspberry Pi OS trixie on the Pi, not the Parallels VM, where Bluetooth passthrough is not representative.

1. **Audio server reachable by OwnTone.** OwnTone runs as a system service, while PipeWire/PulseAudio normally runs per user session. The Pi's OwnTone log currently shows `Pulseaudio failed with error: Connection refused`. Options, in order of preference:
   - a system-wide PipeWire with its PulseAudio layer, running as a system service, with OwnTone allowed to connect (socket permissions / `pulse-access` group);
   - PulseAudio in system mode (`pulseaudio --system`), which is the setup OwnTone's own docs describe;
   - running OwnTone as the desktop user and using that user's session. This is the simplest, but it ties audio to a login, which a headless Pi won't have.
2. **Bluetooth audio stack:** `bluez` plus PipeWire's Bluetooth support (`libspa-0.2-bluetooth`), or `pulseaudio-module-bluetooth` in the PulseAudio option.
3. **Pairing, once per speaker:** `bluetoothctl` → `scan on`, `pair <MAC>`, `trust <MAC>`, `connect <MAC>`. Trusting it lets the speaker reconnect when it is switched on. If a given speaker doesn't reconnect by itself, a small udev/systemd helper that retries `connect` for trusted devices may be needed.
4. **OwnTone config:** check that the `pulseaudio` output section is enabled and that the speaker appears in `GET /api/outputs` (type `pulseaudio`).
5. **Installer:** add an opt-in step (for example `deploy/install.sh <user> --bluetooth`) that installs the packages and configures the system audio server. Pairing stays manual and gets a documented README section, since it needs the speaker in pairing mode.

## Part 2: Tonearm change — starred speakers join mid-record

Today a starred speaker only matters at the needle drop. The one exception is "no starred speaker available", which retries.

Change: while the gate is PLAYING an **auto** start, when a speaker in the default set becomes available (it passes the 10 s appear delay, or returns from grace), select it in OwnTone (`PUT /api/outputs/{id}` `{"selected": true}`) so it joins the running playback.

- It never starts playback on its own; the needle still decides that.
- It does not apply to manual Play, which uses the user's explicit selection.
- If the user turned the speaker off during this playback, don't re-add it until the next record.
- Skip speakers that need a PIN.
- It is not Bluetooth-specific, so it helps AirPlay speakers too. It can be built and tested on the VM before Part 1.

Tests: speaker appears while PLAYING auto → it is selected; while manual → not selected; turned off by the user mid-record → not re-selected; needs PIN → skipped; appears while ARMED/HELD/OFF → nothing.

## Known limits

- **Sync:** Bluetooth adds its own latency (often 100–250 ms, depending on the codec), and OwnTone cannot lock it to AirPlay 2 timing. Fine in another room, but it may echo within earshot of an AirPlay speaker. OwnTone's per-output offset (`offset_ms`) can mostly compensate; consider exposing it later.
- **One host radio:** the Pi's built-in Bluetooth can drive a speaker or two at best. A USB dongle may be needed for range.
- **Volume:** a Bluetooth speaker's volume goes through the PulseAudio sink, and some speakers also keep their own hardware volume.

## Open questions to verify on the Pi

- Which audio-server option OwnTone 29.3 on trixie works with cleanly, and what exact permissions it needs.
- Whether the speaker reconnects by itself after power-on once trusted, and how long it takes to appear as an OwnTone output.
- Whether OwnTone shows a disconnected Bluetooth speaker as missing, which Tonearm's presence tracking handles, or keeps it listed and fails on select.
