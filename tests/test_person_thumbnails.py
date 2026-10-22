"""Tests for last-seen person thumbnails (Issue #3): each detection saves
the person's frame, cropped around them, one file per person, shown by
the person's last-detection image entity."""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image as PILImage

from homeassistant.components.ai_task.task import GenDataTaskResult
from homeassistant.components.camera import Image
from homeassistant.components.image import async_get_image as async_get_entity_image
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.person_tracker import const
from custom_components.person_tracker import detection as detection_module
from custom_components.person_tracker.helpers import _crop_box

from .conftest import add_camera, entry_data, person, setup_entry

DOMAIN = const.DOMAIN
TRIGGER = "binary_sensor.front_door_person"
CAMERA = "camera.front_door"

# A 200x100 frame with a red "person" block on x=20..80, y=10..50 (pixels),
# reported by the AI in 0-1000 normalized coordinates.
FRAME_SIZE = (200, 100)
PERSON_BOX = [100.0, 100.0, 400.0, 500.0]
FAKE_BYTES = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"


def _make_frame() -> bytes:
    """A green 200x100 frame with a red 60x40 block at (20, 10)."""
    frame = PILImage.new("RGB", FRAME_SIZE, (0, 255, 0))
    frame.paste(PILImage.new("RGB", (60, 40), (255, 0, 0)), (20, 10))
    buffer = io.BytesIO()
    frame.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def _ai_result(persons: list[dict[str, Any]]) -> GenDataTaskResult:
    """A well-formed AI task response for the given person entries."""
    return GenDataTaskResult(
        conversation_id="test-conversation",
        data={"persons": persons, "unknown_persons": 0},
    )


def _mock_ai(result: GenDataTaskResult):
    """Patch detection's AI call; the result is returned for assertions."""
    return patch(
        "custom_components.person_tracker.detection.async_generate_data",
        new=AsyncMock(return_value=result),
    )


def _mock_image(content: bytes, content_type: str = "image/jpeg"):
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


def _image_entity_id(hass: HomeAssistant, name: str) -> str:
    """The entity ID of *name*'s last-detection image entity."""
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    subentry_ids = {s.title: s.subentry_id for s in entry.subentries.values()}
    entity_id = er.async_get(hass).async_get_entity_id(
        "image", DOMAIN, f"{subentry_ids[name]}_last_detection"
    )
    assert entity_id is not None
    return entity_id


def _image_state(hass: HomeAssistant, name: str) -> str:
    """The state (last update time) of *name*'s image entity."""
    state = hass.states.get(_image_entity_id(hass, name))
    assert state is not None
    return state.state


async def _fire_edge(hass: HomeAssistant, trigger: str = TRIGGER) -> None:
    """Take *trigger* off then on (a detection edge)."""
    hass.states.async_set(trigger, "off")
    hass.states.async_set(trigger, "on")
    await hass.async_block_till_done()


@pytest.fixture
def thumbnails_dir(hass: HomeAssistant, tmp_path: Path) -> Path:
    """Point the HA config dir at *tmp_path*; return the thumbnails folder."""
    hass.config.config_dir = str(tmp_path)
    return tmp_path / DOMAIN / const.THUMBNAILS_DIR


def _crop_from_saved(path: Path) -> PILImage.Image:
    image = PILImage.open(path)
    image.load()
    return image


def test_crop_box_scales_normalized_coordinates() -> None:
    """0-1000 coordinates scale to the frame, padded and clamped."""
    # 60x40 px box, padded by 15% (9 px, 6 px) on each side.
    assert _crop_box(PERSON_BOX, *FRAME_SIZE) == (11, 4, 89, 56)
    # A full-frame box cannot pad outside the frame.
    assert _crop_box([0, 0, 1000, 1000], *FRAME_SIZE) == (0, 0, 200, 100)
    # Reversed edges are swapped before padding.
    assert _crop_box([400, 500, 100, 100], *FRAME_SIZE) == (11, 4, 89, 56)


def test_crop_box_scales_fractional_coordinates() -> None:
    """Coordinates that fit in the unit range scale to the frame size."""
    assert _crop_box([0.1, 0.1, 0.4, 0.5], *FRAME_SIZE) == (11, 4, 89, 56)


def test_crop_box_takes_large_values_as_pixels() -> None:
    """Values beyond the 0-1000 scale can only be pixels."""
    assert _crop_box([1200, 100, 1800, 500], 2000, 1000) == (1110, 40, 1890, 560)


def test_crop_box_rejects_implausible_boxes() -> None:
    """Empty, off-frame or tiny boxes keep the full frame (None)."""
    assert _crop_box([0, 0, 0, 0], *FRAME_SIZE) is None
    # Entirely off the frame (pixels beyond its right edge).
    assert _crop_box([1500, 0, 1800, 50], *FRAME_SIZE) is None
    # Under 0.5% of the frame's area: 50x50 of 1000x1000 is 0.25%.
    assert _crop_box([0, 0, 50, 50], *FRAME_SIZE) is None


