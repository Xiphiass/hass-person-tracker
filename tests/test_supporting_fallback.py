"""Tests for the Supporting tracker fallback (Issue #30 / ADR 0008).

Once the camera sighting is no longer current, a supporting tracker that
resolves to an area keeps the tracker ``home`` with that area, and the
``source`` attribute names which source leads. The decision rule in
``lifetime.leading_source`` is covered by plain unit tests; the harness tests
cover the tracker's transitions.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.person_tracker.const import (
    ATTR_AREA,
    ATTR_AREA_ID,
    ATTR_FLOOR,
    ATTR_LAST_CAMERA,
    ATTR_LAST_FLOOR,
    ATTR_LAST_LOCATION,
    ATTR_LAST_SEEN,
    ATTR_SOURCE,
)
from custom_components.person_tracker.lifetime import (
    SOURCE_CAMERA,
    SOURCE_SUPPORTING,
    leading_source,
)

from .conftest import person, setup_entry
from .test_state_lifetime import (
    _sight,
    _subentry_id,
    _tracker_attributes,
    _tracker_state,
)
from .test_supporting_attributes import AREA_ENTITY, FLOOR_ENTITY, _house

AUTHORITATIVE = "device_tracker.alice_phone"


def _person_of(entry: Any, name: str) -> Any:
    """The runtime Person of the subentry titled *name*."""
    return entry.runtime_data.people[_subentry_id(entry, name)]


class TestLeadingSource:
    """Unit tests for lifetime.leading_source."""

    def test_current_camera_sighting_leads(self) -> None:
        assert leading_source(False, True, True) == SOURCE_CAMERA
        assert leading_source(False, True, False) == SOURCE_CAMERA

    def test_supporting_area_leads_once_the_sighting_is_not_current(self) -> None:
        assert leading_source(False, False, True) == SOURCE_SUPPORTING

    def test_no_source_without_sighting_or_supporting_area(self) -> None:
        assert leading_source(False, False, False) is None

    def test_authoritative_away_outranks_everything(self) -> None:
        assert leading_source(True, True, True) is None
        assert leading_source(True, False, True) is None


async def test_supporting_area_leads_after_the_sighting_expires(
    hass: HomeAssistant, default_ai_task: None, freezer
) -> None:
    """Expired sighting + supporting area: home in the supporting area."""
    areas = _house(hass)
    hass.states.async_set(AREA_ENTITY, "Bedroom")
    async with setup_entry(
        hass, people=[person("Alice", supporting_area_entity=AREA_ENTITY)]
    ) as entry:
        seen = dt_util.now()
        _sight(hass, entry, "Alice", seen, areas["Garden"].id)
        _person_of(entry, "Alice").last_camera = "camera.front_door"
        await hass.async_block_till_done()
        attrs = _tracker_attributes(hass, "Alice")
        assert _tracker_state(hass, "Alice") == "home"
        assert attrs[ATTR_SOURCE] == "camera"
        assert attrs[ATTR_AREA] == "Garden"
        assert attrs[ATTR_FLOOR] == "Ground Floor"

        freezer.move_to(seen + timedelta(minutes=31))
        async_fire_time_changed(hass, dt_util.now())
        await hass.async_block_till_done()
        attrs = _tracker_attributes(hass, "Alice")
        assert _tracker_state(hass, "Alice") == "home"
        assert attrs["in_zones"] == ["zone.home"]
        assert attrs[ATTR_SOURCE] == AREA_ENTITY
        assert attrs[ATTR_AREA] == "Bedroom"
        assert attrs[ATTR_FLOOR] == "Upstairs"
        # The last_* attributes keep describing the last camera sighting.
        assert attrs[ATTR_LAST_LOCATION] == "Garden"
        assert attrs[ATTR_LAST_FLOOR] == "Ground Floor"
        assert attrs[ATTR_AREA_ID] == areas["Garden"].id
        assert attrs[ATTR_LAST_SEEN] == seen.isoformat()
        assert attrs[ATTR_LAST_CAMERA] == "camera.front_door"


async def test_never_seen_person_is_home_on_the_supporting_area(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """Without any camera sighting the supporting area still gives presence."""
    areas = _house(hass)
    hass.states.async_set(AREA_ENTITY, "unknown")
    async with setup_entry(
        hass, people=[person("Alice", supporting_area_entity=AREA_ENTITY)]
    ):
        await hass.async_block_till_done()
        attrs = _tracker_attributes(hass, "Alice")
        assert _tracker_state(hass, "Alice") == "unknown"
        assert ATTR_SOURCE not in attrs

        hass.states.async_set(AREA_ENTITY, "Kitchen", {"area_id": areas["Kitchen"].id})
        await hass.async_block_till_done()
        attrs = _tracker_attributes(hass, "Alice")
        assert _tracker_state(hass, "Alice") == "home"
        assert attrs[ATTR_SOURCE] == AREA_ENTITY
        assert attrs[ATTR_AREA] == "Kitchen"
        assert attrs[ATTR_FLOOR] == "Ground Floor"
        assert ATTR_LAST_LOCATION not in attrs
        assert ATTR_LAST_SEEN not in attrs


async def test_supporting_area_lost_during_fallback_is_unknown(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """The supporting area becoming unavailable ends the fallback."""
    _house(hass)
    hass.states.async_set(AREA_ENTITY, "Bedroom")
    async with setup_entry(
        hass, people=[person("Alice", supporting_area_entity=AREA_ENTITY)]
    ):
        await hass.async_block_till_done()
        assert _tracker_state(hass, "Alice") == "home"

        hass.states.async_set(AREA_ENTITY, "unavailable")
        await hass.async_block_till_done()
        attrs = _tracker_attributes(hass, "Alice")
        assert _tracker_state(hass, "Alice") == "unknown"
        for attribute in (ATTR_SOURCE, ATTR_AREA, ATTR_FLOOR):
            assert attribute not in attrs


async def test_authoritative_away_outranks_the_supporting_tracker(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """Phone away, BLE tag left at home: unknown."""
    areas = _house(hass)
    hass.states.async_set(AREA_ENTITY, "Bedroom")
    hass.states.async_set(AUTHORITATIVE, "home")
    async with setup_entry(
        hass,
        people=[
            person(
                "Alice",
                supporting_area_entity=AREA_ENTITY,
                authoritative_tracker=AUTHORITATIVE,
            )
        ],
    ) as entry:
        _sight(hass, entry, "Alice", dt_util.now(), areas["Garden"].id)
        await hass.async_block_till_done()
        assert _tracker_attributes(hass, "Alice")[ATTR_SOURCE] == "camera"

        hass.states.async_set(AUTHORITATIVE, "not_home")
        await hass.async_block_till_done()
        attrs = _tracker_attributes(hass, "Alice")
        assert _tracker_state(hass, "Alice") == "unknown"
        for attribute in (ATTR_SOURCE, ATTR_AREA, ATTR_FLOOR):
            assert attribute not in attrs
        # The supporting location is still reported, just not believed.
        assert attrs["supporting_area"] == "Bedroom"


async def test_floor_only_supporting_tracker_gives_no_presence(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """A supporting floor without an area is too coarse for presence."""
    _house(hass)
    hass.states.async_set(FLOOR_ENTITY, "Upstairs")
    async with setup_entry(
        hass, people=[person("Alice", supporting_floor_entity=FLOOR_ENTITY)]
    ):
        await hass.async_block_till_done()
        attrs = _tracker_attributes(hass, "Alice")
        assert _tracker_state(hass, "Alice") == "unknown"
        assert ATTR_SOURCE not in attrs
        assert ATTR_FLOOR not in attrs
        assert attrs["supporting_floor"] == "Upstairs"
