"""Fixtures for AppleTV Plus tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from pyatv.const import (
    DeviceModel,
    OperatingSystem,
    PairingRequirement,
    PowerState,
    Protocol,
)
import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading custom integrations."""
    return


@pytest.fixture(autouse=True)
def auto_mock_zeroconf(mock_async_zeroconf):
    """Zeroconf is a dependency; don't open real multicast sockets in tests."""
    return


def make_conf(
    name: str = "Living Room",
    model: DeviceModel = DeviceModel.AppleTV4KGen3,
    raw_model: str = "AppleTV14,1",
    os: OperatingSystem = OperatingSystem.TvOS,
    protocols: tuple[Protocol, ...] = (Protocol.AirPlay, Protocol.Companion, Protocol.RAOP),
) -> MagicMock:
    """Return a fake pyatv scan result."""
    conf = MagicMock()
    conf.name = name
    conf.identifier = "AA:BB:CC:DD:EE:FF"
    conf.all_identifiers = ["AA:BB:CC:DD:EE:FF", "uuid-1234"]
    conf.address = "192.168.1.50"
    conf.device_info.model = model
    conf.device_info.raw_model = raw_model
    conf.device_info.operating_system = os
    conf.device_info.model_str = "Apple TV 4K (gen 3)"
    conf.device_info.version = "18.1"
    conf.device_info.mac = "AA:BB:CC:DD:EE:FF"
    services = {}
    for proto in protocols:
        svc = MagicMock()
        svc.protocol = proto
        svc.identifier = "AA:BB:CC:DD:EE:FF"
        svc.requires_password = False
        svc.pairing = (
            PairingRequirement.NotNeeded
            if proto == Protocol.RAOP
            else PairingRequirement.Optional
        )
        services[proto] = svc
    conf.get_service.side_effect = services.get
    return conf


def make_atv() -> MagicMock:
    """Return a fake connected pyatv AppleTV."""
    atv = MagicMock()
    atv.features.in_state.return_value = True
    atv.power.power_state = PowerState.On
    atv.audio.volume = 40.0
    atv.audio.output_devices = []
    atv.apps.app_list = AsyncMock(return_value=[])
    atv.metadata.app = None
    atv.metadata.artwork_id = None
    atv.stream.play_url = AsyncMock()
    atv.stream.stream_file = AsyncMock()
    atv.remote_control.stop = AsyncMock()
    atv.remote_control.play = AsyncMock()
    atv.power.turn_off = AsyncMock()
    return atv
