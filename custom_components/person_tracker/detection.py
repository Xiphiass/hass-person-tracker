"""Detection loop: trigger snapshot -> AI task identification -> tracker update.

Subscribes to every configured trigger binary sensor. On an OFF -> ON edge,
the matching camera's snapshot is saved and sent to the configured AI task
entity (the very frame that is later cropped into thumbnails),
together with every person's reference images, and the AI identifies the
household members in the scene (Issue #3). Reference images are re-read from
disk on every run, so added or replaced files apply without a reload. Each
identified person's location, area, last_seen and last_camera are updated,
and their tracker entity is nudged through a per-person dispatcher signal.

Presence re-check (ADR 0007): someone who sits still fires no new trigger
edge, so shortly before a person's sighting would expire, their last camera
is snapshotted once more and the AI is asked about that person only (with
only their reference images). A confirmation refreshes the sighting; anything
else lets the TTL expire as before. People of the same camera who are due
within the re-check lead share one run.

Corroboration (ADR 0009): once the AI has answered, a hit the Person's
supporting tracker agrees with (same area, else same floor) is boosted
before the minimum confidence comparison. The supporting tracker never
reaches the AI prompt, so the camera stays an independent check.

Failure policy (Issue #5): a failed snapshot or a failed AI call logs an
error and aborts the run — no tracker state changes, no retry is scheduled,
and the next trigger edge runs a fresh detection.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from functools import partial
from logging import getLogger
from pathlib import Path
from typing import Any, TypedDict

import probatio
import probatio.error
from homeassistant.components.ai_task.task import (
    GenDataTaskResult,
    async_generate_data,
)
from homeassistant.components.camera import Image, async_get_image
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_track_point_in_utc_time,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .const import (
    AI_TASK_INSTRUCTIONS,
    AI_TASK_NAME,
    AI_TASK_RECHECK_INSTRUCTIONS,
    DOMAIN,
    FRAMES_DIR,
    PEOPLE_DIR,
    RECHECK_LEAD_SECONDS,
    SIGNAL_TRACKER_UPDATE_PERSON,
    UNKNOWN_SNAPSHOT_CLEANUP_HOURS,
    UNKNOWN_SNAPSHOT_RETENTION_DAYS,
)
from .helpers import (
    delete_frame,
    discover_reference_images,
    get_camera_area,
    get_supporting_location,
    purge_unknown_snapshots,
    save_frame,
    save_person_thumbnail,
    save_unknown_snapshot,
)
from .lifetime import authoritative_reports_away, is_expired
from .models import Camera, Person, PersonTrackerData, person_slug
from .supporting import boosted_confidence

_LOGGER = getLogger(__package__)

# Expected shape of the AI task response (Issue #3).
AI_RESULT_STRUCTURE = probatio.Schema(
    {
        "persons": [
            {
                "name": str,
                "confidence": float,
                # A list with a length check, not a tuple or ExactSequence:
                # this renders as a plain number array with
                # minItems/maxItems, which every AI task provider accepts.
                probatio.Optional("bbox"): probatio.All(
                    [float], probatio.Length(min=4, max=4)
                ),
            }
        ],
        "unknown_persons": int,
    }
)


class _PersonHit(TypedDict):
    """One identified household member."""

    name: str
    confidence: float
    bbox: list[float] | None


def _person_bbox(entry: dict[str, Any]) -> list[float] | None:
    """A usable ``[x1, y1, x2, y2]`` box from an entry, or None."""
    raw = entry.get("bbox")
    if (
        isinstance(raw, (list, tuple))
        and len(raw) == 4
        and all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in raw
        )
    ):
        return [float(value) for value in raw]
    return None


def validate_ai_response(data: Any) -> list[dict[str, Any]]:
    """Normalize the AI task's response into person hits.

    The expected shape is
    ``{"persons": [{"name": str, "confidence": float, "bbox": [x1, y1, x2,
    y2]}]}``; a bare list of names (or name dicts) is also accepted. A
    malformed ``bbox`` is dropped, keeping the hit.

    Raises ValueError when *data* is not the documented persons envelope —
    a malformed response is a failed identification (Issue #5), never an
    empty (false-negative) result.
    """
    if isinstance(data, dict):
        entries = data.get("persons", data.get("people"))
        if entries is None:
            raise ValueError(f"AI response has no 'persons' list: {data!r}")
    elif isinstance(data, list):
        entries = data
    else:
        raise ValueError(f"AI response is not a dict or list: {data!r}")
    if not isinstance(entries, list):
        raise ValueError(f"AI response 'persons' is not a list: {entries!r}")

    hits: list[_PersonHit] = []
    for entry in entries:
        if isinstance(entry, str):
            name = entry
            confidence: Any = 1.0
            bbox: list[float] | None = None
        elif isinstance(entry, dict):
            name = entry.get("name", entry.get("person"))
            confidence = entry.get("confidence", 1.0)
            bbox = _person_bbox(entry)
        else:
            continue
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            confidence = 1.0
        hits.append(
            _PersonHit(
                name=name.strip(),
                confidence=float(confidence),
                bbox=bbox,
            )
        )
    return [{k: v for k, v in hit.items() if v is not None} for hit in hits]


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


def _reference_image_id(person: Person, path: Path) -> str:
    """The media source ID of *person*'s reference image *path*."""
    return f"media-source://{DOMAIN}/{PEOPLE_DIR}/{person.name}/{path.name}"


def _frame_id(path: Path) -> str:
    """The media source ID of the saved AI frame *path*."""
    return f"media-source://{DOMAIN}/{FRAMES_DIR}/{path.name}"


def _build_instructions(people: list[Person], *, recheck: bool = False) -> str:
    """The identification prompt, listing the household members to look for.

    Describes the attachment order: the snapshot first, then each of
    *people*'s reference images in order, and appends the people's optional
    descriptions. A *recheck* prompt (ADR 0007) adds the presence re-check
    framing before the response instructions.
    """
    names = ", ".join(sorted(person.name for person in people)) or "no one"
    descriptions = "; ".join(
        f"{person.name}: {person.description}"
        for person in sorted(people, key=lambda p: p.name)
        if person.description
    )
    if descriptions:
        descriptions = (
            f" Descriptions of household members: {descriptions}. Use them "
            "only to tell household members apart; a match on the "
            "description alone, without a visible face, is low confidence."
        )
    task = AI_TASK_INSTRUCTIONS
    if recheck:
        task = f"{AI_TASK_RECHECK_INSTRUCTIONS} {task}"
    references: list[str] = []
    position = 2
    for person in people:
        if count := len(person.reference_images):
            last = position + count - 1
            images = f"image {position}" if count == 1 else f"images {position}-{last}"
            references.append(f"{images}: {person.name}")
            position = last + 1
    if not references:
        return f"Household members to look for: {names}.{descriptions} {task}"
    return (
        f"Household members to look for: {names}.{descriptions} The first "
        "attached image is the camera snapshot. The images after it are "
        "reference photos of household members, use them to recognize who is "
        f"who: {'; '.join(references)}. Report only people visible in the "
        "camera "
        f"snapshot. {task}"
    )


class DetectionCoordinator:
    """Run the trigger -> snapshot -> identification -> update loop.

    Per-camera cooldown: after a detection run, further trigger edges of the
    same camera are ignored until the configured cooldown has elapsed.
    Presence re-checks (ADR 0007) ignore the cooldown but claim it too.
    """

    def __init__(self, hass: HomeAssistant, data: PersonTrackerData) -> None:
        """Initialize the coordinator."""
        self._hass = hass
        self._data = data
        self._last_run: dict[str, datetime] = {}
        self._unsubscribes: list[Callable[[], None]] = []
        # Pending presence re-check timers, keyed by person subentry ID.
        self._recheck_timers: dict[str, Callable[[], None]] = {}

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
        """Unsubscribe all triggers and re-check timers (entry unload)."""
        for unsub in (*self._unsubscribes, *self._recheck_timers.values()):
            unsub()
        self._unsubscribes = []
        self._recheck_timers = {}

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
        last = self._last_run.get(camera.entity_id)
        if (
            last is not None
            and (dt_util.now() - last).total_seconds() < self._data.cooldown_seconds
        ):
            _LOGGER.debug("Skipping detection run for %s (cooldown).", camera.entity_id)
            return
        await self._async_run(camera, partial(self._async_detect, camera))

    async def _async_run(
        self, camera: Camera, run: Callable[[], Awaitable[bool]]
    ) -> None:
        """Run *run* on *camera* holding its cooldown slot.

        The slot is claimed before the (slow) run so edges arriving mid-run
        are suppressed. A failed run must not hold the cooldown: the next
        edge runs a fresh detection (Issue #5).
        """
        last = self._last_run.get(camera.entity_id)
        self._last_run[camera.entity_id] = dt_util.now()
        try:
            succeeded = await run()
        except Exception:
            _LOGGER.exception("Detection run failed for %s", camera.entity_id)
            succeeded = False
        if not succeeded:
            if last is None:
                self._last_run.pop(camera.entity_id, None)
            else:
                self._last_run[camera.entity_id] = last

    async def _async_refresh_reference_images(self) -> None:
        """Re-read every person's reference images from disk.

        Done on each run so files added, replaced or removed after setup
        apply without a reload.
        """
        people = list(self._data.people.values())
        found = await self._hass.async_add_executor_job(
            lambda: [discover_reference_images(self._hass, p.name) for p in people]
        )
        for person, images in zip(people, found, strict=True):
            person.reference_images = images

    async def _async_detect(self, camera: Camera) -> bool:
        """Run the AI task on a snapshot of *camera* and apply its result.

        Failure policy (Issue #5): a failed snapshot or a failed AI call
        logs an error and aborts the run — no tracker state changes,
        nothing is retried, and the next trigger edge runs fresh.

        Returns False when the run failed, True otherwise.
        """
        # The area is looked up at trigger time so post-setup area changes
        # apply without a reload (ADR 0003).
        area = get_camera_area(self._hass, camera.entity_id)
        if area is None:
            _LOGGER.debug(
                "Camera %s is not in an HA area; skipping the run.",
                camera.entity_id,
            )
            return True
        await self._async_refresh_reference_images()
        people = list(self._data.people.values())
        outcome = await self._async_identify(camera, people)
        if outcome is None:
            return False
        image, data, hits = outcome
        if await self._async_apply_hits(camera, area, image, hits, people):
            return True
        # No known person was identified, so no tracker state changes.
        if not hits and unknown_person_count(data) == 0:
            # The AI saw no one at all: a false trigger, not a sighting.
            _LOGGER.debug("No one visible on %s; nothing to save.", camera.entity_id)
            return True
        # Preserve the sighting as an unknown snapshot (Issue #4).
        await self._async_save_unknown_snapshot(camera, area, image)
        return True

    async def _async_identify(
        self, camera: Camera, people: list[Person], *, recheck: bool = False
    ) -> tuple[Image, Any, list[dict[str, Any]]] | None:
        """Snapshot *camera* and ask the AI which of *people* it shows.

        Only *people*'s reference images are attached. Returns the snapshot,
        the raw AI data and the normalized hits, or None when the snapshot
        or the AI call failed (logged; Issue #5).
        """
        # Grab the frame to keep before the (slow) AI call, so a saved unknown
        # snapshot shows the scene at trigger time, not seconds later.
        try:
            image = await self._async_fetch_snapshot(camera)
        except HomeAssistantError as err:
            _LOGGER.error("Snapshot failed for %s: %s", camera.entity_id, err)
            return None
        # The AI gets exactly this frame, not a fresh camera fetch: its bbox
        # must describe the frame the thumbnail is cropped from.
        try:
            frame = await self._hass.async_add_executor_job(
                save_frame,
                self._hass,
                camera.entity_id,
                image.content,
                image.content_type,
            )
        except OSError as err:
            _LOGGER.error("Could not save frame of %s: %s", camera.entity_id, err)
            return None
        attachments = [{"media_content_id": _frame_id(frame)}]
        attachments.extend(
            {"media_content_id": _reference_image_id(person, path)}
            for person in people
            for path in person.reference_images
        )
        result: GenDataTaskResult
        try:
            result = await async_generate_data(
                self._hass,
                task_name=AI_TASK_NAME,
                entity_id=self._data.ai_task_entity_id,
                instructions=_build_instructions(people, recheck=recheck),
                structure=AI_RESULT_STRUCTURE,
                attachments=attachments,
            )
        # Provider-side failures (down, timeout, malformed structured
        # output) surface as HomeAssistantError or a probatio validation
        # error, depending on the AI Task integration.
        except (HomeAssistantError, probatio.error.Error) as err:
            _LOGGER.error("Identification failed for %s: %s", camera.entity_id, err)
            return None
        finally:
            await self._hass.async_add_executor_job(delete_frame, frame)
        try:
            hits = validate_ai_response(result.data)
        except ValueError as err:
            _LOGGER.error(
                "Malformed AI response for %s: %s; response: %r",
                camera.entity_id,
                err,
                result.data,
            )
            return None
        return image, result.data, hits

    async def _async_apply_hits(
        self,
        camera: Camera,
        area: ar.AreaEntry,
        image: Image,
        hits: list[dict[str, Any]],
        people: list[Person],
    ) -> list[Person]:
        """Record a sighting on *camera* for each confident hit among *people*.

        A hit the person's supporting tracker agrees with is boosted before
        the minimum confidence comparison (ADR 0009). Returns the identified
        people.
        """
        identified: list[Person] = []
        now = dt_util.now()
        for hit in hits:
            person = _find_person(people, hit["name"])
            if person is None:
                _LOGGER.debug("Ignoring unrecognized person %r.", hit["name"])
                continue
            confidence = self._boosted_confidence(
                camera, area, person, hit["confidence"]
            )
            # Uncertain hits update no tracker; the sighting is then kept as
            # an unknown snapshot by the caller.
            if confidence < self._data.min_confidence:
                _LOGGER.debug(
                    "Ignoring %r on %s: confidence %.2f is below %.2f.",
                    hit["name"],
                    camera.entity_id,
                    confidence,
                    self._data.min_confidence,
                )
                continue
            identified.append(person)
            person.location = area.name
            person.area_id = area.id
            person.last_seen = now
            person.last_camera = camera.entity_id
            await self._async_save_person_thumbnail(person, image, hit.get("bbox"))
            self._schedule_recheck(person)
            async_dispatcher_send(
                self._hass, f"{SIGNAL_TRACKER_UPDATE_PERSON}{person.subentry_id}"
            )
        return identified

    def _boosted_confidence(
        self, camera: Camera, area: ar.AreaEntry, person: Person, confidence: float
    ) -> float:
        """*confidence* of *person* on *camera*, corroborated (ADR 0009).

        The supporting tracker is read only now, after Identification, so
        it never reaches the AI.
        """
        supporting_area, supporting_floor = get_supporting_location(self._hass, person)
        boosted = boosted_confidence(
            confidence,
            area.id,
            area.floor_id,
            supporting_area.id if supporting_area else None,
            supporting_floor.floor_id if supporting_floor else None,
        )
        if boosted != confidence:
            _LOGGER.debug(
                "Supporting tracker agrees with %s on %s: confidence %.2f "
                "boosted to %.2f.",
                person.name,
                camera.entity_id,
                confidence,
                boosted,
            )
        return boosted

    def _recheck_lead(self, person: Person) -> timedelta:
        """How long before *person*'s expiry their re-check runs (ADR 0007)."""
        return min(timedelta(seconds=RECHECK_LEAD_SECONDS), person.ttl / 2)

    def _recheck_due(self, person: Person) -> datetime | None:
        """When *person*'s sighting is re-checked, or None for never."""
        if (
            not self._data.recheck
            or person.last_seen is None
            or person.ttl <= timedelta(0)
            or person.last_camera not in self._data.cameras
        ):
            return None
        return person.last_seen + person.ttl - self._recheck_lead(person)

    def _cancel_recheck(self, person: Person) -> None:
        """Cancel *person*'s pending re-check, if any."""
        if unsub := self._recheck_timers.pop(person.subentry_id, None):
            unsub()

    def _schedule_recheck(self, person: Person) -> None:
        """(Re)schedule *person*'s presence re-check for their sighting."""
        self._cancel_recheck(person)
        if (due := self._recheck_due(person)) is None:
            return
        self._recheck_timers[person.subentry_id] = async_track_point_in_utc_time(
            self._hass, partial(self._async_on_recheck_due, person), due
        )

    async def _async_on_recheck_due(self, person: Person, _now: datetime) -> None:
        """*person*'s re-check is due: re-check their last camera.

        Everyone else last seen on that camera whose re-check falls within
        the lead joins the same run. People whose authoritative tracker
        already reports them away are not re-checked; they expire.
        """
        self._recheck_timers.pop(person.subentry_id, None)
        camera = self._data.cameras.get(person.last_camera or "")
        if camera is None:
            return
        now = dt_util.now()
        people: list[Person] = []
        for other in self._data.people.values():
            due = self._recheck_due(other)
            if (
                other.last_camera != camera.entity_id
                or due is None
                or due > now + self._recheck_lead(other)
                or is_expired(other.last_seen, other.ttl, now)
                or self._reports_away(other)
            ):
                continue
            self._cancel_recheck(other)
            people.append(other)
        if not people:
            return
        await self._async_run(camera, partial(self._async_recheck, camera, people))

    def _reports_away(self, person: Person) -> bool:
        """Whether *person*'s authoritative tracker reports them away."""
        if not person.authoritative_tracker:
            return False
        state = self._hass.states.get(person.authoritative_tracker)
        return state is not None and authoritative_reports_away(state.state)

    async def _async_recheck(self, camera: Camera, people: list[Person]) -> bool:
        """Ask the AI whether *people* are still on *camera* (ADR 0007).

        Only *people* (with their reference images) are asked about. A
        confirmed person's sighting is refreshed, which schedules their next
        re-check; everyone else is left to expire. Nothing is saved as an
        unknown snapshot. Returns False when the run failed, True otherwise.
        """
        area = get_camera_area(self._hass, camera.entity_id)
        if area is None:
            _LOGGER.debug(
                "Camera %s is not in an HA area; skipping the re-check.",
                camera.entity_id,
            )
            return True
        await self._async_refresh_reference_images()
        # Without reference photos a narrowed question can only be answered
        # by guessing.
        people = [person for person in people if person.reference_images]
        if not people:
            return True
        outcome = await self._async_identify(camera, people, recheck=True)
        if outcome is None:
            return False
        image, _data, hits = outcome
        confirmed = await self._async_apply_hits(camera, area, image, hits, people)
        if missing := [person.name for person in people if person not in confirmed]:
            _LOGGER.debug(
                "Re-check on %s did not confirm %s; letting the sighting expire.",
                camera.entity_id,
                ", ".join(missing),
            )
        return True

    async def _async_fetch_snapshot(self, camera: Camera) -> Image:
        """Fetch a snapshot of *camera*.

        Raises HomeAssistantError when the camera can't provide one; the
        caller skips the run without changing state (Issue #5).
        """
        return await async_get_image(self._hass, camera.entity_id)

    async def _async_save_person_thumbnail(
        self, person: Person, image: Image, bbox: list[float] | None
    ) -> None:
        """Save *image* as *person*'s last-seen thumbnail (Issue #3).

        The frame is cropped around *bbox* when the AI gave a plausible one. A
        failed write keeps the previous thumbnail and must not break the
        detection run (Issue #5).
        """
        try:
            path = await self._hass.async_add_executor_job(
                save_person_thumbnail,
                self._hass,
                person.name,
                image.content,
                image.content_type,
                bbox,
            )
        except (OSError, ValueError) as err:
            _LOGGER.warning(
                "Could not save %s's last-seen thumbnail: %s", person.name, err
            )
            return
        person.last_thumbnail = path
        person.last_thumbnail_updated = dt_util.now()

    async def _async_save_unknown_snapshot(
        self, camera: Camera, area: ar.AreaEntry, image: Image
    ) -> None:
        """Save *image* of *camera* as an unknown snapshot (Issue #4).

        The saved image is the record of an unidentified sighting and the raw
        material for future self-learning. A failed write must not break the
        detection run.
        """
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


def _find_person(people: list[Person], name: str) -> Person | None:
    """Match an identified name to one of *people* (case-insensitive)."""
    wanted = person_slug(name)
    for person in people:
        if person_slug(person.name) == wanted:
            return person
    return None
