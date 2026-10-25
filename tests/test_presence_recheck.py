"""Tests for the presence re-check before a sighting expires (ADR 0007).

Someone who sits still fires no new trigger edge, so shortly before the TTL
runs out the person's last camera is re-checked, asking the AI about that
person only. Uses the harness time mocking (``freezer`` +
``async_fire_time_changed``) to reach the re-check and expiry times.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.components.ai_task.task import GenDataTaskResult
from homeassistant.components.camera import Image
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.person_tracker import const

from .conftest import add_camera, entry_data, person, setup_entry

DOMAIN = const.DOMAIN
TRIGGER = "binary_sensor.front_door_person"
CAMERA = "camera.front_door"
TTL = timedelta(minutes=30)
LEAD = timedelta(seconds=const.RECHECK_LEAD_SECONDS)


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
        data={
            "persons": [{"name": name, "confidence": 0.9} for name in names],
            "unknown_persons": 0,
        },
    )


def _mock_ai(*results: GenDataTaskResult | Exception):
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


def _tracker_state(hass: HomeAssistant, name: str) -> str:
    """The current state of the tracker entity of *name*."""
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    subentry_ids = {s.title: s.subentry_id for s in entry.subentries.values()}
    entity_id = er.async_get(hass).async_get_entity_id(
        "device_tracker", DOMAIN, subentry_ids[name]
    )
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    return state.state


def _add_reference(root: Path, name: str, filename: str = "ref.jpg") -> None:
    """Place a reference image of *name*."""
    (root / name).mkdir(exist_ok=True)
    (root / name / filename).write_bytes(b"ref")


async def _fire_edge(hass: HomeAssistant) -> None:
    """Take the trigger off then on (a detection edge)."""
    hass.states.async_set(TRIGGER, "off")
    hass.states.async_set(TRIGGER, "on")
    await hass.async_block_till_done()


async def _move_to(hass: HomeAssistant, freezer, when) -> None:
    """Move the clock to *when* and fire the due timers."""
    freezer.move_to(when)
    async_fire_time_changed(hass, when)
    await hass.async_block_till_done()


async def test_recheck_keeps_a_still_person_home(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """A confirmed re-check refreshes the sighting past the original TTL."""
    add_camera(hass, CAMERA, area_name="Living room")
    hass.states.async_set(TRIGGER, "off")

    with (
        _mock_ai(_ai_result(["Alice"]), _ai_result(["Alice"])) as ai_mock,
        _mock_image(),
    ):
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            _add_reference(reference_images_dir, "Alice")
            seen = dt_util.now()
            await _fire_edge(hass)
            assert ai_mock.await_count == 1

            # Not yet due.
            await _move_to(hass, freezer, seen + TTL - LEAD - timedelta(seconds=1))
            assert ai_mock.await_count == 1

            await _move_to(hass, freezer, seen + TTL - LEAD)
            assert ai_mock.await_count == 2
            kwargs = ai_mock.call_args.kwargs
            assert const.AI_TASK_RECHECK_INSTRUCTIONS in kwargs["instructions"]

            # Past the original TTL, Alice is still home.
            await _move_to(hass, freezer, seen + TTL + timedelta(minutes=1))
            assert _tracker_state(hass, "Alice") == "home"


async def test_unconfirmed_recheck_lets_the_sighting_expire(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """When the AI no longer sees the person, the TTL expires as before.

    An empty sofa is not saved as an unknown snapshot, and no further
    re-check is scheduled.
    """
    add_camera(hass, CAMERA, area_name="Living room")
    hass.states.async_set(TRIGGER, "off")

    unknown = GenDataTaskResult(
        conversation_id="test-conversation",
        data={"persons": [], "unknown_persons": 1},
    )
    with (
        _mock_ai(_ai_result(["Alice"]), unknown) as ai_mock,
        _mock_image(),
        patch(
            "custom_components.person_tracker.detection.save_unknown_snapshot"
        ) as save_mock,
    ):
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            _add_reference(reference_images_dir, "Alice")
            seen = dt_util.now()
            await _fire_edge(hass)

            await _move_to(hass, freezer, seen + TTL - LEAD)
            assert ai_mock.await_count == 2
            assert _tracker_state(hass, "Alice") == "home"

            await _move_to(hass, freezer, seen + TTL + timedelta(seconds=1))
            assert _tracker_state(hass, "Alice") == "unknown"
            await _move_to(hass, freezer, seen + 2 * TTL)
            assert ai_mock.await_count == 2
            save_mock.assert_not_called()


async def test_recheck_asks_about_the_expiring_person_only(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """Only the re-checked person and their reference images are sent."""
    add_camera(hass, CAMERA, area_name="Living room")
    hass.states.async_set(TRIGGER, "off")

    with (
        _mock_ai(_ai_result(["Alice"]), _ai_result(["Alice"])) as ai_mock,
        _mock_image(),
    ):
        async with setup_entry(
            hass,
            entry_data(cooldown_seconds=0),
            people=[person("Alice"), person("Bob")],
        ):
            _add_reference(reference_images_dir, "Alice")
            _add_reference(reference_images_dir, "Bob")
            seen = dt_util.now()
            await _fire_edge(hass)
            assert len(ai_mock.call_args.kwargs["attachments"]) == 3

            await _move_to(hass, freezer, seen + TTL - LEAD)
            kwargs = ai_mock.call_args.kwargs
            assert kwargs["attachments"][1:] == [
                {"media_content_id": f"media-source://{DOMAIN}/people/Alice/ref.jpg"}
            ]
            assert "Household members to look for: Alice." in kwargs["instructions"]
            assert "Bob" not in kwargs["instructions"]


async def test_recheck_does_not_confirm_someone_else(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """A name the re-check did not ask about updates no tracker."""
    add_camera(hass, CAMERA, area_name="Living room")
    hass.states.async_set(TRIGGER, "off")

    with (
        _mock_ai(_ai_result(["Alice"]), _ai_result(["Bob"])),
        _mock_image(),
    ):
        async with setup_entry(
            hass,
            entry_data(cooldown_seconds=0),
            people=[person("Alice"), person("Bob")],
        ):
            _add_reference(reference_images_dir, "Alice")
            _add_reference(reference_images_dir, "Bob")
            seen = dt_util.now()
            await _fire_edge(hass)

            await _move_to(hass, freezer, seen + TTL - LEAD)
            await _move_to(hass, freezer, seen + TTL + timedelta(seconds=1))
            assert _tracker_state(hass, "Alice") == "unknown"
            assert _tracker_state(hass, "Bob") == "unknown"


async def test_people_on_the_same_camera_share_one_recheck(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """People due within the lead on the same camera are checked together."""
    add_camera(hass, CAMERA, area_name="Living room")
    hass.states.async_set(TRIGGER, "off")

    with (
        _mock_ai(_ai_result(["Alice", "Bob"]), _ai_result(["Alice", "Bob"])) as ai,
        _mock_image(),
    ):
        async with setup_entry(
            hass,
            entry_data(cooldown_seconds=0),
            people=[person("Alice"), person("Bob")],
        ):
            _add_reference(reference_images_dir, "Alice")
            _add_reference(reference_images_dir, "Bob")
            seen = dt_util.now()
            await _fire_edge(hass)

            await _move_to(hass, freezer, seen + TTL - LEAD)
            await _move_to(hass, freezer, seen + TTL - LEAD + timedelta(seconds=30))
            assert ai.await_count == 2
            assert len(ai.call_args.kwargs["attachments"]) == 3
            await _move_to(hass, freezer, seen + TTL + timedelta(minutes=1))
            assert _tracker_state(hass, "Alice") == "home"
            assert _tracker_state(hass, "Bob") == "home"


async def test_recheck_ignores_the_cooldown(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """A short TTL re-checks inside the cooldown; the lead is half the TTL."""
    add_camera(hass, CAMERA, area_name="Living room")
    hass.states.async_set(TRIGGER, "off")

    with (
        _mock_ai(_ai_result(["Alice"]), _ai_result(["Alice"])) as ai_mock,
        _mock_image(),
    ):
        async with setup_entry(
            hass,
            entry_data(cooldown_seconds=300),
            people=[person("Alice", ttl=1)],
        ):
            _add_reference(reference_images_dir, "Alice")
            seen = dt_util.now()
            await _fire_edge(hass)

            await _move_to(hass, freezer, seen + timedelta(seconds=30))
            assert ai_mock.await_count == 2


async def test_recheck_disabled(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """With the re-check off, the sighting expires without an AI call."""
    add_camera(hass, CAMERA, area_name="Living room")
    hass.states.async_set(TRIGGER, "off")

    with _mock_ai(_ai_result(["Alice"])) as ai_mock, _mock_image():
        async with setup_entry(
            hass,
            entry_data(cooldown_seconds=0, recheck=False),
            people=[person("Alice")],
        ):
            _add_reference(reference_images_dir, "Alice")
            seen = dt_util.now()
            await _fire_edge(hass)

            await _move_to(hass, freezer, seen + TTL + timedelta(seconds=1))
            assert ai_mock.await_count == 1
            assert _tracker_state(hass, "Alice") == "unknown"


async def test_no_recheck_when_authoritative_tracker_reports_away(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """A person whose phone says they are away is not re-checked."""
    add_camera(hass, CAMERA, area_name="Living room")
    hass.states.async_set(TRIGGER, "off")
    hass.states.async_set("device_tracker.alice_phone", "home")

    with _mock_ai(_ai_result(["Alice"])) as ai_mock, _mock_image():
        async with setup_entry(
            hass,
            entry_data(cooldown_seconds=0),
            people=[
                person("Alice", authoritative_tracker="device_tracker.alice_phone")
            ],
        ):
            _add_reference(reference_images_dir, "Alice")
            seen = dt_util.now()
            await _fire_edge(hass)
            hass.states.async_set("device_tracker.alice_phone", "not_home")

            await _move_to(hass, freezer, seen + TTL - LEAD)
            assert ai_mock.await_count == 1


async def test_no_recheck_without_reference_images(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """Without reference photos the narrowed question is not asked."""
    add_camera(hass, CAMERA, area_name="Living room")
    hass.states.async_set(TRIGGER, "off")

    with _mock_ai(_ai_result(["Alice"])) as ai_mock, _mock_image():
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            seen = dt_util.now()
            await _fire_edge(hass)

            await _move_to(hass, freezer, seen + TTL - LEAD)
            assert ai_mock.await_count == 1


async def test_failed_recheck_is_not_retried(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """A failed re-check changes nothing; the sighting expires (Issue #5)."""
    add_camera(hass, CAMERA, area_name="Living room")
    hass.states.async_set(TRIGGER, "off")

    with (
        _mock_ai(_ai_result(["Alice"]), HomeAssistantError("provider down")) as ai,
        _mock_image(),
    ):
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            _add_reference(reference_images_dir, "Alice")
            seen = dt_util.now()
            await _fire_edge(hass)

            await _move_to(hass, freezer, seen + TTL - LEAD)
            assert ai.await_count == 2
            await _move_to(hass, freezer, seen + TTL + timedelta(seconds=1))
            assert _tracker_state(hass, "Alice") == "unknown"
            assert ai.await_count == 2


async def test_unload_cancels_pending_rechecks(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path, freezer
) -> None:
    """No re-check fires after the entry is unloaded."""
    add_camera(hass, CAMERA, area_name="Living room")
    hass.states.async_set(TRIGGER, "off")

    with _mock_ai(_ai_result(["Alice"])) as ai_mock, _mock_image():
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            _add_reference(reference_images_dir, "Alice")
            seen = dt_util.now()
            await _fire_edge(hass)

        await _move_to(hass, freezer, seen + TTL - LEAD)
        assert ai_mock.await_count == 1
