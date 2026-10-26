"""Tests for the Supporting tracker attributes on the tracker (Issue #29).

The supporting area and floor entities are resolved live to HA areas and
floors (ADR 0008) and reported as attributes, with ``supporting_agrees``
comparing them to the current camera sighting. How the supporting tracker
changes the tracker's state is covered in ``test_supporting_fallback``.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import floor_registry as fr
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.person_tracker.const import (
    ATTR_SUPPORTING_AGREES,
    ATTR_SUPPORTING_AREA,
    ATTR_SUPPORTING_AREA_ID,
    ATTR_SUPPORTING_FLOOR,
    ATTR_SUPPORTING_FLOOR_ID,
)

from .conftest import add_camera, person, setup_entry
from .test_state_lifetime import _sight, _tracker_attributes, _tracker_state

AREA_ENTITY = "sensor.alice_area"
FLOOR_ENTITY = "sensor.alice_floor"
SUPPORTING_ATTRIBUTES = (
    ATTR_SUPPORTING_AREA,
    ATTR_SUPPORTING_AREA_ID,
    ATTR_SUPPORTING_FLOOR,
    ATTR_SUPPORTING_FLOOR_ID,
    ATTR_SUPPORTING_AGREES,
)


def _house(hass: HomeAssistant) -> dict[str, ar.AreaEntry]:
    """Garden and Kitchen on the ground floor, Bedroom upstairs."""
    floors = fr.async_get(hass)
    ground = floors.async_create("Ground Floor", aliases={"EG"})
    upstairs = floors.async_create("Upstairs")
    areas = ar.async_get(hass)
    add_camera(hass, "camera.front_door", area_name="Garden")
    result = {}
    for name, floor in (
        ("Garden", ground),
        ("Kitchen", ground),
        ("Bedroom", upstairs),
    ):
        area = areas.async_get_or_create(name)
        result[name] = areas.async_update(area.id, floor_id=floor.floor_id)
    areas.async_update(result["Kitchen"].id, aliases={"Küche"})
    return result


async def test_no_supporting_tracker_no_attributes(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """Without a supporting tracker none of its attributes appear."""
    areas = _house(hass)
    async with setup_entry(hass, people=[person("Alice")]) as entry:
        _sight(hass, entry, "Alice", dt_util.now(), areas["Garden"].id)
        await hass.async_block_till_done()
        attrs = _tracker_attributes(hass, "Alice")
        for attribute in SUPPORTING_ATTRIBUTES:
            assert attribute not in attrs


async def test_supporting_area_with_its_floor(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """The area entity resolves by alias; the floor is that area's floor."""
    areas = _house(hass)
    hass.states.async_set(AREA_ENTITY, "küche")
    async with setup_entry(
        hass, people=[person("Alice", supporting_area_entity=AREA_ENTITY)]
    ) as entry:
        _sight(hass, entry, "Alice", dt_util.now(), areas["Garden"].id)
        await hass.async_block_till_done()

        attrs = _tracker_attributes(hass, "Alice")
        assert attrs[ATTR_SUPPORTING_AREA] == "Kitchen"
        assert attrs[ATTR_SUPPORTING_AREA_ID] == areas["Kitchen"].id
        assert attrs[ATTR_SUPPORTING_FLOOR] == "Ground Floor"
        assert attrs[ATTR_SUPPORTING_FLOOR_ID] == areas["Kitchen"].floor_id
        assert attrs[ATTR_SUPPORTING_AGREES] == "floor"


