"""Tests for unknown snapshots: saving on unidentified sightings (Issue #4)
and the fixed 7-day retention purge."""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.components.ai_task.task import GenDataTaskResult
from homeassistant.components.camera import Image
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.person_tracker import const
from custom_components.person_tracker import detection as detection_module
from custom_components.person_tracker.helpers import purge_unknown_snapshots

from .conftest import add_camera, entry_data, person, setup_entry

DOMAIN = const.DOMAIN
TRIGGER = "binary_sensor.front_door_person"
CAMERA = "camera.front_door"

IMAGE_BYTES = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"


def _ai_result(
    names: list[str], unknown_persons: int | None = None
) -> GenDataTaskResult:
    """A well-formed AI task response for *names*.

    *unknown_persons* is left out of the response when ``None``.
    """
    data: dict[str, Any] = {
        "persons": [{"name": name, "confidence": 0.9} for name in names]
    }
    if unknown_persons is not None:
        data["unknown_persons"] = unknown_persons
    return GenDataTaskResult(conversation_id="test-conversation", data=data)


def _mock_ai(result: GenDataTaskResult):
    """Patch detection's AI call; the result is returned for assertions."""
    return patch(
        "custom_components.person_tracker.detection.async_generate_data",
        new=AsyncMock(return_value=result),
    )


