"""The /playback-info 403 workaround, against pyatv's real AirPlayPlayer."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from pyatv import exceptions as atv_exc
from pyatv.protocols.airplay.player import AirPlayPlayer

from custom_components.appletv_plus import airplay_patch


def _player_with_403() -> AirPlayPlayer:
    rtsp = MagicMock()
    rtsp.connection.get = AsyncMock(
        side_effect=atv_exc.AuthenticationError("not authenticated")
    )
    return AirPlayPlayer(rtsp, MagicMock())


async def test_403_uses_fallback_waiter_for_our_playback() -> None:
    """With our context var set, a 403 hands over to the watcher."""
    airplay_patch.install()
    waiter = AsyncMock()

    async def run() -> None:
        airplay_patch.FALLBACK_WAITER.set(waiter)
        # pyatv runs the player in its own task; context must carry over.
        await asyncio.ensure_future(_player_with_403()._wait_for_media_to_end())

    await run()
    waiter.assert_awaited_once()


async def test_403_unchanged_for_other_integrations() -> None:
    """Without our context var, pyatv behaves exactly as before."""
    airplay_patch.install()
    try:
        await _player_with_403()._wait_for_media_to_end()
    except atv_exc.AuthenticationError:
        return
    raise AssertionError("expected AuthenticationError")


def test_install_is_idempotent() -> None:
    """Installing twice doesn't double-wrap."""
    airplay_patch.install()
    first = AirPlayPlayer._wait_for_media_to_end
    airplay_patch._installed = False
    airplay_patch.install()
    assert AirPlayPlayer._wait_for_media_to_end is first
