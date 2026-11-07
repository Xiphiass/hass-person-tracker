"""Tests for the handoff check when a camera loses sight (ADR 0011).

A trigger only detects on an OFF -> ON edge, so someone walking into a room
whose Detection is already on fires nothing. When a camera's Detection goes
off, the people it last saw are looked for on every other camera whose
Detection is on. Uses the harness time mocking (``freezer`` +
``async_fire_time_changed``) to reach the handoff delay.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from homeassistant.components.ai_task.task import GenDataTaskResult
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.person_tracker import const, detection
from custom_components.person_tracker.const import ATTR_AREA, ATTR_LAST_CAMERA

from .conftest import add_camera, entry_data, person, setup_entry
from .test_presence_recheck import (
    _add_reference,
    _ai_result,
    _mock_ai,
    _mock_image,
    _move_to,
    reference_images_dir,  # noqa: F401
)
from .test_state_lifetime import _tracker_attributes

DELAY = timedelta(seconds=const.HANDOFF_DELAY_SECONDS)
DINING = "camera.dining_room"
DINING_TRIGGER = "binary_sensor.dining_room_person"
KITCHEN = "camera.kitchen"
KITCHEN_TRIGGER = "binary_sensor.kitchen_person"
KITCHEN_MOTION = "binary_sensor.kitchen_motion"
CAMERAS = [
    {const.CONF_CAMERA: DINING, const.CONF_TRIGGERS: [DINING_TRIGGER]},
    {const.CONF_CAMERA: KITCHEN, const.CONF_TRIGGERS: [KITCHEN_TRIGGER]},
]


def _nobody_known() -> GenDataTaskResult:
    """An AI task response seeing one person who is no household member."""
    return GenDataTaskResult(
        conversation_id="test-conversation",
        data={"persons": [], "unknown_persons": 1},
    )


def _home(hass: HomeAssistant) -> None:
    """A dining room and a kitchen camera; someone is already in the kitchen."""
    add_camera(hass, DINING, area_name="Dining room")
    add_camera(hass, KITCHEN, area_name="Kitchen")
    hass.states.async_set(DINING_TRIGGER, "off")
    hass.states.async_set(KITCHEN_TRIGGER, "on")


async def _seen_in_dining_room(hass: HomeAssistant) -> None:
    """A dining room edge identifies Alice there."""
    hass.states.async_set(DINING_TRIGGER, "on")
    await hass.async_block_till_done()
    assert _tracker_attributes(hass, "Alice")[ATTR_AREA] == "Dining room"


async def _dining_room_loses_sight(hass: HomeAssistant, freezer) -> None:
    """The dining room Detection goes off and the handoff delay passes."""
    left = dt_util.now()
    hass.states.async_set(DINING_TRIGGER, "off")
    await hass.async_block_till_done()
    await _move_to(hass, freezer, left + DELAY)


async def test_walking_into_an_occupied_room_is_caught(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """Alice leaves the dining room for the occupied kitchen: found there."""
    _home(hass)
    with (
        _mock_ai(_ai_result(["Alice"]), _ai_result(["Alice"])) as ai_mock,
        _mock_image(),
    ):
        async with setup_entry(
            hass,
            entry_data(cameras=CAMERAS, cooldown_seconds=30),
            people=[person("Alice")],
        ):
            _add_reference(reference_images_dir, "Alice")
            await _seen_in_dining_room(hass)
            assert ai_mock.await_count == 1

            left = dt_util.now()
            hass.states.async_set(DINING_TRIGGER, "off")
            await hass.async_block_till_done()
            # Not before the delay.
            await _move_to(hass, freezer, left + DELAY - timedelta(seconds=1))
            assert ai_mock.await_count == 1

            await _move_to(hass, freezer, left + DELAY)
            assert ai_mock.await_count == 2
            kwargs = ai_mock.call_args.kwargs
            assert const.AI_TASK_HANDOFF_INSTRUCTIONS in kwargs["instructions"]
            # The frame and Alice's reference image only.
            assert len(kwargs["attachments"]) == 2

            attrs = _tracker_attributes(hass, "Alice")
            assert attrs[ATTR_AREA] == "Kitchen"
            assert attrs[ATTR_LAST_CAMERA] == KITCHEN


async def test_cooldown_of_the_active_camera_is_ignored(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """The kitchen just ran for the others: the handoff check still asks."""
    _home(hass)
    hass.states.async_set(KITCHEN_TRIGGER, "off")
    with (
        _mock_ai(
            _ai_result(["Alice"]), _nobody_known(), _ai_result(["Alice"])
        ) as ai_mock,
        _mock_image(),
    ):
        async with setup_entry(
            hass,
            entry_data(cameras=CAMERAS, cooldown_seconds=300),
            people=[person("Alice")],
        ):
            _add_reference(reference_images_dir, "Alice")
            await _seen_in_dining_room(hass)
            hass.states.async_set(KITCHEN_TRIGGER, "on")
            await hass.async_block_till_done()
            assert ai_mock.await_count == 2

            await _dining_room_loses_sight(hass, freezer)
            assert ai_mock.await_count == 3
            assert _tracker_attributes(hass, "Alice")[ATTR_AREA] == "Kitchen"


async def test_back_in_view_cancels_the_handoff_check(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """The dining room Detection flickers off and on: no handoff check."""
    _home(hass)
    with _mock_ai(_ai_result(["Alice"])) as ai_mock, _mock_image():
        async with setup_entry(
            hass,
            entry_data(cameras=CAMERAS, cooldown_seconds=30),
            people=[person("Alice")],
        ):
            _add_reference(reference_images_dir, "Alice")
            await _seen_in_dining_room(hass)
            left = dt_util.now()
            hass.states.async_set(DINING_TRIGGER, "off")
            await hass.async_block_till_done()
            # Back on within the delay; still in cooldown, so no detection.
            hass.states.async_set(DINING_TRIGGER, "on")
            await hass.async_block_till_done()

            await _move_to(hass, freezer, left + DELAY * 2)
            assert ai_mock.await_count == 1
            assert _tracker_attributes(hass, "Alice")[ATTR_AREA] == "Dining room"


async def test_miss_changes_nothing(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """Only someone else in the kitchen: Alice stays in the dining room.

    No Unknown snapshot is saved.
    """
    _home(hass)
    with (
        _mock_ai(_ai_result(["Alice"]), _nobody_known()) as ai_mock,
        _mock_image(),
    ):
        async with setup_entry(
            hass,
            entry_data(cameras=CAMERAS, cooldown_seconds=30),
            people=[person("Alice")],
        ):
            _add_reference(reference_images_dir, "Alice")
            await _seen_in_dining_room(hass)
            await _dining_room_loses_sight(hass, freezer)
            assert ai_mock.await_count == 2

            attrs = _tracker_attributes(hass, "Alice")
            assert attrs[ATTR_AREA] == "Dining room"
            assert attrs[ATTR_LAST_CAMERA] == DINING
            unknown = reference_images_dir.parent / const.UNKNOWN_SNAPSHOTS_DIR
            assert not unknown.exists() or not any(unknown.iterdir())


async def test_no_active_camera_no_handoff_check(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """Nobody detected anywhere else: nothing to ask."""
    _home(hass)
    hass.states.async_set(KITCHEN_TRIGGER, "off")
    with _mock_ai(_ai_result(["Alice"])) as ai_mock, _mock_image():
        async with setup_entry(
            hass,
            entry_data(cameras=CAMERAS, cooldown_seconds=30),
            people=[person("Alice")],
        ):
            _add_reference(reference_images_dir, "Alice")
            await _seen_in_dining_room(hass)
            await _dining_room_loses_sight(hass, freezer)
            assert ai_mock.await_count == 1


async def test_turned_off(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """With the option off, losing sight changes nothing."""
    _home(hass)
    with _mock_ai(_ai_result(["Alice"])) as ai_mock, _mock_image():
        async with setup_entry(
            hass,
            entry_data(cameras=CAMERAS, cooldown_seconds=30, handoff_check=False),
            people=[person("Alice")],
        ):
            _add_reference(reference_images_dir, "Alice")
            await _seen_in_dining_room(hass)
            await _dining_room_loses_sight(hass, freezer)
            assert ai_mock.await_count == 1


async def test_another_trigger_still_on_keeps_sight(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """One of two kitchen triggers goes off: the kitchen still sees Alice."""
    _home(hass)
    hass.states.async_set(KITCHEN_TRIGGER, "off")
    hass.states.async_set(KITCHEN_MOTION, "off")
    hass.states.async_set(DINING_TRIGGER, "on")
    cameras = [
        {const.CONF_CAMERA: DINING, const.CONF_TRIGGERS: [DINING_TRIGGER]},
        {
            const.CONF_CAMERA: KITCHEN,
            const.CONF_TRIGGERS: [KITCHEN_TRIGGER, KITCHEN_MOTION],
        },
    ]
    with _mock_ai(_ai_result(["Alice"])) as ai_mock, _mock_image():
        async with setup_entry(
            hass,
            entry_data(cameras=cameras, cooldown_seconds=30),
            people=[person("Alice")],
        ):
            _add_reference(reference_images_dir, "Alice")
            hass.states.async_set(KITCHEN_TRIGGER, "on")
            hass.states.async_set(KITCHEN_MOTION, "on")
            await hass.async_block_till_done()
            assert ai_mock.await_count == 1
            assert _tracker_attributes(hass, "Alice")[ATTR_AREA] == "Kitchen"

            left = dt_util.now()
            hass.states.async_set(KITCHEN_TRIGGER, "off")
            await hass.async_block_till_done()
            await _move_to(hass, freezer, left + DELAY)
            assert ai_mock.await_count == 1


async def test_expired_sighting_is_not_handed_off(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """A sighting that already expired is no one to look for."""
    _home(hass)
    with _mock_ai(_ai_result(["Alice"])) as ai_mock, _mock_image():
        async with setup_entry(
            hass,
            entry_data(cameras=CAMERAS, cooldown_seconds=30, recheck=False),
            people=[person("Alice", ttl=1)],
        ):
            _add_reference(reference_images_dir, "Alice")
            seen = dt_util.now()
            await _seen_in_dining_room(hass)
            await _move_to(hass, freezer, seen + timedelta(minutes=2))
            await _dining_room_loses_sight(hass, freezer)
            assert ai_mock.await_count == 1


def test_evaluate_has_a_handoff_preamble() -> None:
    """The evaluate method frames a handoff check too (ADR 0010)."""
    assert (
        detection._EVALUATE_PREAMBLES[const.AI_TASK_HANDOFF_INSTRUCTIONS]
        == const.EVALUATE_HANDOFF_PREAMBLE
    )
