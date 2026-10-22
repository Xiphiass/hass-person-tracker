"""Tests for the detection loop: trigger -> snapshot -> AI identification."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import probatio
import probatio.error
import pytest

from homeassistant.components.ai_task.task import GenDataTaskResult
from homeassistant.components import media_source
from homeassistant.components.camera import Image
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
FRAME_PREFIX = f"media-source://{DOMAIN}/frames/front-door_"


@pytest.fixture
def reference_images_dir(hass: HomeAssistant, tmp_path: Path) -> Path:
    """Point the HA config dir at *tmp_path*; return the reference-images root."""
    hass.config.config_dir = str(tmp_path)
    people = tmp_path / DOMAIN / "people"
    people.mkdir(parents=True)
    return people


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


def _mock_image(
    content: bytes = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9",
    content_type: str = "image/jpeg",
):
    """Patch detection's snapshot fetch to return *content*."""
    return patch(
        "custom_components.person_tracker.detection.async_get_image",
        new=AsyncMock(return_value=Image(content_type=content_type, content=content)),
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

    with _mock_ai(_ai_result(["Alice"])) as ai_task_mock, _mock_image():
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
            (attachment,) = kwargs["attachments"]
            assert attachment["media_content_id"].startswith(FRAME_PREFIX)

            assert _tracker_state(hass, "Alice") == "Garden"
            attributes = _tracker_attributes(hass, "Alice")
            assert attributes[const.ATTR_LAST_LOCATION] == "Garden"
            assert attributes[const.ATTR_LAST_CAMERA] == CAMERA
            assert attributes[const.ATTR_LAST_SEEN]
            assert attributes[const.ATTR_AREA_ID]


async def test_min_confidence_filters_hits(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """Only hits at or above the minimum confidence update a tracker."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    result = GenDataTaskResult(
        conversation_id="test-conversation",
        data={
            "persons": [
                {"name": "Alice", "confidence": 0.8},
                {"name": "Bob", "confidence": 0.79},
            ]
        },
    )
    with _mock_ai(result), _mock_image():
        async with setup_entry(
            hass,
            entry_data(cooldown_seconds=0, min_confidence=0.8),
            people=[person("Alice"), person("Bob")],
        ):
            await _fire_edge(hass)

            assert _tracker_state(hass, "Alice") == "Garden"
            assert _tracker_state(hass, "Bob") == "unknown"


async def test_descriptions_are_sent_to_the_ai(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """Each person's description is part of the identification prompt."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with _mock_ai(_ai_result([])) as ai_task_mock, _mock_image():
        async with setup_entry(
            hass,
            entry_data(cooldown_seconds=0),
            people=[
                person("Alice", description="long dark hair, 1.65 m"),
                person("Bob"),
            ],
        ):
            await _fire_edge(hass)

            _, kwargs = ai_task_mock.call_args
            instructions = kwargs["instructions"]
            assert (
                "Descriptions of household members: Alice: long dark hair, 1.65 m."
                in instructions
            )
            assert "Bob:" not in instructions


async def test_no_descriptions_section_without_descriptions(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """Without any description the prompt has no descriptions section."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with _mock_ai(_ai_result([])) as ai_task_mock, _mock_image():
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            await _fire_edge(hass)

            _, kwargs = ai_task_mock.call_args
            assert "Descriptions" not in kwargs["instructions"]


async def test_only_off_to_on_edges_detect(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """ON -> ON, ON -> OFF and OFF -> OFF changes do not run detection."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "on")  # no prior OFF state

    with _mock_ai(_ai_result([])) as ai_task_mock, _mock_image():
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

    with _mock_ai(_ai_result(["Alice"])) as ai_task_mock, _mock_image():
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

    with _mock_ai(_ai_result(["Stranger"])) as ai_task_mock, _mock_image():
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

    with _mock_ai(_ai_result(["alice"])) as ai_task_mock, _mock_image():
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

    with _mock_ai(_ai_result(["Alice", "Bob"])) as ai_task_mock, _mock_image():
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

    with _mock_ai(_ai_result(["Alice"])) as ai_task_mock, _mock_image():
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
            assert attachment["media_content_id"].startswith(FRAME_PREFIX)


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

    with _mock_ai(_ai_result(["Alice"])) as ai_task_mock, _mock_image():
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
    """A failed AI call logs an error, changes no state, and retries nothing.

    The next trigger edge after the failure runs a fresh detection (Issue #5).
    """
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with (
        patch(
            "custom_components.person_tracker.detection.async_generate_data",
            new=AsyncMock(
                side_effect=[
                    HomeAssistantError("AI provider exploded"),
                    _ai_result(["Alice"]),
                ]
            ),
        ) as ai_task_mock,
        _mock_image(),
    ):
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            await _fire_edge(hass)

            ai_task_mock.assert_awaited_once()
            assert _tracker_state(hass, "Alice") == "unknown"
            assert any(
                "Identification failed for camera.front_door" in record.message
                for record in caplog.records
                if record.levelno == logging.ERROR
            )

            # The next edge runs a fresh, normal detection.
            await _fire_edge(hass)
            assert _tracker_state(hass, "Alice") == "Garden"


async def test_snapshot_failure_skips_run_and_keeps_state(
    hass: HomeAssistant, default_ai_task: None, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed snapshot logs an error, changes no state, schedules no retry.

    The run is skipped entirely (no AI call), and the next trigger edge runs
    normally with no sticky broken state (Issue #5).
    """
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with _mock_ai(_ai_result(["Alice"])) as ai_task_mock:
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            # The snapshot helper fails: the run must be skipped.
            with patch(
                "custom_components.person_tracker.detection.async_get_image",
                new=AsyncMock(side_effect=HomeAssistantError("camera off")),
            ):
                await _fire_edge(hass)

            ai_task_mock.assert_not_awaited()
            assert _tracker_state(hass, "Alice") == "unknown"
            assert any(
                "Snapshot failed for camera.front_door" in record.message
                for record in caplog.records
                if record.levelno == logging.ERROR
            )

            # The next edge runs a fresh, normal detection.
            with _mock_image():
                await _fire_edge(hass)
            ai_task_mock.assert_awaited_once()
            assert _tracker_state(hass, "Alice") == "Garden"


async def test_probatio_error_keeps_state_unknown(
    hass: HomeAssistant, default_ai_task: None, caplog: pytest.LogCaptureFixture
) -> None:
    """A probatio validation error from the AI boundary is a failed identification.

    Provider integrations validate structured output with probatio; its
    validation errors (not wrapped in HomeAssistantError) must be handled the
    same as any other AI call failure (Issue #5).
    """
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with (
        patch(
            "custom_components.person_tracker.detection.async_generate_data",
            new=AsyncMock(
                side_effect=[
                    probatio.error.Error("response failed schema validation"),
                    _ai_result(["Alice"]),
                ]
            ),
        ) as ai_task_mock,
        _mock_image(),
    ):
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            await _fire_edge(hass)

            ai_task_mock.assert_awaited_once()
            assert _tracker_state(hass, "Alice") == "unknown"
            assert any(
                "Identification failed for camera.front_door" in record.message
                for record in caplog.records
                if record.levelno == logging.ERROR
            )

            # The next edge runs a fresh, normal detection.
            await _fire_edge(hass)
            assert _tracker_state(hass, "Alice") == "Garden"


@pytest.mark.parametrize(
    ("ai_side_effect", "image_side_effect"),
    [
        pytest.param(
            [HomeAssistantError("AI provider exploded"), _ai_result(["Alice"])],
            None,
            id="ai_error",
        ),
        pytest.param(
            [
                GenDataTaskResult(conversation_id="test", data={"nope": 1}),
                _ai_result(["Alice"]),
            ],
            None,
            id="malformed_response",
        ),
        pytest.param(
            [RuntimeError("unexpected"), _ai_result(["Alice"])],
            None,
            id="unexpected_error",
        ),
        pytest.param(
            [_ai_result(["Alice"])],
            [
                HomeAssistantError("camera off"),
                Image(content_type="image/jpeg", content=b"\xff\xd8\xff\xd9"),
            ],
            id="snapshot_error",
        ),
    ],
)
async def test_failed_run_does_not_consume_cooldown(
    hass: HomeAssistant,
    default_ai_task: None,
    ai_side_effect: list[Any],
    image_side_effect: list[Any] | None,
) -> None:
    """A failed run releases the cooldown so the next edge detects (Issue #5)."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    image_mock = (
        AsyncMock(side_effect=image_side_effect)
        if image_side_effect is not None
        else AsyncMock(
            return_value=Image(content_type="image/jpeg", content=b"\xff\xd8\xff\xd9")
        )
    )
    with (
        patch(
            "custom_components.person_tracker.detection.async_generate_data",
            new=AsyncMock(side_effect=ai_side_effect),
        ),
        patch(
            "custom_components.person_tracker.detection.async_get_image",
            new=image_mock,
        ),
    ):
        async with setup_entry(
            hass, entry_data(cooldown_seconds=600), people=[person("Alice")]
        ):
            await _fire_edge(hass)  # fails
            assert _tracker_state(hass, "Alice") == "unknown"

            await _fire_edge(hass)  # inside the cooldown, but runs anyway
            assert _tracker_state(hass, "Alice") == "Garden"


async def test_reference_images_are_attached(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path
) -> None:
    """Each run attaches the reference images found on disk at that moment.

    Images added after setup are picked up without a reload, and the prompt
    says which attachment shows whom.
    """
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with _mock_ai(_ai_result(["Alice"])) as ai_task_mock, _mock_image():
        async with setup_entry(
            hass,
            entry_data(cooldown_seconds=0),
            people=[person("Alice"), person("Bob")],
        ):
            (reference_images_dir / "Alice" / "a.jpg").write_bytes(b"a")
            (reference_images_dir / "Alice" / "b.png").write_bytes(b"b")
            (reference_images_dir / "Alice" / "notes.txt").write_text("x")
            (reference_images_dir / "Bob" / "bob.jpg").write_bytes(b"c")
            await _fire_edge(hass)

            kwargs = ai_task_mock.call_args.kwargs
            assert kwargs["attachments"][0]["media_content_id"].startswith(FRAME_PREFIX)
            assert kwargs["attachments"][1:] == [
                {"media_content_id": f"media-source://{DOMAIN}/people/Alice/a.jpg"},
                {"media_content_id": f"media-source://{DOMAIN}/people/Alice/b.png"},
                {"media_content_id": f"media-source://{DOMAIN}/people/Bob/bob.jpg"},
            ]
            assert "images 2-3: Alice; image 4: Bob" in kwargs["instructions"]
            assert const.AI_TASK_INSTRUCTIONS in kwargs["instructions"]

            # A removed image is no longer sent on the next run.
            (reference_images_dir / "Alice" / "b.png").unlink()
            await _fire_edge(hass)

            kwargs = ai_task_mock.call_args.kwargs
            assert len(kwargs["attachments"]) == 3
            assert "image 2: Alice; image 3: Bob" in kwargs["instructions"]


async def test_reference_image_resolves_to_local_file(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path
) -> None:
    """The AI Task can resolve a reference image ID to its file on disk."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    async with setup_entry(hass, entry_data(), people=[person("Alice Smith")]):
        image = reference_images_dir / "Alice Smith" / "face.jpg"
        image.write_bytes(b"a")

        media = await media_source.async_resolve_media(
            hass, f"media-source://{DOMAIN}/people/Alice Smith/face.jpg", None
        )

        assert media.path == image
        assert media.mime_type == "image/jpeg"


async def test_ai_gets_the_snapshot_that_is_cropped(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path
) -> None:
    """The AI sees exactly the fetched frame, not a fresh camera fetch.

    The frame resolves to a local file while the AI call runs and is
    deleted afterwards, so its bbox describes the frame that is cropped.
    """
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")
    snapshot = b"\xff\xd8\xff\xe0the-frame\xff\xd9"
    seen: dict[str, Any] = {}

    async def _generate(hass: HomeAssistant, **kwargs: Any) -> GenDataTaskResult:
        media = await media_source.async_resolve_media(
            hass, kwargs["attachments"][0]["media_content_id"], None
        )
        seen["path"] = media.path
        seen["mime_type"] = media.mime_type
        seen["content"] = media.path.read_bytes()
        return _ai_result(["Alice"])

    with (
        patch(
            "custom_components.person_tracker.detection.async_generate_data",
            new=_generate,
        ),
        _mock_image(snapshot),
    ):
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            await _fire_edge(hass)

            assert _tracker_state(hass, "Alice") == "Garden"
            assert seen["content"] == snapshot
            assert seen["mime_type"] == "image/jpeg"
            assert not seen["path"].exists()


async def test_frame_is_deleted_when_the_ai_fails(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path
) -> None:
    """A failed AI call still removes the saved frame."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with (
        patch(
            "custom_components.person_tracker.detection.async_generate_data",
            new=AsyncMock(side_effect=HomeAssistantError("down")),
        ),
        _mock_image(),
    ):
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            await _fire_edge(hass)

            frames = reference_images_dir.parent / const.FRAMES_DIR
            assert not any(frames.iterdir())


async def test_frame_write_failure_skips_run(
    hass: HomeAssistant,
    default_ai_task: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unwritable frame aborts the run before the AI call (Issue #5)."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with (
        _mock_ai(_ai_result(["Alice"])) as ai_task_mock,
        _mock_image(),
        patch(
            "custom_components.person_tracker.detection.save_frame",
            side_effect=OSError("disk full"),
        ),
    ):
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            await _fire_edge(hass)

            ai_task_mock.assert_not_awaited()
            assert _tracker_state(hass, "Alice") == "unknown"
            assert any(
                "Could not save frame of" in record.message
                for record in caplog.records
                if record.levelno == logging.ERROR
            )