async def test_identified_person_saves_tight_crop(
    hass: HomeAssistant,
    default_ai_task: None,
    thumbnails_dir: Path,
) -> None:
    """A detection saves the frame cropped tight around the person's bbox."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with (
        _mock_ai(
            _ai_result([{"name": "Alice", "confidence": 0.9, "bbox": PERSON_BOX}])
        ),
        _mock_image(_make_frame()),
    ):
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            await _fire_edge(hass)

            assert _tracker_state(hass, "Alice") == "Garden"

            assert thumbnails_dir.is_dir()
            assert [p.name for p in thumbnails_dir.iterdir()] == ["alice.jpg"]
            crop = _crop_from_saved(thumbnails_dir / "alice.jpg")
            # Around the person: the 60x40 block plus 15% padding.
            assert crop.size == (78, 52)
            # The crop covers the right region: red person, green backdrop.
            center: Any = crop.getpixel((39, 26))
            assert center[0] > 180 and center[1] < 80 and center[2] < 80
            corner: Any = crop.getpixel((0, 0))
            assert corner[1] > 150 and corner[0] < 80


async def test_normalized_bbox_is_scaled_to_the_frame(
    hass: HomeAssistant,
    default_ai_task: None,
    thumbnails_dir: Path,
) -> None:
    """A model answering in fractional coordinates crops the same region."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with (
        _mock_ai(
            _ai_result(
                [{"name": "Alice", "confidence": 0.9, "bbox": [0.1, 0.1, 0.4, 0.5]}]
            )
        ),
        _mock_image(_make_frame()),
    ):
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            await _fire_edge(hass)

            crop = _crop_from_saved(thumbnails_dir / "alice.jpg")
            assert crop.size == (78, 52)


async def test_missing_bbox_saves_the_full_frame(
    hass: HomeAssistant,
    default_ai_task: None,
    thumbnails_dir: Path,
) -> None:
    """No bbox from the AI means the whole snapshot is the thumbnail."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with (
        _mock_ai(_ai_result([{"name": "Alice", "confidence": 0.9}])),
        _mock_image(_make_frame()),
    ):
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            await _fire_edge(hass)

            assert _tracker_state(hass, "Alice") == "Garden"
            crop = _crop_from_saved(thumbnails_dir / "alice.jpg")
            assert crop.size == FRAME_SIZE


async def test_undecodable_snapshot_is_saved_raw(
    hass: HomeAssistant,
    default_ai_task: None,
    thumbnails_dir: Path,
) -> None:
    """A frame Pillow cannot decode is stored unmodified, still a detection."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with (
        _mock_ai(
            _ai_result([{"name": "Alice", "confidence": 0.9, "bbox": PERSON_BOX}])
        ),
        _mock_image(FAKE_BYTES),
    ):
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            await _fire_edge(hass)

            assert _tracker_state(hass, "Alice") == "Garden"
            files = list(thumbnails_dir.iterdir())
            assert len(files) == 1
            assert files[0].read_bytes() == FAKE_BYTES
            image = await async_get_entity_image(hass, _image_entity_id(hass, "Alice"))
            assert image.content == FAKE_BYTES


