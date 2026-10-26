"""HA-facing helpers: areas, the supporting location, AI task entity
resolution, reference images, AI frames, last-seen person thumbnails,
unknown snapshots."""

from __future__ import annotations

import io
from datetime import datetime, timedelta
from logging import getLogger
from mimetypes import guess_extension
from pathlib import Path
from uuid import uuid4

from PIL import Image as PILImage
from homeassistant.components import ai_task as ai_task_component
from homeassistant.const import ATTR_RESTORED
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.util import dt as dt_util

from .const import (
    BBOX_SCALE,
    DOMAIN,
    FRAMES_DIR,
    PEOPLE_DIR,
    REFERENCE_IMAGE_EXTENSIONS,
    THUMBNAIL_MIN_BOX_AREA,
    THUMBNAIL_PADDING,
    THUMBNAILS_DIR,
    UNKNOWN_SNAPSHOTS_DIR,
)
from .models import Person, person_slug
from .supporting import AreaOrFloor, resolve_area_or_floor

_LOGGER = getLogger(__package__)


def get_entity_area_id(hass: HomeAssistant, entity_id: str) -> str | None:
    """Return the area_id for *entity_id*, or None."""
    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    if entry is None:
        return None
    if entry.area_id:
        return entry.area_id
    if entry.device_id:
        device = dr.async_get(hass).async_get(entry.device_id)
        if device is not None and device.area_id:
            return device.area_id
    return None


def get_camera_area(hass: HomeAssistant, entity_id: str) -> ar.AreaEntry | None:
    """Return the HA area (Location) of camera *entity_id*, or None.

    Called when a camera's trigger fires, so area changes made after setup
    apply without a reload (ADR 0003).
    """
    if (area_id := get_entity_area_id(hass, entity_id)) is None:
        return None
    return ar.async_get(hass).async_get_area(area_id)


def _resolve_supporting(
    hass: HomeAssistant,
    entity_id: str | None,
    attribute: str,
    candidates: list[AreaOrFloor],
) -> str | None:
    """The ID of the area or floor *entity_id*'s state names, if any."""
    if entity_id is None or (state := hass.states.get(entity_id)) is None:
        return None
    return resolve_area_or_floor(
        state.state, state.attributes.get(attribute), candidates
    )


def get_supporting_location(
    hass: HomeAssistant, person: Person
) -> tuple[ar.AreaEntry | None, fr.FloorEntry | None]:
    """*person*'s live supporting area and floor (ADR 0008).

    Without a floor entity, the floor is the supporting area's floor.
    """
    area_registry = ar.async_get(hass)
    floor_registry = fr.async_get(hass)
    area = None
    area_id = _resolve_supporting(
        hass,
        person.supporting_area_entity,
        "area_id",
        [
            AreaOrFloor(entry.id, entry.name, frozenset(entry.aliases))
            for entry in area_registry.async_list_areas()
        ],
    )
    if area_id is not None:
        area = area_registry.async_get_area(area_id)
    if person.supporting_floor_entity:
        floor_id = _resolve_supporting(
            hass,
            person.supporting_floor_entity,
            "floor_id",
            [
                AreaOrFloor(entry.floor_id, entry.name, frozenset(entry.aliases))
                for entry in floor_registry.async_list_floors()
            ],
        )
    else:
        floor_id = area.floor_id if area is not None else None
    floor = None
    if floor_id is not None:
        floor = floor_registry.async_get_floor(floor_id)
    return area, floor


def is_entity_pending(hass: HomeAssistant, entity_id: str) -> bool:
    """Return True if *entity_id* is registered but has no live state yet.

    At HA startup an entity's integration may load after ours; until then the
    entity has no state, or only the placeholder HA restores for registered
    entities.
    """
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None or entry.disabled:
        return False
    state = hass.states.get(entity_id)
    return state is None or bool(state.attributes.get(ATTR_RESTORED))


def get_default_ai_task_entity_id(hass: HomeAssistant) -> str | None:
    """Return the default AI task entity, or None."""
    if preferences := hass.data.get(ai_task_component.const.DATA_PREFERENCES):
        return preferences.gen_data_entity_id
    return None


