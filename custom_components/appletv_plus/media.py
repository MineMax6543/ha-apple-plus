"""Media preparation for AppleTV Plus.

The Apple TV's AirPlay video endpoint (AVPlayer) only accepts MP4/MOV/HLS with
H.264/HEVC video. It cannot show still images at all. HomePods (RAOP) only
receive stereo PCM/ALAC, and pyatv can only decode MP3/FLAC/WAV/Vorbis.

This module bridges those gaps with Home Assistant's bundled ffmpeg:

* images  -> short H.264 MP4 (Apple TV)
* foreign containers/codecs -> fMP4 HLS (Apple TV), audio copied untouched
  when AVPlayer can decode it so Dolby Atmos / multichannel survives
* anything a HomePod can't decode -> FLAC (lossless) for RAOP

Prepared files are served by an unauthenticated view under an unguessable
per-item token, because the Apple TV fetches them itself without HA auth.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import mimetypes
from pathlib import Path
import re
import secrets
import shutil
import tempfile
from urllib.parse import urlparse

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import DOMAIN, MEDIA_VIEW_URL

_LOGGER = logging.getLogger(__name__)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
# Things AVPlayer plays when handed the URL directly.
APPLE_NATIVE_EXTS = {
    ".mp4", ".m4v", ".mov", ".m3u8", ".mp3", ".m4a", ".aac", ".wav",
    ".aif", ".aiff", ".caf", ".flac", ".ac3", ".eac3", ".ec3",
}
APPLE_FOREIGN_EXTS = {
    ".mkv", ".webm", ".avi", ".wmv", ".flv", ".ts", ".m2ts", ".mts",
    ".mpg", ".mpeg", ".ogv", ".3gp", ".vob", ".divx",
}
AUDIO_EXTS = {
    ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".oga", ".opus",
    ".aif", ".aiff", ".alac", ".wma", ".ac3", ".eac3", ".ec3", ".mka",
}
# What pyatv's miniaudio decoder can read for RAOP streaming.
RAOP_DIRECT_EXTS = {".mp3", ".flac", ".wav", ".ogg", ".oga"}

APPLE_VIDEO_CODECS = {"h264", "hevc"}
# Audio codecs AVPlayer decodes inside fMP4/HLS. eac3 covers Dolby Atmos (JOC).
APPLE_AUDIO_CODECS = {"aac", "ac3", "eac3"}
APPLE_CONTAINERS = ("mov,mp4", "hls", "applehttp")

_CONTENT_TYPES = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".m4s": "video/iso.segment",
    ".mp4": "video/mp4",
    ".flac": "audio/flac",
}


@dataclass
class ProbeResult:
    """What ffmpeg found in a media URL."""

    container: str | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    audio_profile: str | None = None
    audio_channels: int | None = None
    audio_layout: str | None = None
    atmos: bool = False
    duration: float | None = None

    @property
    def apple_compatible(self) -> bool:
        """Return True if AVPlayer can play this without help."""
        container_ok = self.container is not None and self.container.startswith(
            APPLE_CONTAINERS
        )
        video_ok = self.video_codec is None or self.video_codec in APPLE_VIDEO_CODECS
        audio_ok = self.audio_codec is None or self.audio_codec in APPLE_AUDIO_CODECS | {
            "mp3", "alac", "flac",
        }
        return container_ok and video_ok and audio_ok

    def as_attributes(self) -> dict[str, object]:
        """Return a dict for entity attributes."""
        return {
            "codec": self.audio_codec,
            "profile": self.audio_profile,
            "channels": self.audio_channels,
            "layout": self.audio_layout,
            "dolby_atmos": self.atmos,
        }


@dataclass
class PreparedMedia:
    """Result of preparing a media item for a device."""

    token: str
    url: str | None = None  # served URL (Apple TV)
    path: str | None = None  # local file (HomePod RAOP)
    process: asyncio.subprocess.Process | None = field(default=None, repr=False)


def url_ext(url: str) -> str:
    """Return the lower-case file extension of a URL path."""
    return Path(urlparse(url).path).suffix.lower()


def guess_kind(url: str, media_type: str | None) -> str:
    """Classify media as image, video, audio or unknown."""
    mt = (media_type or "").lower()
    if mt.startswith("image") or mt in ("photo",):
        return "image"
    if mt.startswith("video") or mt in ("movie", "tvshow", "episode"):
        return "video"
    if mt.startswith("audio") or mt in ("music", "podcast", "track"):
        return "audio"
    ext = url_ext(url)
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "audio"
    guessed, _ = mimetypes.guess_type(urlparse(url).path)
    if guessed:
        return guessed.split("/", 1)[0] if guessed.split("/", 1)[0] in (
            "image", "video", "audio"
        ) else "unknown"
    return "unknown"


_CHANNEL_LAYOUTS = {
    "mono": 1, "stereo": 2, "2.1": 3, "3.0": 3, "quad": 4, "4.0": 4, "4.1": 5,
    "5.0": 5, "5.1": 6, "6.0": 6, "6.1": 7, "7.0": 7, "7.1": 8,
}
_RE_INPUT = re.compile(r"^Input #0, ([^ ]+?), from", re.M)
_RE_DURATION = re.compile(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)")
_RE_STREAM = re.compile(r"Stream #0:\d+.*?: (Video|Audio): (.+)$", re.M)


def parse_probe(stderr: str) -> ProbeResult:
    """Parse `ffmpeg -i` stderr output (first video and first audio stream)."""
    result = ProbeResult()
    if m := _RE_INPUT.search(stderr):
        result.container = m.group(1).rstrip(",")
    if m := _RE_DURATION.search(stderr):
        h, mi, s = m.groups()
        result.duration = int(h) * 3600 + int(mi) * 60 + float(s)
    for kind, desc in _RE_STREAM.findall(stderr):
        codec = desc.split()[0].rstrip(",").lower()
        if kind == "Video" and result.video_codec is None:
            if codec in ("mjpeg", "png", "bmp"):  # embedded cover art
                continue
            result.video_codec = codec
        elif kind == "Audio" and result.audio_codec is None:
            result.audio_codec = codec
            if (pm := re.match(r"\S+ \(([^)]*)\)", desc)) and "0x" not in pm.group(1):
                result.audio_profile = pm.group(1)
            result.atmos = "atmos" in desc.lower()
            parts = [p.strip() for p in desc.split(",")]
            for part in parts[1:]:
                base = part.split("(")[0].strip()
                if base in _CHANNEL_LAYOUTS:
                    result.audio_channels = _CHANNEL_LAYOUTS[base]
                    result.audio_layout = part
                    break
                if cm := re.match(r"(\d+) channels", part):
                    result.audio_channels = int(cm.group(1))
                    result.audio_layout = part
                    break
    return result


class MediaManager:
    """Prepare media with ffmpeg and serve the results to devices."""

    def __init__(self, hass: HomeAssistant, ffmpeg_binary: str) -> None:
        """Initialize."""
        self.hass = hass
        self.ffmpeg = ffmpeg_binary
        self.root = Path(tempfile.gettempdir()) / DOMAIN
        self._items: dict[str, PreparedMedia] = {}

    # ----------------------------------------------------------------- utils

    def base_url(self) -> str:
        """URL the Apple device can reach HA on (internal, plain HTTP preferred)."""
        try:
            return get_url(
                self.hass, allow_external=False, allow_cloud=False, prefer_external=False
            )
        except NoURLAvailableError:
            return get_url(self.hass, allow_cloud=False)

    def item_dir(self, token: str) -> Path:
        """Return directory for a token."""
        return self.root / token

    def _new_item(self) -> PreparedMedia:
        token = secrets.token_urlsafe(24)
        self.item_dir(token).mkdir(parents=True, exist_ok=True)
        item = PreparedMedia(token=token)
        self._items[token] = item
        return item

    def is_served(self, token: str) -> bool:
        """Return True if token is a live item."""
        return token in self._items

    async def async_release(self, item: PreparedMedia | None) -> None:
        """Stop any ffmpeg process and delete files for an item."""
        if item is None:
            return
        self._items.pop(item.token, None)
        if item.process and item.process.returncode is None:
            item.process.kill()
            await item.process.wait()
        await self.hass.async_add_executor_job(
            shutil.rmtree, self.item_dir(item.token), True
        )

    async def async_cleanup_all(self) -> None:
        """Remove every prepared item (on unload)."""
        for item in list(self._items.values()):
            await self.async_release(item)
        await self.hass.async_add_executor_job(shutil.rmtree, self.root, True)

    async def _run(self, args: list[str], timeout: float) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            self.ffmpeg, "-nostdin", "-hide_banner", *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise HomeAssistantError("ffmpeg timed out preparing media") from None
        return proc.returncode or 0, err.decode(errors="replace")

    # ----------------------------------------------------------------- probe

    async def async_probe(self, url: str) -> ProbeResult | None:
        """Inspect a media URL's container and streams."""
        try:
            _, err = await self._run(["-i", url], timeout=20)
        except (HomeAssistantError, OSError) as ex:
            _LOGGER.debug("Probe of %s failed: %s", url, ex)
            return None
        result = parse_probe(err)
        _LOGGER.debug("Probe %s -> %s", url, result)
        return result

    # ----------------------------------------------------------------- image

    async def async_image_to_video(
        self, url: str, duration: int, max_w: int, max_h: int
    ) -> PreparedMedia:
        """Turn an image into a short MP4 the Apple TV can display."""
        item = self._new_item()
        folder = self.item_dir(item.token)
        ext = url_ext(url) if url_ext(url) in IMAGE_EXTS else ".img"
        src = folder / f"source{ext}"
        try:
            session = async_get_clientsession(self.hass, verify_ssl=False)
            async with session.get(url) as resp:
                resp.raise_for_status()
                data = await resp.read()
            await self.hass.async_add_executor_job(src.write_bytes, data)

            vf = (
                f"scale=w='min(iw,{max_w})':h='min(ih,{max_h})'"
                ":force_original_aspect_ratio=decrease:force_divisible_by=2,"
                f"pad={max_w}:{max_h}:(ow-iw)/2:(oh-ih)/2:color=black,format=yuv420p"
            )
            input_args = (
                ["-ignore_loop", "0", "-i", str(src)]
                if ext == ".gif"
                else ["-loop", "1", "-framerate", "5", "-i", str(src)]
            )
            code, err = await self._run(
                [
                    "-y", *input_args, "-t", str(duration), "-vf", vf,
                    "-r", "5", "-c:v", "libx264", "-preset", "veryfast",
                    "-tune", "stillimage", "-movflags", "+faststart",
                    "-an", str(folder / "image.mp4"),
                ],
                timeout=120,
            )
            if code != 0:
                raise HomeAssistantError(f"Could not convert image: {err[-400:]}")
        except Exception:
            await self.async_release(item)
            raise
        item.url = self._served_url(item, "image.mp4")
        return item

    # ------------------------------------------------------------------- HLS

    async def async_to_hls(self, url: str, probe: ProbeResult | None) -> PreparedMedia:
        """Repackage (and if needed transcode) media into fMP4 HLS for AVPlayer.

        Video is copied when H.264/HEVC. Audio is copied when AAC/AC-3/E-AC-3,
        which keeps Dolby Atmos (E-AC-3 JOC) bit-exact. Other multichannel audio
        (DTS, TrueHD, multichannel FLAC/PCM/Opus) becomes 5.1 E-AC-3 instead of
        being downmixed, so the Apple TV can still render it spatially.
        """
        probe = probe or ProbeResult()
        item = self._new_item()
        folder = self.item_dir(item.token)

        args = ["-fflags", "+genpts", "-i", url, "-map", "0:v:0?", "-map", "0:a:0?"]
        if probe.video_codec in APPLE_VIDEO_CODECS:
            args += ["-c:v", "copy"]
            if probe.video_codec == "hevc":
                args += ["-tag:v", "hvc1"]
        else:
            args += [
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p", "-force_key_frames", "expr:gte(t,n_forced*4)",
            ]
        if probe.audio_codec in APPLE_AUDIO_CODECS:
            args += ["-c:a", "copy"]
        elif (probe.audio_channels or 2) > 2:
            args += ["-c:a", "eac3", "-b:a", "640k", "-ac", str(min(probe.audio_channels or 6, 6))]
        else:
            args += ["-c:a", "aac", "-b:a", "256k"]
        args += [
            "-f", "hls", "-hls_time", "4", "-hls_playlist_type", "event",
            "-hls_segment_type", "fmp4", "-hls_flags", "independent_segments",
            # Relative names + cwd=folder: otherwise ffmpeg drops init.mp4
            # into HA's working directory (/config).
            "-hls_fmp4_init_filename", "init.mp4",
            "-hls_segment_filename", "seg%05d.m4s",
            "-start_number", "0", "index.m3u8",
        ]
        _LOGGER.debug("Starting HLS repackaging: %s", args)
        item.process = await asyncio.create_subprocess_exec(
            self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(folder),
        )
        playlist = folder / "index.m3u8"

        def _ready() -> bool:
            if not playlist.exists():
                return False
            text = playlist.read_text(errors="ignore")
            return text.count("#EXTINF") >= 2 or "#EXT-X-ENDLIST" in text

        try:
            for _ in range(120):  # up to 60 s for the first segments
                if await self.hass.async_add_executor_job(_ready):
                    break
                if item.process.returncode not in (None, 0):
                    err = await item.process.stderr.read() if item.process.stderr else b""
                    raise HomeAssistantError(
                        f"ffmpeg failed to repackage media: {err.decode(errors='replace')[-400:]}"
                    )
                await asyncio.sleep(0.5)
            else:
                raise HomeAssistantError("Timed out waiting for ffmpeg to start the stream")
        except Exception:
            await self.async_release(item)
            raise
        item.url = self._served_url(item, "index.m3u8")
        return item

    # ------------------------------------------------------------------ FLAC

    async def async_to_flac(self, url: str) -> PreparedMedia:
        """Decode any audio (or a video's audio track) to FLAC for RAOP."""
        item = self._new_item()
        out = self.item_dir(item.token) / "audio.flac"
        try:
            code, err = await self._run(
                ["-y", "-i", url, "-vn", "-map", "0:a:0", "-c:a", "flac",
                 "-ar", "44100", "-ac", "2", str(out)],
                timeout=600,
            )
            if code != 0:
                raise HomeAssistantError(f"Could not decode audio: {err[-400:]}")
        except Exception:
            await self.async_release(item)
            raise
        item.path = str(out)
        return item

    def _served_url(self, item: PreparedMedia, name: str) -> str:
        return f"{self.base_url()}{MEDIA_VIEW_URL}/{item.token}/{name}"


class MediaView(HomeAssistantView):
    """Serve prepared media to Apple devices (token protected, no HA auth)."""

    url = MEDIA_VIEW_URL + "/{token}/{name}"
    name = f"api:{DOMAIN}:media"
    requires_auth = False

    def __init__(self, manager: MediaManager) -> None:
        """Initialize."""
        self.manager = manager

    async def get(self, request: web.Request, token: str, name: str) -> web.StreamResponse:
        """Serve a prepared file."""
        if not self.manager.is_served(token) or "/" in name or "\\" in name or name.startswith("."):
            raise web.HTTPNotFound
        path = self.manager.item_dir(token) / name
        if not await self.manager.hass.async_add_executor_job(path.is_file):
            raise web.HTTPNotFound
        headers = {"Cache-Control": "no-cache"}
        if ctype := _CONTENT_TYPES.get(path.suffix):
            headers["Content-Type"] = ctype
        return web.FileResponse(path, headers=headers)