def _mock_image(content: bytes = IMAGE_BYTES, content_type: str = "image/jpeg"):
    """Patch detection's snapshot fetch."""
    return patch(
        "custom_components.person_tracker.detection.async_get_image",
        new=AsyncMock(return_value=Image(content_type=content_type, content=content)),
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


def _tracker_attributes(hass: HomeAssistant, name: str) -> dict[str, Any]:
    """The current state attributes of the tracker entity of *name*."""
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    subentry_ids = {s.title: s.subentry_id for s in entry.subentries.values()}
    entity_id = er.async_get(hass).async_get_entity_id(
        "device_tracker", DOMAIN, subentry_ids[name]
    )
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    return state.attributes


async def _fire_edge(hass: HomeAssistant, trigger: str = TRIGGER) -> None:
    """Take *trigger* off then on (a detection edge)."""
    hass.states.async_set(trigger, "off")
    hass.states.async_set(trigger, "on")
    await hass.async_block_till_done()


def _make_snapshot(folder: Path, name: str, age_days: float) -> Path:
    """Create a snapshot file in *folder* with mtime *age_days* in the past."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(IMAGE_BYTES)
    mtime = time.time() - age_days * 86400
    os.utime(path, (mtime, mtime))
    return path


def _saved_snapshots(folder: Path) -> list[Path]:
    """The unknown snapshot files currently in *folder*."""
    return sorted(folder.iterdir()) if folder.is_dir() else []


@pytest.fixture
def unknown_snapshots_dir(hass: HomeAssistant, tmp_path: Path) -> Path:
    """Point the HA config dir at *tmp_path*; return the unknown-snapshots folder."""
    hass.config.config_dir = str(tmp_path)
    return tmp_path / DOMAIN / const.UNKNOWN_SNAPSHOTS_DIR


async def test_none_result_saves_unknown_snapshot_and_keeps_state(
    hass: HomeAssistant,
    default_ai_task: None,
    unknown_snapshots_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An AI result of 'none' saves the snapshot and leaves tracker states untouched."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with _mock_ai(_ai_result([])) as ai_task_mock, _mock_image() as image_mock:
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            await _fire_edge(hass)

            ai_task_mock.assert_awaited_once()
            image_mock.assert_awaited_once_with(hass, CAMERA)

            files = _saved_snapshots(unknown_snapshots_dir)
            assert len(files) == 1
            # {YYYYMMDD-HHMMSS}-{area}-{camera}.<ext>
            assert re.fullmatch(
                r"\d{8}-\d{6}_garden_front-door\.(jpg|jpeg|png)", files[0].name
            )
            assert files[0].read_bytes() == IMAGE_BYTES

            assert _tracker_state(hass, "Alice") == "unknown"
            attrs = _tracker_attributes(hass, "Alice")
            assert "last_seen" not in attrs
            assert "last_location" not in attrs
            assert "area_id" not in attrs
            assert "last_camera" not in attrs

            assert any(
                CAMERA in record.message
                and "Garden" in record.message
                and "unknown snapshot" in record.message
                for record in caplog.records
                if record.levelno == logging.INFO
            )


async def test_unrecognized_person_saves_unknown_snapshot(
    hass: HomeAssistant,
    default_ai_task: None,
    unknown_snapshots_dir: Path,
) -> None:
    """A hit that matches no configured person is still an unidentified sighting."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with _mock_ai(_ai_result(["Stranger"])), _mock_image():
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            await _fire_edge(hass)

            assert len(_saved_snapshots(unknown_snapshots_dir)) == 1
            assert _tracker_state(hass, "Alice") == "unknown"


async def test_identified_person_does_not_save_snapshot(
    hass: HomeAssistant,
    default_ai_task: None,
    unknown_snapshots_dir: Path,
) -> None:
    """A run that identifies someone changes state and saves nothing."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with _mock_ai(_ai_result(["Alice"])), _mock_image():
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            await _fire_edge(hass)

            assert _saved_snapshots(unknown_snapshots_dir) == []
            assert _tracker_state(hass, "Alice") == "Garden"


async def test_empty_scene_saves_nothing(
    hass: HomeAssistant,
    default_ai_task: None,
    unknown_snapshots_dir: Path,
) -> None:
    """A trigger with no one in view at all is not an unidentified sighting."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with _mock_ai(_ai_result([], unknown_persons=0)), _mock_image():
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            await _fire_edge(hass)

            assert _saved_snapshots(unknown_snapshots_dir) == []
            assert _tracker_state(hass, "Alice") == "unknown"


async def test_unknown_person_count_saves_snapshot(
    hass: HomeAssistant,
    default_ai_task: None,
    unknown_snapshots_dir: Path,
) -> None:
    """A visible non-household person saves the snapshot."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with _mock_ai(_ai_result([], unknown_persons=1)), _mock_image():
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            await _fire_edge(hass)

            assert len(_saved_snapshots(unknown_snapshots_dir)) == 1
            assert _tracker_state(hass, "Alice") == "unknown"


async def test_saved_snapshot_is_taken_before_the_ai_call(
    hass: HomeAssistant,
    default_ai_task: None,
    unknown_snapshots_dir: Path,
) -> None:
    """The saved frame is the one fetched at trigger time, not after the AI ran."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    calls: list[str] = []
    frames = iter([b"trigger-time-frame", b"later-frame"])

    async def _fake_image(*_args: Any, **_kwargs: Any) -> Image:
        calls.append("image")
        return Image(content_type="image/jpeg", content=next(frames))

    async def _fake_ai(*_args: Any, **_kwargs: Any) -> GenDataTaskResult:
        calls.append("ai")
        return _ai_result([], unknown_persons=1)

    with (
        patch.object(detection_module, "async_generate_data", new=_fake_ai),
        patch.object(detection_module, "async_get_image", new=_fake_image),
    ):
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            await _fire_edge(hass)

            assert calls == ["image", "ai"]
            files = _saved_snapshots(unknown_snapshots_dir)
            assert len(files) == 1
            assert files[0].read_bytes() == b"trigger-time-frame"


async def test_snapshot_write_failure_is_a_warning(
    hass: HomeAssistant,
    default_ai_task: None,
    unknown_snapshots_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unwritable snapshot is a warning, not a failed detection run."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with (
        _mock_ai(_ai_result([])),
        _mock_image(),
        patch.object(
            detection_module, "save_unknown_snapshot", side_effect=OSError("disk full")
        ),
    ):
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            await _fire_edge(hass)

            assert _tracker_state(hass, "Alice") == "unknown"
            assert any(
                "could not be saved" in record.message
                for record in caplog.records
                if record.levelno == logging.WARNING
            )
            assert not any(
                "Detection run failed" in record.message for record in caplog.records
            )


async def test_malformed_response_saves_nothing_and_keeps_state(
    hass: HomeAssistant,
    default_ai_task: None,
    unknown_snapshots_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A malformed AI response fails the run: no snapshot, no state change.

    A malformed response is neither 'no one visible' (a false negative) nor a
    sighting to save (a false positive) — the run is skipped, and the next
    trigger edge runs a fresh detection (Issue #5).
    """
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with (
        _mock_image(),
        patch(
            "custom_components.person_tracker.detection.async_generate_data",
            new=AsyncMock(
                side_effect=[
                    GenDataTaskResult(
                        conversation_id="test-conversation",
                        data="I see a person in the garden.",
                    ),
                    _ai_result(["Alice"]),
                ]
            ),
        ) as ai_task_mock,
    ):
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            await _fire_edge(hass)

            ai_task_mock.assert_awaited_once()
            assert _saved_snapshots(unknown_snapshots_dir) == []
            assert _tracker_state(hass, "Alice") == "unknown"
            assert any(
                "Malformed AI response for camera.front_door" in record.message
                for record in caplog.records
                if record.levelno == logging.ERROR
            )

            # The next edge runs a fresh, normal detection.
            await _fire_edge(hass)
            assert _tracker_state(hass, "Alice") == "Garden"
            assert _saved_snapshots(unknown_snapshots_dir) == []


async def test_snapshot_fetch_failure_keeps_state(
    hass: HomeAssistant,
    default_ai_task: None,
    unknown_snapshots_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unfetchable snapshot fails the run: error, no AI call, no state change.

    The identification is skipped entirely — a failed snapshot must not
    change tracker state or schedule a retry (Issue #5).
    """
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with (
        _mock_ai(_ai_result([])) as ai_task_mock,
        patch(
            "custom_components.person_tracker.detection.async_get_image",
            new=AsyncMock(side_effect=HomeAssistantError("camera off")),
        ),
    ):
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            await _fire_edge(hass)

            ai_task_mock.assert_not_awaited()
            assert _saved_snapshots(unknown_snapshots_dir) == []
            assert _tracker_state(hass, "Alice") == "unknown"
            assert any(
                "Snapshot failed" in record.message
                for record in caplog.records
                if record.levelno == logging.ERROR
            )


async def test_purge_deletes_only_old_files(
    hass: HomeAssistant, unknown_snapshots_dir: Path
) -> None:
    """Files past the 7-day retention are deleted; the rest are kept."""
    stale = _make_snapshot(unknown_snapshots_dir, "stale.jpeg", age_days=8)
    recent = _make_snapshot(unknown_snapshots_dir, "recent.jpeg", age_days=1)

    removed = await hass.async_add_executor_job(
        purge_unknown_snapshots,
        hass,
        timedelta(days=const.UNKNOWN_SNAPSHOT_RETENTION_DAYS),
    )

    assert removed == 1
    assert not stale.exists()
    assert recent.exists()


async def test_purge_skips_files_it_cannot_delete(
    hass: HomeAssistant,
    unknown_snapshots_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One undeletable file is logged and skipped; the others are still purged."""
    stuck = _make_snapshot(unknown_snapshots_dir, "a-stuck.jpeg", age_days=8)
    stale = _make_snapshot(unknown_snapshots_dir, "b-stale.jpeg", age_days=8)
    real_unlink = Path.unlink

    def _unlink(self: Path, missing_ok: bool = False) -> None:
        if self.name == stuck.name:
            raise PermissionError("permission denied")
        real_unlink(self, missing_ok=missing_ok)

    with patch.object(Path, "unlink", _unlink):
        removed = await hass.async_add_executor_job(
            purge_unknown_snapshots,
            hass,
            timedelta(days=const.UNKNOWN_SNAPSHOT_RETENTION_DAYS),
        )

    assert removed == 1
    assert stuck.exists()
    assert not stale.exists()
    assert any(
        "Could not purge unknown snapshot" in record.message
        for record in caplog.records
        if record.levelno == logging.WARNING
    )


async def test_purge_missing_folder_returns_zero(
    hass: HomeAssistant, unknown_snapshots_dir: Path
) -> None:
    """No folder yet means nothing to purge, no error."""
    assert not unknown_snapshots_dir.is_dir()
    removed = await hass.async_add_executor_job(
        purge_unknown_snapshots,
        hass,
        timedelta(days=const.UNKNOWN_SNAPSHOT_RETENTION_DAYS),
    )
    assert removed == 0


async def test_setup_purges_stale_snapshots(
    hass: HomeAssistant,
    default_ai_task: None,
    unknown_snapshots_dir: Path,
) -> None:
    """Setting up the entry purges snapshots from before the retention window."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")
    stale = _make_snapshot(unknown_snapshots_dir, "stale.jpeg", age_days=30)
    recent = _make_snapshot(unknown_snapshots_dir, "recent.jpeg", age_days=1)

    async with setup_entry(
        hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
    ):
        assert not stale.exists()
        assert recent.exists()


async def test_daily_purge_interval_is_registered(
    hass: HomeAssistant,
    default_ai_task: None,
    unknown_snapshots_dir: Path,
) -> None:
    """The entry registers a daily purge whose action deletes stale files."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    captured: dict[str, Any] = {}

    def _fake_track(hass: HomeAssistant, action: Any, interval: Any, **_kwargs: Any):
        captured["action"] = action
        captured["interval"] = interval
        return lambda: None

    with patch.object(detection_module, "async_track_time_interval", _fake_track):
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            assert captured["interval"] == timedelta(
                hours=const.UNKNOWN_SNAPSHOT_CLEANUP_HOURS
            )
            # Simulate the interval firing: stale files are purged.
            stale = _make_snapshot(unknown_snapshots_dir, "stale.jpeg", age_days=30)
            await captured["action"](dt_util.utcnow())
            assert not stale.exists()


async def test_purge_failure_is_swallowed(
    hass: HomeAssistant,
    default_ai_task: None,
    unknown_snapshots_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing purge is logged and never blocks entry setup."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with patch.object(
        detection_module, "purge_unknown_snapshots", side_effect=OSError("read-only fs")
    ):
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            assert _tracker_state(hass, "Alice") == "unknown"
            assert any(
                "Failed to purge old unknown snapshots" in record.message
                for record in caplog.records
            )
