"""AppleTV Plus: a more reliable Apple TV / HomePod integration."""

from __future__ import annotations

import logging

from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from . import airplay_patch
from .const import CONF_DEVICE_KIND, DOMAIN, KIND_APPLE_TV
from .device import AppleDevice
from .media import MediaManager, MediaView

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type AppleConfigEntry = ConfigEntry[AppleDevice]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up shared pieces (media server view, pyatv workaround)."""
    airplay_patch.install()
    manager = MediaManager(hass, get_ffmpeg_manager(hass).binary)
    hass.data[DOMAIN] = manager
    hass.http.register_view(MediaView(manager))
    return True


def _platforms(entry: ConfigEntry) -> list[Platform]:
    if entry.data.get(CONF_DEVICE_KIND) == KIND_APPLE_TV:
        return [Platform.MEDIA_PLAYER, Platform.REMOTE]
    return [Platform.MEDIA_PLAYER]


async def async_setup_entry(hass: HomeAssistant, entry: AppleConfigEntry) -> bool:
    """Connect to the device and set up entities."""
    device = AppleDevice(hass, entry, hass.data[DOMAIN])
    try:
        await device.async_first_connect()
    except Exception as ex:
        await device.async_close()
        raise ConfigEntryNotReady(f"Could not connect to {entry.title}: {ex}") from ex

    entry.runtime_data = device
    await hass.config_entries.async_forward_entry_setups(entry, _platforms(entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AppleConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, _platforms(entry))
    if unloaded:
        await entry.runtime_data.async_close()
    return unloaded
