"""Constants for AppleTV Plus."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "appletv_plus"

CONF_IDENTIFIERS: Final = "identifiers"
CONF_CREDENTIALS: Final = "credentials"
CONF_PASSWORDS: Final = "passwords"
CONF_DEVICE_KIND: Final = "device_kind"
CONF_AIRPLAY_ID: Final = "airplay_id"
CONF_MODEL: Final = "model"

# Options
CONF_IMAGE_DURATION: Final = "image_duration"
CONF_VIDEO_COMPAT: Final = "video_compat"
CONF_START_TIMEOUT: Final = "start_timeout"

DEFAULT_IMAGE_DURATION: Final = 30
DEFAULT_START_TIMEOUT: Final = 4

VIDEO_COMPAT_AUTO: Final = "auto"
VIDEO_COMPAT_DIRECT: Final = "direct"
VIDEO_COMPAT_ALWAYS: Final = "always"
DEFAULT_VIDEO_COMPAT: Final = VIDEO_COMPAT_AUTO

KIND_APPLE_TV: Final = "apple_tv"
KIND_HOMEPOD: Final = "homepod"
KIND_OTHER: Final = "other"

# Signal fired (with entry_id) whenever device state changes.
SIGNAL_UPDATE: Final = f"{DOMAIN}_update_{{}}"

MEDIA_VIEW_URL: Final = f"/api/{DOMAIN}/media"

# Seconds between forced position re-syncs while something is playing.
RESYNC_INTERVAL: Final = 15

RECONNECT_MIN: Final = 5
RECONNECT_MAX: Final = 300

# How the audio of the current item reaches the speaker.
AUDIO_PATH_DEVICE: Final = "device_decoded"
AUDIO_PATH_STEREO: Final = "airplay_stereo"
