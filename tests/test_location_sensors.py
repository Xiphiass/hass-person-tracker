"""Tests for the per-person area and floor sensors.

Each sensor mirrors the tracker's ``area`` / ``floor`` attribute as its
state, so the location shows in the history: the room while a source places
the person at home, unknown otherwise.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.person_tracker import const

from .conftest import add_camera, person, setup_entry
from .test_state_lifetime import _sight, _subentry_id, _tracker_entity

DOMAIN = const.DOMAIN
AREA_ENTITY = "sensor.alice_ble_area"


def _sensor_entity(hass: HomeAssistant, name: str, attribute: str) -> str:
    """The area or floor sensor entity ID of the person titled *name*."""
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{_subentry_id(entry, name)}_{attribute}"
    )
    assert entity_id is not None
    return entity_id


def _sensor_state(hass: HomeAssistant, name: str, attribute: str) -> str:
    """The state of the area or floor sensor of *name*."""
    state = hass.states.get(_sensor_entity(hass, name, attribute))
    assert state is not None
    return state.state


def _garden_on_ground_floor(hass: HomeAssistant) -> ar.AreaEntry:
    """The Garden area on the Ground Floor."""
    floor = fr.async_get(hass).async_create("Ground Floor")
    garden = ar.async_get(hass).async_get_or_create("Garden")
    return ar.async_get(hass).async_update(garden.id, floor_id=floor.floor_id)


async def test_sensors_exist_on_the_person_device(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """Both sensors are named after the person and start unknown."""
    add_camera(hass, "camera.front_door", area_name="Garden")

    async with setup_entry(hass, people=[person("Alice")]) as entry:
        await hass.async_block_till_done()

        registry = er.async_get(hass)
        tracker = registry.async_get(_tracker_entity(hass, "Alice"))
        assert tracker is not None
        for attribute in (const.ATTR_AREA, const.ATTR_FLOOR):
            entity_id = _sensor_entity(hass, "Alice", attribute)
            sensor = registry.async_get(entity_id)
            assert sensor is not None
            assert sensor.device_id == tracker.device_id
            assert sensor.config_subentry_id == _subentry_id(entry, "Alice")
            assert _sensor_state(hass, "Alice", attribute) == "unknown"

        assert _sensor_entity(hass, "Alice", const.ATTR_AREA) == (
            "sensor.alice_current_area"
        )
        assert _sensor_entity(hass, "Alice", const.ATTR_FLOOR) == (
            "sensor.alice_current_floor"
        )


async def test_sensors_follow_a_sighting_and_its_expiry(
    hass: HomeAssistant, default_ai_task: None, freezer
) -> None:
    """A sighting sets area and floor; TTL expiry makes them unknown."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    garden = _garden_on_ground_floor(hass)

    async with setup_entry(hass, people=[person("Alice")]) as entry:
        seen = dt_util.now()
        _sight(hass, entry, "Alice", seen, garden.id)
        await hass.async_block_till_done()
        assert _sensor_state(hass, "Alice", const.ATTR_AREA) == "Garden"
        assert _sensor_state(hass, "Alice", const.ATTR_FLOOR) == "Ground Floor"

        freezer.move_to(seen + timedelta(minutes=31))
        async_fire_time_changed(hass, dt_util.now())
        await hass.async_block_till_done()
        assert _sensor_state(hass, "Alice", const.ATTR_AREA) == "unknown"
        assert _sensor_state(hass, "Alice", const.ATTR_FLOOR) == "unknown"


async def test_area_without_floor_leaves_the_floor_unknown(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """An area not on a floor sets only the area sensor."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    garden = ar.async_get(hass).async_get_or_create("Garden")

    async with setup_entry(hass, people=[person("Alice")]) as entry:
        _sight(hass, entry, "Alice", dt_util.now(), garden.id)
        await hass.async_block_till_done()
        assert _sensor_state(hass, "Alice", const.ATTR_AREA) == "Garden"
        assert _sensor_state(hass, "Alice", const.ATTR_FLOOR) == "unknown"


async def test_sensors_follow_renames(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """Renaming the area or its floor updates the sensors without a reload."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    garden = _garden_on_ground_floor(hass)

    async with setup_entry(hass, people=[person("Alice")]) as entry:
        _sight(hass, entry, "Alice", dt_util.now(), garden.id)
        await hass.async_block_till_done()

        ar.async_get(hass).async_update(garden.id, name="Backyard")
        assert garden.floor_id is not None
        fr.async_get(hass).async_update(garden.floor_id, name="Downstairs")
        await hass.async_block_till_done()
        assert _sensor_state(hass, "Alice", const.ATTR_AREA) == "Backyard"
        assert _sensor_state(hass, "Alice", const.ATTR_FLOOR) == "Downstairs"


async def test_sensors_follow_the_supporting_tracker(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """While the supporting tracker leads, the sensors show its location."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    floor = fr.async_get(hass).async_create("Upstairs")
    bedroom = ar.async_get(hass).async_get_or_create("Bedroom")
    ar.async_get(hass).async_update(bedroom.id, floor_id=floor.floor_id)
    hass.states.async_set(AREA_ENTITY, "unknown")

    async with setup_entry(
        hass, people=[person("Alice", supporting_area_entity=AREA_ENTITY)]
    ):
        hass.states.async_set(AREA_ENTITY, "Bedroom", {"area_id": bedroom.id})
        await hass.async_block_till_done()
        assert _sensor_state(hass, "Alice", const.ATTR_AREA) == "Bedroom"
        assert _sensor_state(hass, "Alice", const.ATTR_FLOOR) == "Upstairs"


async def test_sensors_follow_a_renamed_tracker_entity(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """Changing the tracker's entity ID does not detach the sensors."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    garden = _garden_on_ground_floor(hass)

    async with setup_entry(hass, people=[person("Alice")]) as entry:
        await hass.async_block_till_done()
        er.async_get(hass).async_update_entity(
            _tracker_entity(hass, "Alice"), new_entity_id="device_tracker.alice_cam"
        )
        await hass.async_block_till_done()

        _sight(hass, entry, "Alice", dt_util.now(), garden.id)
        await hass.async_block_till_done()
        assert _sensor_state(hass, "Alice", const.ATTR_AREA) == "Garden"
