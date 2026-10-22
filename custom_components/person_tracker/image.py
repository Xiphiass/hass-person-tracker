"""Image platform for the person_tracker integration.

One image entity per known person showing their last detection: the
snapshot cropped tight around them (Issue #3). It sits on the person's
device next to their tracker; its state is the time of that detection, so
a new thumbnail can drive automations.
"""

from __future__ import annotations

from collections.abc import Callable
from mimetypes import guess_type

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import SIGNAL_TRACKER_UPDATE_PERSON
from .models import Person, PersonTrackerConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PersonTrackerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one last-detection image entity per known person."""
    for person in entry.runtime_data.people.values():
        async_add_entities(
            [PersonLastDetectionImage(hass, person)],
            config_subentry_id=person.subentry_id,
        )


class PersonLastDetectionImage(ImageEntity):
    """The thumbnail of one known Person's last detection."""

    _attr_has_entity_name = True
    _attr_translation_key = "last_detection"

    def __init__(self, hass: HomeAssistant, person: Person) -> None:
        """Initialize the entity for *person*."""
        super().__init__(hass)
        self._person = person
        # Keyed by the subentry ID so renaming the person keeps the entity.
        self._attr_unique_id = f"{person.subentry_id}_last_detection"
        self._attr_device_info = person.device_info
        self._unsub_update: Callable[[], None] | None = None
        self._sync_from_person()

    async def async_added_to_hass(self) -> None:
        """Follow the person's detections."""
        self._unsub_update = async_dispatcher_connect(
            self.hass,
            f"{SIGNAL_TRACKER_UPDATE_PERSON}{self._person.subentry_id}",
            self._async_handle_update,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Stop following the person's detections."""
        if self._unsub_update is not None:
            self._unsub_update()
            self._unsub_update = None

    def _sync_from_person(self) -> None:
        """Take over the person's current thumbnail and its timestamp."""
        path = self._person.last_thumbnail
        self._attr_image_last_updated = self._person.last_thumbnail_updated
        if path is not None:
            self._attr_content_type = guess_type(path.name)[0] or "image/jpeg"

    @callback
    def _async_handle_update(self) -> None:
        """A detection updated the person; publish a new thumbnail, if any."""
        if self._person.last_thumbnail_updated == self._attr_image_last_updated:
            # The thumbnail write failed; the previous image still stands.
            return
        self._sync_from_person()
        self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        """The saved thumbnail, or None before the first detection."""
        path = self._person.last_thumbnail
        if path is None:
            return None
        try:
            return await self.hass.async_add_executor_job(path.read_bytes)
        except OSError:
            return None
