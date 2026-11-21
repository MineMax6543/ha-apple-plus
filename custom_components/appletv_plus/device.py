"""Connection and state manager for one Apple TV / HomePod."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import timedelta
import logging
from typing import Any

import pyatv
from pyatv import exceptions as atv_exc
from pyatv.const import (
    DeviceModel,
    DeviceState,
    FeatureName,
    FeatureState,
    OperatingSystem,
    PowerState,
    Protocol,
)
from pyatv.interface import (
    App,
    AppleTV,
    AudioListener,
    BaseConfig,
    DeviceListener,
    OutputDevice,
    Playing,
    PowerListener,
    PushListener,
)

from homeassistant.components import zeroconf
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CREDENTIALS,
    CONF_IDENTIFIERS,
    CONF_PASSWORDS,
    KIND_APPLE_TV,
    KIND_HOMEPOD,
    KIND_OTHER,
    RECONNECT_MAX,
    RECONNECT_MIN,
    RESYNC_INTERVAL,
    SIGNAL_UPDATE,
)
from .media import MediaManager, PreparedMedia

_LOGGER = logging.getLogger(__name__)

AIRPLAY_PLAYER_BUNDLE = "com.apple.TVAirPlay"
# How long the Apple TV may take to show the AirPlay player after /play.
AIRPLAY_START_GRACE = 20

HOMEPOD_MODELS = {
    DeviceModel.HomePod,
    DeviceModel.HomePodMini,
    getattr(DeviceModel, "HomePodGen2", DeviceModel.HomePod),
}


def device_kind(conf: BaseConfig) -> str:
    """Classify a scanned device."""
    info = conf.device_info
    raw = (info.raw_model or "").lower()
    if info.model in HOMEPOD_MODELS or raw.startswith("audioaccessory"):
        return KIND_HOMEPOD
    if info.operating_system == OperatingSystem.TvOS or raw.startswith("appletv"):
        return KIND_APPLE_TV
    return KIND_OTHER


def apply_secrets(conf: BaseConfig, data: dict[str, Any]) -> None:
    """Apply stored credentials and passwords to a scanned config."""
    for proto_name, creds in data.get(CONF_CREDENTIALS, {}).items():
        if creds and proto_name in Protocol.__members__:
            conf.set_credentials(Protocol[proto_name], creds)
    for proto_name, password in data.get(CONF_PASSWORDS, {}).items():
        if proto_name in Protocol.__members__ and (
            service := conf.get_service(Protocol[proto_name])
        ):
            service.password = password


class AppleDevice(PushListener, DeviceListener, PowerListener, AudioListener):
    """Owns the pyatv connection, tracks state and runs playback jobs."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, media: MediaManager
    ) -> None:
        """Initialize."""
        self.hass = hass
        self.entry = entry
        self.media = media
        self.atv: AppleTV | None = None
        self.conf: BaseConfig | None = None

        self.playing: Playing | None = None
        self.position_updated_at = None
        self.power_state: PowerState = PowerState.Unknown
        self.volume: float | None = None
        self.output_devices: list[OutputDevice] = []
        self.apps: list[App] = []

        # Info about media started by this integration.
        self.current_item: dict[str, Any] = {}
        self.last_error: str | None = None

        self._play_task: asyncio.Task | None = None
        self._prepared: PreparedMedia | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._closing = False
        self._unsub_resync: CALLBACK_TYPE | None = None

    # ------------------------------------------------------------ properties

    @property
    def available(self) -> bool:
        """Return True if connected."""
        return self.atv is not None

    @property
    def name(self) -> str:
        """Return device name."""
        return self.entry.title

    def supports(self, *features: FeatureName) -> bool:
        """Return True if all features are supported (available now or later)."""
        if self.atv is None:
            return False
        return self.atv.features.in_state(
            [FeatureState.Available, FeatureState.Unavailable], *features
        )

    def available_now(self, *features: FeatureName) -> bool:
        """Return True if all features can be used right now."""
        if self.atv is None:
            return False
        return self.atv.features.in_state(FeatureState.Available, *features)

    @property
    def is_playing_media(self) -> bool:
        """Return True if a playback job started by us is running."""
        return self._play_task is not None and not self._play_task.done()

    # ------------------------------------------------------------ lifecycle

    async def async_first_connect(self) -> None:
        """Connect during setup; raise on failure so HA retries."""
        await self._async_connect()

    async def _async_scan(self) -> BaseConfig | None:
        data = self.entry.data
        identifiers = set(data[CONF_IDENTIFIERS])
        aiozc = await zeroconf.async_get_async_instance(self.hass)
        host = data.get(CONF_HOST)
        if host:
            found = await pyatv.scan(
                self.hass.loop, identifier=identifiers, hosts=[host], timeout=5
            )
            if found:
                return found[0]
        # Address may have changed: multicast scan by identifier.
        found = await pyatv.scan(
            self.hass.loop, identifier=identifiers, aiozc=aiozc, timeout=8
        )
        if found:
            new_host = str(found[0].address)
            if new_host != host:
                _LOGGER.info("%s moved to %s", self.name, new_host)
                self.hass.config_entries.async_update_entry(
                    self.entry, data={**data, CONF_HOST: new_host}
                )
            return found[0]
        return None

    async def _async_connect(self) -> None:
        conf = await self._async_scan()
        if conf is None:
            raise HomeAssistantError(f"{self.name} not found on the network")
        apply_secrets(conf, dict(self.entry.data))
        atv = await pyatv.connect(
            conf, self.hass.loop, session=async_get_clientsession(self.hass)
        )
        self.conf = conf
        self.atv = atv
        atv.listener = self
        atv.power.listener = self
        atv.audio.listener = self
        try:
            atv.push_updater.listener = self
            atv.push_updater.start()
        except atv_exc.NotSupportedError:
            _LOGGER.debug("%s does not support push updates", self.name)

        self._read_initial_state()
        if self.supports(FeatureName.AppList):
            try:
                self.apps = sorted(await atv.apps.app_list(), key=lambda a: a.name or "")
            except Exception as ex:  # noqa: BLE001 - app list is optional
                _LOGGER.debug("Could not fetch app list: %s", ex)

        if self._unsub_resync is None:
            self._unsub_resync = async_track_time_interval(
                self.hass, self._async_resync, timedelta(seconds=RESYNC_INTERVAL)
            )
        _LOGGER.info("Connected to %s (%s)", self.name, conf.address)
        self._notify()

    def _read_initial_state(self) -> None:
        atv = self.atv
        assert atv is not None
        try:
            self.power_state = atv.power.power_state
        except atv_exc.NotSupportedError:
            self.power_state = PowerState.Unknown
        try:
            self.volume = atv.audio.volume
        except atv_exc.NotSupportedError:
            self.volume = None
        try:
            self.output_devices = list(atv.audio.output_devices)
        except atv_exc.NotSupportedError:
            self.output_devices = []

    async def async_close(self) -> None:
        """Close the connection for good."""
        self._closing = True
        if self._unsub_resync:
            self._unsub_resync()
            self._unsub_resync = None
        if self._reconnect_task:
            self._reconnect_task.cancel()
        await self.async_stop_playback()
        self._disconnect()

    def _disconnect(self) -> None:
        if self.atv is not None:
            atv, self.atv = self.atv, None
            with contextlib.suppress(atv_exc.NotSupportedError):
                atv.push_updater.stop()
            atv.close()
        self.playing = None

    @callback
    def _schedule_reconnect(self) -> None:
        if self._closing or (self._reconnect_task and not self._reconnect_task.done()):
            return
        self._reconnect_task = self.entry.async_create_background_task(
            self.hass, self._reconnect_loop(), f"appletv_plus reconnect {self.name}"
        )

    async def _reconnect_loop(self) -> None:
        delay = RECONNECT_MIN
        while not self._closing:
            await asyncio.sleep(delay)
            try:
                await self._async_connect()
                return
            except Exception as ex:  # noqa: BLE001
                _LOGGER.debug("Reconnect to %s failed: %s", self.name, ex)
                self._disconnect()
                delay = min(delay * 2, RECONNECT_MAX)

    # ------------------------------------------------------------ pyatv listeners

    def playstatus_update(self, updater, playstatus: Playing) -> None:
        """Handle new play status (pushed by the device)."""
        self.playing = playstatus
        self.position_updated_at = dt_util.utcnow()
        self._notify()

    def playstatus_error(self, updater, exception: Exception) -> None:
        """Handle push update errors."""
        _LOGGER.debug("Push update error from %s: %s", self.name, exception)
        self.playing = None
        self._notify()

    def connection_lost(self, exception: Exception) -> None:
        """Handle unexpected disconnect."""
        _LOGGER.warning("Lost connection to %s: %s", self.name, exception)
        self._disconnect()
        self._notify()
        self._schedule_reconnect()

    def connection_closed(self) -> None:
        """Handle disconnect initiated by the device."""
        self._disconnect()
        self._notify()
        self._schedule_reconnect()

    def powerstate_update(self, old_state: PowerState, new_state: PowerState) -> None:
        """Handle power change."""
        self.power_state = new_state
        self._notify()

    def volume_update(self, old_level: float, new_level: float) -> None:
        """Handle volume change."""
        self.volume = new_level
        self._notify()

    def outputdevices_update(
        self, old_devices: list[OutputDevice], new_devices: list[OutputDevice]
    ) -> None:
        """Handle change of audio output devices (HomePod grouping)."""
        self.output_devices = list(new_devices)
        self._notify()

    def volume_device_update(
        self, output_device: OutputDevice, old_level: float, new_level: float
    ) -> None:
        """Handle per output device volume change."""
        self._notify()

    async def _async_resync(self, _now=None) -> None:
        """Periodically re-read position so the HA timeline never drifts."""
        if self.atv is None or self.playing is None:
            return
        if self.playing.device_state not in (DeviceState.Playing, DeviceState.Seeking):
            return
        try:
            self.playing = await self.atv.metadata.playing()
            self.position_updated_at = dt_util.utcnow()
            self._notify()
        except Exception as ex:  # noqa: BLE001
            _LOGGER.debug("Resync failed: %s", ex)

    @callback
    def _notify(self) -> None:
        # pyatv callbacks run in the event loop, so this is safe.
        async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(self.entry.entry_id))

    # ------------------------------------------------------------ playback jobs

    async def async_start_playback(
        self,
        job: Callable[[], Awaitable[None]],
        prepared: PreparedMedia | None,
        item_info: dict[str, Any],
        start_timeout: float,
    ) -> None:
        """Run a blocking pyatv playback call in the background.

        pyatv's play_url/stream_file only return once playback *ends*. The core
        integration awaits that inside the service call, which is a big source
        of the lag people see. Here the job runs in the background; we only
        wait `start_timeout` seconds to surface early failures (bad URL, missing
        pairing) to the caller.
        """
        await self.async_stop_playback()
        self._prepared = prepared
        self.current_item = item_info
        self.last_error = None

        async def _runner() -> None:
            try:
                await job()
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                self.last_error = str(ex) or type(ex).__name__
                _LOGGER.error("Playback on %s failed: %s", self.name, self.last_error)
                raise
            finally:
                if self._prepared is prepared:
                    self._prepared = None
                await self.media.async_release(prepared)
                self._notify()

        task = self.entry.async_create_background_task(
            self.hass, _runner(), f"appletv_plus playback {self.name}"
        )
        self._play_task = task
        self._notify()
        done, _ = await asyncio.wait({task}, timeout=start_timeout)
        if done and not task.cancelled() and (exc := task.exception()):
            raise HomeAssistantError(f"Playback failed: {exc}") from exc

    async def async_stop_playback(self) -> None:
        """Cancel a running playback job."""
        task, self._play_task = self._play_task, None
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._prepared is not None:
            await self.media.async_release(self._prepared)
            self._prepared = None

    def _airplay_player_active(self) -> bool | None:
        """Return whether tvOS' AirPlay player is the active app (None = unknown)."""
        if self.atv is None:
            return False
        try:
            app = self.atv.metadata.app
        except Exception:  # noqa: BLE001 - not supported without MRP
            return None
        return app is not None and app.identifier == AIRPLAY_PLAYER_BUNDLE

    async def async_wait_airplay_session(self, max_seconds: float | None) -> None:
        """Block while the Apple TV is showing our AirPlay stream.

        Used when tvOS refuses `/playback-info` (403). Ends when the AirPlay
        player stops being the active app, or after `max_seconds` (+ margin).
        """
        loop = asyncio.get_running_loop()
        start = loop.time()
        started = False
        inactive = 0
        while True:
            await asyncio.sleep(1)
            elapsed = loop.time() - start
            if max_seconds and elapsed > max_seconds + 10:
                return
            active = self._airplay_player_active()
            if active is None:
                continue
            if active:
                started, inactive = True, 0
                continue
            inactive += 1
            if started and inactive >= 3:
                _LOGGER.debug("AirPlay player closed on %s; playback ended", self.name)
                return
            if not started and elapsed > AIRPLAY_START_GRACE:
                raise HomeAssistantError(
                    f"{self.name} accepted the stream but never started playing it"
                )

    async def async_run(self, command: Callable[[AppleTV], Awaitable[Any]]) -> Any:
        """Run a pyatv command (given as `lambda atv: ...`) and translate errors."""
        if self.atv is None:
            raise HomeAssistantError(f"{self.name} is not connected")
        try:
            return await command(self.atv)
        except atv_exc.NotSupportedError as ex:
            raise HomeAssistantError(f"Not supported by {self.name}: {ex}") from ex
        except atv_exc.AuthenticationError as ex:
            raise HomeAssistantError(
                f"{self.name} rejected the command; re-pair the device: {ex}"
            ) from ex
        except (atv_exc.ConnectionFailedError, atv_exc.ProtocolError, OSError) as ex:
            raise HomeAssistantError(f"Command to {self.name} failed: {ex}") from ex
