"""Tests for the detection loop: trigger -> snapshot -> AI identification."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.components.ai_task.task import GenDataTaskResult
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.person_tracker import const
from custom_components.person_tracker.detection import (
    AI_RESULT_STRUCTURE,
    AI_TASK_NAME,
)

from .conftest import add_camera, entry_data, person, setup_entry

DOMAIN = const.DOMAIN
TRIGGER = "binary_sensor.front_door_person"
CAMERA = "camera.front_door"


def _ai_result(names: list[str]) -> GenDataTaskResult:
    """A well-formed AI task response for *names*."""
    return GenDataTaskResult(
        conversation_id="test-conversation",
        data={"persons": [{"name": name, "confidence": 0.9} for name in names]},
    )


def _tracker_entity(hass: HomeAssistant, name: str) -> str:
    """The device_tracker entity ID for the person subentry titled *name*."""
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    subentry_ids = {s.title: s.subentry_id for s in entry.subentries.values()}
    entity_id = er.async_get(hass).async_get_entity_id(
        "device_tracker", DOMAIN, subentry_ids[name]
    )
    assert entity_id is not None
    return entity_id


def _tracker_state(hass: HomeAssistant, name: str) -> str:
    """The current state of the tracker entity of *name*."""
    state = hass.states.get(_tracker_entity(hass, name))
    assert state is not None
    return state.state


def _tracker_attributes(hass: HomeAssistant, name: str) -> dict[str, Any]:
    """The current state attributes of the tracker entity of *name*."""
    state = hass.states.get(_tracker_entity(hass, name))
    assert state is not None
    return state.attributes


def _mock_ai(result: GenDataTaskResult):
    """Patch detection's AI call; the result is returned for assertions."""
    return patch(
        "custom_components.person_tracker.detection.async_generate_data",
        new=AsyncMock(return_value=result),
    )


async def _fire_edge(hass: HomeAssistant, trigger: str = TRIGGER) -> None:
    """Take *trigger* off then on (a detection edge)."""
    hass.states.async_set(trigger, "off")
    hass.states.async_set(trigger, "on")
    await hass.async_block_till_done()


