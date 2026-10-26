"""Tests for skipping the presence re-check after a move (Issue #33).

ADR 0008 refines ADR 0007: while the supporting tracker leads because the
Person moved somewhere the cameras did not see, the re-check would snapshot
the camera the Person left, so it is skipped. Uses the harness time mocking
(``freezer`` + ``async_fire_time_changed``) to reach the settle, re-check and
expiry times.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.person_tracker.const import ATTR_AREA, ATTR_SOURCE

from .conftest import entry_data, person, setup_entry
from .test_presence_recheck import (
    LEAD,
    TRIGGER,
    TTL,
    _add_reference,
    _ai_result,
    _fire_edge,
    _mock_ai,
    _mock_image,
    _move_to,
    _tracker_state,
    reference_images_dir,  # noqa: F401
)
from .test_supporting_attributes import AREA_ENTITY, _house
from .test_state_lifetime import _tracker_attributes

BOB_AREA_ENTITY = "sensor.bob_area"


async def test_recheck_skipped_after_a_settled_move(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """Seen in the garden, then settled in the bedroom: no re-check runs.

    The camera sighting expires without an AI call and the supporting
    tracker keeps Alice home.
    """
    _house(hass)
    hass.states.async_set(TRIGGER, "off")
    hass.states.async_set(AREA_ENTITY, "Garden")

    with _mock_ai(_ai_result(["Alice"])) as ai_mock, _mock_image():
        async with setup_entry(
            hass,
            entry_data(cooldown_seconds=0),
            people=[person("Alice", supporting_area_entity=AREA_ENTITY)],
        ):
            _add_reference(reference_images_dir, "Alice")
            seen = dt_util.now()
            await _fire_edge(hass)
            assert ai_mock.await_count == 1

            await _move_to(hass, freezer, seen + timedelta(minutes=1))
            hass.states.async_set(AREA_ENTITY, "Bedroom")
            await _move_to(hass, freezer, seen + timedelta(minutes=3))

            await _move_to(hass, freezer, seen + TTL - LEAD)
            assert ai_mock.await_count == 1

            # No retry: the sighting expires, the supporting tracker leads.
            await _move_to(hass, freezer, seen + TTL + timedelta(minutes=1))
            assert ai_mock.await_count == 1
            assert _tracker_state(hass, "Alice") == "home"
            attrs = _tracker_attributes(hass, "Alice")
            assert attrs[ATTR_SOURCE] == AREA_ENTITY
            assert attrs[ATTR_AREA] == "Bedroom"


async def test_shared_recheck_drops_only_the_moved_person(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """Alice moved, Bob did not: the shared re-check asks about Bob only."""
    _house(hass)
    hass.states.async_set(TRIGGER, "off")
    hass.states.async_set(AREA_ENTITY, "Garden")
    hass.states.async_set(BOB_AREA_ENTITY, "Garden")

    with (
        _mock_ai(_ai_result(["Alice", "Bob"]), _ai_result(["Bob"])) as ai_mock,
        _mock_image(),
    ):
        async with setup_entry(
            hass,
            entry_data(cooldown_seconds=0),
            people=[
                person("Alice", supporting_area_entity=AREA_ENTITY),
                person("Bob", supporting_area_entity=BOB_AREA_ENTITY),
            ],
        ):
            _add_reference(reference_images_dir, "Alice")
            _add_reference(reference_images_dir, "Bob")
            seen = dt_util.now()
            await _fire_edge(hass)

            await _move_to(hass, freezer, seen + timedelta(minutes=1))
            hass.states.async_set(AREA_ENTITY, "Bedroom")
            await _move_to(hass, freezer, seen + timedelta(minutes=3))

            await _move_to(hass, freezer, seen + TTL - LEAD)
            await _move_to(hass, freezer, seen + TTL - LEAD + timedelta(seconds=30))
            assert ai_mock.await_count == 2
            kwargs = ai_mock.call_args.kwargs
            # The frame and Bob's reference image; Alice is not asked about.
            assert len(kwargs["attachments"]) == 2
            assert "Bob" in kwargs["instructions"]
            assert "Alice" not in kwargs["instructions"]


async def test_no_run_when_everyone_moved(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """Alice and Bob both moved: the shared re-check does not run at all."""
    _house(hass)
    hass.states.async_set(TRIGGER, "off")
    hass.states.async_set(AREA_ENTITY, "Garden")
    hass.states.async_set(BOB_AREA_ENTITY, "Garden")

    with _mock_ai(_ai_result(["Alice", "Bob"])) as ai_mock, _mock_image():
        async with setup_entry(
            hass,
            entry_data(cooldown_seconds=0),
            people=[
                person("Alice", supporting_area_entity=AREA_ENTITY),
                person("Bob", supporting_area_entity=BOB_AREA_ENTITY),
            ],
        ):
            _add_reference(reference_images_dir, "Alice")
            _add_reference(reference_images_dir, "Bob")
            seen = dt_util.now()
            await _fire_edge(hass)

            await _move_to(hass, freezer, seen + timedelta(minutes=1))
            hass.states.async_set(AREA_ENTITY, "Bedroom")
            hass.states.async_set(BOB_AREA_ENTITY, "Kitchen")
            await _move_to(hass, freezer, seen + timedelta(minutes=3))

            await _move_to(hass, freezer, seen + TTL - LEAD)
            await _move_to(hass, freezer, seen + TTL - LEAD + timedelta(seconds=30))
            assert ai_mock.await_count == 1


async def test_recheck_runs_while_the_camera_leads(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """A supporting tracker that agrees with the camera: re-check as before."""
    _house(hass)
    hass.states.async_set(TRIGGER, "off")
    hass.states.async_set(AREA_ENTITY, "Garden")

    with (
        _mock_ai(_ai_result(["Alice"]), _ai_result(["Alice"])) as ai_mock,
        _mock_image(),
    ):
        async with setup_entry(
            hass,
            entry_data(cooldown_seconds=0),
            people=[person("Alice", supporting_area_entity=AREA_ENTITY)],
        ):
            _add_reference(reference_images_dir, "Alice")
            seen = dt_util.now()
            await _fire_edge(hass)

            await _move_to(hass, freezer, seen + TTL - LEAD)
            assert ai_mock.await_count == 2


async def test_recheck_runs_when_the_move_has_not_settled(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """A move still within the settle period at the due time: re-check."""
    _house(hass)
    hass.states.async_set(TRIGGER, "off")
    hass.states.async_set(AREA_ENTITY, "Garden")

    with (
        _mock_ai(_ai_result(["Alice"]), _ai_result(["Alice"])) as ai_mock,
        _mock_image(),
    ):
        async with setup_entry(
            hass,
            entry_data(cooldown_seconds=0),
            people=[person("Alice", supporting_area_entity=AREA_ENTITY)],
        ):
            _add_reference(reference_images_dir, "Alice")
            seen = dt_util.now()
            await _fire_edge(hass)

            await _move_to(hass, freezer, seen + TTL - LEAD - timedelta(seconds=30))
            hass.states.async_set(AREA_ENTITY, "Bedroom")
            await _move_to(hass, freezer, seen + TTL - LEAD)
            assert ai_mock.await_count == 2


async def test_recheck_runs_when_supporting_disagreed_before_the_sighting(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """The camera overruled the supporting tracker, so it still leads."""
    _house(hass)
    hass.states.async_set(TRIGGER, "off")
    hass.states.async_set(AREA_ENTITY, "Bedroom")

    with (
        _mock_ai(_ai_result(["Alice"]), _ai_result(["Alice"])) as ai_mock,
        _mock_image(),
    ):
        async with setup_entry(
            hass,
            entry_data(cooldown_seconds=0),
            people=[person("Alice", supporting_area_entity=AREA_ENTITY)],
        ):
            _add_reference(reference_images_dir, "Alice")
            await _move_to(hass, freezer, dt_util.now() + timedelta(minutes=5))
            seen = dt_util.now()
            await _fire_edge(hass)

            await _move_to(hass, freezer, seen + TTL - LEAD)
            assert ai_mock.await_count == 2
