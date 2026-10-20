"""HA-facing helpers: areas, AI task entity resolution, reference images."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components import ai_task as ai_task_component
from homeassistant.const import ATTR_RESTORED
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, PEOPLE_DIR, REFERENCE_IMAGE_EXTENSIONS


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
    if not folder.is_dir():
        return []
    return sorted(
        path
        for path in folder.iterdir()
        if path.is_file() and path.suffix.lower() in REFERENCE_IMAGE_EXTENSIONS
    )
