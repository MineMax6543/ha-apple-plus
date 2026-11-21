"""Base entity for AppleTV Plus."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import CONF_MODEL, DOMAIN
from .device import AppleDevice


class AppleEntity(Entity):
    """Common entity behaviour."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, device: AppleDevice) -> None:
        """Initialize."""
        self.device = device
        entry = device.entry
        info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
            manufacturer="Apple",
            name=entry.title,
            model=entry.data.get(CONF_MODEL),
        )
        if device.conf is not None:
            dinfo = device.conf.device_info
            if dinfo.version:
                info["sw_version"] = dinfo.version
            if dinfo.mac:
                info["connections"] = {(CONNECTION_NETWORK_MAC, dinfo.mac.lower())}
        self._attr_device_info = info

    @property
    def available(self) -> bool:
        """Return True if the device is connected."""
        return self.device.available
