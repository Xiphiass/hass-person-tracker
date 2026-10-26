"""Device tracker platform for the person_tracker integration.

One device (entry_type=SERVICE) and one device_tracker entity per known
person (ADR 0002), both tied to the person's config subentry so removing the
person removes them. The entity is a presence (connection) tracker in
``zone.home``: the state is "home" while a sighting is current, so a linked
HA person is recorded at home, and "unknown" until the first sighting and
again once the TTL expires or the authoritative tracker demotes it (ADR
0004). The room lives in the attributes: ``area`` while the sighting is
current, ``last_location``/``last_seen`` retained throughout so staleness
stays queryable; the area's floor follows the same rule (``floor`` vs
``last_floor``/``floor_id``). The optional Supporting tracker (ADR 0008) is
reported alongside: its area and floor, resolved live, and whether it agrees
with the current camera sighting; it does not change the state (Issue #29).
The entity picture mirrors the picture of the HA person that tracks this
entity, so dashboards can show the profile picture.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, override

from homeassistant.components.device_tracker import SourceType
from homeassistant.components.device_tracker.entity import BaseScannerEntity
from homeassistant.const import ATTR_ENTITY_PICTURE, EVENT_STATE_CHANGED
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
)
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_AREA,
    ATTR_AREA_ID,
    ATTR_FLOOR,
    ATTR_FLOOR_ID,
    ATTR_LAST_CAMERA,
    ATTR_LAST_FLOOR,
    ATTR_LAST_LOCATION,
    ATTR_LAST_SEEN,
    ATTR_SUPPORTING_AGREES,
    ATTR_SUPPORTING_AREA,
    ATTR_SUPPORTING_AREA_ID,
    ATTR_SUPPORTING_FLOOR,
    ATTR_SUPPORTING_FLOOR_ID,
    DOMAIN,
    SIGNAL_TRACKER_UPDATE_PERSON,
)
from .lifetime import authoritative_reports_away, is_expired
from .models import Person, PersonTrackerConfigEntry
from .supporting import AreaOrFloor, resolve_area_or_floor, supporting_agrees


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PersonTrackerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one tracker entity per known person."""
    for person in entry.runtime_data.people.values():
        async_add_entities(
            [PersonTrackerEntity(person)], config_subentry_id=person.subentry_id
        )


