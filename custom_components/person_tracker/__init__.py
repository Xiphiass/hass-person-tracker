"""Track household members' locations via camera detections and AI Task
identification.

Configuration is a UI config entry (ADR 0005):
- the entry holds the global settings: the cameras to watch with their
  trigger binary sensors, and the AI task entity used for identification;
  editable via Reconfigure,
- each person to track (identity + optional TTL + optional authoritative
  tracker) is a config subentry of the entry.

Reference images live on disk under
``<config>/person_tracker/people/<person>/`` and are re-discovered on every
setup and reload (ADR 0005).
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_AI_TASK_ENTITY,
    CONF_AUTHORITATIVE_TRACKER,
    CONF_CAMERA,
    CONF_CAMERAS,
    CONF_COOLDOWN,
    CONF_PERSON_NAME,
    CONF_TTL,
    CONF_TRIGGERS,
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_TTL_MINUTES,
    DOMAIN,
    PLATFORMS,
    SUBENTRY_PERSON,
)
from .detection import DetectionCoordinator
from .helpers import (
    discover_reference_images,
    find_person_thumbnail,
    get_person_images_dir,
    is_entity_pending,
)
from .models import Camera, Person, PersonTrackerConfigEntry, PersonTrackerData
from .validation import ai_task_error, camera_error, is_trigger

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """The integration is only set up through config entries."""
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: PersonTrackerConfigEntry
) -> bool:
    """Set up the entry and its person subentries."""
    entry.runtime_data = data = await _async_build_entry_data(hass, entry)
    # The detection loop watches the configured triggers (Issue #3).
    coordinator = DetectionCoordinator(hass, data)
    data.coordinator = coordinator
    await coordinator.async_start()
    entry.async_on_unload(coordinator.async_stop)
    # Reconfiguring the entry or adding/editing/removing a person updates the
    # entry; reload to apply it.
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: PersonTrackerConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload_entry(
    hass: HomeAssistant, entry: PersonTrackerConfigEntry
) -> None:
    """Reload the entry after its data or subentries changed."""
    hass.config_entries.async_schedule_reload(entry.entry_id)


async def _async_build_entry_data(
    hass: HomeAssistant, entry: PersonTrackerConfigEntry
) -> PersonTrackerData:
    """Validate the entry against live HA state and build the runtime data.

    Raises ConfigEntryNotReady while a configured entity is registered but its
    integration has not loaded yet, so HA retries setup. Raises
    ConfigEntryError when the entry cannot work at all (AI task entity
    missing). Cameras and triggers that are no longer valid are skipped with a
    visible error.
    """
    ai_task_entity_id = entry.data.get(CONF_AI_TASK_ENTITY)
    camera_configs = entry.data.get(CONF_CAMERAS, [])
    if pending := [
        entity_id
        for entity_id in (
            ai_task_entity_id,
            *(c[CONF_CAMERA] for c in camera_configs),
            *(t for c in camera_configs for t in c.get(CONF_TRIGGERS, [])),
        )
        if entity_id and is_entity_pending(hass, entity_id)
    ]:
        raise ConfigEntryNotReady(
            f"Waiting for {', '.join(pending)} to become available"
        )

    if error := ai_task_error(hass, ai_task_entity_id):
        raise ConfigEntryError(error)

    people: dict[str, Person] = {}
    for subentry in entry.get_subentries_of_type(SUBENTRY_PERSON):
        person = Person(
            subentry_id=subentry.subentry_id,
            name=subentry.data[CONF_PERSON_NAME],
            ttl_minutes=subentry.data.get(CONF_TTL, DEFAULT_TTL_MINUTES),
            authoritative_tracker=subentry.data.get(CONF_AUTHORITATIVE_TRACKER),
        )
        person.reference_images = await hass.async_add_executor_job(
            discover_reference_images, hass, person.name
        )
        if not person.reference_images:
            _LOGGER.warning(
                "Person %r has no reference images yet; place some under "
                "%s to enable identification.",
                person.name,
                get_person_images_dir(hass, person.name),
            )
        if thumbnail := await hass.async_add_executor_job(
            find_person_thumbnail, hass, person.name
        ):
            person.last_thumbnail, person.last_thumbnail_updated = thumbnail
        people[subentry.subentry_id] = person

    cameras: dict[str, Camera] = {}
    for camera_data in camera_configs:
        camera_entity_id = camera_data[CONF_CAMERA]
        if camera_error(hass, camera_entity_id):
            _LOGGER.error(
                "%s is no longer a camera entity; skipping it.", camera_entity_id
            )
            continue
        configured_triggers = camera_data.get(CONF_TRIGGERS, [])
        triggers = [t for t in configured_triggers if is_trigger(hass, t)]
        if dropped := [t for t in configured_triggers if t not in triggers]:
            _LOGGER.error(
                "Camera %s: triggers %s are no longer binary_sensor entities "
                "and were skipped.",
                camera_entity_id,
                ", ".join(dropped),
            )
        if not triggers:
            _LOGGER.error(
                "Camera %s has no valid trigger binary sensors; skipping it.",
                camera_entity_id,
            )
            continue
        cameras[camera_entity_id] = Camera(
            entity_id=camera_entity_id, triggers=triggers
        )

    return PersonTrackerData(
        ai_task_entity_id=ai_task_entity_id,
        people=people,
        cameras=cameras,
        cooldown_seconds=int(entry.data.get(CONF_COOLDOWN, DEFAULT_COOLDOWN_SECONDS)),
    )
