"""Tests for tracker state lifetime: TTL expiry and authoritative demotion.

Issue #6 / ADR 0004. The pure decision rules in ``lifetime`` are covered by
plain unit tests; the HA tests use the harness time mocking (``freezer`` +
``async_fire_time_changed``) to exercise the entity's TTL timer and state
re-publication.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.person_tracker import const
from custom_components.person_tracker.const import (
    ATTR_AREA_ID,
    ATTR_LAST_LOCATION,
    ATTR_LAST_SEEN,
    SIGNAL_TRACKER_UPDATE_PERSON,
)
from custom_components.person_tracker.lifetime import (
    authoritative_reports_away,
    is_expired,
)

from .conftest import add_camera, person, setup_entry

DOMAIN = const.DOMAIN
TTL = timedelta(minutes=30)


def _subentry_id(entry: Any, name: str) -> str:
    """The subentry ID of the person subentry titled *name*."""
    return {s.title: s.subentry_id for s in entry.subentries.values()}[name]


def _tracker_entity(hass: HomeAssistant, name: str) -> str:
    """The device_tracker entity ID for the person subentry titled *name*."""
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    subentry_id = _subentry_id(entry, name)
    entity_id = er.async_get(hass).async_get_entity_id(
        "device_tracker", DOMAIN, subentry_id
    )
    assert entity_id is not None
    return entity_id


def _tracker_state(hass: HomeAssistant, name: str) -> str:
    """The current state of the tracker entity of *name*."""
    state = hass.states.get(_tracker_entity(hass, name))
    # If the state is not available yet, it might be None
    if state is None:
        return "unknown"
    return state.state


def _tracker_attributes(hass: HomeAssistant, name: str) -> dict[str, Any]:
    """The current state attributes of the tracker entity of *name*."""
    state = hass.states.get(_tracker_entity(hass, name))
    assert state is not None
    return state.attributes


def _sight(
    hass: HomeAssistant, entry: Any, name: str, when: datetime, area_id: str
) -> None:
    """Simulate the detection pipeline recording a fresh sighting."""
    data = entry.runtime_data
    person_entry = data.people[_subentry_id(entry, name)]
    person_entry.last_seen = when
    person_entry.area_id = area_id
    person_entry.location = "Garden"
    person_entry.last_camera = "camera.front_door"
    async_dispatcher_send(
        hass, f"{SIGNAL_TRACKER_UPDATE_PERSON}{person_entry.subentry_id}"
    )


# --- Plain unit tests of the decision rules (no HA involved) ---------------


class TestIsExpired:
    """Unit tests for lifetime.is_expired."""

    def test_no_sighting_is_expired(self) -> None:
        now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
        assert is_expired(None, TTL, now) is True

    def test_fresh_sighting_is_not_expired(self) -> None:
        now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
        assert is_expired(now - timedelta(minutes=5), TTL, now) is False

    def test_sighting_older_than_ttl_is_expired(self) -> None:
        now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
        assert is_expired(now - TTL - timedelta(seconds=1), TTL, now) is True

    def test_exactly_at_ttl_boundary_is_not_expired(self) -> None:
        now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
        assert is_expired(now - TTL, TTL, now) is False

    def test_zero_ttl_never_expires(self) -> None:
        now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
        assert is_expired(now - timedelta(days=1), timedelta(0), now) is False

    def test_mixed_timezones_compare_by_absolute_time(self) -> None:
        # last_seen in a non-UTC zone, now in UTC: the comparison must use
        # absolute time, not the wall clock.
        utc = timezone.utc
        plus_two = timezone(timedelta(hours=2))
        now = datetime(2026, 10, 21, 12, 0, tzinfo=utc)
        last_seen = datetime(2026, 10, 21, 13, 29, tzinfo=plus_two)  # 11:29 UTC
        # last_seen is 31 mins before now. TTL is 30 mins.
        assert is_expired(last_seen, TTL, now) is True


class TestAuthoritativeReportsAway:
    """Unit tests for lifetime.authoritative_reports_away."""

    def test_no_state_is_not_away(self) -> None:
        assert authoritative_reports_away(None) is False

    def test_unknown_is_not_away(self) -> None:
        assert authoritative_reports_away("unknown") is False

    def test_home_is_not_away(self) -> None:
        assert authoritative_reports_away("home") is False

    def test_not_home_is_away(self) -> None:
        assert authoritative_reports_away("not_home") is True

    def test_named_zone_is_away(self) -> None:
        assert authoritative_reports_away("work") is True


# --- HA tests: TTL expiry and demotion through the entity -------------------


async def test_ttl_expiry_demotes_to_unknown_and_keeps_attributes(
    hass: HomeAssistant, default_ai_task: None, freezer
) -> None:
    """After the TTL elapses the state is unknown, attributes are retained."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    garden = ar.async_get(hass).async_get_or_create("Garden")

    async with setup_entry(hass, people=[person("Alice")]) as entry:
        seen = dt_util.now()
        _sight(hass, entry, "Alice", seen, garden.id)
        await hass.async_block_till_done()
        assert _tracker_state(hass, "Alice") == "Garden"

        # 29 minutes in: re-evaluation still finds the sighting fresh.
        freezer.move_to(seen + timedelta(minutes=29))
        _sight(
            hass,
            entry,
            "Alice",
            seen,
            garden.id,
        )  # same sighting: re-evaluates without moving last_seen
        await hass.async_block_till_done()
        assert _tracker_state(hass, "Alice") == "Garden"

        # 31 minutes in: the TTL timer fires and the state becomes unknown.
        freezer.move_to(seen + timedelta(minutes=31))
        async_fire_time_changed(hass, dt_util.now())
        await hass.async_block_till_done()
        assert _tracker_state(hass, "Alice") == "unknown"

        # ADR 0004: last_location/last_seen are retained so staleness stays
        # queryable.
        attrs = _tracker_attributes(hass, "Alice")
        assert attrs[ATTR_LAST_LOCATION] == "Garden"
        assert attrs[ATTR_LAST_SEEN] == seen.isoformat()
        assert attrs[ATTR_AREA_ID] == garden.id