def get_person_images_dir(hass: HomeAssistant, person_name: str) -> Path:
    """Return user-managed reference-image folder for *person_name*."""
    return Path(hass.config.path(DOMAIN, PEOPLE_DIR, person_name))


def discover_reference_images(hass: HomeAssistant, person_name: str) -> list[Path]:
    """Return image files placed for *person_name*, sorted.

    Does blocking I/O; run it in the executor.
    """
    folder = get_person_images_dir(hass, person_name)
    folder.mkdir(parents=True, exist_ok=True)
    return sorted(
        path
        for path in folder.iterdir()
        if path.is_file() and path.suffix.lower() in REFERENCE_IMAGE_EXTENSIONS
    )


def get_unknown_snapshots_dir(hass: HomeAssistant) -> Path:
    """Return the unknown-snapshots folder under the config dir (Issue #4)."""
    return Path(hass.config.path(DOMAIN, UNKNOWN_SNAPSHOTS_DIR))


def save_unknown_snapshot(
    hass: HomeAssistant,
    camera_entity_id: str,
    area_name: str,
    content: bytes,
    content_type: str,
) -> Path:
    """Save an unidentified snapshot to the unknown-snapshots folder (Issue #4).

    The file name records the timestamp, area and camera. Does blocking I/O;
    run it in the executor.
    """
    folder = get_unknown_snapshots_dir(hass)
    folder.mkdir(parents=True, exist_ok=True)
    timestamp = dt_util.now().strftime("%Y%m%d-%H%M%S")
    camera_name = camera_entity_id.split(".", 1)[-1]
    base = f"{timestamp}_{person_slug(area_name)}_{person_slug(camera_name)}"
    extension = guess_extension(content_type) or ".jpg"
    path = folder / f"{base}{extension}"
    counter = 1
    while path.exists():
        path = folder / f"{base}_{counter}{extension}"
        counter += 1
    path.write_bytes(content)
    return path


def purge_unknown_snapshots(hass: HomeAssistant, max_age: timedelta) -> int:
    """Delete unknown snapshots older than *max_age*; return how many.

    A file that can't be checked or deleted is logged and skipped, so it
    never blocks the purge of the others. Does blocking I/O; run it in the
    executor.
    """
    folder = get_unknown_snapshots_dir(hass)
    if not folder.is_dir():
        return 0
    cutoff = dt_util.now() - max_age
    removed = 0
    for path in folder.iterdir():
        try:
            if not path.is_file():
                continue
            if dt_util.utc_from_timestamp(path.stat().st_mtime) < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError as err:
            _LOGGER.warning("Could not purge unknown snapshot %s: %s", path, err)
    return removed


def get_thumbnails_dir(hass: HomeAssistant) -> Path:
    """Return the last-seen thumbnails folder under the config dir.

    Deliberately separate from the per-person reference-image folders:
    auto-saved frames must never be re-discovered as reference images
    (ADR 0005).
    """
    return Path(hass.config.path(DOMAIN, THUMBNAILS_DIR))


def find_person_thumbnail(
    hass: HomeAssistant, person_name: str
) -> tuple[Path, datetime] | None:
    """*person_name*'s saved thumbnail and its modification time, or None.

    Lets the image entity show the last detection again after a restart or
    reload. Does blocking I/O; run it in the executor.
    """
    folder = get_thumbnails_dir(hass)
    slug = person_slug(person_name)
    try:
        candidates = [
            (path, path.stat().st_mtime)
            for path in folder.iterdir()
            if path.is_file() and path.stem == slug
        ]
    except OSError:
        return None
    if not candidates:
        return None
    path, mtime = max(candidates, key=lambda candidate: candidate[1])
    return path, dt_util.utc_from_timestamp(mtime)


def get_frames_dir(hass: HomeAssistant) -> Path:
    """Return the folder of frames being sent to the AI (Issue #3)."""
    return Path(hass.config.path(DOMAIN, FRAMES_DIR))


