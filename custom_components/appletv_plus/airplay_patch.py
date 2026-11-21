"""Work around newer tvOS rejecting AirPlay `/playback-info` with 403.

pyatv's `play_url` keeps the AirPlay connection open and polls
`GET /playback-info` once per second to know when the video ends. Recent tvOS
releases answer that poll with `403 Forbidden`, pyatv turns it into
`AuthenticationError("not authenticated")` and closes the connection, which
stops playback ~20 ms after it started.

We wrap the wait loop. Only for playback started by this integration (marked
with a context variable, so the core `apple_tv` integration is unaffected), a
403 falls back to a watcher supplied by us that tracks the session through the
Media Remote state instead (is `com.apple.TVAirPlay` the active player?). The
connection stays open for as long as the watcher says playback is running.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import contextvars
import logging

from pyatv import exceptions as atv_exc

_LOGGER = logging.getLogger(__name__)

# Set around our own play_url calls; holds the fallback watcher coroutine factory.
FALLBACK_WAITER: contextvars.ContextVar[Callable[[], Awaitable[None]] | None] = (
    contextvars.ContextVar("appletv_plus_fallback_waiter", default=None)
)

_installed = False


async def _wait_with_fallback(original, player) -> None:
    waiter = FALLBACK_WAITER.get()
    if waiter is None:
        await original(player)
        return
    try:
        await original(player)
    except atv_exc.AuthenticationError:
        _LOGGER.debug(
            "Apple TV refused /playback-info (403); tracking playback via Media Remote"
        )
        await waiter()


def install() -> None:
    """Install the wrapper once (idempotent)."""
    global _installed  # noqa: PLW0603
    if _installed:
        return
    try:
        from pyatv.protocols.airplay.player import AirPlayPlayer  # noqa: PLC0415
    except ImportError:  # pragma: no cover - pyatv internals moved
        _LOGGER.warning("pyatv AirPlay player not found; 403 workaround disabled")
        return
    original = AirPlayPlayer._wait_for_media_to_end  # noqa: SLF001
    if getattr(original, "_appletv_plus_wrapped", False):
        _installed = True
        return

    async def wrapped(self) -> None:
        await _wait_with_fallback(original, self)

    wrapped._appletv_plus_wrapped = True  # type: ignore[attr-defined]  # noqa: SLF001
    AirPlayPlayer._wait_for_media_to_end = wrapped  # noqa: SLF001
    _installed = True
