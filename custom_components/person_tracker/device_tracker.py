"""Device tracker platform for the person_tracker integration.

One device (entry_type=SERVICE) and one device_tracker entity per known
person (ADR 0002), both tied to the person's config subentry so removing the
person removes them. The state is the display name of the HA area the person
was last seen in, or "unknown" until the first sighting / after the TTL
expires. Detection itself is implemented in later tickets; this ticket
establishes the identity and initial state.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, override

from homeassistant.components.device_tracker import TrackerEntity
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    ATTR_AREA_ID,
    ATTR_LAST_CAMERA,
    ATTR_LAST_LOCATION,
    ATTR_LAST_SEEN,
    DOMAIN,
    SIGNAL_TRACKER_UPDATE_PERSON,
)
from .models import Person, PersonTrackerConfigEntry


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


class PersonTrackerEntity(TrackerEntity):
    """A device tracker for one known Person."""

    _attr_should_poll = False
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
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, person.subentry_id)},
            name=person.name,
            entry_type=DeviceEntryType.SERVICE,
        )
        self._unsub_update: Callable[[], None] | None = None

    async def async_added_to_hass(self) -> None:
        """Re-publish state when a detection run updates this person."""
        self._unsub_update = async_dispatcher_connect(
            self.hass,
            f"{SIGNAL_TRACKER_UPDATE_PERSON}{self._person.subentry_id}",
            self._async_handle_update,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Stop listening for detection updates."""
        if self._unsub_update is not None:
            self._unsub_update()
            self._unsub_update = None

    @callback
    def _async_handle_update(self) -> None:
        """A detection run updated this person; re-publish the state."""
        self.async_write_ha_state()

    @property
    def state(self) -> str:
        """The display name of the last seen HA area, or unknown."""
        return self._person.location or STATE_UNKNOWN

    @property
    @override
    def extra_state_attributes(
        self,
    ) -> dict[str, Any] | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Last seen (timestamp), last location, area ID, last camera."""
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
