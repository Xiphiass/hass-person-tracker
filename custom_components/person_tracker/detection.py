"""Detection loop: trigger snapshot -> AI task identification -> tracker update.

Subscribes to every configured trigger binary sensor. On an OFF -> ON edge,
the matching camera's snapshot is sent to the configured AI task entity,
which identifies the household members in the scene (Issue #3). Each
identified person's location, area, last_seen and last_camera are updated,
and their tracker entity is nudged through a per-person dispatcher signal.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from logging import getLogger
from typing import Any, TypedDict

import probatio
from homeassistant.components.ai_task.task import async_generate_data
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_track_state_change_event,
)
from homeassistant.util import dt as dt_util

from .const import (
    AI_TASK_INSTRUCTIONS,
    AI_TASK_NAME,
    SIGNAL_TRACKER_UPDATE_PERSON,
)
from .helpers import get_camera_area
from .models import Camera, Person, PersonTrackerData, person_slug

_LOGGER = getLogger(__package__)

# Expected shape of the AI task response (Issue #3).
AI_RESULT_STRUCTURE = probatio.Schema({"persons": [{"name": str, "confidence": float}]})


class _PersonHit(TypedDict):
    """One identified household member."""

    name: str
    confidence: float


def validate_ai_response(data: Any) -> list[_PersonHit]:
    """Normalize the AI task's response into person hits.

    The expected shape is ``{"persons": [{"name": str, "confidence": float}]}``;
    a bare list of names (or name dicts) is also accepted. Unparseable content
    yields an empty list — a bad AI response must never corrupt tracker state.
    """
    if isinstance(data, dict):
        entries = data.get("persons", data.get("people"))
        if entries is None:
            _LOGGER.warning("AI task response has no 'persons' list: %r", data)
            return []
    elif isinstance(data, list):
        entries = data
    else:
        _LOGGER.warning("AI task response is not a dict or list: %r", data)
        return []
    if not isinstance(entries, list):
        _LOGGER.warning("AI task 'persons' is not a list: %r", entries)
        return []

    hits: list[_PersonHit] = []
    for entry in entries:
        if isinstance(entry, str):
            name = entry
            confidence: Any = 1.0
        elif isinstance(entry, dict):
            name = entry.get("name", entry.get("person"))
            confidence = entry.get("confidence", 1.0)
        else:
            continue
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            confidence = 1.0
        hits.append(_PersonHit(name=name.strip(), confidence=float(confidence)))
    return hits


def _build_instructions(data: PersonTrackerData) -> str:
    """The identification prompt, listing the configured household members."""
    names = ", ".join(sorted(data.person_names)) or "no one"
    return f"Household members to look for: {names}. {AI_TASK_INSTRUCTIONS}"


class DetectionCoordinator:
    """Run the trigger -> snapshot -> identification -> update loop.

    Per-camera cooldown: after a detection run, further trigger edges of the
    same camera are ignored until the configured cooldown has elapsed.
    """

    def __init__(self, hass: HomeAssistant, data: PersonTrackerData) -> None:
        """Initialize the coordinator."""
        self._hass = hass
        self._data = data
        self._last_run: dict[str, datetime] = {}
        self._unsubscribes: list[Callable[[], None]] = []

    @property
    def cooldown_seconds(self) -> int:
        """Seconds between detection runs of the same camera."""
        return self._data.cooldown_seconds

    async def async_start(self) -> None:
        """Watch every configured trigger."""
        for camera in self._data.cameras.values():
            if not camera.triggers:
                continue
            self._unsubscribes.append(
                async_track_state_change_event(
                    self._hass, camera.triggers, self._async_on_trigger
                )
            )

    def async_stop(self) -> None:
        """Unsubscribe all triggers (entry unload)."""
        for unsub in self._unsubscribes:
            unsub()
        self._unsubscribes = []

    def _camera_for_trigger(self, trigger_entity_id: str) -> Camera | None:
        """The camera whose trigger list contains *trigger_entity_id*."""
        for camera in self._data.cameras.values():
            if trigger_entity_id in camera.triggers:
                return camera
        return None

    async def _async_on_trigger(self, event: Event[EventStateChangedData]) -> None:
        """Handle a trigger state change; only OFF -> ON edges detect."""
        old_state = event.data["old_state"]
        new_state = event.data["new_state"]
        if (
            old_state is None
            or new_state is None
            or old_state.state != STATE_OFF
            or new_state.state != STATE_ON
        ):
            return
        camera = self._camera_for_trigger(event.data["entity_id"])
        if camera is None:
            return
        now = dt_util.now()
        last = self._last_run.get(camera.entity_id)
        if (
            last is not None
            and (now - last).total_seconds() < self._data.cooldown_seconds
        ):
            _LOGGER.debug("Skipping detection run for %s (cooldown).", camera.entity_id)
            return
        self._last_run[camera.entity_id] = now
        try:
            await self._async_detect(camera)
        except Exception:  # noqa: BLE001 - a failed run must not take HA down
            _LOGGER.exception("Detection run failed for %s", camera.entity_id)

    async def _async_detect(self, camera: Camera) -> None:
        """Run the AI task on a snapshot of *camera* and apply its result."""
        # The area is looked up at trigger time so post-setup area changes
        # apply without a reload (ADR 0003).
        area = get_camera_area(self._hass, camera.entity_id)
        if area is None:
            _LOGGER.debug(
                "Camera %s is not in an HA area; skipping the run.",
                camera.entity_id,
            )
            return
        result = await async_generate_data(
            self._hass,
            task_name=AI_TASK_NAME,
            entity_id=self._data.ai_task_entity_id,
            instructions=_build_instructions(self._data),
            structure=AI_RESULT_STRUCTURE,
            attachments=[
                {"media_content_id": f"media-source://camera/{camera.entity_id}"}
            ],
        )
        hits = validate_ai_response(result.data)
        if not hits:
            _LOGGER.debug("No household members detected for %s.", camera.entity_id)
            return
        now = dt_util.now()
        for hit in hits:
            person = self._find_person(hit["name"])
            if person is None:
                _LOGGER.debug("Ignoring unrecognized person %r.", hit["name"])
                continue
            person.location = area.name
            person.area_id = area.id
            person.last_seen = now
            person.last_camera = camera.entity_id
            async_dispatcher_send(
                self._hass, f"{SIGNAL_TRACKER_UPDATE_PERSON}{person.subentry_id}"
            )

    def _find_person(self, name: str) -> Person | None:
        """Match an identified name to a tracked person (case-insensitive)."""
        wanted = person_slug(name)
        for person in self._data.people.values():
            if person_slug(person.name) == wanted:
                return person
        return None