async def test_floor_entity_overrides_area_floor(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """A configured floor entity names the supporting floor."""
    areas = _house(hass)
    hass.states.async_set(AREA_ENTITY, "Kitchen")
    hass.states.async_set(FLOOR_ENTITY, "Upstairs")
    async with setup_entry(
        hass,
        people=[
            person(
                "Alice",
                supporting_area_entity=AREA_ENTITY,
                supporting_floor_entity=FLOOR_ENTITY,
            )
        ],
    ):
        await hass.async_block_till_done()
        attrs = _tracker_attributes(hass, "Alice")
        assert attrs[ATTR_SUPPORTING_AREA] == "Kitchen"
        assert attrs[ATTR_SUPPORTING_FLOOR] == "Upstairs"
        assert attrs[ATTR_SUPPORTING_FLOOR_ID] == areas["Bedroom"].floor_id


async def test_floor_entity_alone(hass: HomeAssistant, default_ai_task: None) -> None:
    """A floor entity alone gives a supporting floor and floor agreement."""
    areas = _house(hass)
    hass.states.async_set(FLOOR_ENTITY, "unknown", {"floor_id": "ground_floor"})
    async with setup_entry(
        hass, people=[person("Alice", supporting_floor_entity=FLOOR_ENTITY)]
    ) as entry:
        _sight(hass, entry, "Alice", dt_util.now(), areas["Garden"].id)
        await hass.async_block_till_done()
        attrs = _tracker_attributes(hass, "Alice")
        # "unknown" is no report, whatever the attribute says.
        assert ATTR_SUPPORTING_FLOOR not in attrs
        assert ATTR_SUPPORTING_AGREES not in attrs

        hass.states.async_set(FLOOR_ENTITY, "EG", {"floor_id": "ground_floor"})
        await hass.async_block_till_done()
        attrs = _tracker_attributes(hass, "Alice")
        assert ATTR_SUPPORTING_AREA not in attrs
        assert attrs[ATTR_SUPPORTING_FLOOR] == "Ground Floor"
        assert attrs[ATTR_SUPPORTING_AGREES] == "floor"


async def test_supporting_changes_republish(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """Supporting entity changes re-publish the supporting attributes."""
    areas = _house(hass)
    hass.states.async_set(AREA_ENTITY, "not_home")
    async with setup_entry(
        hass, people=[person("Alice", supporting_area_entity=AREA_ENTITY)]
    ) as entry:
        await hass.async_block_till_done()
        assert _tracker_state(hass, "Alice") == "unknown"
        assert ATTR_SUPPORTING_AREA not in _tracker_attributes(hass, "Alice")

        # Without a camera sighting there is nothing to agree with.
        hass.states.async_set(AREA_ENTITY, "Bedroom", {"area_id": areas["Bedroom"].id})
        await hass.async_block_till_done()
        attrs = _tracker_attributes(hass, "Alice")
        assert attrs[ATTR_SUPPORTING_AREA] == "Bedroom"
        assert ATTR_SUPPORTING_AGREES not in attrs

        _sight(hass, entry, "Alice", dt_util.now(), areas["Garden"].id)
        await hass.async_block_till_done()
        assert _tracker_state(hass, "Alice") == "home"
        assert _tracker_attributes(hass, "Alice")[ATTR_SUPPORTING_AGREES] == "none"

        hass.states.async_set(AREA_ENTITY, "garden")
        await hass.async_block_till_done()
        assert _tracker_attributes(hass, "Alice")[ATTR_SUPPORTING_AGREES] == "area"

        # Unresolvable again: the attributes go, the camera sighting stays.
        hass.states.async_set(AREA_ENTITY, "unavailable")
        await hass.async_block_till_done()
        attrs = _tracker_attributes(hass, "Alice")
        assert _tracker_state(hass, "Alice") == "home"
        for attribute in SUPPORTING_ATTRIBUTES:
            assert attribute not in attrs


async def test_expired_sighting_drops_agreement_only(
    hass: HomeAssistant, default_ai_task: None, freezer
) -> None:
    """After the TTL the supporting location stays, the agreement goes."""
    areas = _house(hass)
    hass.states.async_set(AREA_ENTITY, "Garden")
    async with setup_entry(
        hass, people=[person("Alice", supporting_area_entity=AREA_ENTITY)]
    ) as entry:
        seen = dt_util.now()
        _sight(hass, entry, "Alice", seen, areas["Garden"].id)
        await hass.async_block_till_done()
        assert _tracker_attributes(hass, "Alice")[ATTR_SUPPORTING_AGREES] == "area"

        freezer.move_to(seen + timedelta(minutes=31))
        async_fire_time_changed(hass, dt_util.now())
        await hass.async_block_till_done()
        attrs = _tracker_attributes(hass, "Alice")
        assert attrs[ATTR_SUPPORTING_AREA] == "Garden"
        assert ATTR_SUPPORTING_AGREES not in attrs


async def test_supporting_area_rename_republishes(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """Renaming the supporting area shows without a reload."""
    areas = _house(hass)
    hass.states.async_set(AREA_ENTITY, "Bedroom", {"area_id": areas["Bedroom"].id})
    async with setup_entry(
        hass, people=[person("Alice", supporting_area_entity=AREA_ENTITY)]
    ):
        await hass.async_block_till_done()
        ar.async_get(hass).async_update(areas["Bedroom"].id, name="Master Bedroom")
        await hass.async_block_till_done()
        attrs = _tracker_attributes(hass, "Alice")
        assert attrs[ATTR_SUPPORTING_AREA] == "Master Bedroom"
