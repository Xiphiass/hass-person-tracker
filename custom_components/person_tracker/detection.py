"""Detection loop: trigger snapshot -> AI task identification -> tracker update.

Subscribes to every configured trigger binary sensor. On an OFF -> ON edge,
the matching camera's snapshot is sent to the configured AI task entity,
which identifies the household members in the scene (Issue #3). Each
identified person's location, area, last_seen and last_camera are updated,
and their tracker entity is nudged through a per-person dispatcher signal.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from logging import getLogger
from typing import Any, TypedDict

import probatio
from homeassistant.components.ai_task.task import async_generate_data
from homeassistant.components.camera import Image, async_get_image
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .const import (
    AI_TASK_INSTRUCTIONS,
    AI_TASK_NAME,
    SIGNAL_TRACKER_UPDATE_PERSON,
    UNKNOWN_SNAPSHOT_CLEANUP_HOURS,
    UNKNOWN_SNAPSHOT_RETENTION_DAYS,
)
from .helpers import (
    get_camera_area,
    purge_unknown_snapshots,
    save_unknown_snapshot,
)
from .models import Camera, Person, PersonTrackerData, person_slug

_LOGGER = getLogger(__package__)

# Expected shape of the AI task response (Issue #3).
AI_RESULT_STRUCTURE = probatio.Schema(
    {"persons": [{"name": str, "confidence": float}], "unknown_persons": int}
)


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


def unknown_person_count(data: Any) -> int | None:
    """The AI task's count of visible non-household people, if it gave one.

    ``None`` means the response carries no usable count, so it cannot tell
    an unknown person apart from an empty scene.
    """
    if not isinstance(data, dict):
        return None
    count = data.get("unknown_persons")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return None
    return count


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
        """Watch every configured trigger and purge stale unknown snapshots."""
        for camera in self._data.cameras.values():
            if not camera.triggers:
                continue
            self._unsubscribes.append(
                async_track_state_change_event(
                    self._hass, camera.triggers, self._async_on_trigger
                )
            )
        # Unknown snapshots live for a fixed retention window (Issue #4):
        # purge once now (catches files from a long shutdown), then daily.
        await self._async_purge_unknown_snapshots()
        self._unsubscribes.append(
            async_track_time_interval(
                self._hass,
                self._async_purge_unknown_snapshots,
                timedelta(hours=UNKNOWN_SNAPSHOT_CLEANUP_HOURS),
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
        # Grab the frame to keep before the (slow) AI call, so a saved unknown
        # snapshot shows the scene at trigger time, not seconds later.
        image = await self._async_fetch_snapshot(camera)
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
        identified = False
        now = dt_util.now()
        for hit in hits:
            person = self._find_person(hit["name"])
            if person is None:
                _LOGGER.debug("Ignoring unrecognized person %r.", hit["name"])
                continue
            identified = True
            person.location = area.name
            person.area_id = area.id
            person.last_seen = now
            person.last_camera = camera.entity_id
            async_dispatcher_send(
                self._hass, f"{SIGNAL_TRACKER_UPDATE_PERSON}{person.subentry_id}"
            )
        if identified:
            return
        # No known person was identified, so no tracker state changes.
        if not hits and unknown_person_count(result.data) == 0:
            # The AI saw no one at all: a false trigger, not a sighting.
            _LOGGER.debug("No one visible on %s; nothing to save.", camera.entity_id)
            return
        # Preserve the sighting as an unknown snapshot (Issue #4).
        await self._async_save_unknown_snapshot(camera, area, image)

    async def _async_fetch_snapshot(self, camera: Camera) -> Image | None:
        """Fetch a snapshot of *camera*; ``None`` if the camera can't provide one.

        A failed fetch must not stop the identification run.
        """
        try:
            return await async_get_image(self._hass, camera.entity_id)
        except HomeAssistantError as err:
            _LOGGER.debug("Could not fetch a snapshot of %s: %s", camera.entity_id, err)
            return None

    async def _async_save_unknown_snapshot(
        self, camera: Camera, area: ar.AreaEntry, image: Image | None
    ) -> None:
        """Save *image* of *camera* as an unknown snapshot (Issue #4).

        The saved image is the record of an unidentified sighting and the raw
        material for future self-learning. A failed fetch or write must not
        break the detection run.
        """
        if image is None:
            _LOGGER.warning(
                "No household member identified on %s (%s), but the snapshot "
                "could not be fetched.",
                camera.entity_id,
                area.name,
            )
            return
        try:
            path = await self._hass.async_add_executor_job(
                save_unknown_snapshot,
                self._hass,
                camera.entity_id,
                area.name,
                image.content,
                image.content_type,
            )
        except OSError as err:
            _LOGGER.warning(
                "No household member identified on %s (%s), but the snapshot "
                "could not be saved: %s",
                camera.entity_id,
                area.name,
                err,
            )
            return
        _LOGGER.info(
            "No household member identified on %s (%s) at %s; saved unknown "
            "snapshot %s.",
            camera.entity_id,
            area.name,
            dt_util.now().isoformat(),
            path,
        )

    async def _async_purge_unknown_snapshots(
        self, _now: datetime | None = None
    ) -> None:
        """Delete unknown snapshots past the retention window (Issue #4).

        Runs on setup and once a day; the time interval listener passes the
        UTC fire time. A failed purge must never take HA down.
        """
        try:
            removed = await self._hass.async_add_executor_job(
                purge_unknown_snapshots,
                self._hass,
                timedelta(days=UNKNOWN_SNAPSHOT_RETENTION_DAYS),
            )
        except Exception:  # noqa: BLE001 - cleanup must never take HA down
            _LOGGER.exception("Failed to purge old unknown snapshots")
            return
        if removed:
            _LOGGER.info(
                "Purged %d unknown snapshot(s) older than %d day(s).",
                removed,
                UNKNOWN_SNAPSHOT_RETENTION_DAYS,
            )

    def _find_person(self, name: str) -> Person | None:
        """Match an identified name to a tracked person (case-insensitive)."""
        wanted = person_slug(name)
        for person in self._data.people.values():
            if person_slug(person.name) == wanted:
                return person
        return None
