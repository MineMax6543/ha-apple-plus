"""Remote entity (Apple TV only) for navigation commands."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

from pyatv.const import InputAction, PowerState

from homeassistant.components.remote import (
    ATTR_DELAY_SECS,
    ATTR_HOLD_SECS,
    ATTR_NUM_REPEATS,
    DEFAULT_DELAY_SECS,
    RemoteEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AppleConfigEntry
from .const import SIGNAL_UPDATE
from .device import AppleDevice
from .entity import AppleEntity

# Commands that accept an InputAction (tap / double tap / hold).
ACTION_COMMANDS = {"up", "down", "left", "right", "select", "menu", "home"}
SIMPLE_COMMANDS = {
    "play", "pause", "play_pause", "stop", "next", "previous", "top_menu",
    "home_hold", "volume_up", "volume_down", "skip_forward", "skip_backward",
    "suspend", "wakeup", "screensaver", "control_center", "guide",
    "channel_up", "channel_down",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AppleConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the remote."""
    async_add_entities([AppleRemote(entry.runtime_data)])


class AppleRemote(AppleEntity, RemoteEntity):
    """Send Siri Remote button presses."""

    _attr_name = "Remote"

    def __init__(self, device: AppleDevice) -> None:
        """Initialize."""
        super().__init__(device)
        self._attr_unique_id = f"{device.entry.unique_id}_remote"

    @property
    def is_on(self) -> bool:
        """Return True unless the Apple TV reports standby."""
        return self.device.power_state != PowerState.Off

    async def async_added_to_hass(self) -> None:
        """Subscribe to updates."""
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

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Wake the Apple TV."""
        await self.device.async_run(lambda atv: atv.power.turn_on())

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Put the Apple TV to sleep."""
        await self.device.async_run(lambda atv: atv.power.turn_off())

    async def async_send_command(self, command: Iterable[str], **kwargs: Any) -> None:
        """Send one or more commands, e.g. ["down", "down", "select"]."""
        repeats = kwargs.get(ATTR_NUM_REPEATS, 1)
        delay = kwargs.get(ATTR_DELAY_SECS, DEFAULT_DELAY_SECS)
        hold = kwargs.get(ATTR_HOLD_SECS, 0) or 0

        for _ in range(repeats):
            for raw in command:
                cmd = raw.strip().lower()
                if cmd in ACTION_COMMANDS:
                    action = InputAction.Hold if hold else InputAction.SingleTap
                    await self.device.async_run(
                        lambda atv, c=cmd, a=action: getattr(atv.remote_control, c)(a)
                    )
                elif cmd.removesuffix("_double") in ACTION_COMMANDS:
                    base = cmd.removesuffix("_double")
                    await self.device.async_run(
                        lambda atv, c=base: getattr(atv.remote_control, c)(
                            InputAction.DoubleTap
                        )
                    )
                elif cmd in SIMPLE_COMMANDS:
                    await self.device.async_run(
                        lambda atv, c=cmd: getattr(atv.remote_control, c)()
                    )
                else:
                    raise HomeAssistantError(
                        f"Unknown command '{raw}'. Valid: "
                        + ", ".join(sorted(ACTION_COMMANDS | SIMPLE_COMMANDS))
                    )
                await asyncio.sleep(delay)
