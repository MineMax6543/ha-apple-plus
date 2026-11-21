# AppleTV Plus for Home Assistant

A custom integration for **Apple TV** and **HomePod** that fixes the parts of the built-in `apple_tv` integration that tend to break in practice: sending images and videos from Home Assistant, laggy/out-of-sync state, and not knowing what's happening with multichannel / spatial audio.

It uses [pyatv](https://pyatv.dev), the same library as the core integration, but drives it differently.

## What's different from the built-in integration

| Problem | What AppleTV Plus does |
|---|---|
| `play_media` hangs, everything feels laggy | pyatv's `play_url` / `stream_file` only return when playback **ends**. The core integration waits on that inside the service call. AppleTV Plus runs playback in the background and only waits a few seconds (configurable) so it can still report early errors like a bad URL or missing pairing. |
| Images don't show up | The Apple TV's AirPlay video endpoint can't show still images. Images (JPG, PNG, WebP, GIF, …) are turned into a short H.264 clip with HA's bundled ffmpeg and played from there. Display time is configurable, or per call with `extra.duration`. |
| Videos don't play | AVPlayer only accepts MP4/MOV/HLS with H.264/HEVC. MKV, WebM, AVI, VP9, AV1, DTS and similar sources are repackaged on the fly into fMP4 HLS. Video is copied when possible and only transcoded when the codec is unsupported. Playback starts after the first ~8 seconds are ready. |
| Position drifts / out of sync | Uses push updates and also re-reads the position from the device every 15 s while playing, so the HA timeline doesn't drift. |
| Drops off the network | Reconnects automatically with backoff. If the IP changed it finds the device again by its identifier and updates the entry. |
| HomePods | Audio is streamed over AirPlay (RAOP). Formats pyatv can't decode (AAC, ALAC, Opus, the audio track of a video …) are decoded to lossless FLAC first. |

## Spatial audio: what is and isn't possible

AppleTV Plus doesn't damage spatial audio anywhere it can avoid it, and it tells you which path each stream takes.

- **Apple TV, direct or repackaged:** the Apple TV fetches and decodes the stream itself. **Dolby Atmos (E-AC-3 JOC), AC-3 and AAC are always copied bit-for-bit**, so tvOS renders them spatially to your HomePods/AirPods/AVR as usual.
- **Other multichannel audio** (DTS, TrueHD, multichannel FLAC/PCM/Opus) is converted to **5.1 E-AC-3** instead of being downmixed to stereo. TrueHD Atmos *objects* can't be carried over, but the 5.1 bed is kept.
- **HomePod via AirPlay audio (RAOP):** this protocol is **stereo only** (44.1 kHz ALAC), and third-party senders can't get around that. For spatial playback on HomePods, play to the **Apple TV** and let it output to the HomePods (see grouping below).

The media player exposes this as attributes:

| Attribute | Meaning |
|---|---|
| `audio_path` | `device_decoded` (Apple TV decodes the original, spatial-capable) or `airplay_stereo` (RAOP stereo stream) |
| `source_audio` | Codec, channels, layout and `dolby_atmos` of the source, detected with ffmpeg |
| `spatial_capable` | `true` when the source is multichannel/Atmos **and** it's reaching the device undownmixed |
| `delivery` | `direct`, `hls_repackaged`, `image_as_video` or `decoded_to_flac` |
| `audio_outputs` | Devices the Apple TV currently outputs audio to |

These describe media that **you send through Home Assistant**. For apps playing on their own (Apple TV+, Netflix, …), Apple doesn't expose the active audio format over any protocol pyatv speaks, so it can't be shown.

## Features

- Media player: play/pause/stop/next/previous, seek, volume, shuffle/repeat, power (Apple TV), artwork, app launching (source list + media browser)
- Media browser: Apple TV apps + all HA media sources (Local media, TTS, Jellyfin, …)
- **Grouping:** `media_player.join` with an Apple TV and HomePods routes the Apple TV's audio to those HomePods (with spatial audio, unlike RAOP). `unjoin` routes it back.
- Remote entity (Apple TV): `remote.send_command` with `up`, `down`, `left`, `right`, `select`, `menu`, `home`, `home_hold`, `top_menu`, `play_pause`, `skip_forward`, `skip_backward`, `control_center`, `screensaver`, … Hold with `hold_secs`, double tap with e.g. `select_double`.
- Zeroconf discovery of Apple TVs and HomePods
- English and German UI

## Installation

**HACS:** add this repository as a custom repository (category *Integration*), install **AppleTV Plus**, restart Home Assistant.

**Manual:** copy `custom_components/appletv_plus` into your `config/custom_components/` folder and restart.

Then go to **Settings → Devices & services → Add integration → AppleTV Plus**.

It can run alongside the built-in Apple TV integration. Running both against the same device is fine for testing, but disable the device in one of them afterwards so they don't fight over it.

### Pairing

- **Apple TV:** you'll be asked for a PIN for **AirPlay** (needed to play media) and then **Companion** (power on/off, app list). Both PINs appear on the TV.
- **HomePod:** usually no pairing is needed.
- If pairing fails, set *Settings → AirPlay and HomeKit → Allow Access* on the Apple TV (or in the Home app for HomePods) to *Everyone on the same network*.

### Network requirement

Images and repackaged videos are served **from Home Assistant to the Apple TV**. The Apple TV has to be able to reach HA's *internal* URL (Settings → System → Network). A plain `http://<ha-ip>:8123` internal URL works best, because the Apple TV won't accept self-signed HTTPS certificates.

## Usage examples

```yaml
# Show a camera snapshot for 15 seconds
action: media_player.play_media
target:
  entity_id: media_player.living_room
data:
  media_content_id: "http://192.168.1.10/snapshots/door.jpg"
  media_content_type: image/jpeg
  extra:
    duration: 15

# Play an MKV from local media, starting at 1:30
action: media_player.play_media
target:
  entity_id: media_player.living_room
data:
  media_content_id: media-source://media_source/local/movies/film.mkv
  media_content_type: video
  extra:
    position: 90

# TTS / music on a HomePod
action: media_player.play_media
target:
  entity_id: media_player.kitchen_homepod
data:
  media_content_id: "http://192.168.1.10/music/song.m4a"
  media_content_type: music
  extra:
    title: "Song"
    artist: "Artist"

# Spatial audio on HomePods: send the Apple TV's audio to them
action: media_player.join
target:
  entity_id: media_player.living_room
data:
  group_members:
    - media_player.homepod_left
```

`extra` keys: `duration` (images), `position` (start offset), `compat` (`auto`/`direct`/`always`, overrides the option for one call), `title`/`artist`/`album` (HomePod metadata).

## Options

| Option | Default | |
|---|---|---|
| Video compatibility | Auto | *Auto* repackages only what the Apple TV can't play. *Direct* never touches URLs. *Always* routes everything through HA. |
| Image display time | 30 s | How long images stay on screen |
| Wait for playback start | 4 s | How long `play_media` waits to report startup errors |

## Development

```bash
pip install -r requirements_test.txt
pytest
```

Tests need Linux or macOS (Home Assistant's test harness doesn't run on Windows) and `ffmpeg` on the PATH for the image test.
