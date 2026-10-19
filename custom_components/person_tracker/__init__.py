"""Track household members' locations via camera detections and AI Task
identification.

Everything is configured through a single config entry (ADR 0004):
- the people to track (identity + optional TTL + optional authoritative
  tracker),
- the cameras to watch with their trigger binary sensors,
- an optional AI task entity (defaults to the HA default AI task entity).

Reference images live on disk under
``<config>/person_tracker/people/<person>/`` and are re-discovered on every
setup and reload (ADR 0005).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_AI_TASK_ENTITY,
    CONF_AUTHORITATIVE_TRACKER,
    CONF_CAMERA,
    CONF_CAMERAS,
    CONF_PEOPLE,
    CONF_PERSON_NAME,
    CONF_TTL,
    CONF_TRIGGERS,
    DEFAULT_TTL_MINUTES,
    DOMAIN,
    ERR_INVALID_AI_TASK_ENTITY,
    ERR_NO_AI_TASK_ENTITY,
    PLATFORMS,
)
from .helpers import (
    discover_reference_images,
    get_default_ai_task_entity_id,
    get_entity_area_id,
    get_entity_domain,
    valid_triggers,
)
from .models import Camera, Person, PersonTrackerData

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """The integration is only set up through config entries."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    _LOGGER.info("Person tracker setup entry: %s", entry.entry_id)
    data = await _async_build_entry_data(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = data
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def _async_build_entry_data(
    hass: HomeAssistant, entry: ConfigEntry
) -> PersonTrackerData:
    """Validate the entry against live HA state and build the runtime data.

    Raises ConfigEntryError when the entry cannot work at all (missing AI task
    entity). Cameras without an HA area are skipped with a visible error.
    """
    override = entry.data.get(CONF_AI_TASK_ENTITY)
    if override:
        if get_entity_domain(hass, override) != "ai_task":
            raise ConfigEntryError(ERR_INVALID_AI_TASK_ENTITY)
        ai_task_entity_id = override
    elif (ai_task_entity_id := get_default_ai_task_entity_id(hass)) is None:
        raise ConfigEntryError(ERR_NO_AI_TASK_ENTITY)

    people: dict[str, Person] = {}
    for person_data in entry.data.get(CONF_PEOPLE, []):
        person = Person(
            name=person_data[CONF_PERSON_NAME],
            ttl_minutes=person_data.get(CONF_TTL, DEFAULT_TTL_MINUTES),
            authoritative_tracker=person_data.get(CONF_AUTHORITATIVE_TRACKER),
        )
        person.reference_images = discover_reference_images(hass, person.name)
        if not person.reference_images:
            _LOGGER.warning(
                "Person %r has no reference images yet; place some under "
                "%s to enable identification.",
                person.name,
                Path(hass.config.path(DOMAIN, "people", person.name)),
            )
        people[person.name] = person

    cameras: dict[str, Camera] = {}
    for camera_data in entry.data.get(CONF_CAMERAS, []):
        camera_entity_id = camera_data[CONF_CAMERA]
        area_id = get_entity_area_id(hass, camera_entity_id)
        if area_id is None:
            _LOGGER.error(
                "Camera %s is not assigned to an HA area; skipping it. "
                "Assign an area (Settings -> Areas) and reload this "
                "integration.",
                camera_entity_id,
            )
            continue
        triggers = valid_triggers(hass, camera_data.get(CONF_TRIGGERS, []))
        if len(triggers) < len(camera_data.get(CONF_TRIGGERS, [])):
            _LOGGER.error(
                "Camera %s: one or more triggers are no longer binary_sensor "
                "entities and were skipped.",
                camera_entity_id,
            )
        if not triggers:
            _LOGGER.error(
                "Camera %s has no valid trigger binary sensors; skipping it.",
                camera_entity_id,
            )
            continue
        cameras[camera_entity_id] = Camera(
            entity_id=camera_entity_id, area_id=area_id, triggers=triggers
        )

    return PersonTrackerData(
        entry_id=entry.entry_id,
        ai_task_entity_id=ai_task_entity_id,
        people=people,
        cameras=cameras,
    )