async def test_trigger_edge_updates_tracker(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """An OFF -> ON edge runs the AI task and updates the tracker entity."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with _mock_ai(_ai_result(["Alice"])) as ai_task_mock:
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            await _fire_edge(hass)

            ai_task_mock.assert_awaited_once()
            _, kwargs = ai_task_mock.call_args
            assert kwargs["task_name"] == AI_TASK_NAME
            assert kwargs["entity_id"] == "ai_task.openai"
            assert "Alice" in kwargs["instructions"]
            assert const.AI_TASK_INSTRUCTIONS in kwargs["instructions"]
            assert kwargs["structure"] is AI_RESULT_STRUCTURE
            assert kwargs["attachments"] == [
                {"media_content_id": f"media-source://camera/{CAMERA}"}
            ]

            assert _tracker_state(hass, "Alice") == "Garden"
            attributes = _tracker_attributes(hass, "Alice")
            assert attributes[const.ATTR_LAST_LOCATION] == "Garden"
            assert attributes[const.ATTR_LAST_CAMERA] == CAMERA
            assert attributes[const.ATTR_LAST_SEEN]
            assert attributes[const.ATTR_AREA_ID]


async def test_only_off_to_on_edges_detect(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """ON -> ON, ON -> OFF and OFF -> OFF changes do not run detection."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "on")  # no prior OFF state

    with _mock_ai(_ai_result([])) as ai_task_mock:
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            hass.states.async_set(TRIGGER, "on")
            await hass.async_block_till_done()
            ai_task_mock.assert_not_awaited()

            hass.states.async_set(TRIGGER, "off")
            await hass.async_block_till_done()
            ai_task_mock.assert_not_awaited()

            await _fire_edge(hass)  # OFF -> ON: first real run
            ai_task_mock.assert_awaited_once()

            hass.states.async_set(TRIGGER, "off")
            hass.states.async_set(TRIGGER, "off")
            await hass.async_block_till_done()
            ai_task_mock.assert_awaited_once()


async def test_cooldown_suppresses_rapid_edges(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """Further edges of the same camera are skipped until the cooldown passes."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with _mock_ai(_ai_result(["Alice"])) as ai_task_mock:
        async with setup_entry(
            hass, entry_data(cooldown_seconds=600), people=[person("Alice")]
        ):
            await _fire_edge(hass)
            ai_task_mock.assert_awaited_once()

            await _fire_edge(hass)  # inside the cooldown
            ai_task_mock.assert_awaited_once()


async def test_unrecognized_person_is_ignored(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """A name that matches no configured person leaves the tracker unknown."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with _mock_ai(_ai_result(["Stranger"])) as ai_task_mock:
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            await _fire_edge(hass)

            ai_task_mock.assert_awaited_once()
            assert _tracker_state(hass, "Alice") == "unknown"


async def test_person_match_is_case_insensitive(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """The AI's casing must not matter: 'alice' matches 'Alice'."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with _mock_ai(_ai_result(["alice"])) as ai_task_mock:
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            await _fire_edge(hass)

            ai_task_mock.assert_awaited_once()
            assert _tracker_state(hass, "Alice") == "Garden"


async def test_multiple_people_in_one_run(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """Every identified person is updated by the same run."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with _mock_ai(_ai_result(["Alice", "Bob"])) as ai_task_mock:
        async with setup_entry(
            hass,
            entry_data(cooldown_seconds=0),
            people=[person("Alice"), person("Bob")],
        ):
            await _fire_edge(hass)

            ai_task_mock.assert_awaited_once()
            assert _tracker_state(hass, "Alice") == "Garden"
            assert _tracker_state(hass, "Bob") == "Garden"


async def test_any_trigger_of_the_camera_runs_detection(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """A camera can be watched by several triggers; any one fires the run."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set("binary_sensor.front_motion", "off")

    with _mock_ai(_ai_result(["Alice"])) as ai_task_mock:
        async with setup_entry(
            hass,
            entry_data(
                cameras=[
                    {
                        const.CONF_CAMERA: CAMERA,
                        const.CONF_TRIGGERS: [
                            TRIGGER,
                            "binary_sensor.front_motion",
                        ],
                    }
                ]
            ),
            people=[person("Alice")],
        ):
            await _fire_edge(hass, "binary_sensor.front_motion")

            ai_task_mock.assert_awaited_once()
            attachment = ai_task_mock.call_args.kwargs["attachments"][0]
            assert attachment["media_content_id"] == (f"media-source://camera/{CAMERA}")


async def test_camera_without_area_is_skipped(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """A camera that lost its area is skipped, not a failure (ADR 0003)."""
    add_camera(hass, CAMERA)  # no area
    hass.states.async_set(TRIGGER, "off")

    with _mock_ai(_ai_result(["Alice"])) as ai_task_mock:
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            await _fire_edge(hass)

            ai_task_mock.assert_not_awaited()
            assert _tracker_state(hass, "Alice") == "unknown"


async def test_camera_without_triggers_is_not_watched(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """A camera with no triggers subscribes to nothing."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with _mock_ai(_ai_result(["Alice"])) as ai_task_mock:
        async with setup_entry(
            hass,
            entry_data(cameras=[{const.CONF_CAMERA: CAMERA, const.CONF_TRIGGERS: []}]),
            people=[person("Alice")],
        ):
            await _fire_edge(hass)

            ai_task_mock.assert_not_awaited()


async def test_unload_stops_detection(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """After the entry unloads, trigger edges no longer run the AI task."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with _mock_ai(_ai_result(["Alice"])) as ai_task_mock:
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            await _fire_edge(hass)
            ai_task_mock.assert_awaited_once()

        # The entry is unloaded now.
        await _fire_edge(hass)
        ai_task_mock.assert_awaited_once()


async def test_ai_error_keeps_state_unknown(
    hass: HomeAssistant, default_ai_task: None, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed AI run logs the error and never corrupts tracker state."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with patch(
        "custom_components.person_tracker.detection.async_generate_data",
        new=AsyncMock(side_effect=HomeAssistantError("AI provider exploded")),
    ):
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            await _fire_edge(hass)

            assert _tracker_state(hass, "Alice") == "unknown"
            assert any(
                "Detection run failed for camera.front_door" in record.message
                for record in caplog.records
            )