async def test_new_sighting_resets_the_ttl(
    hass: HomeAssistant, default_ai_task: None, freezer
) -> None:
    """A fresh sighting restarts the TTL clock from its last_seen."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    garden = ar.async_get(hass).async_get_or_create("Garden")

    async with setup_entry(hass, people=[person("Alice")]) as entry:
        t0 = dt_util.now()
        _sight(hass, entry, "Alice", t0, garden.id)
        await hass.async_block_till_done()
        assert _tracker_state(hass, "Alice") == "Garden"

        # A sighting 20 minutes in resets the clock.
        freezer.move_to(t0 + timedelta(minutes=20))
        t1 = dt_util.now()
        _sight(hass, entry, "Alice", t1, garden.id)
        await hass.async_block_till_done()
        assert _tracker_state(hass, "Alice") == "Garden"

        # 31 minutes after the first sighting but only 11 after the second:
        # still fresh.
        freezer.move_to(t0 + timedelta(minutes=31))
        async_fire_time_changed(hass, dt_util.now())
        await hass.async_block_till_done()
        assert _tracker_state(hass, "Alice") == "Garden"

        # 31 minutes after the second sighting: expired.
        freezer.move_to(t1 + timedelta(minutes=31))
        async_fire_time_changed(hass, dt_util.now())
        await hass.async_block_till_done()
        assert _tracker_state(hass, "Alice") == "unknown"


async def test_authoritative_tracker_demotes_immediately(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """The authoritative tracker saying away demotes the camera tracker at
    once - no TTL wait - and home/unknown do not demote."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    garden = ar.async_get(hass).async_get_or_create("Garden")

    async with setup_entry(
        hass,
        people=[person("Alice", authoritative_tracker="device_tracker.alice_phone")],
    ) as entry:
        _sight(hass, entry, "Alice", dt_util.now(), garden.id)
        await hass.async_block_till_done()
        assert _tracker_state(hass, "Alice") == "Garden"

        # Away from home per the phone: demoted immediately.
        hass.states.async_set("device_tracker.alice_phone", "not_home")
        await hass.async_block_till_done()
        assert _tracker_state(hass, "Alice") == "unknown"
        # The camera sighting's attributes are retained while demoted.
        assert _tracker_attributes(hass, "Alice")[ATTR_LAST_LOCATION] == "Garden"

        # A named zone is also away from home.
        hass.states.async_set("device_tracker.alice_phone", "work")
        await hass.async_block_till_done()
        assert _tracker_state(hass, "Alice") == "unknown"

        # Back home: the (still fresh) camera sighting is believed again.
        hass.states.async_set("device_tracker.alice_phone", "home")
        await hass.async_block_till_done()
        assert _tracker_state(hass, "Alice") == "Garden"

        # "unknown" is no report at all, not an away report.
        hass.states.async_set("device_tracker.alice_phone", "unknown")
        await hass.async_block_till_done()
        assert _tracker_state(hass, "Alice") == "Garden"
