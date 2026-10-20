"""Detection loop and identification validation."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components import camera as camera_comp
from homeassistant.components.ai_task import async_generate_data
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    CONF_AI_TASK_ENTITY,
    CONF_CAMERA,
    CONF_PERSON_NAME,
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_TTL_MINUTES,
    DOMAIN,
    SIGNAL_TRACKER_UPDATE_PERSON,
)
from .models import PersonTrackerData

_LOGGER = logging.getLogger(__name__)


def validate_ai_response(response: Any, known_people: set[str]) -> set[str]:
    """Validate AI response and return identified known people.

    Any name outside the known set is discarded; "none" means no sighting.
    """
    if not isinstance(response, list):
        _LOGGER.warning("Malformed AI response: expected list, got %r", response)
        return set()

    identified = set()
    for item in response:
        if not isinstance(item, str):
            _LOGGER.warning("Invalid item in AI response: expected str, got %r", item)
            continue

        name = item.strip()
        if name.lower() == "none":
            continue

        if name in known_people:
            identified.add(name)
        else:
            _LOGGER.warning("Discarding unknown person in AI response: %s", name)

    return identified


class DetectionCoordinator:
    """Coordinates detection loop per camera."""

    def __init__(
        self, hass: HomeAssistant, data: PersonTrackerData, cooldown: int = 30
    ):
        self.hass = hass
        self.data = data
        self.cooldown = cooldown
        self.last_runs: dict[str, datetime] = {}
        self.listeners: list[CALLBACK_TYPE] = []

        self._trigger_map = {}
        for camera_id, camera in self.data.cameras.items():
            for trigger in camera.triggers:
                self._trigger_map[trigger] = camera_id

    async def async_start(self):
        """Register listeners."""
        for trigger in self._trigger_map:
            self.listeners.append(
                async_track_state_change_event(
                    self.hass, [trigger], self._async_on_state_change
                )
            )

    async def async_stop(self):
        """Unregister listeners."""
        for listener in self.listeners:
            listener()
        self.listeners.clear()

    async def _async_on_state_change(self, event: Event):
        """Handle state change event of triggers."""
        # OFF->ON edge check
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        entity_id = event.data.get("entity_id")

        if not new_state or new_state.state != STATE_ON:
            return
        if not old_state or old_state.state != STATE_OFF:
            return

        camera_id = self._trigger_map.get(entity_id)  # type: ignore
        if not camera_id:
            return

        # Cooldown check
        now = datetime.now()
        last_run = self.last_runs.get(camera_id)
        if last_run and (now - last_run) < timedelta(seconds=self.cooldown):
            _LOGGER.debug("Cooldown active for %s", camera_id)
            return

        self.last_runs[camera_id] = now
        await self._async_run_detection(camera_id)

    async def _async_run_detection(self, camera_id: str):
        """Run the detection loop for a camera."""
        try:
            # 1. Snapshot
            image = await camera_comp.async_get_image(self.hass, camera_id)

            # 2. Attachments
            attachments = [{"media_content_id": f"media-source://camera/{camera_id}"}]
            for person in self.data.people.values():
                for img_path in person.reference_images:
                    attachments.append({"media_content_id": f"file://{img_path}"})

            # 3. AI call
            result = await async_generate_data(
                self.hass,
                task_name="identify_persons",
                entity_id=self.data.ai_task_entity_id,
                instructions="Identify all persons in this snapshot, restricted to known names. Output a list of names or 'none'.",
                attachments=attachments,
            )

            # 4. Validation
            identified_names = validate_ai_response(result.data, self.data.person_names)

            # 5. Update trackers
            camera = self.data.cameras[camera_id]
            for name in identified_names:
                person = self.data.people[name]
                camera_state = self.hass.states.get(camera_id)
                person.location = (
                    camera_state.attributes.get("friendly_name")
                    if camera_state
                    else None
                ) or camera.area_id
                person.area_id = camera.area_id
                person.last_seen = datetime.now()
                person.last_camera = camera_id

                # Signal entities to update
                async_dispatcher_send(
                    self.hass, f"{SIGNAL_TRACKER_UPDATE_PERSON}{person.slug}"
                )

        except Exception as e:
            _LOGGER.exception("Error in detection loop for %s: %s", camera_id, e)
