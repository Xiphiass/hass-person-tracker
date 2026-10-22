"""Constants for the person_tracker integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "person_tracker"

PLATFORMS = [Platform.DEVICE_TRACKER, Platform.IMAGE]

# Config entry data keys (global settings).
CONF_CAMERAS = "cameras"
CONF_AI_TASK_ENTITY = "ai_task_entity"
CONF_COOLDOWN = "cooldown"

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
DEFAULT_COOLDOWN_SECONDS = 30
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

# Per-person dispatcher signal prefix, followed by the person's subentry ID.
# Sent when a detection run updates that person (Issue #3).
SIGNAL_TRACKER_UPDATE_PERSON = f"{DOMAIN}_tracker_update_person"

# AI task identification run (Issue #3): fixed task name and prompt.
AI_TASK_NAME = "identify_persons"
AI_TASK_INSTRUCTIONS = (
    "Identify which of the listed household members are visible in the "
    'camera snapshot. Respond in JSON: an object with a "persons" list '
    'containing {"name": string, "confidence": number, "bbox": [x1, y1, '
    "x2, y2]} objects, one per visible household member, using their exact "
    'configured names. "bbox" is the pixel bounding box tightly around '
    "that person: top-left x, top-left y, bottom-right x, bottom-right y. "
    'If no household member is visible, use an empty "persons" list. '
    'Also include "unknown_persons": the number of visible people who '
    "are not listed household members (0 if there are none, or if no one "
    "is visible at all)."
)

# Per-person last-seen thumbnails (Issue #3): the folder under
# <config>/person_tracker/ holding one cropped image per person (the
# person's last detection, cropped tight around them), and the fraction
# of the box size added around each edge of the crop.
THUMBNAILS_DIR = "thumbnails"
THUMBNAIL_PADDING = 0.05

# Unknown snapshots (Issue #4): the folder under <config>/person_tracker/,
# the retention of its files, and the purge cadence.
UNKNOWN_SNAPSHOTS_DIR = "unknown_snapshots"
UNKNOWN_SNAPSHOT_RETENTION_DAYS = 7
UNKNOWN_SNAPSHOT_CLEANUP_HOURS = 24

# Extra state attributes exposed on the tracker entities (ADR 0003).
ATTR_LAST_SEEN = "last_seen"
ATTR_LAST_LOCATION = "last_location"
ATTR_AREA_ID = "area_id"
ATTR_LAST_CAMERA = "last_camera"
