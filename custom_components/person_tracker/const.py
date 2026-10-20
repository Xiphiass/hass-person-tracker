"""Constants for the person_tracker integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "person_tracker"

PLATFORMS = [Platform.DEVICE_TRACKER]

# Config entry data keys.
CONF_PEOPLE = "people"
CONF_CAMERAS = "cameras"
CONF_AI_TASK_ENTITY = "ai_task_entity"
CONF_COOLDOWN = "cooldown"

# Per-person config keys.
CONF_PERSON_NAME = "name"
CONF_TTL = "ttl"
CONF_AUTHORITATIVE_TRACKER = "authoritative_tracker"

# Per-camera config keys.
CONF_CAMERA = "camera"
CONF_TRIGGERS = "triggers"

# Config flow helper keys.
CONF_ADD_PERSON = "add_person"
CONF_ADD_CAMERA = "add_camera"

DEFAULT_TTL_MINUTES = 30
DEFAULT_COOLDOWN_SECONDS = 30
MAX_TTL_MINUTES = 1440
MAX_COOLDOWN_SECONDS = 300

# Reference images (ADR 0005): user-placed files under
# <config>/person_tracker/people/<person>/<file>.
PEOPLE_DIR = "people"
REFERENCE_IMAGE_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
)

# Config entry error keys (translations in translations/en.json).
ERR_UNIQUE_PERSON = "unique_person"
ERR_INVALID_PERSON_NAME = "invalid_person_name"
ERR_INVALID_TTL = "invalid_ttl"
ERR_INVALID_AUTHORITATIVE_TRACKER = "invalid_authoritative_tracker"
ERR_DUPLICATE_CAMERA = "duplicate_camera"
ERR_INVALID_CAMERA = "invalid_camera"
ERR_CAMERA_WITHOUT_AREA = "camera_without_area"
ERR_NO_TRIGGERS = "no_triggers"
ERR_INVALID_TRIGGER = "invalid_trigger"
ERR_NO_AI_TASK_ENTITY = "no_ai_task_entity"
ERR_INVALID_AI_TASK_ENTITY = "invalid_ai_task_entity"

ABORT_SINGLE_INSTANCE = "single_instance_allowed"

# Extra state attributes exposed on the tracker entities (ADR 0003).
ATTR_LAST_SEEN = "last_seen"
ATTR_LAST_LOCATION = "last_location"
ATTR_AREA_ID = "area_id"
ATTR_LAST_CAMERA = "last_camera"

# Signals.
SIGNAL_TRACKER_UPDATE = f"{DOMAIN}_tracker_update"
SIGNAL_TRACKER_UPDATE_PERSON = f"{SIGNAL_TRACKER_UPDATE}_"
