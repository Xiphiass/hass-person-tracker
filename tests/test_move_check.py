"""Tests for the move check after a supporting move (Issue #34).

When the supporting tracker settles on a new area that has cameras, and no
camera sighting is newer than the move, that area's cameras are asked about
the moved Person (ADR 0008). Uses the harness time mocking (``freezer`` +
``async_fire_time_changed``) to reach the settle time.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from homeassistant.components.ai_task.task import GenDataTaskResult
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from custom_components.person_tracker import const
from custom_components.person_tracker.const import (
    ATTR_AREA,
    ATTR_LAST_CAMERA,
    ATTR_SOURCE,
)

from .conftest import add_camera, entry_data, person, setup_entry
from .test_presence_recheck import (
    CAMERA,
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

SETTLE = timedelta(seconds=const.SUPPORTING_SETTLE_SECONDS)
BOB_AREA_ENTITY = "sensor.bob_area"
PHONE = "device_tracker.alice_phone"
GARDEN_CAMERA = "camera.garden"
GARDEN_TRIGGER = "binary_sensor.garden_person"
TWO_GARDEN_CAMERAS = [
    {const.CONF_CAMERA: CAMERA, const.CONF_TRIGGERS: [TRIGGER]},
    {const.CONF_CAMERA: GARDEN_CAMERA, const.CONF_TRIGGERS: [GARDEN_TRIGGER]},
]


def _scored(name: str, confidence: float) -> GenDataTaskResult:
    """An AI task response identifying *name* with *confidence*."""
    return GenDataTaskResult(
        conversation_id="test-conversation",
        data={
            "persons": [{"name": name, "confidence": confidence}],
            "unknown_persons": 0,
        },
    )


def _nobody_known() -> GenDataTaskResult:
    """An AI task response seeing one person who is no household member."""
    return GenDataTaskResult(
        conversation_id="test-conversation",
        data={"persons": [], "unknown_persons": 1},
    )


async def _settle_move(
    hass: HomeAssistant, freezer, area: str, *entities: str
) -> datetime:
    """Move the supporting *entities* to *area* and wait out the settle period.

    Returns when the move happened.
    """
    moved = dt_util.now()
    for entity_id in entities or (AREA_ENTITY,):
        hass.states.async_set(entity_id, area)
    await hass.async_block_till_done()
    await _move_to(hass, freezer, moved + SETTLE)
    return moved


async def test_settled_move_into_a_camera_area_is_confirmed(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """Alice's BLE moves to the Garden: its camera confirms her there."""
    _house(hass)
    hass.states.async_set(TRIGGER, "off")
    hass.states.async_set(AREA_ENTITY, "Bedroom")

    with (
        _mock_ai(_ai_result(["Alice"]), _ai_result(["Alice"])) as ai_mock,
        _mock_image(),
    ):
        async with setup_entry(
            hass,
            entry_data(cooldown_seconds=30),
            people=[person("Alice", supporting_area_entity=AREA_ENTITY)],
        ):
            _add_reference(reference_images_dir, "Alice")
            moved = dt_util.now()
            hass.states.async_set(AREA_ENTITY, "Garden")
            await hass.async_block_till_done()

            # Not settled yet.
            await _move_to(hass, freezer, moved + SETTLE - timedelta(seconds=1))
            assert ai_mock.await_count == 0

            await _move_to(hass, freezer, moved + SETTLE)
            assert ai_mock.await_count == 1
            kwargs = ai_mock.call_args.kwargs
            assert const.AI_TASK_MOVE_CHECK_INSTRUCTIONS in kwargs["instructions"]
            # The frame and Alice's reference image only.
            assert len(kwargs["attachments"]) == 2

            assert _tracker_state(hass, "Alice") == "home"
            attrs = _tracker_attributes(hass, "Alice")
            assert attrs[ATTR_SOURCE] == "camera"
            assert attrs[ATTR_AREA] == "Garden"
            assert attrs[ATTR_LAST_CAMERA] == CAMERA

            # The camera leads again, so the re-check runs as usual.
            seen = moved + SETTLE
            await _move_to(hass, freezer, seen + TTL - LEAD)
            assert ai_mock.await_count == 2
            kwargs = ai_mock.call_args.kwargs
            assert const.AI_TASK_RECHECK_INSTRUCTIONS in kwargs["instructions"]