def save_frame(
    hass: HomeAssistant, camera_entity_id: str, content: bytes, content_type: str
) -> Path:
    """Save the frame of *camera_entity_id* that is sent to the AI.

    Each run gets its own file, so overlapping runs never share one. The
    caller deletes it once the AI call is done. Does blocking I/O; run it in
    the executor. Raises OSError when the write fails.
    """
    folder = get_frames_dir(hass)
    folder.mkdir(parents=True, exist_ok=True)
    camera_name = person_slug(camera_entity_id.split(".", 1)[-1])
    extension = guess_extension(content_type) or ".jpg"
    path = folder / f"{camera_name}_{uuid4().hex}{extension}"
    path.write_bytes(content)
    return path


def delete_frame(path: Path) -> None:
    """Delete a saved AI frame; a failure is logged, never raised.

    Does blocking I/O; run it in the executor.
    """
    try:
        path.unlink(missing_ok=True)
    except OSError as err:
        _LOGGER.warning("Could not delete frame %s: %s", path, err)


def _crop_box(
    bbox: list[float], width: int, height: int
) -> tuple[int, int, int, int] | None:
    """A bbox ``[x1, y1, x2, y2]`` as clamped pixel bounds, padded.

    The AI is asked for coordinates normalized to ``0..BBOX_SCALE``. Values
    that fit in the unit range are treated as fractions of the frame (some
    models answer that way regardless), and values beyond ``BBOX_SCALE`` as
    pixels. Reversed edges are swapped. Returns None when the box is empty
    or too small to be a plausible person, so the full frame is kept.
    """
    x1, y1, x2, y2 = (float(value) for value in bbox)
    largest = max(x1, y1, x2, y2)
    if largest <= 1.0:
        scale_x, scale_y = float(width), float(height)
    elif largest <= BBOX_SCALE:
        scale_x, scale_y = width / BBOX_SCALE, height / BBOX_SCALE
    else:
        scale_x = scale_y = 1.0
    x1, x2 = sorted((x1 * scale_x, x2 * scale_x))
    y1, y2 = sorted((y1 * scale_y, y2 * scale_y))
    x1, x2 = max(0.0, x1), min(float(width), x2)
    y1, y2 = max(0.0, y1), min(float(height), y2)
    if x2 <= x1 or y2 <= y1:
        return None
    if (x2 - x1) * (y2 - y1) < THUMBNAIL_MIN_BOX_AREA * width * height:
        return None
    pad_x = (x2 - x1) * THUMBNAIL_PADDING
    pad_y = (y2 - y1) * THUMBNAIL_PADDING
    left = max(0, int(x1 - pad_x))
    top = max(0, int(y1 - pad_y))
    right = min(width, int(x2 + pad_x))
    bottom = min(height, int(y2 + pad_y))
    return left, top, right, bottom


def save_person_thumbnail(
    hass: HomeAssistant,
    person_name: str,
    content: bytes,
    content_type: str,
    bbox: list[float] | None = None,
) -> Path:
    """Save *content* as *person_name*'s last-seen thumbnail (Issue #3).

    The file is one per person, overwritten on every detection. When *bbox*
    is given and plausible, the frame is cropped around that box first;
    otherwise the full frame is kept. A format
    Pillow cannot decode (e.g. HEIC) is kept raw. Does blocking I/O; run it
    in the executor. Raises OSError when the write fails.
    """
    folder = get_thumbnails_dir(hass)
    folder.mkdir(parents=True, exist_ok=True)
    slug = person_slug(person_name)
    try:
        image = PILImage.open(io.BytesIO(content))
        image.load()
    except OSError, ValueError:
        path = folder / f"{slug}{guess_extension(content_type) or '.jpg'}"
        path.write_bytes(content)
        return path
    if bbox is not None:
        if (box := _crop_box(bbox, *image.size)) is not None:
            image = image.crop(box)
        else:
            _LOGGER.debug(
                "Implausible bbox %s for %s; keeping the full frame.",
                bbox,
                person_name,
            )
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    path = folder / f"{slug}.jpg"
    image.save(path, format="JPEG")
    return path
