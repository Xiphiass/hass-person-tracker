"""HA-facing helpers: areas, AI task entity resolution, reference images."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from homeassistant.components import ai_task as ai_task_component
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_AI_TASK_ENTITY,
    CONF_TRIGGERS,
    DOMAIN,
    PEOPLE_DIR,
    REFERENCE_IMAGE_EXTENSIONS,
)

_LOGGER = logging.getLogger(__name__)

_PERSON_NAME_MAX_LEN = 100
_PERSON_NAME_INVALID = re.compile(r"[/\\<>:\"|?*\s]|^\.*$")


def is_valid_person_name(name: str) -> bool:
    """Return True if *name* is a usable person name.

    Names are used as folder names and unique keys, so whitespace and
    path/OS-reserved characters are not allowed.
    """
    if not name or len(name) > _PERSON_NAME_MAX_LEN:
        return False
    return not _PERSON_NAME_INVALID.search(name)


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


def get_area_name(hass: HomeAssistant, area_id: str) -> str:
    """Return the name for *area_id*, or the ID itself if name unknown."""
    area = ar.async_get(hass).async_get_area(area_id)
    return area.name if area is not None else area_id


def get_entity_domain(hass: HomeAssistant, entity_id: str) -> str | None:
    """Return the domain of *entity_id*'s current state, or None."""
    state = hass.states.get(entity_id)
    return state.domain if state is not None else None


def get_default_ai_task_entity_id(hass: HomeAssistant) -> str | None:
    """Return the default AI task entity, or None."""
    if preferences := hass.data.get(ai_task_component.const.DATA_PREFERENCES):
        return preferences.gen_data_entity_id
    return None


def is_usable_ai_task_entity(hass: HomeAssistant, entity_id: str) -> bool:
    """Return True if *entity_id* is an available ai_task entity."""
    state = hass.states.get(entity_id)
    return (
        state is not None
        and state.domain == "ai_task"
        and state.state != STATE_UNAVAILABLE
    )


def is_camera_in_area(hass: HomeAssistant, entity_id: str) -> bool:
    """Return True if camera is assigned to an area."""
    return get_entity_area_id(hass, entity_id) is not None


def get_person_images_dir(hass: HomeAssistant, person_name: str) -> Path:
    """Return user-managed reference-image folder for *person_name*."""
    return Path(hass.config.path(DOMAIN, PEOPLE_DIR, person_name))


def discover_reference_images(hass: HomeAssistant, person_name: str) -> list[Path]:
    """Return image files placed for *person_name*, sorted."""
    folder = get_person_images_dir(hass, person_name)
    if not folder.is_dir():
        return []
    return sorted(
        path
        for path in folder.iterdir()
        if path.is_file() and path.suffix.lower() in REFERENCE_IMAGE_EXTENSIONS
    )


def valid_triggers(hass: HomeAssistant, triggers: list[str]) -> list[str]:
    """Filter *triggers* down to entities currently binary sensors."""
    return [
        trigger
        for trigger in triggers
        if get_entity_domain(hass, trigger) == "binary_sensor"
    ]
