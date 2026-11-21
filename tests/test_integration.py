"""End-to-end tests with a mocked pyatv device."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from unittest.mock import AsyncMock, MagicMock, patch

from pyatv.const import DeviceState, MediaType as AtvMediaType, Protocol
import pytest

from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    ATTR_MEDIA_EXTRA,
    DOMAIN as MP_DOMAIN,
    SERVICE_PLAY_MEDIA,
)
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import ATTR_ENTITY_ID, CONF_HOST, SERVICE_MEDIA_STOP
from homeassistant.core import HomeAssistant
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.data_entry_flow import FlowResultType

from custom_components.appletv_plus.const import (
    CONF_AIRPLAY_ID,
    CONF_CREDENTIALS,
    CONF_DEVICE_KIND,
    CONF_IDENTIFIERS,
    CONF_MODEL,
    CONF_PASSWORDS,
    DOMAIN,
    KIND_APPLE_TV,
    KIND_HOMEPOD,
)

from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import make_atv, make_conf

ENTITY = "media_player.living_room"


def _handler(creds: str) -> MagicMock:
    handler = MagicMock()
    handler.device_provides_pin = True
    handler.begin = AsyncMock()
    handler.finish = AsyncMock()
    handler.close = AsyncMock()
    handler.has_paired = True
    handler.service.credentials = creds
    return handler


async def test_config_flow_pairs_airplay_and_companion(hass: HomeAssistant) -> None:
    """User enters host, pairs AirPlay then Companion, entry is created."""
    conf = make_conf()
    handlers = [_handler("airplay-creds"), _handler("companion-creds")]
    with (
        patch("pyatv.scan", AsyncMock(return_value=[conf])),
        patch("pyatv.pair", AsyncMock(side_effect=handlers)) as pair,
        patch("custom_components.appletv_plus.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.1.50"}
        )
        assert result["step_id"] == "pair"
        assert result["description_placeholders"]["protocol"] == "AirPlay"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"pin": "1234"}
        )
        assert result["step_id"] == "pair"
        assert result["description_placeholders"]["protocol"] == "Companion"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"pin": "5678"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data[CONF_CREDENTIALS] == {
        "AirPlay": "airplay-creds",
        "Companion": "companion-creds",
    }
    assert data[CONF_DEVICE_KIND] == KIND_APPLE_TV
    assert [c.args[1] for c in pair.call_args_list] == [Protocol.AirPlay, Protocol.Companion]
    handlers[0].pin.assert_called_once_with(1234)


async def test_optional_pairing_can_be_skipped(hass: HomeAssistant) -> None:
    """Empty PIN skips an optional protocol."""
    with (
        patch("pyatv.scan", AsyncMock(return_value=[make_conf()])),
        patch("pyatv.pair", AsyncMock(side_effect=[_handler("a"), _handler("c")])),
        patch("custom_components.appletv_plus.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.1.50"}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CREDENTIALS] == {}


def _entry(kind: str = KIND_APPLE_TV) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Living Room",
        unique_id="AA:BB:CC:DD:EE:FF",
        data={
            CONF_HOST: "192.168.1.50",
            CONF_IDENTIFIERS: ["AA:BB:CC:DD:EE:FF"],
            CONF_CREDENTIALS: {"AirPlay": "x"},
            CONF_PASSWORDS: {},
            CONF_DEVICE_KIND: kind,
            CONF_MODEL: "Apple TV 4K",
            CONF_AIRPLAY_ID: "AA:BB:CC:DD:EE:FF",
        },
    )


@pytest.fixture
async def setup_atv(hass: HomeAssistant):
    """Set up an Apple TV entry with a mocked connection."""
    await async_process_ha_core_config(hass, {"internal_url": "http://192.168.1.2:8123"})
    atv = make_atv()
    entry = _entry()
    entry.add_to_hass(hass)
    with (
        patch("pyatv.scan", AsyncMock(return_value=[make_conf()])),
        patch("pyatv.connect", AsyncMock(return_value=atv)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    yield atv, entry
    await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_and_push_update(hass: HomeAssistant, setup_atv) -> None:
    """Entities appear and follow pushed play status."""
    atv, entry = setup_atv
    assert hass.states.get(ENTITY).state == "on"
    assert hass.states.get("remote.living_room_remote") is not None

    playing = MagicMock()
    playing.device_state = DeviceState.Playing
    playing.media_type = AtvMediaType.Video
    playing.title = "Big Buck Bunny"
    playing.position = 42
    playing.total_time = 600
    entry.runtime_data.playstatus_update(None, playing)
    await hass.async_block_till_done()

    state = hass.states.get(ENTITY)
    assert state.state == "playing"
    assert state.attributes["media_title"] == "Big Buck Bunny"
    assert state.attributes["media_position"] == 42
    assert state.attributes["media_content_type"] == "video"


async def test_play_url_runs_in_background(hass: HomeAssistant, setup_atv) -> None:
    """play_media returns although pyatv's play_url blocks until playback ends."""
    atv, entry = setup_atv
    started = asyncio.Event()

    async def blocking_play_url(url, **kwargs):
        started.set()
        await asyncio.sleep(3600)

    atv.stream.play_url = AsyncMock(side_effect=blocking_play_url)
    hass.config_entries.async_update_entry(entry, options={"start_timeout": 0.2})

    t0 = time.monotonic()
    await hass.services.async_call(
        MP_DOMAIN,
        SERVICE_PLAY_MEDIA,
        {
            ATTR_ENTITY_ID: ENTITY,
            ATTR_MEDIA_CONTENT_ID: "http://192.168.1.10/movie.mp4",
            ATTR_MEDIA_CONTENT_TYPE: "video/mp4",
        },
        blocking=True,
    )
    assert time.monotonic() - t0 < 5
    assert started.is_set()
    assert atv.stream.play_url.call_args.args[0] == "http://192.168.1.10/movie.mp4"
    state = hass.states.get(ENTITY)
    assert state.attributes["audio_path"] == "device_decoded"
    assert state.attributes["delivery"] == "direct"

    await hass.services.async_call(
        MP_DOMAIN, SERVICE_MEDIA_STOP, {ATTR_ENTITY_ID: ENTITY}, blocking=True
    )
    assert not entry.runtime_data.is_playing_media


