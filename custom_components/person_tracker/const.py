"""Constants for the person_tracker integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "person_tracker"

PLATFORMS = [Platform.DEVICE_TRACKER, Platform.IMAGE, Platform.SENSOR]

# Config entry data keys (global settings).
CONF_CAMERAS = "cameras"
CONF_AI_TASK_ENTITY = "ai_task_entity"
CONF_COOLDOWN = "cooldown"
CONF_MIN_CONFIDENCE = "min_confidence"
CONF_RECHECK = "recheck"
CONF_MOVE_CHECK = "move_check"

# Each Person is a config subentry of this type.
SUBENTRY_PERSON = "person"

# Person subentry data keys.
CONF_PERSON_NAME = "name"
CONF_TTL = "ttl"
CONF_AUTHORITATIVE_TRACKER = "authoritative_tracker"
CONF_DESCRIPTION = "description"
# Supporting tracker (ADR 0008): entities whose states name an HA area and an
# HA floor, both optional.
CONF_SUPPORTING_AREA_ENTITY = "supporting_area_entity"
CONF_SUPPORTING_FLOOR_ENTITY = "supporting_floor_entity"

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
# Supporting tracker (ADR 0008): a supporting area that changed after the
# last camera sighting and has held this long leads over the current sighting,
# because the person moved somewhere the cameras did not see.
SUPPORTING_SETTLE_SECONDS = 120
# Move check (Issue #34 / ADR 0008): once a supporting move has settled into an
# area with cameras, and no camera has confirmed it, those cameras are asked
# about the moved person. On by default.
DEFAULT_MOVE_CHECK = True
# Moves into the same area that settle within this window share one run.
MOVE_CHECK_GROUP_SECONDS = 10
# Supporting tracker corroboration (Issue #32 / ADR 0009): after
# Identification, a hit on a camera in the supporting area gains the area
# boost, else one on the supporting floor gains the floor boost, before the
# minimum confidence comparison. Never cumulative, capped at 1.0.
SUPPORTING_AREA_BOOST = 0.15
SUPPORTING_FLOOR_BOOST = 0.05

# Reference images (ADR 0005): user-placed files under
# <config>/person_tracker/people/<person>/<file>.
PEOPLE_DIR = "people"
REFERENCE_IMAGE_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
)
# Vision models bill an image by its pixel size, so a reference image whose
# longer edge exceeds this is sent as a downscaled JPEG copy (Issue #43),
# cached per person under <config>/person_tracker/reference_cache/ and
# rebuilt when the original changes. The originals are never touched.
REFERENCE_IMAGE_MAX_SIZE = 512
REFERENCE_CACHE_DIR = "reference_cache"

# Config entry error keys (translations in translations/en.json).
ERR_UNIQUE_PERSON = "unique_person"
ERR_INVALID_PERSON_NAME = "invalid_person_name"
ERR_INVALID_AUTHORITATIVE_TRACKER = "invalid_authoritative_tracker"
ERR_INVALID_SUPPORTING_ENTITY = "invalid_supporting_entity"
ERR_OWN_SUPPORTING_ENTITY = "own_supporting_entity"
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

# Move check prompt (Issue #34 / ADR 0008), prepended like the re-check prompt.
# It does not say why the listed people are expected here: the supporting
# tracker never reaches the AI (ADR 0009).
AI_TASK_MOVE_CHECK_INSTRUCTIONS = (
    "This snapshot is a move check: it was taken to confirm whether the "
    "listed household members are here. Only report a listed household "
    "member who is clearly visible now. Anyone else in the snapshot, "
    "including people who merely resemble a listed household member, counts "
    'in "unknown_persons".'
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
# area is only present while home (the camera's, or the supporting tracker's
# while it leads, ADR 0008); last_location is the camera's and is kept.
ATTR_AREA = "area"
ATTR_LAST_SEEN = "last_seen"
ATTR_LAST_LOCATION = "last_location"
ATTR_AREA_ID = "area_id"
ATTR_LAST_CAMERA = "last_camera"
# The HA floor of that area (Issue #28), looked up live like the area name:
# floor only while the sighting is current, last_floor and floor_id kept.
ATTR_FLOOR = "floor"
ATTR_LAST_FLOOR = "last_floor"
ATTR_FLOOR_ID = "floor_id"
# The Supporting tracker's location (Issue #29 / ADR 0008), resolved live
# from the supporting entities: names and IDs only while resolvable, and
# whether it agrees with the current camera sighting (area / floor / none).
ATTR_SUPPORTING_AREA = "supporting_area"
ATTR_SUPPORTING_AREA_ID = "supporting_area_id"
ATTR_SUPPORTING_FLOOR = "supporting_floor"
ATTR_SUPPORTING_FLOOR_ID = "supporting_floor_id"
ATTR_SUPPORTING_AGREES = "supporting_agrees"
# Which source places the person at home (Issue #30 / ADR 0008): "camera"
# while the camera sighting leads, else the supporting area entity's ID.
ATTR_SOURCE = "source"
