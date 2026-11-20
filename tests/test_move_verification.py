"""Tests for verifying a supporting move with the cameras (ADR 0013).

Once a supporting move settles against a current camera sighting, the camera
keeps leading while the person's last camera is asked whether they are still
there (the old camera check). Only a miss goes on to the move check on the
new area's cameras, and only when no camera confirms the person does the
supporting tracker lead. Uses the harness time mocking (``freezer`` +
``async_fire_time_changed``) to reach the settle time.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

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
from .test_move_check import SETTLE, _nobody_known, _scored
from .test_presence_recheck import (
    CAMERA,
    TRIGGER,
    _add_reference,
    _ai_result,
    _fire_edge,
    _mock_ai,
    _mock_image,
    _move_to,
    reference_images_dir,  # noqa: F401
)
from .test_state_lifetime import _tracker_attributes
from .test_supporting_attributes import AREA_ENTITY, _house

KITCHEN_CAMERA = "camera.kitchen"
KITCHEN_TRIGGER = "binary_sensor.kitchen_person"
WITH_KITCHEN = [
    {const.CONF_CAMERA: CAMERA, const.CONF_TRIGGERS: [TRIGGER]},
    {const.CONF_CAMERA: KITCHEN_CAMERA, const.CONF_TRIGGERS: [KITCHEN_TRIGGER]},
]


def _kitchen_house(hass: HomeAssistant) -> None:
    """The test house plus a camera in the Kitchen."""
    _house(hass)
    add_camera(hass, KITCHEN_CAMERA, area_name="Kitchen")
    hass.states.async_set(TRIGGER, "off")
    hass.states.async_set(KITCHEN_TRIGGER, "off")


async def _seen_in_the_garden_then_moved(
    hass: HomeAssistant, freezer, reference_images_dir: Path, area: str
) -> None:
    """Alice is seen on the front door (Garden), then her BLE moves to *area*.

    Returns once the move has settled and its verification ran.
    """
    _add_reference(reference_images_dir, "Alice")
    seen = dt_util.now()
    await _fire_edge(hass)
    await _move_to(hass, freezer, seen + timedelta(minutes=1))
    moved = dt_util.now()
    hass.states.async_set(AREA_ENTITY, area)
    await _move_to(hass, freezer, moved + SETTLE - timedelta(seconds=1))
    assert _tracker_attributes(hass, "Alice")[ATTR_SOURCE] == "camera"
    await _move_to(hass, freezer, moved + SETTLE)


def _instructions(ai_mock: AsyncMock, call: int) -> str:
    """The instructions of the AI mock's *call*-th call (0-based)."""
    return ai_mock.call_args_list[call].kwargs["instructions"]


