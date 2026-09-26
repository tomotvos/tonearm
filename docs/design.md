# Tonearm — Design

## Purpose

Play a USB turntable to AirPlay speakers around the house with no manual steps. Drop the needle and music plays in the usual rooms; lift the arm, or let the side end, and the speakers are released.

OwnTone does all audio output: synchronised multi-room AirPlay 2, pairing, Chromecast. Tonearm supplies what OwnTone lacks:
- audio capture;
- needle-drop detection (auto-on);
- the override controls;
- a phone-first UI.

## Hardware and environment

- Turntable: Sony PS-LX3BT, fully automatic (the arm lifts and returns at the end of a side). USB output via a TI PCM2900C codec, 16-bit, up to 48 kHz.
- Host: Raspberry Pi 4 with Raspberry Pi OS (Debian trixie, Python 3.13). Also runs on Debian 12 amd64 with Python 3.11.
- OwnTone 29.x on the same host. `pipe_autostart` must be disabled in `/etc/owntone.conf`, because this service is the only thing that starts pipe playback.
- A named pipe in OwnTone's library (default `/srv/music/Turntable`), writable by the service user.
- LAN only, with no authentication (the same as OwnTone).

## Requirements

1. Select the capture input.
2. Show a live list of AirPlay targets. Select them, and enter a PIN when a target requires one.
3. Individual volume per speaker.
4. Playback control: hands-off auto-on by default, with manual override.
5. A phone-first UI that also works on desktop.
6. A small REST API usable by Home Assistant and Siri Shortcuts.

## Behaviour decisions

- **Everyday mode:** hands-off by default, with manual override.
- **Auto-on speakers:** a fixed default set, configured once. Session changes do not alter it. Starring a speaker adds it to the set and turns it on, unless it still needs pairing.
- **End of record:** playback stops once the stylus has been up for 3 minutes. This is fixed and configurable in the environment, not in the UI, and covers flipping a side without a restart.
- **Stop mid-record:** holds playback until the current record ends (stylus up for 3 minutes), then auto-on re-arms. A separate auto-on switch disables automatic starts until it is turned back on.

## Architecture

One Python service, `tonearm`, run by systemd and ordered after `owntone.service`. It is a single asyncio process whose only dependency is `aiohttp`.

| Module | Responsibility |
|---|---|
| `capture.py` | Runs `arecord` on the selected input (44.1 kHz, S16_LE, stereo, raw), reads 50 ms chunks, restarts with backoff |
| `levels.py` | RMS level of a chunk in dBFS, using the standard `array` module (`audioop` is gone in 3.13) |
| `gate.py` | Auto-on state machine. Pure logic with no I/O: consumes (level, timestamp) and commands, emits state and start/stop actions |
| `pipe.py` | Writes audio to OwnTone's pipe on its own thread with a bounded queue, so a stalled reader never blocks capture |
| `owntone.py` | OwnTone client: outputs, per-output volume, PIN, queue/play/stop, websocket notifications |
| `settings.py` | Persists the input, the default speaker set and the auto-on switch as JSON |
| `service.py` | Wires the modules together: start/stop sequencing, retries, speaker presence |
| `api.py` | REST API, SSE event stream, static UI |
| `web/` | `index.html`, `app.js`, `style.css`, with no build step |

OwnTone is the source of truth for speakers, volume and pairing. The service stores only what OwnTone does not: the input, the default speaker set and the auto-on switch. The input is stored by ALSA card name (`plughw:CARD=<id>,DEV=<n>`), so it survives changes in card numbering.

## Auto-on state machine (`gate.py`)

Detection is based on **stylus position, not music**. With the stylus up there is only the converter's noise floor. With the stylus in the groove there is continuous surface noise, even in silent grooves and pianissimo passages. The thresholds sit between the two, so a quiet opening cannot be missed and a quiet passage is never mistaken for the end of a record.

- "Down" means level ≥ `TONEARM_START_DB`.
- "Up" means level ≤ `TONEARM_FLOOR_DB`.
- `TONEARM_START_DB` > `TONEARM_FLOOR_DB` (hysteresis). A level between the two keeps the current reading.

| State | Meaning | Transitions |
|---|---|---|
| ARMED | Waiting for a record | Down continuously for 1 s → PLAYING (default speakers). Play → PLAYING. Auto-on switched off → OFF |
| PLAYING | Audio flows to OwnTone | Up continuously for the quiet timeout → stop → ARMED (or OFF if auto-on is off). Stop → HELD |
| HELD | Stopped by the user; the record may still be playing | Up continuously for the quiet timeout → ARMED (or OFF if auto-on is off). Play → PLAYING |
| OFF | Auto-on disabled | Auto-on switched on → ARMED. Play → PLAYING |

Rules:

- A 2 s rolling buffer of audio is kept. On start it is sent before live audio, and so is everything captured while OwnTone was starting, so the needle drop and lead-in are never lost. The cost is that a slow start (for example, a macOS "Allow AirPlay" prompt) leaves the whole side playing that much behind the turntable.
- The quiet timeout applies however playback started, including manual Play.
- Switching auto-on off never stops current playback. It only changes where PLAYING and HELD end up.
- Auto-start uses the default speaker set. Manual Play uses the current selection, falling back to the default set. Changing speakers during playback applies immediately and does not change the default set.
- If no default speaker is available when the needle drops, the start is retried every 5 s until one is, or until the gate leaves PLAYING.
- A default speaker that becomes available during a needle-drop playback (it comes back within its grace period, or passes the appear delay) joins the running playback. This never starts playback, never applies to manual Play, skips speakers that need a PIN, and never re-adds a speaker the user turned off during the current record.
- State and the remaining countdown are exposed to the API and UI.

## Data flow and OwnTone interaction

- Capture runs continuously on the selected input. Each chunk feeds the gate and the rolling buffer.
- **Start** (auto or manual) always happens in this order:
  1. `PUT /api/outputs/set` with the speaker IDs.
  2. `POST /api/queue/items/add?uris=<pipe track uri>&clear=true&playback=start`. This call can block while a receiver waits for the user, so it gets a 20 s timeout; other calls get 5 s.
  3. Open the pipe, write the buffered audio, then stream live chunks.

  A bare `play` is never used, because it can leave a pipe stuck in pause.
- **Stop:** `PUT /api/player/stop`, then close the pipe.
- The pipe's library track is looked up once: the library track whose path equals `TONEARM_PIPE`.
- **Live updates:** the service subscribes to OwnTone's websocket for `outputs`, `player` and `volume` events. On each event it re-reads the affected state and pushes a combined snapshot (plus gate state, countdown and level) to browsers via SSE.
- **Resilience:** if OwnTone drops playback while the gate is PLAYING, the start sequence is re-run. Playback stopped from outside Tonearm is respected.
- **Speaker presence:** a newly discovered speaker is shown after 10 s. A speaker that disappears stays listed as unavailable for 30 s. This keeps the list stable while devices flap.
- **Volume:** `PUT /api/outputs/{id}` with `{"volume": n}`.
- **PIN:** when an output reports `needs_auth_key`, the UI shows a PIN field on that speaker. Submitting it sends `PUT /api/outputs/{id}` with `{"pin": "NNNN"}`; if the gate is PLAYING, playback is then restarted with the start sequence.
- **Inputs:** listed from `arecord -l`. Changing input restarts capture, with a brief gap if playing.

## REST API

| Endpoint | Purpose |
|---|---|
| `GET /api/status` | Gate state, countdown, level, auto-on, input, default speakers, speakers (id, name, type, selected, volume, needs PIN, available), OwnTone reachability, last error |
| `GET /api/events` | SSE stream of the same snapshot on change |
| `POST /api/play` `{speakers?}` | Manual play. Speakers default to the current selection |
| `POST /api/stop` | Stop → HELD |
| `PUT /api/auto-on` `{enabled}` | Auto-on switch |
| `PUT /api/speakers/{id}` `{selected?, volume?, pin?}` | Toggle, volume, PIN |
| `PUT /api/default-speakers` `{ids}` | Set the auto-on speaker set |
| `GET /api/inputs` | Available capture inputs |
| `PUT /api/input` `{id}` | Select the input |

## Error handling

- **OwnTone unreachable:** the gate keeps running and the UI shows "OwnTone unavailable". If the gate is still PLAYING when OwnTone returns, the start sequence is re-run. The websocket reconnects with backoff.
- **Input missing or `arecord` exits:** capture restarts with backoff and the UI shows "Input unavailable". No audio reads as stylus up, so playback ends through the normal quiet timeout.
- **Default speaker offline or unpaired:** play to the available speakers; the UI flags the rest. Selecting an unpaired speaker reports that it needs pairing.
- **Pipe writes stall:** the bounded queue (30 s) drops chunks and logs; capture and the gate are never blocked.

## Detection measurements

Measured on the Sony PS-LX3BT:
- arm up: −91.9 to −91.6 dBFS;
- empty groove between tracks: −53.9 to −48.9 dBFS.

That is a gap of about 38 dB. The thresholds split it with about 11 dB of margin on each side: `TONEARM_FLOOR_DB = -80` and `TONEARM_START_DB = -65`, giving 15 dB of hysteresis. A 5 dB margin above the floor was rejected, because arm-up hum could then never read as "up".

For another turntable, the stylus-up floor and the lead-in surface noise must differ by at least 15 dB. Measure both with `python3 -m tonearm.measure <input> 10`.

If the gap is too small, the fallback is to detect music level instead and enlarge the rolling buffer to about 30 s, playing from the moment the level first rose. This adds a constant lag but loses nothing; only the `gate.py` thresholds and the buffer length change.

## Out of scope

- Speaker presets beyond the single default set.
- Native Home Assistant integration (HA's OwnTone integration already covers speakers and volume).
- Authentication.
- Any change to OwnTone itself.
