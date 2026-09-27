<img src="docs/ux/wordmark.svg" alt="Tonearm" width="317">

Drop the needle and your record plays on your AirPlay speakers. Lift the arm, or let the side end, and the speakers are released.

Tonearm owes its starting point to [Pinyl](https://github.com/marktiddy/Pinyl), Mark Tiddy's turntable-to-AirPlay bridge. Pinyl's clean, single-page UX showed how simple this could be: pick an input, pick speakers, press play. Tonearm keeps that spirit and takes a different route under the hood:

- **OwnTone does the audio.** Pinyl streams to each speaker itself. Tonearm hands the audio to [OwnTone](https://github.com/owntone/owntone-server), which keeps multi-room AirPlay 2 in sync, handles pairing, and adds Chromecast.
- **Hands-off playback.** Pinyl starts and stops on command. Tonearm listens for the stylus, starts when the needle drops, and stops a few minutes after the arm lifts. Manual Play and Stop are still there.
- **Per-speaker volume**, and a live-updating, phone-first UI.

Pinyl stays the lighter choice: one self-contained app, with no OwnTone to install.

OwnTone can't read an audio input, and its pipe autostart fires on any data, which a USB input sends constantly. So Tonearm captures the turntable, decides when a record is playing, and feeds OwnTone only then. It also offers a small REST API for Home Assistant, Siri Shortcuts and scripts.

## How it works

- **Capture:** Tonearm runs `arecord` on the turntable's USB input (44.1 kHz, 16-bit stereo) and measures the level of each 50 ms chunk.
- **Stylus detection:** it watches where the stylus is, not whether music is playing. With the arm up, the converter produces only its noise floor, around −92 dBFS. With the stylus in the groove, there is always surface noise, around −54 to −49 dBFS even between tracks. Thresholds between the two mean quiet openings are never missed and quiet passages never look like the end of a side.
- **Start:** after 1 s of stylus-down, Tonearm selects your default speakers in OwnTone, starts OwnTone's pipe track, and streams the audio into the pipe. It keeps a 2 s rolling buffer, so the needle drop and lead-in are played too.
- **Stop:** after 3 minutes of stylus-up (enough time to flip the record), playback stops and the speakers are released.

The auto-on states:

| State | Meaning |
|---|---|
| Armed | Waiting for a record. The needle drop starts playback on the default speakers. |
| Playing | Audio is streaming to OwnTone. It stops 3 minutes after the arm goes up. |
| Held | You pressed Stop. It stays stopped until the current record ends, then re-arms. |
| Off | Auto-on is switched off. Only Play starts playback. |

OwnTone is the source of truth for speakers, volume and pairing. Tonearm stores only:
- the input;
- the default ("starred") speakers;
- the auto-on switch.

## Requirements

- A Raspberry Pi 4 or other Linux host on the same network as your speakers. Tested on Raspberry Pi OS (Debian 13 "trixie", arm64) and Debian 12 (amd64).
- Python 3.11 or later with `aiohttp` 3.8 or later, plus `alsa-utils`. The installer handles both from apt.
- OwnTone 29.x on the same host.
- A turntable with USB audio output. Developed with a Sony PS-LX3BT (TI PCM2900C codec).

## Install

### 1. Install

```sh
git clone https://github.com/tomotvos/tonearm.git
cd tonearm
sudo deploy/install.sh "$USER"
```

The installer:

- installs OwnTone if it isn't installed yet, from [OwnTone's apt repository](https://github.com/owntone/owntone-apt) for Raspberry Pi OS. On other systems, install OwnTone first by following [its installation guide](https://owntone.github.io/owntone-server/installation/), keeping the default library directory `/srv/music`;
- installs `python3-aiohttp` and `alsa-utils`, and adds the user to the `audio` group;
- copies the service to `/opt/tonearm`;
- creates the named pipe `/srv/music/Turntable`, which OwnTone indexes as a "Turntable" track;
- sets `pipe_autostart = false` in `/etc/owntone.conf`, so only Tonearm starts pipe playback;
- installs `tonearm.service` and enables it and OwnTone to start at boot;
- restarts OwnTone and Tonearm.

To upgrade, pull the latest code and run the installer again. Settings are kept.

### 2. Set up

Open `http://<host>/` on your phone, then:

1. Tap the gear icon and choose the turntable's input.
2. Star the speakers that should play when the needle drops. Starring a speaker also turns it on.
3. If a speaker asks for a PIN (some Apple TVs), turn it on. A PIN appears on its screen; enter it in the speaker's card.

Drop the needle.

## Configuration

Set these as `Environment=` lines in `/etc/systemd/system/tonearm.service`, then run `sudo systemctl daemon-reload && sudo systemctl restart tonearm`.

| Variable | Default | Installed unit | Meaning |
|---|---|---|---|
| `TONEARM_PORT` | `8080` | `80` | Web UI and API port |
| `TONEARM_START_DB` | `-65` | `-65` | Level at or above this means stylus down |
| `TONEARM_FLOOR_DB` | `-80` | `-80` | Level at or below this means stylus up; must be lower than `TONEARM_START_DB` |
| `TONEARM_QUIET_TIMEOUT_S` | `180` | | Stylus-up time before playback stops |
| `TONEARM_PIPE` | `/srv/music/Turntable` | | Named pipe in OwnTone's library |
| `TONEARM_SETTINGS` | `~/.config/tonearm/settings.json` | | Where input, default speakers and auto-on are saved |
| `OWNTONE_URL` | `http://localhost:3689` | | OwnTone JSON API |

### Calibrating detection

The default thresholds fit the PS-LX3BT. For another turntable, measure both levels. Stop Tonearm first so the input is free:

```sh
sudo systemctl stop tonearm
cd /opt/tonearm
python3 -m tonearm.measure plughw:CARD=CODEC,DEV=0 10   # arm up
python3 -m tonearm.measure plughw:CARD=CODEC,DEV=0 10   # stylus in the lead-in groove
sudo systemctl start tonearm
```

Use your input's ID from `GET /api/inputs`. Each run prints min, p10, median, p90 and max in dBFS. Set `TONEARM_FLOOR_DB` well above the arm-up p90 and `TONEARM_START_DB` well below the groove p10. The gap should be at least 15 dB.

## REST API

The API is for Home Assistant, Siri Shortcuts, scripts and so on. There is no authentication, the same as OwnTone, so keep it on your LAN.

| Endpoint | Purpose |
|---|---|
| `GET /api/status` | State, countdown, level, stylus, auto-on, input, default speakers, speakers, OwnTone reachability, last error |
| `GET /api/events` | Server-sent events stream of the same snapshot |
| `POST /api/play` `{"speakers": [ids]?}` | Play now. With no speakers given, it uses the ones currently on, or else the defaults |
| `POST /api/stop` | Stop and hold until the record ends |
| `PUT /api/auto-on` `{"enabled": bool}` | Auto-on switch |
| `PUT /api/speakers/{id}` `{"selected"?, "volume"?, "pin"?}` | Turn a speaker on or off, set its volume (0–100), or submit a 4-digit PIN |
| `PUT /api/default-speakers` `{"ids": [ids]}` | Set the auto-on speakers |
| `GET /api/inputs` | Capture inputs |
| `PUT /api/input` `{"id": "..."}` | Select the capture input |

Example: `curl -X POST http://tonearm.local/api/stop`

## Troubleshooting

- **Logs:** `journalctl -u tonearm -f`. Every start logs a timeline: when the start was decided, when OwnTone accepted the speakers and the play request, and when the pipe was connected and drained.
- **Playback starts late on a Mac speaker:** macOS asks "Allow AirPlay?" for each new stream from OwnTone, and OwnTone waits until you answer. Tonearm keeps everything captured in the meantime, so the record plays in full but later by that much. Real speakers (HomePod, Sonos, Apple TV) don't prompt.
- **"… needs pairing":** the speaker wants a PIN. Turn it on, then enter the PIN it displays.
- **Input unavailable:** check `arecord -l`. Inputs are stored by ALSA card name (`plughw:CARD=CODEC,DEV=0`), so they survive changes in card numbering.
- **Speakers appear and disappear:** new speakers are shown after 10 s, and a speaker that drops off stays listed as unavailable for 30 s. This stops the list jumping around while you tap it.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The tests use a fake OwnTone server, a fake pipe and a fake clock, so no hardware is needed. To run against a real OwnTone:

```sh
OWNTONE_URL=http://<owntone-host>:3689 TONEARM_PORT=8099 .venv/bin/python -m tonearm
```

The UI is plain HTML, CSS and JavaScript in `web/`, with no build step.

| Path | Contents |
|---|---|
| `tonearm/capture.py` | `arecord` capture and input listing |
| `tonearm/levels.py` | Chunk RMS in dBFS |
| `tonearm/gate.py` | Auto-on state machine (pure logic) |
| `tonearm/pipe.py` | Threaded, bounded writer to OwnTone's pipe |
| `tonearm/owntone.py` | OwnTone JSON API and websocket client |
| `tonearm/service.py` | Wires capture, gate, pipe and OwnTone together |
| `tonearm/api.py` | REST, SSE and static files |
| `tonearm/settings.py` | Persisted settings |
| `deploy/` | Installer and systemd unit |
| `docs/design.md` | Design: detection, state machine, OwnTone interaction |
| `docs/ux/mockup.html` | Original UI mockup |
| `docs/ux/wordmark.svg` | README wordmark, generated from the Yellowtail typeface |

## Credits

Inspired by [Pinyl](https://github.com/marktiddy/Pinyl). All audio output is handled by [OwnTone](https://github.com/owntone/owntone-server).

The UI bundles the Yellowtail (Apache License 2.0), Oswald and Karla (SIL Open Font License) typefaces; their licences are in `web/fonts/`.

## License

MIT. See [LICENSE](LICENSE).
