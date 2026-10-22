"""HA-facing helpers: areas, AI task entity resolution, reference images,
last-seen person thumbnails, unknown snapshots."""

from __future__ import annotations

import io
from datetime import timedelta
from logging import getLogger
from mimetypes import guess_extension
from pathlib import Path

from PIL import Image as PILImage
from homeassistant.components import ai_task as ai_task_component
from homeassistant.const import ATTR_RESTORED
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    PEOPLE_DIR,
    REFERENCE_IMAGE_EXTENSIONS,
    THUMBNAIL_PADDING,
    THUMBNAILS_DIR,
    UNKNOWN_SNAPSHOTS_DIR,
)
from .models import person_slug

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


def _crop_box(bbox: list[float], width: int, height: int) -> tuple[int, int, int, int]:
    """A bbox ``[x1, y1, x2, y2]`` as clamped pixel bounds, padded.

    Values that fit in the unit range are treated as fractions of the frame
    (some models answer in normalized coordinates). Reversed edges are
    swapped. Raises ValueError when the box is empty after clamping.
    """
    x1, y1, x2, y2 = (float(value) for value in bbox)
    if max(x2, y2) <= 1.0 and max(width, height) > 1:
        x1, x2 = x1 * width, x2 * width
        y1, y2 = y1 * height, y2 * height
    x1, x2 = sorted((x1, x2))
    y1, y2 = sorted((y1, y2))
    pad_x = (x2 - x1) * THUMBNAIL_PADDING
    pad_y = (y2 - y1) * THUMBNAIL_PADDING
    left = max(0, int(x1 - pad_x))
    top = max(0, int(y1 - pad_y))
    right = min(width, int(x2 + pad_x))
    bottom = min(height, int(y2 + pad_y))
    if right <= left or bottom <= top:
        raise ValueError("empty bounding box")
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
    is given, the frame is cropped tight around that box first. A format
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
        image = image.crop(_crop_box(bbox, *image.size))
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    path = folder / f"{slug}.jpg"
    image.save(path, format="JPEG")
    return path