async def test_thumbnail_is_overwritten_not_accumulated(
    hass: HomeAssistant,
    default_ai_task: None,
    thumbnails_dir: Path,
) -> None:
    """Repeated detections keep one thumbnail per person, the latest."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with (
        _mock_ai(
            _ai_result([{"name": "Alice", "confidence": 0.9, "bbox": PERSON_BOX}])
        ),
        _mock_image(_make_frame()),
    ):
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            await _fire_edge(hass)
            await _fire_edge(hass)

            assert [p.name for p in thumbnails_dir.iterdir()] == ["alice.jpg"]


async def test_thumbnail_write_failure_keeps_state(
    hass: HomeAssistant,
    default_ai_task: None,
    thumbnails_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unwritable thumbnail is a warning, not a failed detection run."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with (
        _mock_ai(
            _ai_result([{"name": "Alice", "confidence": 0.9, "bbox": PERSON_BOX}])
        ),
        _mock_image(_make_frame()),
        patch.object(
            detection_module, "save_person_thumbnail", side_effect=OSError("disk full")
        ),
    ):
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            await _fire_edge(hass)

            # The identification itself succeeded.
            assert _tracker_state(hass, "Alice") == "Garden"
            # No thumbnail was ever saved, so the image entity has none.
            assert _image_state(hass, "Alice") == STATE_UNKNOWN
            assert any(
                "Could not save Alice's last-seen thumbnail" in record.message
                for record in caplog.records
                if record.levelno == logging.WARNING
            )
            assert not any(
                "Detection run failed" in record.message for record in caplog.records
            )


async def test_invalid_bbox_falls_back_to_full_frame(
    hass: HomeAssistant,
    default_ai_task: None,
    thumbnails_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A bbox the validator rejects means the full frame is saved instead."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with (
        _mock_ai(
            _ai_result([{"name": "Alice", "confidence": 0.9, "bbox": [1.0, 2.0]}])
        ),
        _mock_image(_make_frame()),
    ):
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            await _fire_edge(hass)

            assert _tracker_state(hass, "Alice") == "Garden"
            crop = _crop_from_saved(thumbnails_dir / "alice.jpg")
            assert crop.size == FRAME_SIZE


async def test_image_entity_shows_the_last_detection(
    hass: HomeAssistant,
    default_ai_task: None,
    thumbnails_dir: Path,
) -> None:
    """The person's image entity serves the saved thumbnail, timestamped."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with (
        _mock_ai(
            _ai_result([{"name": "Alice", "confidence": 0.9, "bbox": PERSON_BOX}])
        ),
        _mock_image(_make_frame()),
    ):
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            entity_id = _image_entity_id(hass, "Alice")
            # Nothing to show before the first detection.
            assert _image_state(hass, "Alice") == STATE_UNKNOWN
            with pytest.raises(HomeAssistantError):
                await async_get_entity_image(hass, entity_id)

            before = dt_util.now()
            await _fire_edge(hass)

            updated = dt_util.parse_datetime(_image_state(hass, "Alice"))
            assert updated is not None and updated >= before
            image = await async_get_entity_image(hass, entity_id)
            assert image.content_type == "image/jpeg"
            assert image.content == (thumbnails_dir / "alice.jpg").read_bytes()


async def test_failed_write_keeps_the_previous_image(
    hass: HomeAssistant,
    default_ai_task: None,
    thumbnails_dir: Path,
) -> None:
    """A failed thumbnail write leaves the last good image in place."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with (
        _mock_ai(
            _ai_result([{"name": "Alice", "confidence": 0.9, "bbox": PERSON_BOX}])
        ),
        _mock_image(_make_frame()),
    ):
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            await _fire_edge(hass)
            first = _image_state(hass, "Alice")

            with patch.object(
                detection_module,
                "save_person_thumbnail",
                side_effect=OSError("disk full"),
            ):
                await _fire_edge(hass)

            assert _image_state(hass, "Alice") == first
            image = await async_get_entity_image(hass, _image_entity_id(hass, "Alice"))
            assert image.content == (thumbnails_dir / "alice.jpg").read_bytes()


async def test_image_entity_is_on_the_person_device(
    hass: HomeAssistant,
    default_ai_task: None,
    thumbnails_dir: Path,
) -> None:
    """The image entity sits on the same device as the person's tracker."""
    add_camera(hass, CAMERA, area_name="Garden")

    async with setup_entry(hass, entry_data(), people=[person("Alice")]) as entry:
        registry = er.async_get(hass)
        image_entry = registry.async_get(_image_entity_id(hass, "Alice"))
        (tracker_entry,) = [
            e
            for e in er.async_entries_for_config_entry(registry, entry.entry_id)
            if e.domain == "device_tracker"
        ]
        assert image_entry is not None
        assert image_entry.entity_id == "image.alice_last_detection"
        assert image_entry.device_id == tracker_entry.device_id
        device = dr.async_get(hass).async_get(image_entry.device_id or "")
        assert device is not None and device.name == "Alice"


async def test_image_entity_shows_saved_thumbnail_after_restart(
    hass: HomeAssistant,
    default_ai_task: None,
    thumbnails_dir: Path,
) -> None:
    """A thumbnail saved before a restart is shown again, with its time."""
    add_camera(hass, CAMERA, area_name="Garden")
    thumbnails_dir.mkdir(parents=True)
    saved = thumbnails_dir / "alice.jpg"
    saved.write_bytes(_make_frame())

    async with setup_entry(hass, entry_data(), people=[person("Alice")]):
        updated = dt_util.parse_datetime(_image_state(hass, "Alice"))
        assert updated == dt_util.utc_from_timestamp(saved.stat().st_mtime)
        image = await async_get_entity_image(hass, _image_entity_id(hass, "Alice"))
        assert image.content == saved.read_bytes()


async def test_implausible_bbox_saves_the_full_frame(
    hass: HomeAssistant,
    default_ai_task: None,
    thumbnails_dir: Path,
) -> None:
    """A tiny box is not trusted for a crop; the whole frame is kept."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with (
        _mock_ai(
            _ai_result([{"name": "Alice", "confidence": 0.9, "bbox": [10, 10, 40, 50]}])
        ),
        _mock_image(_make_frame()),
    ):
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            await _fire_edge(hass)

            assert _tracker_state(hass, "Alice") == "Garden"
            crop = _crop_from_saved(thumbnails_dir / "alice.jpg")
            assert crop.size == FRAME_SIZE
