"""Tests for the Supporting tracker corroborating detections (Issue #32).

A hit below the minimum confidence is accepted when the supporting tracker
agrees with the camera's area by enough (ADR 0009). The boost rule itself
is covered in ``test_supporting_boost``.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.components.ai_task.task import GenDataTaskResult
from homeassistant.components.camera import Image
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import floor_registry as fr
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.person_tracker import const

from .conftest import add_camera, entry_data, person, setup_entry
from .test_detection import _tracker_attributes

DOMAIN = const.DOMAIN
TRIGGER = "binary_sensor.front_door_person"
CAMERA = "camera.front_door"
AREA_ENTITY = "sensor.alice_area"
FLOOR_ENTITY = "sensor.alice_floor"
TTL = timedelta(minutes=30)
LEAD = timedelta(seconds=const.RECHECK_LEAD_SECONDS)


def _house(hass: HomeAssistant) -> None:
    """The camera in the Garden; Garden and Kitchen downstairs, Bedroom up."""
    floors = fr.async_get(hass)
    ground = floors.async_create("Ground Floor")
    upstairs = floors.async_create("Upstairs")
    add_camera(hass, CAMERA, area_name="Garden")
    areas = ar.async_get(hass)
    for name, floor in (("Garden", ground), ("Kitchen", ground), ("Bedroom", upstairs)):
        area = areas.async_get_or_create(name)
        areas.async_update(area.id, floor_id=floor.floor_id)


def _ai_result(confidence: float) -> GenDataTaskResult:
    """The AI identifies Alice with *confidence*."""
    return GenDataTaskResult(
        conversation_id="test-conversation",
        data={
            "persons": [{"name": "Alice", "confidence": confidence}],
            "unknown_persons": 0,
        },
    )


def _mock_ai(*results: GenDataTaskResult):
    """Patch detection's AI call to answer *results* in turn."""
    return patch(
        "custom_components.person_tracker.detection.async_generate_data",
        new=AsyncMock(side_effect=list(results)),
    )


def _mock_image():
    """Patch detection's snapshot fetch."""
    return patch(
        "custom_components.person_tracker.detection.async_get_image",
        new=AsyncMock(
            return_value=Image(content_type="image/jpeg", content=b"\xff\xd8\xff\xd9")
        ),
    )


async def _fire_edge(hass: HomeAssistant) -> None:
    """Take the trigger off then on (a detection edge)."""
    hass.states.async_set(TRIGGER, "off")
    hass.states.async_set(TRIGGER, "on")
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("supporting", "confidence", "accepted"),
    [
        # The area boost (0.15) lifts 0.6 over 0.7; the floor boost does not.
        ({AREA_ENTITY: "Garden"}, 0.6, True),
        ({AREA_ENTITY: "Kitchen"}, 0.6, False),
        # The floor boost (0.05) lifts 0.66 over 0.7.
        ({AREA_ENTITY: "Kitchen"}, 0.66, True),
        ({FLOOR_ENTITY: "Ground Floor"}, 0.66, True),
        # Disagreement or no supporting data changes nothing.
        ({AREA_ENTITY: "Bedroom"}, 0.66, False),
        ({AREA_ENTITY: "unknown"}, 0.66, False),
        # A good hit stays accepted however the supporting tracker disagrees.
        ({AREA_ENTITY: "Bedroom"}, 0.9, True),
    ],
)
async def test_detection_boost(
    hass: HomeAssistant,
    default_ai_task: None,
    supporting: dict[str, str],
    confidence: float,
    accepted: bool,
) -> None:
    """A low hit is accepted when the supporting tracker agrees enough."""
    _house(hass)
    for entity_id, state in supporting.items():
        hass.states.async_set(entity_id, state)
    hass.states.async_set(TRIGGER, "off")
    alice = person(
        "Alice",
        supporting_area_entity=AREA_ENTITY if AREA_ENTITY in supporting else None,
        supporting_floor_entity=FLOOR_ENTITY if FLOOR_ENTITY in supporting else None,
    )

    with _mock_ai(_ai_result(confidence)) as ai_mock, _mock_image():
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0, min_confidence=0.7), people=[alice]
        ):
            await _fire_edge(hass)
            assert ai_mock.await_count == 1
            # The supporting tracker may place Alice at home on its own; only
            # a camera sighting records the camera.
            attributes = _tracker_attributes(hass, "Alice")
            assert (attributes.get(const.ATTR_LAST_CAMERA) == CAMERA) is accepted


async def test_prompt_does_not_mention_the_supporting_tracker(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """The supporting area never reaches the AI (ADR 0009)."""
    _house(hass)
    hass.states.async_set(AREA_ENTITY, "Garden")
    hass.states.async_set(TRIGGER, "off")

    with _mock_ai(_ai_result(0.6)) as ai_mock, _mock_image():
        async with setup_entry(
            hass,
            entry_data(cooldown_seconds=0),
            people=[person("Alice", supporting_area_entity=AREA_ENTITY)],
        ):
            await _fire_edge(hass)
            instructions = ai_mock.call_args.kwargs["instructions"]
            assert "Garden" not in instructions
            assert "Kitchen" not in instructions


@pytest.fixture
def reference_images_dir(hass: HomeAssistant, tmp_path: Path) -> Path:
    """Point the HA config dir at *tmp_path*; return the reference-images root."""
    hass.config.config_dir = str(tmp_path)
    people = tmp_path / DOMAIN / "people"
    people.mkdir(parents=True)
    return people


async def _move_to(hass: HomeAssistant, freezer, when) -> None:
    """Move the clock to *when* and fire the due timers."""
    freezer.move_to(when)
    async_fire_time_changed(hass, when)
    await hass.async_block_till_done()


@pytest.mark.parametrize(("supporting", "confirmed"), [("Garden", True), (None, False)])
async def test_recheck_boost(
    hass: HomeAssistant,
    default_ai_task: None,
    reference_images_dir: Path,
    freezer,
    supporting: str | None,
    confirmed: bool,
) -> None:
    """A low re-check answer confirms the sighting when the area agrees."""
    _house(hass)
    hass.states.async_set(TRIGGER, "off")
    if supporting:
        hass.states.async_set(AREA_ENTITY, supporting)
    (reference_images_dir / "Alice").mkdir()
    (reference_images_dir / "Alice" / "ref.jpg").write_bytes(b"ref")

    with _mock_ai(_ai_result(0.9), _ai_result(0.6)) as ai_mock, _mock_image():
        async with setup_entry(
            hass,
            entry_data(cooldown_seconds=0, min_confidence=0.7),
            people=[person("Alice", supporting_area_entity=AREA_ENTITY)],
        ):
            seen = dt_util.now()
            await _fire_edge(hass)
            await _move_to(hass, freezer, seen + TTL - LEAD)
            assert ai_mock.await_count == 2
            # The re-check refreshes last_seen only when it confirms Alice.
            last_seen = _tracker_attributes(hass, "Alice")[const.ATTR_LAST_SEEN]
            assert (dt_util.parse_datetime(last_seen) > seen) is confirmed
