"""Tests for the Supporting tracker overriding a current sighting (Issue #31).

ADR 0008: the camera sighting stops leading early when the supporting area
changed after the last sighting, differs from the camera's area, and has
held for a settle period. The decision rule in ``lifetime.supporting_moved``
is covered by plain unit tests with explicit timestamps; the harness tests
cover the tracker's transitions and the settle wake-up.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.person_tracker.const import (
    ATTR_AREA,
    ATTR_FLOOR,
    ATTR_LAST_LOCATION,
    ATTR_SOURCE,
)
from custom_components.person_tracker.lifetime import (
    SOURCE_CAMERA,
    SOURCE_SUPPORTING,
    leading_source,
    supporting_moved,
)

from .conftest import person, setup_entry
from .test_state_lifetime import _sight, _tracker_attributes, _tracker_state
from .test_supporting_attributes import AREA_ENTITY, _house

SEEN = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
SETTLE = timedelta(seconds=120)


def _moved(
    *,
    changed: datetime | None = SEEN + timedelta(minutes=1),
    now: datetime = SEEN + timedelta(minutes=5),
    last_seen: datetime | None = SEEN,
    camera_area: str | None = "garden",
    supporting_area: str | None = "bedroom",
) -> bool:
    return supporting_moved(
        last_seen=last_seen,
        camera_area_id=camera_area,
        supporting_area_id=supporting_area,
        supporting_changed=changed,
        settle=SETTLE,
        now=now,
    )


class TestSupportingMoved:
    """Unit tests for lifetime.supporting_moved."""

    def test_settled_move_after_the_sighting(self) -> None:
        assert _moved() is True

    def test_settles_exactly_at_the_settle_period(self) -> None:
        changed = SEEN + timedelta(minutes=1)
        assert _moved(changed=changed, now=changed + SETTLE) is True
        assert (
            _moved(changed=changed, now=changed + SETTLE - timedelta(seconds=1))
            is False
        )

    def test_change_back_to_the_camera_area_is_no_move(self) -> None:
        assert _moved(supporting_area="garden") is False

    def test_disagreeing_since_before_the_sighting_is_no_move(self) -> None:
        assert _moved(changed=SEEN - timedelta(minutes=5)) is False
        assert _moved(changed=SEEN) is False

    def test_newer_sighting_ends_the_move(self) -> None:
        assert _moved(last_seen=SEEN + timedelta(minutes=4)) is False

    def test_no_move_without_a_sighting_or_supporting_area(self) -> None:
        assert _moved(last_seen=None) is False
        assert _moved(supporting_area=None) is False
        assert _moved(changed=None) is False


def test_settled_move_outranks_a_current_sighting() -> None:
    assert (
        leading_source(
            authoritative_away=False,
            camera_current=True,
            has_supporting_area=True,
            supporting_moved=True,
        )
        == SOURCE_SUPPORTING
    )
    assert (
        leading_source(
            authoritative_away=True,
            camera_current=True,
            has_supporting_area=True,
            supporting_moved=True,
        )
        is None
    )


async def _at(hass: HomeAssistant, freezer, when: datetime) -> None:
    """Move the clock to *when* and fire any timers due by then."""
    freezer.move_to(when)
    async_fire_time_changed(hass, when)
    await hass.async_block_till_done()


async def test_settled_move_overrides_the_current_sighting(
    hass: HomeAssistant, default_ai_task: None, freezer
) -> None:
    """Seen in the garden, BLE moves to the bedroom and holds: bedroom."""
    areas = _house(hass)
    hass.states.async_set(AREA_ENTITY, "Garden")
    async with setup_entry(
        hass, people=[person("Alice", supporting_area_entity=AREA_ENTITY)]
    ) as entry:
        seen = dt_util.now()
        _sight(hass, entry, "Alice", seen, areas["Garden"].id)
        await hass.async_block_till_done()

        await _at(hass, freezer, seen + timedelta(minutes=1))
        hass.states.async_set(AREA_ENTITY, "Bedroom")
        await hass.async_block_till_done()
        assert _tracker_attributes(hass, "Alice")[ATTR_SOURCE] == SOURCE_CAMERA

        await _at(hass, freezer, seen + timedelta(minutes=2, seconds=59))
        assert _tracker_attributes(hass, "Alice")[ATTR_SOURCE] == SOURCE_CAMERA

        # The settle wake-up alone re-publishes; no other event arrives.
        await _at(hass, freezer, seen + timedelta(minutes=3))
        attrs = _tracker_attributes(hass, "Alice")
        assert _tracker_state(hass, "Alice") == "home"
        assert attrs[ATTR_SOURCE] == AREA_ENTITY
        assert attrs[ATTR_AREA] == "Bedroom"
        assert attrs[ATTR_FLOOR] == "Upstairs"
        assert attrs[ATTR_LAST_LOCATION] == "Garden"

        # A newer camera sighting puts the camera back in the lead.
        _sight(hass, entry, "Alice", dt_util.now(), areas["Garden"].id)
        await hass.async_block_till_done()
        attrs = _tracker_attributes(hass, "Alice")
        assert attrs[ATTR_SOURCE] == SOURCE_CAMERA
        assert attrs[ATTR_AREA] == "Garden"


async def test_flap_back_within_the_settle_period_keeps_the_camera(
    hass: HomeAssistant, default_ai_task: None, freezer
) -> None:
    """BLE briefly reports the bedroom, then the garden again: camera leads."""
    areas = _house(hass)
    hass.states.async_set(AREA_ENTITY, "Garden")
    async with setup_entry(
        hass, people=[person("Alice", supporting_area_entity=AREA_ENTITY)]
    ) as entry:
        seen = dt_util.now()
        _sight(hass, entry, "Alice", seen, areas["Garden"].id)
        await _at(hass, freezer, seen + timedelta(minutes=1))
        hass.states.async_set(AREA_ENTITY, "Bedroom")
        await _at(hass, freezer, seen + timedelta(minutes=2))
        hass.states.async_set(AREA_ENTITY, "Garden")
        await _at(hass, freezer, seen + timedelta(minutes=10))
        attrs = _tracker_attributes(hass, "Alice")
        assert attrs[ATTR_SOURCE] == SOURCE_CAMERA
        assert attrs[ATTR_AREA] == "Garden"


async def test_further_change_restarts_the_settle_period(
    hass: HomeAssistant, default_ai_task: None, freezer
) -> None:
    """Bedroom, then Kitchen a minute later: Kitchen leads two minutes on."""
    areas = _house(hass)
    hass.states.async_set(AREA_ENTITY, "Garden")
    async with setup_entry(
        hass, people=[person("Alice", supporting_area_entity=AREA_ENTITY)]
    ) as entry:
        seen = dt_util.now()
        _sight(hass, entry, "Alice", seen, areas["Garden"].id)
        await _at(hass, freezer, seen + timedelta(minutes=1))
        hass.states.async_set(AREA_ENTITY, "Bedroom")
        await _at(hass, freezer, seen + timedelta(minutes=2))
        hass.states.async_set(AREA_ENTITY, "Kitchen")
        await _at(hass, freezer, seen + timedelta(minutes=3, seconds=30))
        assert _tracker_attributes(hass, "Alice")[ATTR_SOURCE] == SOURCE_CAMERA

        await _at(hass, freezer, seen + timedelta(minutes=4))
        attrs = _tracker_attributes(hass, "Alice")
        assert attrs[ATTR_SOURCE] == AREA_ENTITY
        assert attrs[ATTR_AREA] == "Kitchen"


async def test_supporting_disagreeing_before_the_sighting_is_overruled(
    hass: HomeAssistant, default_ai_task: None, freezer
) -> None:
    """BLE says bedroom since before the camera sighting in the garden."""
    areas = _house(hass)
    hass.states.async_set(AREA_ENTITY, "Bedroom")
    async with setup_entry(
        hass, people=[person("Alice", supporting_area_entity=AREA_ENTITY)]
    ) as entry:
        await _at(hass, freezer, dt_util.now() + timedelta(minutes=5))
        seen = dt_util.now()
        _sight(hass, entry, "Alice", seen, areas["Garden"].id)
        await _at(hass, freezer, seen + timedelta(minutes=10))
        attrs = _tracker_attributes(hass, "Alice")
        assert attrs[ATTR_SOURCE] == SOURCE_CAMERA
        assert attrs[ATTR_AREA] == "Garden"
