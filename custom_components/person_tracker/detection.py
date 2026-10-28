"""Detection loop: trigger snapshot -> AI task identification -> tracker update.

Subscribes to every configured trigger binary sensor. On an OFF -> ON edge,
the matching camera's snapshot is saved and sent to the configured AI task
entity (the very frame that is later cropped into thumbnails),
together with every person's reference images, and the AI identifies the
household members in the scene (Issue #3). Reference images are re-read from
disk on every run, so added or replaced files apply without a reload, and
large ones are sent as downscaled copies (Issue #43). Each
identified person's location, area, last_seen and last_camera are updated,
and their tracker entity is nudged through a per-person dispatcher signal.

Presence re-check (ADR 0007): someone who sits still fires no new trigger
edge, so shortly before a person's sighting would expire, their last camera
is snapshotted once more and the AI is asked about that person only (with
only their reference images). A confirmation refreshes the sighting; anything
else lets the TTL expire as before. People of the same camera who are due
within the re-check lead share one run. A person whom the supporting tracker
leads after a move is left out (Issue #33 / ADR 0008): the re-check would
snapshot the camera they left.

Move check (Issue #34 / ADR 0008): once the supporting area settles on a new
area that has cameras, and no camera sighting is newer than that change,
every camera of the area not in cooldown is asked about the moved person
only, worded like the re-check. A confirmation is a normal sighting (without
the corroboration boost); a miss changes nothing. People moving into the
same area at the same time share one run per camera.

Corroboration (ADR 0009): once the AI has answered, a hit the Person's
supporting tracker agrees with (same area, else same floor) is boosted
before the minimum confidence comparison. The supporting tracker never
reaches the AI prompt, so the camera stays an independent check.

Failure policy (Issue #5): a failed snapshot or a failed AI call logs an
error and aborts the run — no tracker state changes, no retry is scheduled,
and the next trigger edge runs a fresh detection.
"""

from __future__ import annotations

import asyncio
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
from homeassistant.core import Event, HomeAssistant, State
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
    AI_TASK_MOVE_CHECK_INSTRUCTIONS,
    AI_TASK_NAME,
    AI_TASK_RECHECK_INSTRUCTIONS,
    DOMAIN,
    FRAMES_DIR,
    RECHECK_LEAD_SECONDS,
    SIGNAL_TRACKER_UPDATE_PERSON,
    MOVE_CHECK_GROUP_SECONDS,
    SUPPORTING_SETTLE_SECONDS,
    UNKNOWN_SNAPSHOT_CLEANUP_HOURS,
    UNKNOWN_SNAPSHOT_RETENTION_DAYS,
)
from .helpers import (
    delete_frame,
    discover_reference_images,
    get_camera_area,
    get_entity_area_id,
    get_leading_source,
    get_supporting_changed,
    get_supporting_location,
    resolve_supporting_area,
    purge_unknown_snapshots,
    is_authoritative_away,
    prepare_reference_images,
    save_frame,
    save_person_thumbnail,
    save_unknown_snapshot,
)
from .lifetime import SOURCE_SUPPORTING, is_expired
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


def _reference_image_id(hass: HomeAssistant, path: Path) -> str:
    """The media source ID of reference image *path*.

    *path* is an original under the people folder or its downscaled copy
    under the reference cache; both are served below the same root.
    """
    relative = path.relative_to(hass.config.path(DOMAIN)).as_posix()
    return f"media-source://{DOMAIN}/{relative}"


def _frame_id(path: Path) -> str:
    """The media source ID of the saved AI frame *path*."""
    return f"media-source://{DOMAIN}/{FRAMES_DIR}/{path.name}"


