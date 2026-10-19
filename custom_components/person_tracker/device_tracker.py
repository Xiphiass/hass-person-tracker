"""Device tracker platform for the person_tracker integration.

One device (entry_type=SERVICE) and one device_tracker entity per known
person (ADR 0002). The state is the display name of the HA area the person
was last seen in, or "unknown" until the first sighting / after the TTL
expires. Detection itself is implemented in later tickets; this ticket
establishes the identity and initial state.
"""

from __future__ import annotations

from typing import Any, override

from homeassistant.components.device_tracker import TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import ATTR_AREA_ID, ATTR_LAST_CAMERA, ATTR_LAST_LOCATION, ATTR_LAST_SEEN, DOMAIN
from .models import Person, PersonTrackerData


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Create one tracker entity per known person."""
    data: PersonTrackerData = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        PersonTrackerEntity(entry, person) for person in data.people.values()
    )


class PersonTrackerEntity(TrackerEntity):
    """A device tracker for one known Person."""

    _attr_should_poll = False
    _attr_name = None
    # BaseTrackerEntity annotates `_attr_device_info` as None, but platform
    # classes are expected to override it (see Entity.device_info).
    _attr_device_info: DeviceInfo | None  # pyright: ignore[reportIncompatibleVariableOverride]

    def __init__(self, entry: ConfigEntry, person: Person) -> None:
        """Initialize the entity for *person* from config entry *entry*."""
        super().__init__()
        self._person = person
        self._attr_unique_id = f"{entry.entry_id}:{person.slug}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}-{person.slug}")},
            name=person.name,
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def state(self) -> str:
        """The display name of the last seen HA area, or unknown."""
        return self._person.location or STATE_UNKNOWN

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:  # pyright: ignore[reportIncompatibleVariableOverride]
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
