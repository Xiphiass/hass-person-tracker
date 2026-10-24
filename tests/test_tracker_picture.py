"""Tests for the tracker's entity picture.

The camera tracker mirrors the picture of the HA person that tracks it, so a
dashboard can show the person's profile picture on the tracker.
"""

from __future__ import annotations

from homeassistant.const import ATTR_ENTITY_PICTURE
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from .conftest import person, setup_entry
from .test_state_lifetime import _tracker_attributes, _tracker_entity


async def test_tracker_mirrors_linked_person_picture(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """A real HA person linking the tracker lends it their picture."""
    async with setup_entry(hass, people=[person("Alice")]):
        tracker = _tracker_entity(hass, "Alice")
        assert ATTR_ENTITY_PICTURE not in _tracker_attributes(hass, "Alice")

        assert await async_setup_component(
            hass,
            "person",
            {
                "person": [
                    {
                        "id": "alice",
                        "name": "Alice",
                        "device_trackers": [tracker],
                        "picture": "/local/alice.jpg",
                    }
                ]
            },
        )
        await hass.async_block_till_done()

        attrs = _tracker_attributes(hass, "Alice")
        assert attrs[ATTR_ENTITY_PICTURE] == "/local/alice.jpg"


async def test_tracker_picture_follows_person_changes(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """Picture changes and unlinking are reflected; unrelated people are not."""
    async with setup_entry(hass, people=[person("Alice")]):
        tracker = _tracker_entity(hass, "Alice")

        # A person who does not track us lends nothing.
        hass.states.async_set(
            "person.bob",
            "home",
            {"device_trackers": ["device_tracker.bob_phone"], "entity_picture": "/b"},
        )
        await hass.async_block_till_done()
        assert ATTR_ENTITY_PICTURE not in _tracker_attributes(hass, "Alice")

        hass.states.async_set(
            "person.alice",
            "home",
            {"device_trackers": [tracker], "entity_picture": "/a1"},
        )
        await hass.async_block_till_done()
        assert _tracker_attributes(hass, "Alice")[ATTR_ENTITY_PICTURE] == "/a1"

        hass.states.async_set(
            "person.alice",
            "home",
            {"device_trackers": [tracker], "entity_picture": "/a2"},
        )
        await hass.async_block_till_done()
        assert _tracker_attributes(hass, "Alice")[ATTR_ENTITY_PICTURE] == "/a2"

        # Unlinked: back to the default icon.
        hass.states.async_set(
            "person.alice",
            "home",
            {"device_trackers": [], "entity_picture": "/a2"},
        )
        await hass.async_block_till_done()
        assert ATTR_ENTITY_PICTURE not in _tracker_attributes(hass, "Alice")
