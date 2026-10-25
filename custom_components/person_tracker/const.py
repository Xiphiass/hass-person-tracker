"""Constants for the person_tracker integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "person_tracker"

PLATFORMS = [Platform.DEVICE_TRACKER, Platform.IMAGE]

# Config entry data keys (global settings).
CONF_CAMERAS = "cameras"
CONF_AI_TASK_ENTITY = "ai_task_entity"
CONF_COOLDOWN = "cooldown"
CONF_MIN_CONFIDENCE = "min_confidence"
CONF_RECHECK = "recheck"

# Each Person is a config subentry of this type.
SUBENTRY_PERSON = "person"

# Person subentry data keys.
CONF_PERSON_NAME = "name"
CONF_TTL = "ttl"
CONF_AUTHORITATIVE_TRACKER = "authoritative_tracker"
CONF_DESCRIPTION = "description"

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
# Identifications the AI reports with a lower confidence are not trusted: they
# update no tracker and the snapshot is kept as an unknown snapshot.
DEFAULT_MIN_CONFIDENCE = 0.7
# Presence re-check (ADR 0007): shortly before a sighting's TTL runs out, the
# person's last camera is checked once more, so someone who sits still (and
# so fires no new trigger edge) stays current. On by default. The re-check
# runs this long before expiry (capped at half the TTL) to leave room for the
# AI call, and people of the same camera due within that window share one run.
DEFAULT_RECHECK = True
RECHECK_LEAD_SECONDS = 60

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
    'containing {"name": string, "confidence": number, "bbox": [x_min, '
    "y_min, x_max, y_max]} objects, one per visible household member, using "
    'their exact configured names. "bbox" is the box tightly around that '
    "person's whole visible body in the camera snapshot, in coordinates "
    "normalized to 0-1000 regardless of the image's pixel size: x runs from "
    "0 (left edge) to 1000 (right edge), y from 0 (top edge) to 1000 "
    "(bottom edge), and the order is x_min, y_min, x_max, y_max. "
    '"confidence" is the probability (0-1) that the name is correct. Use a '
    "high confidence only when the face is clearly visible and matches the "
    "reference photos; when only body, clothing or posture is visible, use "
    "0.5 or less. Give each visible person at most one name, and if you "
    "cannot tell which household member someone is, count them in "
    '"unknown_persons" instead of guessing. '
    'If no household member is visible, use an empty "persons" list. '
    'Also include "unknown_persons": the number of visible people who '
    "are not listed household members (0 if there are none, or if no one "
    "is visible at all)."
)

# Presence re-check prompt (ADR 0007), prepended to AI_TASK_INSTRUCTIONS. Only
# the re-checked people are listed (with their reference photos), so the AI
# is told explicitly not to force a match onto anyone else in the frame.
AI_TASK_RECHECK_INSTRUCTIONS = (
    "This snapshot is a presence re-check: it was taken by the camera that "
    "last saw the listed household members, to confirm whether they are "
    "still there. Only report a listed household member who is clearly "
    "visible now. Anyone else in the snapshot, including people who merely "
    'resemble a listed household member, counts in "unknown_persons".'
)

# The scale of the bbox coordinates the AI is asked for (0..BBOX_SCALE on
# each axis), independent of the frame's pixel size: providers resize
# images before the model sees them, so pixel coordinates don't map back.
BBOX_SCALE = 1000

# The frame sent to the AI (Issue #3): saved under <config>/person_tracker/
# for the duration of the AI call, so the AI sees exactly the frame that is
# later cropped, and served to the AI Task through the media source.
FRAMES_DIR = "frames"

# Per-person last-seen thumbnails (Issue #3): the folder under
# <config>/person_tracker/ holding one cropped image per person (the
# person's last detection, cropped around them), the fraction of the box
# size added around each edge of the crop (generous, as AI boxes are
# approximate), and the smallest box, as a fraction of the frame's area,
# still trusted for a crop; anything smaller keeps the full frame.
THUMBNAILS_DIR = "thumbnails"
THUMBNAIL_PADDING = 0.15
THUMBNAIL_MIN_BOX_AREA = 0.005

# Unknown snapshots (Issue #4): the folder under <config>/person_tracker/,
# the retention of its files, and the purge cadence.
UNKNOWN_SNAPSHOTS_DIR = "unknown_snapshots"
UNKNOWN_SNAPSHOT_RETENTION_DAYS = 7
UNKNOWN_SNAPSHOT_CLEANUP_HOURS = 24

# Extra state attributes exposed on the tracker entities (ADR 0003). The
# area is only present while the sighting is current; last_location is kept.
ATTR_AREA = "area"
ATTR_LAST_SEEN = "last_seen"
ATTR_LAST_LOCATION = "last_location"
ATTR_AREA_ID = "area_id"
ATTR_LAST_CAMERA = "last_camera"
