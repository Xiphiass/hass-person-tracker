"""Tests for the tracker's floor attributes (Issue #28).

The floor of the person's area is looked up live from the area and floor
registries when the state is published (like area names, ADR 0003): ``floor``
only while the sighting is current, ``last_floor``/``floor_id`` kept after it
goes stale. Floor renames and moving the area to another floor re-publish
without a reload.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import floor_registry as fr
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.person_tracker.const import (
    ATTR_AREA_ID,
    ATTR_FLOOR,
    ATTR_FLOOR_ID,
    ATTR_LAST_FLOOR,
)

from .conftest import add_camera, person, setup_entry
from .test_state_lifetime import _sight, _tracker_attributes, _tracker_state


def _garden_on_floor(hass: HomeAssistant, floor_name: str | None) -> ar.AreaEntry:
    """The Garden area, assigned to a floor named *floor_name* (if any)."""
    floor_id = None
    if floor_name is not None:
        floor_id = fr.async_get(hass).async_create(floor_name).floor_id
    garden = ar.async_get(hass).async_get_or_create("Garden")
    return ar.async_get(hass).async_update(garden.id, floor_id=floor_id)


async def test_floor_attributes_while_home(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """A current sighting exposes floor, last_floor and floor_id."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    garden = _garden_on_floor(hass, "Ground Floor")

    async with setup_entry(hass, people=[person("Alice")]) as entry:
        _sight(hass, entry, "Alice", dt_util.now(), garden.id)
        await hass.async_block_till_done()

        attrs = _tracker_attributes(hass, "Alice")
        assert attrs[ATTR_FLOOR] == "Ground Floor"
        assert attrs[ATTR_LAST_FLOOR] == "Ground Floor"
        assert attrs[ATTR_FLOOR_ID] == garden.floor_id


async def test_floor_dropped_on_ttl_expiry(
    hass: HomeAssistant, default_ai_task: None, freezer
) -> None:
    """On TTL expiry floor goes, last_floor and floor_id stay."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    garden = _garden_on_floor(hass, "Ground Floor")

    async with setup_entry(hass, people=[person("Alice")]) as entry:
        seen = dt_util.now()
        _sight(hass, entry, "Alice", seen, garden.id)
        await hass.async_block_till_done()

        freezer.move_to(seen + timedelta(minutes=31))
        async_fire_time_changed(hass, dt_util.now())
        await hass.async_block_till_done()
        assert _tracker_state(hass, "Alice") == "unknown"

        attrs = _tracker_attributes(hass, "Alice")
        assert ATTR_FLOOR not in attrs
        assert attrs[ATTR_LAST_FLOOR] == "Ground Floor"
        assert attrs[ATTR_FLOOR_ID] == garden.floor_id


async def test_floor_dropped_on_authoritative_demotion(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """Authoritative demotion drops floor, keeps last_floor and floor_id."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    garden = _garden_on_floor(hass, "Ground Floor")

    async with setup_entry(
        hass,
        people=[person("Alice", authoritative_tracker="device_tracker.alice_phone")],
    ) as entry:
        _sight(hass, entry, "Alice", dt_util.now(), garden.id)
        await hass.async_block_till_done()

        hass.states.async_set("device_tracker.alice_phone", "not_home")
        await hass.async_block_till_done()
        assert _tracker_state(hass, "Alice") == "unknown"

        attrs = _tracker_attributes(hass, "Alice")
        assert ATTR_FLOOR not in attrs
        assert attrs[ATTR_LAST_FLOOR] == "Ground Floor"
        assert attrs[ATTR_FLOOR_ID] == garden.floor_id


async def test_area_without_floor_has_no_floor_attributes(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """An area not assigned to a floor exposes no floor attributes."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    garden = _garden_on_floor(hass, None)

    async with setup_entry(hass, people=[person("Alice")]) as entry:
        _sight(hass, entry, "Alice", dt_util.now(), garden.id)
        await hass.async_block_till_done()

        attrs = _tracker_attributes(hass, "Alice")
        assert attrs[ATTR_AREA_ID] == garden.id
        assert ATTR_FLOOR not in attrs
        assert ATTR_LAST_FLOOR not in attrs
        assert ATTR_FLOOR_ID not in attrs


async def test_floor_rename_republishes(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """Renaming the floor updates the attributes without a reload."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    garden = _garden_on_floor(hass, "Ground Floor")

    async with setup_entry(hass, people=[person("Alice")]) as entry:
        _sight(hass, entry, "Alice", dt_util.now(), garden.id)
        await hass.async_block_till_done()

        assert garden.floor_id is not None
        fr.async_get(hass).async_update(garden.floor_id, name="Downstairs")
        await hass.async_block_till_done()

        attrs = _tracker_attributes(hass, "Alice")
        assert attrs[ATTR_FLOOR] == "Downstairs"
        assert attrs[ATTR_LAST_FLOOR] == "Downstairs"


async def test_area_moved_to_another_floor_republishes(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """Assigning the area to a different floor (or none) re-publishes."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    garden = _garden_on_floor(hass, "Ground Floor")
    upstairs = fr.async_get(hass).async_create("Upstairs")

    async with setup_entry(hass, people=[person("Alice")]) as entry:
        _sight(hass, entry, "Alice", dt_util.now(), garden.id)
        await hass.async_block_till_done()

        ar.async_get(hass).async_update(garden.id, floor_id=upstairs.floor_id)
        await hass.async_block_till_done()
        attrs = _tracker_attributes(hass, "Alice")
        assert attrs[ATTR_FLOOR] == "Upstairs"
        assert attrs[ATTR_LAST_FLOOR] == "Upstairs"
        assert attrs[ATTR_FLOOR_ID] == upstairs.floor_id

        ar.async_get(hass).async_update(garden.id, floor_id=None)
        await hass.async_block_till_done()
        attrs = _tracker_attributes(hass, "Alice")
        assert ATTR_FLOOR not in attrs
        assert ATTR_LAST_FLOOR not in attrs
        assert ATTR_FLOOR_ID not in attrs
