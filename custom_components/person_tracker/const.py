"""Constants for the person_tracker integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "person_tracker"

PLATFORMS = [Platform.DEVICE_TRACKER]

# Config entry data keys (global settings).
CONF_CAMERAS = "cameras"
CONF_AI_TASK_ENTITY = "ai_task_entity"

# Each Person is a config subentry of this type.
SUBENTRY_PERSON = "person"

# Person subentry data keys.
CONF_PERSON_NAME = "name"
CONF_TTL = "ttl"
CONF_AUTHORITATIVE_TRACKER = "authoritative_tracker"

# Per-camera config keys.
CONF_CAMERA = "camera"
CONF_TRIGGERS = "triggers"

# Config flow helper keys.
CONF_ADD_CAMERA = "add_camera"
CONF_REMOVE_CAMERAS = "remove_cameras"

DEFAULT_TTL_MINUTES = 30
MAX_TTL_MINUTES = 1440

# Reference images (ADR 0005): user-placed files under
# <config>/person_tracker/people/<person>/<file>.
PEOPLE_DIR = "people"
REFERENCE_IMAGE_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
)

# Config entry error keys (translations in translations/en.json).
ERR_UNIQUE_PERSON = "unique_person"
ERR_INVALID_PERSON_NAME = "invalid_person_name"
ERR_INVALID_AUTHORITATIVE_TRACKER = "invalid_authoritative_tracker"
ERR_DUPLICATE_CAMERA = "duplicate_camera"
ERR_INVALID_CAMERA = "invalid_camera"
ERR_CAMERA_WITHOUT_AREA = "camera_without_area"
ERR_NO_CAMERAS = "no_cameras"
ERR_NO_TRIGGERS = "no_triggers"
ERR_INVALID_TRIGGER = "invalid_trigger"
ERR_INVALID_AI_TASK_ENTITY = "invalid_ai_task_entity"

ABORT_SINGLE_INSTANCE = "single_instance_allowed"
ABORT_NO_AI_TASK_ENTITY = "no_ai_task_entity"

# Extra state attributes exposed on the tracker entities (ADR 0003).
ATTR_LAST_SEEN = "last_seen"
ATTR_LAST_LOCATION = "last_location"
ATTR_AREA_ID = "area_id"
ATTR_LAST_CAMERA = "last_camera"
