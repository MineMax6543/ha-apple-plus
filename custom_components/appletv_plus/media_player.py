"""Media player entity for AppleTV Plus."""

from __future__ import annotations

import logging
from typing import Any

from pyatv.const import (
    DeviceModel,
    DeviceState,
    FeatureName,
    MediaType as AtvMediaType,
    PowerState,
    RepeatState,
    ShuffleState,
)
from pyatv.interface import MediaMetadata

from homeassistant.components import media_source
from homeassistant.components.media_player import (
    ATTR_MEDIA_EXTRA,
    BrowseMedia,
    MediaClass,
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    RepeatMode,
    async_process_play_media_url,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AppleConfigEntry
from .airplay_patch import FALLBACK_WAITER
from .const import (
    AUDIO_PATH_DEVICE,
    AUDIO_PATH_STEREO,
    CONF_AIRPLAY_ID,
    CONF_DEVICE_KIND,
    CONF_IDENTIFIERS,
    CONF_IMAGE_DURATION,
    CONF_START_TIMEOUT,
    CONF_VIDEO_COMPAT,
    DEFAULT_IMAGE_DURATION,
    DEFAULT_START_TIMEOUT,
    DEFAULT_VIDEO_COMPAT,
    DOMAIN,
    KIND_APPLE_TV,
    KIND_HOMEPOD,
    SIGNAL_UPDATE,
    VIDEO_COMPAT_ALWAYS,
    VIDEO_COMPAT_DIRECT,
)
from .device import AppleDevice
from .entity import AppleEntity
from .media import (
    APPLE_FOREIGN_EXTS,
    APPLE_NATIVE_EXTS,
    RAOP_DIRECT_EXTS,
    ProbeResult,
    guess_kind,
    url_ext,
)

_LOGGER = logging.getLogger(__name__)

STATE_MAP = {
    DeviceState.Idle: MediaPlayerState.IDLE,
    DeviceState.Loading: MediaPlayerState.BUFFERING,
    DeviceState.Paused: MediaPlayerState.PAUSED,
    DeviceState.Playing: MediaPlayerState.PLAYING,
    DeviceState.Seeking: MediaPlayerState.PLAYING,
    DeviceState.Stopped: MediaPlayerState.IDLE,
}
TYPE_MAP = {
    AtvMediaType.Music: MediaType.MUSIC,
    AtvMediaType.Video: MediaType.VIDEO,
    AtvMediaType.TV: MediaType.TVSHOW,
}
APPS_ID = "apps"
APP_PREFIX = "app:"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AppleConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the media player."""
    async_add_entities([AppleMediaPlayer(entry.runtime_data)])


class AppleMediaPlayer(AppleEntity, MediaPlayerEntity):
    """Apple TV / HomePod media player."""

    _attr_name = None
    _attr_media_image_remotely_accessible = False

    def __init__(self, device: AppleDevice) -> None:
        """Initialize."""
        super().__init__(device)
        self._attr_unique_id = device.entry.unique_id
        kind = device.entry.data.get(CONF_DEVICE_KIND)
        self._kind = kind
        self._attr_device_class = (
            MediaPlayerDeviceClass.TV
            if kind == KIND_APPLE_TV
            else MediaPlayerDeviceClass.SPEAKER
        )

    # ------------------------------------------------------------ helpers

    @property
    def _options(self) -> dict[str, Any]:
        return dict(self.device.entry.options)

    @property
    def _playing(self):
        return self.device.playing

    # ------------------------------------------------------------ state

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        """Flag features based on what the device actually reports."""
        dev = self.device
        f = MediaPlayerEntityFeature(0)
        if not dev.available:
            return f
        f |= MediaPlayerEntityFeature.PLAY_MEDIA | MediaPlayerEntityFeature.BROWSE_MEDIA
        f |= MediaPlayerEntityFeature.STOP
        mapping = {
            FeatureName.Play: MediaPlayerEntityFeature.PLAY,
            FeatureName.Pause: MediaPlayerEntityFeature.PAUSE,
            FeatureName.Next: MediaPlayerEntityFeature.NEXT_TRACK,
            FeatureName.Previous: MediaPlayerEntityFeature.PREVIOUS_TRACK,
            FeatureName.SetPosition: MediaPlayerEntityFeature.SEEK,
            FeatureName.SetShuffle: MediaPlayerEntityFeature.SHUFFLE_SET,
            FeatureName.SetRepeat: MediaPlayerEntityFeature.REPEAT_SET,
            FeatureName.TurnOn: MediaPlayerEntityFeature.TURN_ON,
            FeatureName.TurnOff: MediaPlayerEntityFeature.TURN_OFF,
            FeatureName.SetVolume: MediaPlayerEntityFeature.VOLUME_SET,
            FeatureName.VolumeUp: MediaPlayerEntityFeature.VOLUME_STEP,
            FeatureName.LaunchApp: MediaPlayerEntityFeature.SELECT_SOURCE,
            FeatureName.SetOutputDevices: MediaPlayerEntityFeature.GROUPING,
        }
        for name, flag in mapping.items():
            if dev.supports(name):
                f |= flag
        return f

    @property
    def state(self) -> MediaPlayerState | None:
        """Return the state."""
        dev = self.device
        if dev.power_state == PowerState.Off:
            return MediaPlayerState.OFF
        if self._playing is not None:
            state = STATE_MAP.get(self._playing.device_state)
            if state is not None:
                if state == MediaPlayerState.IDLE and dev.is_playing_media:
                    return MediaPlayerState.BUFFERING
                return state
        if dev.is_playing_media:
            return MediaPlayerState.PLAYING
        return MediaPlayerState.IDLE if self._kind == KIND_HOMEPOD else MediaPlayerState.ON

    @property
    def media_content_type(self) -> MediaType | str | None:
        """Return content type."""
        if self._playing is None:
            return None
        if mt := TYPE_MAP.get(self._playing.media_type):
            return mt
        return self.device.current_item.get("kind")

    @property
    def media_content_id(self) -> str | None:
        """Return content id."""
        if self._playing is None:
            return None
        return self._playing.content_identifier or self.device.current_item.get("url")

    @property
    def media_title(self) -> str | None:
        """Return title."""
        if self._playing is None:
            return None
        return self._playing.title or self.device.current_item.get("title")

    @property
    def media_artist(self) -> str | None:
        """Return artist."""
        return self._playing.artist if self._playing else None

    @property
    def media_album_name(self) -> str | None:
        """Return album."""
        return self._playing.album if self._playing else None

    @property
    def media_series_title(self) -> str | None:
        """Return series title."""
        return self._playing.series_name if self._playing else None

    @property
    def media_season(self) -> str | None:
        """Return season."""
        if self._playing and self._playing.season_number is not None:
            return str(self._playing.season_number)
        return None

    @property
    def media_episode(self) -> str | None:
        """Return episode."""
        if self._playing and self._playing.episode_number is not None:
            return str(self._playing.episode_number)
        return None

    @property
    def media_duration(self) -> int | None:
        """Return duration in seconds."""
        return self._playing.total_time if self._playing else None

    @property
    def media_position(self) -> int | None:
        """Return position in seconds."""
        return self._playing.position if self._playing else None

    @property
    def media_position_updated_at(self):
        """Return when the position was read from the device."""
        return self.device.position_updated_at if self._playing else None

    @property
    def shuffle(self) -> bool | None:
        """Return shuffle state."""
        if self._playing is None or self._playing.shuffle is None:
            return None
        return self._playing.shuffle != ShuffleState.Off

    @property
    def repeat(self) -> RepeatMode | None:
        """Return repeat state."""
        if self._playing is None or self._playing.repeat is None:
            return None
        return {
            RepeatState.Track: RepeatMode.ONE,
            RepeatState.All: RepeatMode.ALL,
        }.get(self._playing.repeat, RepeatMode.OFF)

    @property
    def volume_level(self) -> float | None:
        """Return volume 0..1."""
        if self.device.volume is None or not self.device.supports(FeatureName.Volume):
            return None
        return self.device.volume / 100

    @property
    def app_id(self) -> str | None:
        """Return the bundle id of the foreground app."""
        atv = self.device.atv
        if atv is None or not self.device.supports(FeatureName.App):
            return None
        try:
            return atv.metadata.app.identifier if atv.metadata.app else None
        except Exception:  # noqa: BLE001
            return None

    @property
    def app_name(self) -> str | None:
        """Return the name of the foreground app."""
        atv = self.device.atv
        if atv is None or not self.device.supports(FeatureName.App):
            return None
        try:
            return atv.metadata.app.name if atv.metadata.app else None
        except Exception:  # noqa: BLE001
            return None

    @property
    def source(self) -> str | None:
        """Return current app."""
        return self.app_name

    @property
    def source_list(self) -> list[str] | None:
        """Return installed apps."""
        return [a.name for a in self.device.apps if a.name] or None

    @property
    def media_image_hash(self) -> str | None:
        """Return artwork hash so HA re-fetches only on change."""
        atv = self.device.atv
        if atv is None or self._playing is None or not self.device.supports(
            FeatureName.Artwork
        ):
            return None
        try:
            return atv.metadata.artwork_id
        except Exception:  # noqa: BLE001
            return None

    async def async_get_media_image(self) -> tuple[bytes | None, str | None]:
        """Fetch artwork."""
        atv = self.device.atv
        if atv is None:
            return None, None
        try:
            art = await atv.metadata.artwork(width=512)
        except Exception:  # noqa: BLE001
            return None, None
        if art is None:
            return None, None
        return art.bytes, art.mimetype

    @property
    def group_members(self) -> list[str] | None:
        """Return entity ids of HomePods this Apple TV/HomePod outputs to."""
        if not self.device.output_devices:
            return None
        ent_reg = er.async_get(self.hass)
        members: list[str] = []
        if self.entity_id:
            members.append(self.entity_id)
        for out in self.device.output_devices:
            for entry in self.hass.config_entries.async_entries(DOMAIN):
                if entry.entry_id == self.device.entry.entry_id:
                    continue
                matches = out.identifier in entry.data.get(
                    CONF_IDENTIFIERS, []
                ) or out.identifier == entry.data.get(CONF_AIRPLAY_ID)
                if matches and (
                    eid := ent_reg.async_get_entity_id(
                        "media_player", DOMAIN, entry.unique_id
                    )
                ):
                    members.append(eid)
        return members if len(members) > 1 else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose how audio is being delivered (spatial-audio relevant)."""
        item = self.device.current_item
        attrs: dict[str, Any] = {}
        if self.device.conf is not None:
            info = self.device.conf.device_info
            attrs["model"] = info.model_str if info.model != DeviceModel.Unknown else info.raw_model
        if self.device.output_devices:
            attrs["audio_outputs"] = [o.name for o in self.device.output_devices]
        if item:
            attrs["audio_path"] = item.get("audio_path")
            attrs["spatial_capable"] = item.get("spatial_capable")
            if src := item.get("source_audio"):
                attrs["source_audio"] = src
            attrs["delivery"] = item.get("delivery")
        if self.device.last_error:
            attrs["last_error"] = self.device.last_error
        return attrs

    # ------------------------------------------------------------ updates

    async def async_added_to_hass(self) -> None:
        """Subscribe to device updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_UPDATE.format(self.device.entry.entry_id),
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    # ------------------------------------------------------------ commands

    async def async_turn_on(self) -> None:
        """Turn on."""
        await self.device.async_run(lambda atv: atv.power.turn_on())

    async def async_turn_off(self) -> None:
        """Turn off."""
        await self.device.async_stop_playback()
        await self.device.async_run(lambda atv: atv.power.turn_off())

    async def async_media_play(self) -> None:
        """Play."""
        await self.device.async_run(lambda atv: atv.remote_control.play())

    async def async_media_pause(self) -> None:
        """Pause."""
        await self.device.async_run(lambda atv: atv.remote_control.pause())

    async def async_media_play_pause(self) -> None:
        """Toggle play/pause."""
        await self.device.async_run(lambda atv: atv.remote_control.play_pause())

    async def async_media_stop(self) -> None:
        """Stop; also ends media we are streaming."""
        if self.device.is_playing_media:
            await self.device.async_stop_playback()
            return
        await self.device.async_run(lambda atv: atv.remote_control.stop())

    async def async_media_next_track(self) -> None:
        """Next."""
        await self.device.async_run(lambda atv: atv.remote_control.next())

    async def async_media_previous_track(self) -> None:
        """Previous."""
        await self.device.async_run(lambda atv: atv.remote_control.previous())

    async def async_media_seek(self, position: float) -> None:
        """Seek."""
        await self.device.async_run(
            lambda atv: atv.remote_control.set_position(int(position))
        )

    async def async_set_volume_level(self, volume: float) -> None:
        """Set volume."""
        await self.device.async_run(lambda atv: atv.audio.set_volume(volume * 100))

    async def async_volume_up(self) -> None:
        """Volume up."""
        await self.device.async_run(lambda atv: atv.audio.volume_up())

    async def async_volume_down(self) -> None:
        """Volume down."""
        await self.device.async_run(lambda atv: atv.audio.volume_down())

    async def async_set_shuffle(self, shuffle: bool) -> None:
        """Set shuffle."""
        await self.device.async_run(
            lambda atv: atv.remote_control.set_shuffle(
                ShuffleState.Songs if shuffle else ShuffleState.Off
            )
        )

    async def async_set_repeat(self, repeat: RepeatMode) -> None:
        """Set repeat."""
        state = {
            RepeatMode.ONE: RepeatState.Track,
            RepeatMode.ALL: RepeatState.All,
        }.get(repeat, RepeatState.Off)
        await self.device.async_run(lambda atv: atv.remote_control.set_repeat(state))

    async def async_select_source(self, source: str) -> None:
        """Launch an app by name."""
        for app in self.device.apps:
            if app.name == source:
                await self.device.async_run(
                    lambda atv, bundle=app.identifier: atv.apps.launch_app(bundle)
                )
                return
        raise HomeAssistantError(f"Unknown app: {source}")

    # ------------------------------------------------------------ grouping

    def _airplay_id_for(self, entity_id: str) -> str:
        ent = er.async_get(self.hass).async_get(entity_id)
        if ent is None or ent.platform != DOMAIN or ent.config_entry_id is None:
            raise HomeAssistantError(
                f"{entity_id} is not an AppleTV Plus device and cannot be grouped"
            )
        entry = self.hass.config_entries.async_get_entry(ent.config_entry_id)
        assert entry is not None
        if airplay_id := entry.data.get(CONF_AIRPLAY_ID):
            return airplay_id
        return entry.data[CONF_IDENTIFIERS][0]

    async def async_join_players(self, group_members: list[str]) -> None:
        """Send this device's audio to other HomePods/Apple TVs as well."""
        ids = [self._airplay_id_for(e) for e in group_members if e != self.entity_id]
        if ids:
            await self.device.async_run(lambda atv: atv.audio.add_output_devices(*ids))

    async def async_unjoin_player(self) -> None:
        """Route audio back to this device only."""
        own = self.device.entry.data.get(CONF_AIRPLAY_ID)
        others = [
            o.identifier
            for o in self.device.output_devices
            if o.identifier != own
            and o.identifier not in self.device.entry.data.get(CONF_IDENTIFIERS, [])
        ]
        if others:
            await self.device.async_run(
                lambda atv: atv.audio.remove_output_devices(*others)
            )

    # ------------------------------------------------------------ browse

    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """Browse apps (Apple TV) and HA media sources."""
        if media_content_id == APPS_ID:
            return self._apps_folder(with_children=True)

        if self._kind == KIND_HOMEPOD:
            content_filter = lambda item: item.media_content_type.startswith("audio/")  # noqa: E731
        else:
            content_filter = lambda item: item.media_content_type.startswith(  # noqa: E731
                ("audio/", "video/", "image/", "application/vnd.apple.mpegurl")
            )

        if media_content_id is not None:
            return await media_source.async_browse_media(
                self.hass, media_content_id, content_filter=content_filter
            )

        sources = await media_source.async_browse_media(
            self.hass, None, content_filter=content_filter
        )
        if not self.device.apps:
            return sources
        return BrowseMedia(
            media_class=MediaClass.DIRECTORY,
            media_content_id="",
            media_content_type="",
            title=self.device.name,
            can_play=False,
            can_expand=True,
            children=[self._apps_folder(with_children=False), sources],
            children_media_class=MediaClass.DIRECTORY,
        )

    def _apps_folder(self, with_children: bool) -> BrowseMedia:
        children = None
        if with_children:
            children = [
                BrowseMedia(
                    media_class=MediaClass.APP,
                    media_content_id=f"{APP_PREFIX}{app.identifier}",
                    media_content_type=MediaType.APP,
                    title=app.name or app.identifier,
                    can_play=True,
                    can_expand=False,
                )
                for app in self.device.apps
            ]
        return BrowseMedia(
            media_class=MediaClass.DIRECTORY,
            media_content_id=APPS_ID,
            media_content_type=MediaType.APPS,
            title="Apps",
            can_play=False,
            can_expand=True,
            children=children,
            children_media_class=MediaClass.APP,
        )

    # ------------------------------------------------------------ play_media

    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: Any
    ) -> None:
        """Play media: apps, URLs, media-source items, images, video, audio.

        `extra` (in the play_media service data) may contain:
          duration: seconds an image stays on screen
          position: start offset in seconds (Apple TV)
          compat: "auto" | "direct" | "always" overrides the option
          title / artist / album: metadata shown on HomePods
        """
        extra: dict[str, Any] = kwargs.get(ATTR_MEDIA_EXTRA) or {}

        if media_type == MediaType.APP or media_id.startswith(APP_PREFIX):
            bundle = media_id.removeprefix(APP_PREFIX)
            await self.device.async_run(lambda atv: atv.apps.launch_app(bundle))
            return

        title = extra.get("title")
        if media_source.is_media_source_id(media_id):
            play_item = await media_source.async_resolve_media(
                self.hass, media_id, self.entity_id
            )
            media_id = play_item.url
            media_type = play_item.mime_type or media_type
        media_id = async_process_play_media_url(self.hass, media_id)
        kind = guess_kind(media_id, str(media_type))

        if self.device.supports(FeatureName.PlayUrl):
            await self._play_on_apple_tv(media_id, kind, extra, title)
        elif self.device.supports(FeatureName.StreamFile):
            await self._play_on_speaker(media_id, kind, extra, title)
        else:
            raise HomeAssistantError(
                f"{self.device.name} can't play URLs. Apple TVs need AirPlay "
                "pairing for this; re-add the device and complete the AirPlay step."
            )

    async def _play_on_apple_tv(
        self, url: str, kind: str, extra: dict[str, Any], title: str | None
    ) -> None:
        dev = self.device
        media = dev.media
        compat = extra.get("compat") or self._options.get(
            CONF_VIDEO_COMPAT, DEFAULT_VIDEO_COMPAT
        )
        position = int(extra.get("position", 0))
        prepared = None
        probe: ProbeResult | None = None
        delivery = "direct"
        max_seconds: float | None = None

        if kind == "image":
            duration = int(
                extra.get("duration")
                or self._options.get(CONF_IMAGE_DURATION, DEFAULT_IMAGE_DURATION)
            )
            four_k = dev.conf is not None and dev.conf.device_info.model not in (
                DeviceModel.Gen4,
                DeviceModel.Gen3,
                DeviceModel.Gen2,
            )
            w, h = (3840, 2160) if four_k else (1920, 1080)
            prepared = await media.async_image_to_video(url, duration, w, h)
            max_seconds = duration
            play_url, delivery = prepared.url, "image_as_video"
        else:
            ext = url_ext(url)
            needs_check = compat == VIDEO_COMPAT_ALWAYS or (
                compat != VIDEO_COMPAT_DIRECT
                and (ext in APPLE_FOREIGN_EXTS or ext not in APPLE_NATIVE_EXTS)
            )
            if needs_check:
                probe = await media.async_probe(url)
            if compat == VIDEO_COMPAT_ALWAYS or (
                needs_check
                and (
                    ext in APPLE_FOREIGN_EXTS
                    or (probe is not None and not probe.apple_compatible)
                )
            ):
                prepared = await media.async_to_hls(url, probe)
                play_url, delivery = prepared.url, "hls_repackaged"
            else:
                play_url = url

        if probe is not None and probe.duration and max_seconds is None:
            max_seconds = probe.duration - position
        info = self._item_info(url, kind, title, probe, AUDIO_PATH_DEVICE, delivery)

        async def job() -> None:
            assert dev.atv is not None
            # pyatv copies this context into its play task; see airplay_patch.
            token = FALLBACK_WAITER.set(
                lambda: dev.async_wait_airplay_session(max_seconds)
            )
            try:
                await dev.atv.stream.play_url(play_url, position=position)
            finally:
                FALLBACK_WAITER.reset(token)

        await dev.async_start_playback(job, prepared, info, self._start_timeout)
        if probe is None and kind in ("video", "audio"):
            # Fill in spatial-audio info without delaying playback.
            self.hass.async_create_task(self._late_probe(url, info))

    async def _play_on_speaker(
        self, url: str, kind: str, extra: dict[str, Any], title: str | None
    ) -> None:
        dev = self.device
        if kind == "image":
            raise HomeAssistantError(f"{dev.name} has no screen; it can't show images")
        prepared = None
        source: str = url
        delivery = "direct"
        if url_ext(url) not in RAOP_DIRECT_EXTS:
            prepared = await dev.media.async_to_flac(url)
            source, delivery = prepared.path, "decoded_to_flac"

        meta = MediaMetadata(
            title=title, artist=extra.get("artist"), album=extra.get("album")
        )
        info = self._item_info(url, kind, title, None, AUDIO_PATH_STEREO, delivery)

        async def job() -> None:
            assert dev.atv is not None
            await dev.atv.stream.stream_file(
                source, metadata=meta, override_missing_metadata=True
            )

        await dev.async_start_playback(job, prepared, info, self._start_timeout)
        self.hass.async_create_task(self._late_probe(url, info))

    @property
    def _start_timeout(self) -> float:
        return float(self._options.get(CONF_START_TIMEOUT, DEFAULT_START_TIMEOUT))

    def _item_info(
        self,
        url: str,
        kind: str,
        title: str | None,
        probe: ProbeResult | None,
        audio_path: str,
        delivery: str,
    ) -> dict[str, Any]:
        info: dict[str, Any] = {
            "url": url,
            "kind": kind,
            "title": title,
            "audio_path": audio_path,
            "delivery": delivery,
            "spatial_capable": None,
        }
        self._apply_probe(info, probe)
        return info

    @staticmethod
    def _apply_probe(info: dict[str, Any], probe: ProbeResult | None) -> None:
        if probe is None or probe.audio_codec is None:
            return
        info["source_audio"] = probe.as_attributes()
        multichannel = (probe.audio_channels or 2) > 2 or probe.atmos
        # Only true when the Apple TV decodes the original multichannel/Atmos
        # stream itself; RAOP to a HomePod is always stereo.
        info["spatial_capable"] = info["audio_path"] == AUDIO_PATH_DEVICE and multichannel

    async def _late_probe(self, url: str, info: dict[str, Any]) -> None:
        probe = await self.device.media.async_probe(url)
        if self.device.current_item is info:
            self._apply_probe(info, probe)
            self.async_write_ha_state()
