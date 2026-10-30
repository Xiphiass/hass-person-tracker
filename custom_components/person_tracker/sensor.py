"""Sensor platform for the person_tracker integration.

Two sensors per known person, the area and the floor they are in, so the
recorder keeps their history: the tracker's state is only home/unknown and
attributes are not shown in the history graph. Each sensor mirrors the
``area`` / ``floor`` attribute of the person's tracker, so the tracker stays
the single source of truth for which source leads, TTL expiry, authoritative
demotion and live renames (ADR 0004, ADR 0008). The state is unknown
whenever the tracker has no area or floor.
"""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.device_tracker import DOMAIN as DEVICE_TRACKER_DOMAIN
from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import ATTR_AREA, ATTR_FLOOR, DOMAIN
from .models import Person, PersonTrackerConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PersonTrackerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the area and floor sensors of each known person."""
    for person in entry.runtime_data.people.values():
        async_add_entities(
            [
                PersonLocationSensor(person, ATTR_AREA, "mdi:map-marker"),
                PersonLocationSensor(person, ATTR_FLOOR, "mdi:home-floor-0"),
            ],
            config_subentry_id=person.subentry_id,
        )


class PersonLocationSensor(SensorEntity):
    """One attribute of a Person's tracker (area or floor), as a sensor."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, person: Person, attribute: str, icon: str) -> None:
        """Initialize the sensor mirroring *attribute* of *person*'s tracker."""
        self._person = person
        self._attribute = attribute
        self._attr_translation_key = attribute
        self._attr_icon = icon
        # Keyed by the subentry ID so renaming the person keeps the entity.
        self._attr_unique_id = f"{person.subentry_id}_{attribute}"
        self._attr_device_info = person.device_info
        self._unsub_tracker: Callable[[], None] | None = None

    async def async_added_to_hass(self) -> None:
        """Follow the tracker's state and take over its current value."""
        self._unsub_tracker = self.hass.bus.async_listen(
            EVENT_STATE_CHANGED,
            self._async_handle_tracker_change,
            event_filter=self._is_tracker_event,
        )
        self._sync_from_tracker()

    async def async_will_remove_from_hass(self) -> None:
        """Stop following the tracker."""
        if self._unsub_tracker is not None:
            self._unsub_tracker()
            self._unsub_tracker = None

    def _tracker_entity_id(self) -> str | None:
        """The tracker's current entity ID; looked up so renames are followed."""
        return er.async_get(self.hass).async_get_entity_id(
            DEVICE_TRACKER_DOMAIN, DOMAIN, self._person.subentry_id
        )

    @callback
    def _is_tracker_event(self, event_data: EventStateChangedData) -> bool:
        """Whether a state change is for this person's tracker."""
        return (
            event_data["entity_id"].startswith(f"{DEVICE_TRACKER_DOMAIN}.")
            and event_data["entity_id"] == self._tracker_entity_id()
        )

    def _sync_from_tracker(self) -> None:
        """Take over the tracker's attribute, or unknown without one."""
        value = None
        if (entity_id := self._tracker_entity_id()) is not None and (
            state := self.hass.states.get(entity_id)
        ) is not None:
            value = state.attributes.get(self._attribute)
        self._attr_native_value = value

    @callback
    def _async_handle_tracker_change(
        self, _event: Event[EventStateChangedData]
    ) -> None:
        """The tracker re-published; write only if our value changed."""
        previous = self._attr_native_value
        self._sync_from_tracker()
        if self._attr_native_value != previous:
            self.async_write_ha_state()