async def test_old_camera_confirms_and_overrules_the_move(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """BLE drifts to the Kitchen; the front door still sees Alice.

    The camera leads throughout, even while the old camera check runs, and
    the Kitchen camera is not asked.
    """
    _kitchen_house(hass)
    hass.states.async_set(AREA_ENTITY, "Garden")
    during_check: list[Any] = []

    async def _answer(*_args: Any, **_kwargs: Any) -> GenDataTaskResult:
        during_check.append(_tracker_attributes(hass, "Alice")[ATTR_SOURCE])
        return _ai_result(["Alice"])

    with (
        patch(
            "custom_components.person_tracker.detection.async_generate_data",
            new=AsyncMock(side_effect=_answer),
        ) as ai_mock,
        _mock_image() as image_mock,
    ):
        async with setup_entry(
            hass,
            entry_data(cameras=WITH_KITCHEN, cooldown_seconds=30),
            people=[person("Alice", supporting_area_entity=AREA_ENTITY)],
        ):
            await _seen_in_the_garden_then_moved(
                hass, freezer, reference_images_dir, "Kitchen"
            )
            assert ai_mock.await_count == 2
            assert const.AI_TASK_RECHECK_INSTRUCTIONS in _instructions(ai_mock, 1)
            assert image_mock.call_args.args[1] == CAMERA
            # Recorded during the old camera check (the first is the trigger
            # run before any sighting).
            assert during_check[1] == "camera"

            attrs = _tracker_attributes(hass, "Alice")
            assert attrs[ATTR_SOURCE] == "camera"
            assert attrs[ATTR_AREA] == "Garden"

            # The move stays overruled: nothing else runs.
            await _move_to(hass, freezer, dt_util.now() + SETTLE * 3)
            assert ai_mock.await_count == 2
            assert _tracker_attributes(hass, "Alice")[ATTR_SOURCE] == "camera"


async def test_old_camera_miss_then_new_camera_confirms(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """The front door misses, the Kitchen camera sees Alice."""
    _kitchen_house(hass)
    hass.states.async_set(AREA_ENTITY, "Garden")

    with (
        _mock_ai(
            _ai_result(["Alice"]), _nobody_known(), _ai_result(["Alice"])
        ) as ai_mock,
        _mock_image() as image_mock,
    ):
        async with setup_entry(
            hass,
            entry_data(cameras=WITH_KITCHEN, cooldown_seconds=30),
            people=[person("Alice", supporting_area_entity=AREA_ENTITY)],
        ):
            await _seen_in_the_garden_then_moved(
                hass, freezer, reference_images_dir, "Kitchen"
            )
            assert ai_mock.await_count == 3
            assert const.AI_TASK_RECHECK_INSTRUCTIONS in _instructions(ai_mock, 1)
            assert const.AI_TASK_MOVE_CHECK_INSTRUCTIONS in _instructions(ai_mock, 2)
            assert [call.args[1] for call in image_mock.call_args_list] == [
                CAMERA,
                CAMERA,
                KITCHEN_CAMERA,
            ]
            attrs = _tracker_attributes(hass, "Alice")
            assert attrs[ATTR_SOURCE] == "camera"
            assert attrs[ATTR_AREA] == "Kitchen"
            assert attrs[ATTR_LAST_CAMERA] == KITCHEN_CAMERA


async def test_no_camera_confirms_so_the_supporting_tracker_leads(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """Both checks miss: the Kitchen from the supporting tracker leads."""
    _kitchen_house(hass)
    hass.states.async_set(AREA_ENTITY, "Garden")

    with (
        _mock_ai(_ai_result(["Alice"]), _nobody_known(), _nobody_known()) as ai_mock,
        _mock_image(),
    ):
        async with setup_entry(
            hass,
            entry_data(cameras=WITH_KITCHEN, cooldown_seconds=30),
            people=[person("Alice", supporting_area_entity=AREA_ENTITY)],
        ):
            await _seen_in_the_garden_then_moved(
                hass, freezer, reference_images_dir, "Kitchen"
            )
            assert ai_mock.await_count == 3
            attrs = _tracker_attributes(hass, "Alice")
            assert attrs[ATTR_SOURCE] == AREA_ENTITY
            assert attrs[ATTR_AREA] == "Kitchen"


async def test_move_to_an_area_without_cameras(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """Bedroom has no camera: only the old camera check runs, then BLE leads."""
    _house(hass)
    hass.states.async_set(TRIGGER, "off")
    hass.states.async_set(AREA_ENTITY, "Garden")

    with _mock_ai(_ai_result(["Alice"]), _nobody_known()) as ai_mock, _mock_image():
        async with setup_entry(
            hass,
            entry_data(cooldown_seconds=30),
            people=[person("Alice", supporting_area_entity=AREA_ENTITY)],
        ):
            await _seen_in_the_garden_then_moved(
                hass, freezer, reference_images_dir, "Bedroom"
            )
            assert ai_mock.await_count == 2
            attrs = _tracker_attributes(hass, "Alice")
            assert attrs[ATTR_SOURCE] == AREA_ENTITY
            assert attrs[ATTR_AREA] == "Bedroom"


async def test_failed_old_camera_check_falls_back_to_the_move(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """A failed AI call must not pin Alice to the room she may have left."""
    _house(hass)
    hass.states.async_set(TRIGGER, "off")
    hass.states.async_set(AREA_ENTITY, "Garden")

    with (
        _mock_ai(_ai_result(["Alice"]), HomeAssistantError("down")) as ai_mock,
        _mock_image(),
    ):
        async with setup_entry(
            hass,
            entry_data(cooldown_seconds=30),
            people=[person("Alice", supporting_area_entity=AREA_ENTITY)],
        ):
            await _seen_in_the_garden_then_moved(
                hass, freezer, reference_images_dir, "Bedroom"
            )
            assert ai_mock.await_count == 2
            assert _tracker_attributes(hass, "Alice")[ATTR_SOURCE] == AREA_ENTITY


async def test_no_floor_boost_on_the_old_camera_check(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """Garden and Kitchen share a floor; 0.78 must not be boosted past 0.8."""
    _house(hass)
    hass.states.async_set(TRIGGER, "off")
    hass.states.async_set(AREA_ENTITY, "Garden")

    with (
        _mock_ai(_ai_result(["Alice"]), _scored("Alice", 0.78)) as ai_mock,
        _mock_image(),
    ):
        async with setup_entry(
            hass,
            entry_data(cooldown_seconds=30, min_confidence=0.8),
            people=[person("Alice", supporting_area_entity=AREA_ENTITY)],
        ):
            await _seen_in_the_garden_then_moved(
                hass, freezer, reference_images_dir, "Kitchen"
            )
            assert ai_mock.await_count == 2
            assert _tracker_attributes(hass, "Alice")[ATTR_SOURCE] == AREA_ENTITY


async def test_option_off_lets_the_move_lead_unverified(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """With the move check off, the settled move leads without any AI call."""
    _house(hass)
    hass.states.async_set(TRIGGER, "off")
    hass.states.async_set(AREA_ENTITY, "Garden")

    with _mock_ai(_ai_result(["Alice"])) as ai_mock, _mock_image():
        async with setup_entry(
            hass,
            entry_data(cooldown_seconds=30, move_check=False),
            people=[person("Alice", supporting_area_entity=AREA_ENTITY)],
        ):
            await _seen_in_the_garden_then_moved(
                hass, freezer, reference_images_dir, "Bedroom"
            )
            assert ai_mock.await_count == 1
            assert _tracker_attributes(hass, "Alice")[ATTR_SOURCE] == AREA_ENTITY


async def test_return_to_the_settled_area_is_verified_against_a_sighting(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """BLE settled in the Bedroom, the camera saw Alice in the Garden since.

    A dropout and back to the Bedroom contradicts that sighting, so it is
    verified like any move; the front door still sees her.
    """
    _house(hass)
    hass.states.async_set(TRIGGER, "off")
    hass.states.async_set(AREA_ENTITY, "Bedroom")
    await _move_to(hass, freezer, dt_util.now() + SETTLE * 2)

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
            await _fire_edge(hass)
            await _move_to(hass, freezer, dt_util.now() + timedelta(seconds=10))
            hass.states.async_set(AREA_ENTITY, "unavailable")
            await hass.async_block_till_done()
            moved = dt_util.now()
            hass.states.async_set(AREA_ENTITY, "Bedroom")
            await _move_to(hass, freezer, moved + SETTLE)
            assert ai_mock.await_count == 2
            attrs = _tracker_attributes(hass, "Alice")
            assert attrs[ATTR_SOURCE] == "camera"
            assert attrs[ATTR_AREA] == "Garden"