async def test_playback_error_is_reported(hass: HomeAssistant, setup_atv) -> None:
    """Early pyatv failures surface as service errors."""
    atv, entry = setup_atv
    atv.stream.play_url = AsyncMock(side_effect=RuntimeError("status code: 403"))
    from homeassistant.exceptions import HomeAssistantError

    with pytest.raises(HomeAssistantError, match="403"):
        await hass.services.async_call(
            MP_DOMAIN,
            SERVICE_PLAY_MEDIA,
            {
                ATTR_ENTITY_ID: ENTITY,
                ATTR_MEDIA_CONTENT_ID: "http://192.168.1.10/movie.mp4",
                ATTR_MEDIA_CONTENT_TYPE: "video/mp4",
            },
            blocking=True,
        )


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
async def test_image_becomes_video(
    hass: HomeAssistant, setup_atv, aioclient_mock, tmp_path
) -> None:
    """An image is converted to MP4 and served through the media view."""
    atv, entry = setup_atv
    png = tmp_path / "x.png"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240",
         "-frames:v", "1", str(png)],
        check=True,
    )
    aioclient_mock.get("http://192.168.1.10/photo.png", content=png.read_bytes())

    await hass.services.async_call(
        MP_DOMAIN,
        SERVICE_PLAY_MEDIA,
        {
            ATTR_ENTITY_ID: ENTITY,
            ATTR_MEDIA_CONTENT_ID: "http://192.168.1.10/photo.png",
            ATTR_MEDIA_CONTENT_TYPE: "image/png",
            ATTR_MEDIA_EXTRA: {"duration": 3},
        },
        blocking=True,
    )
    url = atv.stream.play_url.call_args.args[0]
    assert url.startswith("http://192.168.1.2:8123/api/appletv_plus/media/")
    assert url.endswith("/image.mp4")
    assert hass.states.get(ENTITY).attributes["delivery"] == "image_as_video"


async def test_homepod_rejects_images_and_streams_audio(hass: HomeAssistant) -> None:
    """HomePods get audio over RAOP and refuse images."""
    await async_process_ha_core_config(hass, {"internal_url": "http://192.168.1.2:8123"})
    atv = make_atv()
    atv.features.in_state.side_effect = lambda states, *f: all(
        x.name != "PlayUrl" for x in f
    )
    entry = _entry(KIND_HOMEPOD)
    entry.add_to_hass(hass)
    with (
        patch("pyatv.scan", AsyncMock(return_value=[make_conf()])),
        patch("pyatv.connect", AsyncMock(return_value=atv)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    from homeassistant.exceptions import HomeAssistantError

    with pytest.raises(HomeAssistantError, match="no screen"):
        await hass.services.async_call(
            MP_DOMAIN,
            SERVICE_PLAY_MEDIA,
            {
                ATTR_ENTITY_ID: ENTITY,
                ATTR_MEDIA_CONTENT_ID: "http://192.168.1.10/a.jpg",
                ATTR_MEDIA_CONTENT_TYPE: "image/jpeg",
            },
            blocking=True,
        )
    await hass.services.async_call(
        MP_DOMAIN,
        SERVICE_PLAY_MEDIA,
        {
            ATTR_ENTITY_ID: ENTITY,
            ATTR_MEDIA_CONTENT_ID: "http://192.168.1.10/song.mp3",
            ATTR_MEDIA_CONTENT_TYPE: "music",
        },
        blocking=True,
    )
    assert atv.stream.stream_file.call_args.args[0] == "http://192.168.1.10/song.mp3"
    assert hass.states.get(ENTITY).attributes["audio_path"] == "airplay_stereo"
    assert hass.states.get("remote.living_room_remote") is None
    await hass.config_entries.async_unload(entry.entry_id)