def _build_instructions(people: list[Person], *, preamble: str | None = None) -> str:
    """The identification prompt, listing the household members to look for.

    Describes the attachment order: the snapshot first, then each of
    *people*'s reference images in order, and appends the people's optional
    descriptions. A *preamble* (the re-check framing of ADR 0007, or the move
    check's) goes before the response instructions.
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
    if preamble:
        task = f"{preamble} {task}"
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
    Presence re-checks (ADR 0007) ignore the cooldown but claim it too;
    move checks (Issue #34) leave out cameras in cooldown and claim it.
    """

    def __init__(self, hass: HomeAssistant, data: PersonTrackerData) -> None:
        """Initialize the coordinator."""
        self._hass = hass
        self._data = data
        self._last_run: dict[str, datetime] = {}
        self._unsubscribes: list[Callable[[], None]] = []
        # Pending presence re-check timers, keyed by person subentry ID.
        self._recheck_timers: dict[str, Callable[[], None]] = {}
        # Pending move check timers (Issue #34), keyed by person subentry ID.
        self._move_check_timers: dict[str, Callable[[], None]] = {}
        # The area ID each person's supporting tracker last settled in, so
        # a return to it (e.g. after a dropout) is no move.
        self._settled_areas: dict[str, str | None] = {}

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
        # Move checks (Issue #34) start when a supporting area entity changes.
        if self._data.move_check and (
            supporting_area_entities := [
                p.supporting_area_entity
                for p in self._data.people.values()
                if p.supporting_area_entity
            ]
        ):
            self._start_move_checks()
            self._unsubscribes.append(
                async_track_state_change_event(
                    self._hass,
                    supporting_area_entities,
                    self._async_on_supporting_change,
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
        """Unsubscribe all triggers and pending timers (entry unload)."""
        for unsub in (
            *self._unsubscribes,
            *self._recheck_timers.values(),
            *self._move_check_timers.values(),
        ):
            unsub()
        self._unsubscribes = []
        self._recheck_timers = {}
        self._move_check_timers = {}

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
        if self._in_cooldown(camera, dt_util.now()):
            _LOGGER.debug("Skipping detection run for %s (cooldown).", camera.entity_id)
            return
        await self._async_run(camera, partial(self._async_detect, camera))

    def _in_cooldown(self, camera: Camera, now: datetime) -> bool:
        """Whether *camera* ran within the cooldown as of *now*."""
        last = self._last_run.get(camera.entity_id)
        return (
            last is not None
            and (now - last).total_seconds() < self._data.cooldown_seconds
        )

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
        self, camera: Camera, people: list[Person], *, preamble: str | None = None
    ) -> tuple[Image, Any, list[dict[str, Any]]] | None:
        """Snapshot *camera* and ask the AI which of *people* it shows.

        Only *people*'s reference images are attached; *preamble* frames a
        re-check or move check prompt. Returns the snapshot,
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
        references = await self._hass.async_add_executor_job(
            lambda: [
                path
                for person in people
                for path in prepare_reference_images(
                    self._hass, person.name, person.reference_images
                )
            ]
        )
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
            {"media_content_id": _reference_image_id(self._hass, path)}
            for path in references
        )
        result: GenDataTaskResult
        try:
            result = await async_generate_data(
                self._hass,
                task_name=AI_TASK_NAME,
                entity_id=self._data.ai_task_entity_id,
                instructions=_build_instructions(people, preamble=preamble),
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
        *,
        corroborate: bool = True,
    ) -> list[Person]:
        """Record a sighting on *camera* for each confident hit among *people*.

        When *corroborate*, a hit the person's supporting tracker agrees with
        is boosted before the minimum confidence comparison (ADR 0009).
        Returns the identified people.
        """
        identified: list[Person] = []
        now = dt_util.now()
        for hit in hits:
            person = _find_person(people, hit["name"])
            if person is None:
                _LOGGER.debug("Ignoring unrecognized person %r.", hit["name"])
                continue
            confidence = hit["confidence"]
            if corroborate:
                confidence = self._corroborated_confidence(
                    camera, area, person, confidence
                )
            # Uncertain hits update no tracker; the sighting is then kept as
            # an unknown snapshot by the caller.
            if confidence < self._data.min_confidence:
                _LOGGER.debug(
                    "Ignoring %r on %s: confidence %.2f (raw %.2f) is below %.2f.",
                    hit["name"],
                    camera.entity_id,
                    confidence,
                    hit["confidence"],
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

    def _corroborated_confidence(
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
        already reports them away are not re-checked; they expire. Nor are
        people the supporting tracker leads after a move (Issue #33): their
        sighting expires without a retry.
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
                or is_authoritative_away(self._hass, other)
            ):
                continue
            # Cancelled first, so a skipped person is not re-checked later.
            self._cancel_recheck(other)
            if self._supporting_leads_after_move(other):
                continue
            people.append(other)
        if not people:
            return
        await self._async_run(camera, partial(self._async_recheck, camera, people))

    def _supporting_leads_after_move(self, person: Person) -> bool:
        """Whether the supporting tracker leads *person* after a move.

        Only called for a current sighting, so the supporting tracker can
        only lead because of a settled move (ADR 0008).
        """
        supporting_area, _ = get_supporting_location(self._hass, person)
        if get_leading_source(self._hass, person, supporting_area) != SOURCE_SUPPORTING:
            return False
        _LOGGER.debug(
            "Skipping the re-check of %s on %s: the supporting tracker places "
            "them in %s since they moved.",
            person.name,
            person.last_camera,
            supporting_area.name if supporting_area else None,
        )
        return True

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
        if not (people := _with_reference_images(people)):
            return True
        missing = await self._async_ask(
            camera, area, people, preamble=AI_TASK_RECHECK_INSTRUCTIONS
        )
        if missing is None:
            return False
        if missing:
            _LOGGER.debug(
                "Re-check on %s did not confirm %s; letting the sighting expire.",
                camera.entity_id,
                ", ".join(person.name for person in missing),
            )
        return True

    async def _async_ask(
        self,
        camera: Camera,
        area: ar.AreaEntry,
        people: list[Person],
        *,
        preamble: str,
        corroborate: bool = True,
    ) -> list[Person] | None:
        """Ask the AI whether *people* are on *camera* in *area*.

        The narrowed question of a re-check or move check: a confirmed
        person gets a normal sighting, nothing is saved as an unknown
        snapshot. Returns the people not confirmed, or None when the run
        failed.
        """
        outcome = await self._async_identify(camera, people, preamble=preamble)
        if outcome is None:
            return None
        image, _data, hits = outcome
        confirmed = await self._async_apply_hits(
            camera, area, image, hits, people, corroborate=corroborate
        )
        return [person for person in people if person not in confirmed]

    def _start_move_checks(self) -> None:
        """Note where each supporting tracker settled; resume pending moves.

        A move that has not settled yet (e.g. across a reload) is scheduled,
        as there is no telling where it came from.
        """
        now = dt_util.now()
        for person in self._data.people.values():
            if not person.supporting_area_entity:
                continue
            state = self._hass.states.get(person.supporting_area_entity)
            area = resolve_supporting_area(self._hass, state)
            if state is not None and area is not None and not _settled(state, now):
                self._settled_areas[person.subentry_id] = None
                self._schedule_move_check(person, state)
            else:
                self._settled_areas[person.subentry_id] = area.id if area else None

    async def _async_on_supporting_change(
        self, event: Event[EventStateChangedData]
    ) -> None:
        """A supporting area entity changed: (re)schedule its move checks.

        Only a changed state is a move; attribute updates keep the timer. A
        state naming no area, or the area the tracker last settled in, is no
        move and cancels a pending one.
        """
        old_state = event.data["old_state"]
        new_state = event.data["new_state"]
        if new_state is None or old_state is None or old_state.state == new_state.state:
            return
        area = resolve_supporting_area(self._hass, new_state)
        for person in self._data.people.values():
            if person.supporting_area_entity != event.data["entity_id"]:
                continue
            self._cancel_move_check(person)
            if area is not None and area.id != self._settled_areas.get(
                person.subentry_id
            ):
                self._schedule_move_check(person, new_state)

    def _schedule_move_check(self, person: Person, state: State) -> None:
        """Schedule *person*'s move check for when *state* has settled."""
        self._move_check_timers[person.subentry_id] = async_track_point_in_utc_time(
            self._hass,
            partial(self._async_on_move_check_due, person),
            state.last_changed + timedelta(seconds=SUPPORTING_SETTLE_SECONDS),
        )

    def _cancel_move_check(self, person: Person) -> None:
        """Cancel *person*'s pending move check, if any."""
        if unsub := self._move_check_timers.pop(person.subentry_id, None):
            unsub()

    def _move_check_area(self, person: Person) -> ar.AreaEntry | None:
        """The area a move took *person* to, if it needs a move check.

        That is while the supporting tracker leads (so not when the
        authoritative tracker reports them away, nor when a current camera
        sighting already places them there) and no camera sighting is newer
        than the move. The caller decides whether the move has settled.
        """
        changed = get_supporting_changed(self._hass, person)
        if changed is None or (
            person.last_seen is not None and person.last_seen >= changed
        ):
            return None
        area, _ = get_supporting_location(self._hass, person)
        if get_leading_source(self._hass, person, area) != SOURCE_SUPPORTING:
            return None
        return area

    async def _async_on_move_check_due(self, person: Person, _now: datetime) -> None:
        """*person*'s supporting move settled: ask that area's cameras.

        Everyone else whose move into the same area settles within the
        grouping window joins the run, a few seconds early. Cameras in
        cooldown are left out: their recent run already answered. A trigger
        run that is still in flight holds the cooldown too, and if it then
        fails, the move check is not retried.
        """
        self._move_check_timers.pop(person.subentry_id, None)
        area, _ = get_supporting_location(self._hass, person)
        self._settled_areas[person.subentry_id] = area.id if area else None
        if (area := self._move_check_area(person)) is None:
            return
        cameras = [
            camera
            for camera in self._data.cameras.values()
            if get_entity_area_id(self._hass, camera.entity_id) == area.id
        ]
        if not cameras:
            return
        now = dt_util.now()
        window = timedelta(seconds=MOVE_CHECK_GROUP_SECONDS)
        people = [person]
        for other in self._data.people.values():
            if (
                other is person
                or other.subentry_id not in self._move_check_timers
                or (changed := get_supporting_changed(self._hass, other)) is None
                or changed + timedelta(seconds=SUPPORTING_SETTLE_SECONDS) > now + window
                or (other_area := self._move_check_area(other)) is None
                or other_area.id != area.id
            ):
                continue
            self._cancel_move_check(other)
            self._settled_areas[other.subentry_id] = area.id
            people.append(other)
        await self._async_refresh_reference_images()
        if not (people := _with_reference_images(people)):
            return
        if not (cameras := [c for c in cameras if not self._in_cooldown(c, now)]):
            _LOGGER.debug(
                "Skipping the move check in %s: every camera is in cooldown.",
                area.name,
            )
            return
        await asyncio.gather(
            *(
                self._async_run(
                    camera, partial(self._async_move_check, camera, area, people)
                )
                for camera in cameras
            )
        )

    async def _async_move_check(
        self, camera: Camera, area: ar.AreaEntry, people: list[Person]
    ) -> bool:
        """Ask the AI whether *people* are on *camera* (Issue #34).

        A confirmation is a normal sighting, but not corroborated: the
        supporting tracker chose the camera and the people, so its agreement
        would be circular. A miss changes nothing. Returns False when the
        run failed, True otherwise.
        """
        missing = await self._async_ask(
            camera,
            area,
            people,
            preamble=AI_TASK_MOVE_CHECK_INSTRUCTIONS,
            corroborate=False,
        )
        if missing is None:
            return False
        if missing:
            _LOGGER.debug(
                "Move check on %s did not confirm %s; the supporting tracker "
                "keeps leading.",
                camera.entity_id,
                ", ".join(person.name for person in missing),
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


def _with_reference_images(people: list[Person]) -> list[Person]:
    """*people* who have reference images.

    Without reference photos a narrowed question can only be answered by
    guessing.
    """
    return [person for person in people if person.reference_images]


def _settled(state: State, now: datetime) -> bool:
    """Whether a supporting area *state* has held for the settle period."""
    return now - state.last_changed >= timedelta(seconds=SUPPORTING_SETTLE_SECONDS)


def _find_person(people: list[Person], name: str) -> Person | None:
    """Match an identified name to one of *people* (case-insensitive)."""
    wanted = person_slug(name)
    for person in people:
        if person_slug(person.name) == wanted:
            return person
    return None