class PersonTrackerEntity(BaseScannerEntity):
    """A device tracker for one known Person."""

    _attr_should_poll = False
    # Cameras are fixed endpoints in the home, like a router or beacon:
    # presence by "connection" rather than by GPS position, so HA's person
    # entity reports "home" (and counts the person in zone.home).
    _attr_source_type = SourceType.ROUTER
    _attr_name = None
    # BaseTrackerEntity annotates `_attr_device_info` as None, but platform
    # classes are expected to override it (see Entity.device_info).
    _attr_device_info: (
        DeviceInfo | None
    )  # pyright: ignore[reportIncompatibleVariableOverride]

    def __init__(self, person: Person) -> None:
        """Initialize the entity for *person*."""
        super().__init__()
        self._person = person
        # Keyed by the subentry ID so renaming the person keeps the entity.
        self._attr_unique_id = person.subentry_id
        self._attr_device_info = (
            person.device_info
        )  # pyright: ignore[reportIncompatibleVariableOverride]
        self._unsub_update: Callable[[], None] | None = None
        self._unsub_area: Callable[[], None] | None = None
        self._unsub_floor: Callable[[], None] | None = None
        self._unsub_authoritative: Callable[[], None] | None = None
        self._unsub_supporting: Callable[[], None] | None = None
        self._unsub_ttl: Callable[[], None] | None = None
        self._unsub_person: Callable[[], None] | None = None

    async def async_added_to_hass(self) -> None:
        """Subscribe to all state-affecting sources and start the TTL timer."""
        self._unsub_update = async_dispatcher_connect(
            self.hass,
            f"{SIGNAL_TRACKER_UPDATE_PERSON}{self._person.subentry_id}",
            self._async_republish,
        )

        # Live area renames (ADR 0004): the area we point at may be renamed
        # without a restart, so re-publish whenever our area changes.
        self._unsub_area = self.hass.bus.async_listen(
            ar.EVENT_AREA_REGISTRY_UPDATED,
            self._async_handle_area_registry_update,
        )
        # Likewise the floor of that area may be renamed (Issue #28); moving
        # the area to another floor is an area registry update, caught above.
        self._unsub_floor = self.hass.bus.async_listen(
            fr.EVENT_FLOOR_REGISTRY_UPDATED,
            self._async_handle_floor_registry_update,
        )

        # Authoritative demotion (ADR 0004): re-publish whenever the
        # person's authoritative tracker (e.g. their phone) changes state.
        if self._person.authoritative_tracker:
            self._unsub_authoritative = async_track_state_change_event(
                self.hass,
                self._person.authoritative_tracker,
                self._async_republish,
            )

        # Supporting tracker (ADR 0008): re-publish whenever either of its
        # entities changes state.
        if supporting := self._supporting_entities():
            self._unsub_supporting = async_track_state_change_event(
                self.hass, supporting, self._async_republish
            )

        # Profile picture: follow any HA person that links (or unlinks) this
        # tracker or changes its picture.
        self._unsub_person = self.hass.bus.async_listen(
            EVENT_STATE_CHANGED,
            self._async_handle_person_change,
            event_filter=_is_person_event,
        )
        self._attr_entity_picture = self._linked_person_picture()

        self._schedule_expiry()

    async def async_will_remove_from_hass(self) -> None:
        """Stop listening for updates and cancel the TTL timer."""
        for unsub in (
            self._unsub_update,
            self._unsub_area,
            self._unsub_floor,
            self._unsub_authoritative,
            self._unsub_supporting,
            self._unsub_ttl,
            self._unsub_person,
        ):
            if unsub is not None:
                unsub()
        self._unsub_update = None
        self._unsub_area = None
        self._unsub_floor = None
        self._unsub_authoritative = None
        self._unsub_supporting = None
        self._unsub_ttl = None
        self._unsub_person = None

    def _schedule_expiry(self) -> None:
        """(Re)schedule the TTL wake-up for the current last_seen.

        The timer only wakes the state property; the expiry decision itself
        lives in ``lifetime.is_expired`` so the property stays the single
        source of truth (Issue #6 / ADR 0004).
        """
        if self._unsub_ttl is not None:
            self._unsub_ttl()
            self._unsub_ttl = None
        person = self._person
        if person.last_seen is None or person.ttl <= timedelta(0):
            return
        expiry = person.last_seen + person.ttl
        if expiry <= dt_util.now():
            # Already expired; the state property reports unknown.
            return
        self._unsub_ttl = async_track_point_in_utc_time(
            self.hass, self._async_handle_ttl, expiry
        )

    @callback
    def _async_republish(self, *_: Any) -> None:
        """A detection run or tracker change affected this person."""
        # A detection may have moved last_seen, so the TTL wake-up must be
        # re-derived from the fresh sighting.
        self._schedule_expiry()
        self.async_write_ha_state()

    @callback
    def _async_handle_area_registry_update(
        self, event: Event[ar.EventAreaRegistryUpdatedData]
    ) -> None:
        """An area registry change; re-publish if it may affect us.

        Any area change may change what the supporting entities resolve to
        (a new name or alias), so with a supporting tracker every change
        re-publishes.
        """
        if (
            event.data.get("area_id") != self._person.area_id
            and not self._supporting_entities()
        ):
            return
        self.async_write_ha_state()

    @callback
    def _async_handle_floor_registry_update(
        self, event: Event[fr.EventFloorRegistryUpdatedData]
    ) -> None:
        """A floor registry change; re-publish if it may affect us."""
        floor = self._floor()
        if not self._supporting_entities() and (
            floor is None or event.data.get("floor_id") != floor.floor_id
        ):
            return
        self.async_write_ha_state()

    def _linked_person_picture(self) -> str | None:
        """The picture of the first HA person tracking this entity, if any."""
        for state in self.hass.states.async_all("person"):
            if self.entity_id in state.attributes.get("device_trackers", ()):
                return state.attributes.get(ATTR_ENTITY_PICTURE)
        return None

    @callback
    def _async_handle_person_change(self, _event: Event[EventStateChangedData]) -> None:
        """A person changed; re-publish only if our picture changed.

        Our own state changes update the linked person too, so writing
        unconditionally would loop.
        """
        picture = self._linked_person_picture()
        if picture == self._attr_entity_picture:
            return
        self._attr_entity_picture = picture
        self.async_write_ha_state()

    @callback
    def _async_handle_ttl(self, _now: datetime) -> None:
        """The TTL wake-up fired; re-publish the (now stale) state."""
        self.async_write_ha_state()

    @property
    @override
    def is_connected(self) -> bool | None:
        """True while a sighting is current; None (unknown) otherwise."""
        person = self._person

        # 1. Authoritative demotion (ADR 0004): the person's phone says they
        # are away from home, so the camera sighting is no longer believed.
        if person.authoritative_tracker:
            tracker_state = self.hass.states.get(person.authoritative_tracker)
            if tracker_state is not None and authoritative_reports_away(
                tracker_state.state
            ):
                return None

        # 2. TTL expiry (ADR 0004): no sighting within the person's TTL.
        if is_expired(person.last_seen, person.ttl, dt_util.now()):
            return None

        return True

    def _area_name(self) -> str | None:
        """The live display name of the last seen area, so renames show."""
        person = self._person
        if person.area_id is not None:
            area = ar.async_get(self.hass).async_get_area(person.area_id)
            if area is not None:
                return area.name
        return person.location

    def _floor(self) -> fr.FloorEntry | None:
        """The live floor of the last seen area, so renames and moves show."""
        if self._person.area_id is None:
            return None
        area = ar.async_get(self.hass).async_get_area(self._person.area_id)
        if area is None or area.floor_id is None:
            return None
        return fr.async_get(self.hass).async_get_floor(area.floor_id)

    def _supporting_entities(self) -> list[str]:
        """The configured supporting area and floor entities."""
        person = self._person
        return [
            entity_id
            for entity_id in (
                person.supporting_area_entity,
                person.supporting_floor_entity,
            )
            if entity_id
        ]

    def _resolve_supporting(
        self, entity_id: str | None, attribute: str, candidates: list[AreaOrFloor]
    ) -> str | None:
        """The ID of the area or floor *entity_id*'s state names, if any."""
        if entity_id is None or (state := self.hass.states.get(entity_id)) is None:
            return None
        return resolve_area_or_floor(
            state.state, state.attributes.get(attribute), candidates
        )

    def _supporting(self) -> tuple[ar.AreaEntry | None, fr.FloorEntry | None]:
        """The live supporting area and floor (ADR 0008).

        Without a floor entity, the floor is the supporting area's floor.
        """
        person = self._person
        area_registry = ar.async_get(self.hass)
        floor_registry = fr.async_get(self.hass)
        area = None
        area_id = self._resolve_supporting(
            person.supporting_area_entity,
            "area_id",
            [
                AreaOrFloor(entry.id, entry.name, frozenset(entry.aliases))
                for entry in area_registry.async_list_areas()
            ],
        )
        if area_id is not None:
            area = area_registry.async_get_area(area_id)
        if person.supporting_floor_entity:
            floor_id = self._resolve_supporting(
                person.supporting_floor_entity,
                "floor_id",
                [
                    AreaOrFloor(entry.floor_id, entry.name, frozenset(entry.aliases))
                    for entry in floor_registry.async_list_floors()
                ],
            )
        else:
            floor_id = area.floor_id if area is not None else None
        floor = None
        if floor_id is not None:
            floor = floor_registry.async_get_floor(floor_id)
        return area, floor

    def _supporting_attributes(self, is_connected: bool | None) -> dict[str, Any]:
        """The supporting area and floor, and whether they agree."""
        if not self._supporting_entities():
            return {}
        attributes: dict[str, Any] = {}
        area, floor = self._supporting()
        if area is not None:
            attributes[ATTR_SUPPORTING_AREA] = area.name
            attributes[ATTR_SUPPORTING_AREA_ID] = area.id
        if floor is not None:
            attributes[ATTR_SUPPORTING_FLOOR] = floor.name
            attributes[ATTR_SUPPORTING_FLOOR_ID] = floor.floor_id
        if is_connected:
            camera_floor = self._floor()
            agrees = supporting_agrees(
                self._person.area_id,
                camera_floor.floor_id if camera_floor is not None else None,
                area.id if area is not None else None,
                floor.floor_id if floor is not None else None,
            )
            if agrees is not None:
                attributes[ATTR_SUPPORTING_AGREES] = agrees
        return attributes

    @property
    @override
    def extra_state_attributes(
        self,
    ) -> dict[str, Any] | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Current and last area and floor, their IDs, last seen and camera.

        Plus the supporting tracker's area and floor, when configured.
        """
        person = self._person
        attributes: dict[str, Any] = {}
        is_connected = self.is_connected
        area_name = self._area_name()
        if area_name is not None:
            if is_connected:
                attributes[ATTR_AREA] = area_name
            attributes[ATTR_LAST_LOCATION] = area_name
        floor = self._floor()
        if floor is not None:
            if is_connected:
                attributes[ATTR_FLOOR] = floor.name
            attributes[ATTR_LAST_FLOOR] = floor.name
            attributes[ATTR_FLOOR_ID] = floor.floor_id
        if person.last_seen is not None:
            attributes[ATTR_LAST_SEEN] = person.last_seen.isoformat()
        if person.area_id is not None:
            attributes[ATTR_AREA_ID] = person.area_id
        if person.last_camera is not None:
            attributes[ATTR_LAST_CAMERA] = person.last_camera
        attributes |= self._supporting_attributes(is_connected)
        return attributes or None


@callback
def _is_person_event(event_data: EventStateChangedData) -> bool:
    """Whether a state change is for a person entity."""
    return event_data["entity_id"].startswith("person.")
