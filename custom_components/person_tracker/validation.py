"""Validation rules shared by the config flows and entry setup.

Each rule returns an error key (see ``const.ERR_*``) or None. The flows map
the keys onto form fields; setup turns them into setup errors or log lines.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    DOMAIN,
    ERR_CAMERA_WITHOUT_AREA,
    ERR_DUPLICATE_CAMERA,
    ERR_INVALID_AI_TASK_ENTITY,
    ERR_INVALID_AUTHORITATIVE_TRACKER,
    ERR_INVALID_CAMERA,
    ERR_INVALID_PERSON_NAME,
    ERR_INVALID_SUPPORTING_ENTITY,
    ERR_INVALID_TRIGGER,
    ERR_NO_TRIGGERS,
    ERR_OWN_SUPPORTING_ENTITY,
    ERR_UNIQUE_PERSON,
)
from .helpers import get_entity_area_id
from .models import person_slug

_PERSON_NAME_MAX_LEN = 100
_PERSON_NAME_INVALID = re.compile(r"[/\\<>:\"|?*\s]|^\.*$")


def _domain_of(hass: HomeAssistant, entity_id: str) -> str | None:
    """Return the domain of *entity_id*'s current state, or None if unseen."""
    return (state := hass.states.get(entity_id)) and state.domain


def person_name_error(name: str, taken_slugs: Iterable[str]) -> str | None:
    """Check a person name.

    Names are used as folder names, so whitespace and path/OS-reserved
    characters are not allowed. Names that differ only in case or punctuation
    share a slug and would collide.
    """
    if (
        not name
        or len(name) > _PERSON_NAME_MAX_LEN
        or _PERSON_NAME_INVALID.search(name)
    ):
        return ERR_INVALID_PERSON_NAME
    if person_slug(name) in taken_slugs:
        return ERR_UNIQUE_PERSON
    return None


def authoritative_tracker_error(hass: HomeAssistant, entity_id: str) -> str | None:
    """Check an authoritative tracker is a device_tracker entity."""
    if _domain_of(hass, entity_id) != "device_tracker":
        return ERR_INVALID_AUTHORITATIVE_TRACKER
    return None


def supporting_entity_error(hass: HomeAssistant, entity_id: str) -> str | None:
    """Check a supporting area or floor entity exists (any domain fits).

    Our own entities are refused: the area and floor sensors mirror the
    tracker, so as its supporting tracker they would feed it its own output.
    """
    if hass.states.get(entity_id) is None:
        return ERR_INVALID_SUPPORTING_ENTITY
    if (
        entry := er.async_get(hass).async_get(entity_id)
    ) is not None and entry.platform == DOMAIN:
        return ERR_OWN_SUPPORTING_ENTITY
    return None


def camera_error(hass: HomeAssistant, entity_id: str) -> str | None:
    """Check *entity_id* is a camera entity."""
    if _domain_of(hass, entity_id) != "camera":
        return ERR_INVALID_CAMERA
    return None


def new_camera_error(
    hass: HomeAssistant, entity_id: str, taken_cameras: Iterable[str]
) -> str | None:
    """Check a camera being added to the map.

    On top of :func:`camera_error`, a new camera must be in an HA area (ADR
    0003; the area itself is looked up when a trigger fires) and must not
    already be mapped.
    """
    if error := camera_error(hass, entity_id):
        return error
    if get_entity_area_id(hass, entity_id) is None:
        return ERR_CAMERA_WITHOUT_AREA
    if entity_id in taken_cameras:
        return ERR_DUPLICATE_CAMERA
    return None


def is_trigger(hass: HomeAssistant, entity_id: str) -> bool:
    """Return True if *entity_id* can trigger a camera (a binary sensor)."""
    return _domain_of(hass, entity_id) == "binary_sensor"


def triggers_error(hass: HomeAssistant, triggers: list[str]) -> str | None:
    """Check a camera's trigger list."""
    if not triggers:
        return ERR_NO_TRIGGERS
    if not all(is_trigger(hass, trigger) for trigger in triggers):
        return ERR_INVALID_TRIGGER
    return None


def ai_task_error(hass: HomeAssistant, entity_id: str | None) -> str | None:
    """Check the AI task entity used for identification."""
    if not entity_id or _domain_of(hass, entity_id) != "ai_task":
        return ERR_INVALID_AI_TASK_ENTITY
    return None