async def test_miss_changes_nothing_and_is_not_retried(
    hass: HomeAssistant,
    default_ai_task: None,
    reference_images_dir: Path,
    freezer,
) -> None:
    """Someone else in the Garden: the supporting tracker keeps leading.

    No Unknown snapshot is saved and no further move check runs.
    """
    _house(hass)
    hass.states.async_set(TRIGGER, "off")
    hass.states.async_set(AREA_ENTITY, "Bedroom")

    with _mock_ai(_nobody_known()) as ai_mock, _mock_image():
        async with setup_entry(
            hass,
            entry_data(cooldown_seconds=30),
            people=[person("Alice", supporting_area_entity=AREA_ENTITY)],
        ):
            _add_reference(reference_images_dir, "Alice")
            moved = await _settle_move(hass, freezer, "Garden")
            assert ai_mock.await_count == 1

            attrs = _tracker_attributes(hass, "Alice")
            assert attrs[ATTR_SOURCE] == AREA_ENTITY
            assert attrs[ATTR_AREA] == "Garden"
            assert ATTR_LAST_CAMERA not in attrs
            unknown = reference_images_dir.parent / const.UNKNOWN_SNAPSHOTS_DIR
            assert not unknown.exists() or not any(unknown.iterdir())

            await _move_to(hass, freezer, moved + SETTLE * 5)
            assert ai_mock.await_count == 1


