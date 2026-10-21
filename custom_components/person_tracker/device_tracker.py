"""Device tracker platform person_tracker integration.

One device (entry_type=SERVICE) and one device_tracker entity per known
person (ADR 0002), both tied person's config subentry so removing the
person removes them. state display name HA area person
was last seen in, or "unknown" until first sighting and again once the
TTL expires (ADR 0004); last_location/last_seen attributes retained
throughout so staleness stays queryable.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import Any

from homeassistant.components.device_tracker.entity import TrackerEntity
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
)
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_AREA_ID,
    ATTR_LAST_CAMERA,
    ATTR_LAST_LOCATION,
    ATTR_LAST_SEEN,
    DOMAIN,
    SIGNAL_TRACKER_UPDATE_PERSON,
)
from .lifetime import authoritative_reports_away, is_expired
from .models import Person, PersonTrackerConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PersonTrackerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the tracker entities."""
    async_add_entities(
        [PersonTrackerEntity(person) for person in entry.runtime_data.people.values()]
    )


class PersonTrackerEntity(TrackerEntity):
    """Tracker entity."""

    _attr_has_entity_name: bool = True
    _attr_device_info: DeviceInfo
    def __init__(self, person: Person) -> None:
        """Initialize."""
        super().__init__()
        self._person = person
        self._attr_unique_id = person.subentry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, person.subentry_id)},
            name=person.name,
            entry_type=DeviceEntryType.SERVICE,
        )
        self._unsub_update: Callable[[], None] | None = None
        self._unsub_area: Callable[[], None] | None = None
        self._unsub_authoritative: Callable[[], None] | None = None
        self._unsub_ttl: Callable[[], None] | None = None

    async def async_added_to_hass(self) -> None:
        """Register listeners."""
        self._unsub_update = async_dispatcher_connect(
            self.hass,
            f"{SIGNAL_TRACKER_UPDATE_PERSON}{self._person.subentry_id}",
            self._async_republish,
        )
        self._unsub_area = self.hass.bus.async_listen(
            ar.EVENT_AREA_REGISTRY_UPDATED,
            self._async_handle_area_registry_update,
        )
        self._unsub_authoritative = async_track_state_change_event(
            self.hass,
            [self._person.authoritative_tracker]
            if self._person.authoritative_tracker
            else [],
            self._async_republish,
        )
        self._schedule_expiry()

    async def async_will_remove_from_hass(self) -> None:
        """Unregister listeners."""
        for unsub in (
            self._unsub_update,
            self._unsub_area,
            self._unsub_authoritative,
            self._unsub_ttl,
        ):
            if unsub is not None:
                unsub()
        self._unsub_update = None
        self._unsub_area = None
        self._unsub_authoritative = None
        self._unsub_ttl = None

    def _schedule_expiry(self) -> None:
        """(Re)schedule TTL wake-up."""
        if self._unsub_ttl is not None:
            self._unsub_ttl()
            self._unsub_ttl = None
        person = self._person
        if person.last_seen is None or person.ttl_minutes <= 0:
            return
        expiry = person.last_seen + timedelta(minutes=person.ttl_minutes)
        if expiry <= dt_util.now():
            return
        self._unsub_ttl = async_track_point_in_utc_time(
            self.hass, self._async_handle_ttl, expiry
        )

    @callback
    def _async_republish(self, *_: Any) -> None:
        """Republish state."""
        self._schedule_expiry()
        self.async_write_ha_state()

    @callback
    def _async_handle_area_registry_update(
        self, event: Event[ar.EventAreaRegistryUpdatedData]
    ) -> None:
        """Re-publish on area registry updates."""
        if event.data.get("area_id") == self._person.area_id:
            self.async_write_ha_state()

    @callback
    def _async_handle_ttl(self, _: Any) -> None:
        """TTL wake-up."""
        self.async_write_ha_state()

    @property
    def state(self) -> str:
        """Return state."""
        person = self._person
        if authoritative_reports_away(self.hass, person):
            return STATE_UNKNOWN
        if is_expired(person.last_seen, timedelta(minutes=person.ttl_minutes), dt_util.now()):
            return STATE_UNKNOWN
        if person.area_id is not None:
            if area := ar.async_get(self.hass).async_get_area(person.area_id):
                return area.name
        return person.location or STATE_UNKNOWN

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Return attributes."""
        person = self._person
        attributes: dict[str, Any] = {}
        if person.last_seen is not None:
            attributes[ATTR_LAST_SEEN] = person.last_seen.isoformat()
        if person.location is not None:
            attributes[ATTR_LAST_LOCATION] = person.location
        if person.area_id is not None:
            attributes[ATTR_AREA_ID] = person.area_id
        if person.last_camera is not None:
            attributes[ATTR_LAST_CAMERA] = person.last_camera
        return attributes or None
