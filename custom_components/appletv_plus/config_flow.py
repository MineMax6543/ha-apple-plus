"""Config flow for AppleTV Plus (discovery, pairing, options)."""

from __future__ import annotations

import contextlib
import logging
from typing import Any

import pyatv
from pyatv import exceptions as atv_exc
from pyatv.const import PairingRequirement, Protocol
from pyatv.interface import BaseConfig, PairingHandler
import voluptuous as vol

from homeassistant.components import zeroconf
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PASSWORD, CONF_PIN
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

try:  # HA >= 2025.2
    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
except ImportError:  # pragma: no cover
    from homeassistant.components.zeroconf import ZeroconfServiceInfo

from .const import (
    CONF_AIRPLAY_ID,
    CONF_CREDENTIALS,
    CONF_DEVICE_KIND,
    CONF_IDENTIFIERS,
    CONF_IMAGE_DURATION,
    CONF_MODEL,
    CONF_PASSWORDS,
    CONF_START_TIMEOUT,
    CONF_VIDEO_COMPAT,
    DEFAULT_IMAGE_DURATION,
    DEFAULT_START_TIMEOUT,
    DEFAULT_VIDEO_COMPAT,
    DOMAIN,
    KIND_OTHER,
    VIDEO_COMPAT_ALWAYS,
    VIDEO_COMPAT_AUTO,
    VIDEO_COMPAT_DIRECT,
)
from .device import device_kind

_LOGGER = logging.getLogger(__name__)

CONF_DEVICE = "device"
# Pair in this order: AirPlay first (needed for play_url and, on tvOS 15+,
# carries the MRP remote-control tunnel), then Companion (power, apps).
PAIR_ORDER = [Protocol.AirPlay, Protocol.Companion, Protocol.MRP, Protocol.RAOP]
OWN_PIN = "1111"  # used when the device wants us to choose the PIN
OPTIONAL_PAIR = {Protocol.AirPlay, Protocol.Companion, Protocol.MRP}


class AppleTVPlusConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize."""
        self._found: dict[str, BaseConfig] = {}
        self._conf: BaseConfig | None = None
        self._queue: list[Protocol] = []
        self._protocol: Protocol | None = None
        self._handler: PairingHandler | None = None
        self._credentials: dict[str, str] = {}
        self._passwords: dict[str, str] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return options flow."""
        return AppleTVPlusOptionsFlow()

    # ------------------------------------------------------------ scanning

    async def _scan(self, host: str | None = None) -> list[BaseConfig]:
        if host:
            return await pyatv.scan(self.hass.loop, hosts=[host], timeout=5)
        aiozc = await zeroconf.async_get_async_instance(self.hass)
        found = await pyatv.scan(self.hass.loop, aiozc=aiozc, timeout=5)
        return [c for c in found if device_kind(c) != KIND_OTHER]

    # ------------------------------------------------------------ user

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a host or scan the network."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = (user_input.get(CONF_HOST) or "").strip() or None
            try:
                found = await self._scan(host)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Scan failed")
                found = []
            if not found:
                errors["base"] = "no_devices_found" if host is None else "cannot_connect"
            elif len(found) == 1:
                return await self._async_start(found[0])
            else:
                self._found = {c.identifier: c for c in found}
                return await self.async_step_pick()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Optional(CONF_HOST): str}),
            errors=errors,
        )

    async def async_step_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick one of several discovered devices."""
        if user_input is not None:
            return await self._async_start(self._found[user_input[CONF_DEVICE]])
        configured = self._async_current_ids()
        options = [
            SelectOptionDict(
                value=ident,
                label=f"{c.name} ({c.device_info.model_str}, {c.address})",
            )
            for ident, c in self._found.items()
            if ident not in configured
        ]
        if not options:
            return self.async_abort(reason="already_configured")
        return self.async_show_form(
            step_id="pick",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DEVICE): SelectSelector(
                        SelectSelectorConfig(
                            options=options, mode=SelectSelectorMode.LIST
                        )
                    )
                }
            ),
        )

    # ------------------------------------------------------------ zeroconf

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle an AirPlay advertisement from an Apple TV or HomePod."""
        host = str(discovery_info.ip_address)
        try:
            found = await self._scan(host)
        except Exception:  # noqa: BLE001
            return self.async_abort(reason="cannot_connect")
        if not found:
            return self.async_abort(reason="cannot_connect")
        conf = found[0]
        await self._async_set_ids(conf)
        self._conf = conf
        self.context["title_placeholders"] = {"name": conf.name}
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm adding a discovered device."""
        assert self._conf is not None
        if user_input is not None:
            return await self._async_start(self._conf)
        return self.async_show_form(
            step_id="confirm",
            description_placeholders={
                CONF_NAME: self._conf.name,
                "model": self._conf.device_info.model_str,
            },
        )

    # ------------------------------------------------------------ pairing

    async def _async_set_ids(self, conf: BaseConfig) -> None:
        await self.async_set_unique_id(conf.identifier)
        self._abort_if_unique_id_configured(updates={CONF_HOST: str(conf.address)})
        # Also abort if any other identifier of this device is configured.
        for entry in self._async_current_entries(include_ignore=False):
            if set(entry.data.get(CONF_IDENTIFIERS, [])) & set(conf.all_identifiers):
                raise AbortFlow("already_configured")

    async def _async_start(self, conf: BaseConfig) -> ConfigFlowResult:
        await self._async_set_ids(conf)
        self._conf = conf
        self._queue = []
        for proto in PAIR_ORDER:
            service = conf.get_service(proto)
            if service is None:
                continue
            if service.pairing == PairingRequirement.Mandatory or (
                service.pairing == PairingRequirement.Optional and proto in OPTIONAL_PAIR
            ):
                self._queue.append(proto)
            if service.requires_password:
                self._queue.append(proto)  # handled as a password step
        return await self._async_next()

    async def _async_next(self) -> ConfigFlowResult:
        assert self._conf is not None
        while self._queue:
            proto = self._queue.pop(0)
            service = self._conf.get_service(proto)
            assert service is not None
            if service.requires_password and proto.name not in self._passwords:
                self._protocol = proto
                return await self.async_step_password()
            if proto.name in self._credentials:
                continue
            self._protocol = proto
            return await self._async_begin_pairing()
        return self._async_create()

    async def async_step_password(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for an AirPlay password."""
        assert self._conf is not None and self._protocol is not None
        if user_input is not None:
            self._passwords[self._protocol.name] = user_input[CONF_PASSWORD]
            service = self._conf.get_service(self._protocol)
            if service is not None:
                service.password = user_input[CONF_PASSWORD]
            return await self._async_next()
        return self.async_show_form(
            step_id="password",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"protocol": self._protocol.name},
        )

    async def _async_open_handler(self) -> None:
        assert self._conf is not None and self._protocol is not None
        await self._async_close_handler()
        self._handler = await pyatv.pair(
            self._conf,
            self._protocol,
            self.hass.loop,
            session=async_get_clientsession(self.hass),
            name="Home Assistant",
        )
        if not self._handler.device_provides_pin:
            self._handler.pin(int(OWN_PIN))
        await self._handler.begin()

    async def _async_begin_pairing(self) -> ConfigFlowResult:
        assert self._protocol is not None
        try:
            await self._async_open_handler()
        except Exception as ex:  # noqa: BLE001
            _LOGGER.warning("Could not start %s pairing: %s", self._protocol.name, ex)
            await self._async_close_handler()
            if self._is_optional(self._protocol):
                return await self._async_next()
            return self.async_abort(reason="pairing_failed")
        return await self.async_step_pair()

    def _is_optional(self, proto: Protocol) -> bool:
        assert self._conf is not None
        service = self._conf.get_service(proto)
        return service is not None and service.pairing != PairingRequirement.Mandatory

    async def async_step_pair(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Enter the PIN shown on the TV (or type ours into the device)."""
        assert self._handler is not None and self._protocol is not None
        errors: dict[str, str] = {}
        device_pin = self._handler.device_provides_pin
        if user_input is not None:
            pin = user_input.get(CONF_PIN)
            if not pin and device_pin and self._is_optional(self._protocol):
                await self._async_close_handler()
                return await self._async_next()
            try:
                if device_pin:
                    self._handler.pin(int(pin))
                await self._handler.finish()
            except (ValueError, TypeError):
                errors["base"] = "invalid_pin"
            except atv_exc.PairingError:
                errors["base"] = "invalid_pin"
            except Exception as ex:  # noqa: BLE001
                _LOGGER.warning("%s pairing failed: %s", self._protocol.name, ex)
                errors["base"] = "pairing_failed"
            else:
                if self._handler.has_paired:
                    creds = self._handler.service.credentials
                    if creds:
                        self._credentials[self._protocol.name] = creds
                    await self._async_close_handler()
                    return await self._async_next()
                errors["base"] = "pairing_failed"
            if errors:
                # A failed attempt invalidates the session; start a new one.
                await self._async_close_handler()
                return await self._async_retry(errors)

        return self._pair_form(errors)

    async def _async_retry(self, errors: dict[str, str]) -> ConfigFlowResult:
        try:
            await self._async_open_handler()
        except Exception:  # noqa: BLE001
            return self.async_abort(reason="pairing_failed")
        return self._pair_form(errors)

    def _pair_form(self, errors: dict[str, str]) -> ConfigFlowResult:
        assert self._handler is not None and self._protocol is not None
        device_pin = self._handler.device_provides_pin
        schema = (
            vol.Schema({vol.Optional(CONF_PIN): str})
            if device_pin
            else vol.Schema({})
        )
        return self.async_show_form(
            step_id="pair" if device_pin else "pair_no_pin",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "protocol": self._protocol.name,
                "name": self._conf.name if self._conf else "",
                "pin": OWN_PIN,
                "optional": "yes" if self._is_optional(self._protocol) else "no",
            },
        )

    async def async_step_pair_no_pin(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """User typed our PIN into the device."""
        return await self.async_step_pair(user_input)

    async def _async_close_handler(self) -> None:
        if self._handler is not None:
            handler, self._handler = self._handler, None
            with contextlib.suppress(Exception):
                await handler.close()

    @callback
    def _async_create(self) -> ConfigFlowResult:
        conf = self._conf
        assert conf is not None
        airplay = conf.get_service(Protocol.AirPlay)
        data = {
            CONF_HOST: str(conf.address),
            CONF_IDENTIFIERS: list(conf.all_identifiers),
            CONF_CREDENTIALS: self._credentials,
            CONF_PASSWORDS: self._passwords,
            CONF_DEVICE_KIND: device_kind(conf),
            CONF_MODEL: conf.device_info.model_str,
            CONF_AIRPLAY_ID: airplay.identifier if airplay else None,
        }
        return self.async_create_entry(title=conf.name, data=data)


class AppleTVPlusOptionsFlow(OptionsFlow):
    """Options for media handling."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        opts = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_VIDEO_COMPAT,
                        default=opts.get(CONF_VIDEO_COMPAT, DEFAULT_VIDEO_COMPAT),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                VIDEO_COMPAT_AUTO,
                                VIDEO_COMPAT_DIRECT,
                                VIDEO_COMPAT_ALWAYS,
                            ],
                            translation_key="video_compat",
                        )
                    ),
                    vol.Required(
                        CONF_IMAGE_DURATION,
                        default=opts.get(CONF_IMAGE_DURATION, DEFAULT_IMAGE_DURATION),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=3, max=3600, step=1, unit_of_measurement="s",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(
                        CONF_START_TIMEOUT,
                        default=opts.get(CONF_START_TIMEOUT, DEFAULT_START_TIMEOUT),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=0, max=30, step=1, unit_of_measurement="s",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                }
            ),
        )