async def test_failed_run_changes_nothing(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """A failed AI call: the supporting tracker keeps leading, no retry."""
    _house(hass)
    hass.states.async_set(TRIGGER, "off")
    hass.states.async_set(AREA_ENTITY, "Bedroom")

    with _mock_ai(HomeAssistantError("down")) as ai_mock, _mock_image():
        async with setup_entry(
            hass,
            entry_data(cooldown_seconds=30),
            people=[person("Alice", supporting_area_entity=AREA_ENTITY)],
        ):
            _add_reference(reference_images_dir, "Alice")
            moved = await _settle_move(hass, freezer, "Garden")
            assert ai_mock.await_count == 1
            assert _tracker_attributes(hass, "Alice")[ATTR_SOURCE] == AREA_ENTITY

            await _move_to(hass, freezer, moved + SETTLE * 5)
            assert ai_mock.await_count == 1


async def test_no_confidence_boost_on_a_move_check(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """0.7 would pass 0.8 with the area boost; a move check gets none."""
    _house(hass)
    hass.states.async_set(TRIGGER, "off")
    hass.states.async_set(AREA_ENTITY, "Bedroom")

    with _mock_ai(_scored("Alice", 0.7)) as ai_mock, _mock_image():
        async with setup_entry(
            hass,
            entry_data(cooldown_seconds=30, min_confidence=0.8),
            people=[person("Alice", supporting_area_entity=AREA_ENTITY)],
        ):
            _add_reference(reference_images_dir, "Alice")
            await _settle_move(hass, freezer, "Garden")
            assert ai_mock.await_count == 1
            assert _tracker_attributes(hass, "Alice")[ATTR_SOURCE] == AREA_ENTITY


async def test_every_camera_of_the_area_takes_part(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """Two cameras in the Garden: each is asked once."""
    _house(hass)
    add_camera(hass, GARDEN_CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")
    hass.states.async_set(GARDEN_TRIGGER, "off")
    hass.states.async_set(AREA_ENTITY, "Bedroom")

    with (
        _mock_ai(_nobody_known(), _ai_result(["Alice"])) as ai_mock,
        _mock_image() as image_mock,
    ):
        async with setup_entry(
            hass,
            entry_data(cameras=TWO_GARDEN_CAMERAS, cooldown_seconds=30),
            people=[person("Alice", supporting_area_entity=AREA_ENTITY)],
        ):
            _add_reference(reference_images_dir, "Alice")
            await _settle_move(hass, freezer, "Garden")
            assert ai_mock.await_count == 2
            assert {call.args[1] for call in image_mock.call_args_list} == {
                CAMERA,
                GARDEN_CAMERA,
            }
            attrs = _tracker_attributes(hass, "Alice")
            assert attrs[ATTR_SOURCE] == "camera"
            # The cameras run concurrently; either may have caught her.
            assert attrs[ATTR_LAST_CAMERA] in (CAMERA, GARDEN_CAMERA)


async def test_cameras_in_cooldown_are_left_out(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """The front door just ran: only the Garden camera is asked."""
    _house(hass)
    add_camera(hass, GARDEN_CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")
    hass.states.async_set(GARDEN_TRIGGER, "off")
    hass.states.async_set(AREA_ENTITY, "Bedroom")

    with (
        _mock_ai(_nobody_known(), _nobody_known()) as ai_mock,
        _mock_image() as image_mock,
    ):
        async with setup_entry(
            hass,
            entry_data(cameras=TWO_GARDEN_CAMERAS, cooldown_seconds=60),
            people=[person("Alice", supporting_area_entity=AREA_ENTITY)],
        ):
            _add_reference(reference_images_dir, "Alice")
            moved = dt_util.now()
            hass.states.async_set(AREA_ENTITY, "Garden")
            await _move_to(hass, freezer, moved + SETTLE - timedelta(seconds=10))
            await _fire_edge(hass)
            assert ai_mock.await_count == 1

            await _move_to(hass, freezer, moved + SETTLE)
            assert ai_mock.await_count == 2
            assert image_mock.call_args.args[1] == GARDEN_CAMERA

            # The move check claimed the Garden camera's cooldown.
            hass.states.async_set(GARDEN_TRIGGER, "off")
            hass.states.async_set(GARDEN_TRIGGER, "on")
            await hass.async_block_till_done()
            assert ai_mock.await_count == 2


async def test_people_moving_together_share_one_run(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """Alice and Bob both move to the Garden: one run asks about both."""
    _house(hass)
    hass.states.async_set(TRIGGER, "off")
    hass.states.async_set(AREA_ENTITY, "Bedroom")
    hass.states.async_set(BOB_AREA_ENTITY, "Kitchen")

    with _mock_ai(_ai_result(["Alice", "Bob"])) as ai_mock, _mock_image():
        async with setup_entry(
            hass,
            entry_data(cooldown_seconds=30),
            people=[
                person("Alice", supporting_area_entity=AREA_ENTITY),
                person("Bob", supporting_area_entity=BOB_AREA_ENTITY),
            ],
        ):
            _add_reference(reference_images_dir, "Alice")
            _add_reference(reference_images_dir, "Bob")
            await _settle_move(hass, freezer, "Garden", AREA_ENTITY, BOB_AREA_ENTITY)
            assert ai_mock.await_count == 1
            kwargs = ai_mock.call_args.kwargs
            assert len(kwargs["attachments"]) == 3
            assert "Alice" in kwargs["instructions"]
            assert "Bob" in kwargs["instructions"]
            for name in ("Alice", "Bob"):
                assert _tracker_attributes(hass, name)[ATTR_SOURCE] == "camera"


async def test_no_move_check_after_a_newer_camera_sighting(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """The trigger already caught Alice in the Garden: no move check."""
    _house(hass)
    hass.states.async_set(TRIGGER, "off")
    hass.states.async_set(AREA_ENTITY, "Bedroom")

    with _mock_ai(_ai_result(["Alice"])) as ai_mock, _mock_image():
        async with setup_entry(
            hass,
            entry_data(cooldown_seconds=0),
            people=[person("Alice", supporting_area_entity=AREA_ENTITY)],
        ):
            _add_reference(reference_images_dir, "Alice")
            moved = dt_util.now()
            hass.states.async_set(AREA_ENTITY, "Garden")
            await _move_to(hass, freezer, moved + timedelta(seconds=30))
            await _fire_edge(hass)
            assert ai_mock.await_count == 1

            await _move_to(hass, freezer, moved + SETTLE)
            assert ai_mock.await_count == 1


@pytest.mark.parametrize(
    ("area", "phone", "reference", "move_check"),
    [
        pytest.param("Bedroom", None, True, True, id="area-without-camera"),
        pytest.param("Garden", "not_home", True, True, id="authoritative-away"),
        pytest.param("Garden", None, False, True, id="no-reference-images"),
        pytest.param("Garden", None, True, False, id="option-off"),
    ],
)
async def test_no_move_check(
    hass: HomeAssistant,
    default_ai_task: None,
    reference_images_dir: Path,
    freezer,
    area: str,
    phone: str | None,
    reference: bool,
    move_check: bool,
) -> None:
    """A settled move that must not run a move check."""
    _house(hass)
    hass.states.async_set(TRIGGER, "off")
    hass.states.async_set(AREA_ENTITY, "Kitchen")
    if phone:
        hass.states.async_set(PHONE, phone)

    with _mock_ai(_ai_result(["Alice"])) as ai_mock, _mock_image():
        async with setup_entry(
            hass,
            entry_data(cooldown_seconds=30, move_check=move_check),
            people=[
                person(
                    "Alice",
                    supporting_area_entity=AREA_ENTITY,
                    authoritative_tracker=PHONE if phone else None,
                )
            ],
        ):
            if reference:
                _add_reference(reference_images_dir, "Alice")
            await _settle_move(hass, freezer, area)
            assert ai_mock.await_count == 0


async def test_moves_a_few_seconds_apart_share_one_run(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """Bob arrives five seconds after Alice: still one run about both."""
    _house(hass)
    hass.states.async_set(TRIGGER, "off")
    hass.states.async_set(AREA_ENTITY, "Bedroom")
    hass.states.async_set(BOB_AREA_ENTITY, "Kitchen")

    with _mock_ai(_ai_result(["Alice", "Bob"])) as ai_mock, _mock_image():
        async with setup_entry(
            hass,
            entry_data(cooldown_seconds=30),
            people=[
                person("Alice", supporting_area_entity=AREA_ENTITY),
                person("Bob", supporting_area_entity=BOB_AREA_ENTITY),
            ],
        ):
            _add_reference(reference_images_dir, "Alice")
            _add_reference(reference_images_dir, "Bob")
            moved = dt_util.now()
            hass.states.async_set(AREA_ENTITY, "Garden")
            await _move_to(hass, freezer, moved + timedelta(seconds=5))
            hass.states.async_set(BOB_AREA_ENTITY, "Garden")
            await _move_to(hass, freezer, moved + SETTLE)
            await _move_to(hass, freezer, moved + SETTLE + timedelta(seconds=5))
            assert ai_mock.await_count == 1
            for name in ("Alice", "Bob"):
                assert _tracker_attributes(hass, name)[ATTR_SOURCE] == "camera"


async def test_supporting_dropout_is_no_move(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """Garden, briefly unavailable, Garden again: Alice never moved."""
    _house(hass)
    hass.states.async_set(TRIGGER, "off")
    hass.states.async_set(AREA_ENTITY, "Garden")
    # Settled in the Garden long before setup.
    await _move_to(hass, freezer, dt_util.now() + SETTLE * 2)

    with _mock_ai(_ai_result(["Alice"])) as ai_mock, _mock_image():
        async with setup_entry(
            hass,
            entry_data(cooldown_seconds=30),
            people=[person("Alice", supporting_area_entity=AREA_ENTITY)],
        ):
            _add_reference(reference_images_dir, "Alice")
            hass.states.async_set(AREA_ENTITY, "unavailable")
            await hass.async_block_till_done()
            await _settle_move(hass, freezer, "Garden")
            assert ai_mock.await_count == 0


async def test_move_pending_across_a_reload_is_checked(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """The entry reloads mid-settle: the move is still checked once."""
    _house(hass)
    hass.states.async_set(TRIGGER, "off")
    hass.states.async_set(AREA_ENTITY, "Bedroom")

    with _mock_ai(_ai_result(["Alice"])) as ai_mock, _mock_image():
        async with setup_entry(
            hass,
            entry_data(cooldown_seconds=30),
            people=[person("Alice", supporting_area_entity=AREA_ENTITY)],
        ) as entry:
            _add_reference(reference_images_dir, "Alice")
            moved = dt_util.now()
            hass.states.async_set(AREA_ENTITY, "Garden")
            await _move_to(hass, freezer, moved + timedelta(seconds=30))
            assert await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()

            await _move_to(hass, freezer, moved + SETTLE)
            assert ai_mock.await_count == 1
