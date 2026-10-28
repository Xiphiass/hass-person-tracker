"""Tests for downscaling large reference images before the AI call (Issue #43)."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image as PILImage

from homeassistant.components import media_source
from homeassistant.components.ai_task.task import GenDataTaskResult
from homeassistant.components.camera import Image
from homeassistant.core import HomeAssistant

from custom_components.person_tracker import const
from custom_components.person_tracker.helpers import (
    get_reference_cache_dir,
    prepare_reference_images,
)

from .conftest import add_camera, entry_data, person, setup_entry

DOMAIN = const.DOMAIN
MAX_SIZE = const.REFERENCE_IMAGE_MAX_SIZE
TRIGGER = "binary_sensor.front_door_person"
CAMERA = "camera.front_door"


@pytest.fixture
def people_dir(hass: HomeAssistant, tmp_path: Path) -> Path:
    """Point the HA config dir at *tmp_path*; return the reference-images root."""
    hass.config.config_dir = str(tmp_path)
    people = tmp_path / DOMAIN / const.PEOPLE_DIR
    people.mkdir(parents=True)
    return people


def _write_image(path: Path, size: tuple[int, int], **save_kwargs) -> Path:
    """Write a solid image of *size* pixels to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    PILImage.new("RGB", size, "red").save(path, **save_kwargs)
    return path


def _prepare(hass: HomeAssistant, name: str, images: list[Path]) -> list[Path]:
    return prepare_reference_images(hass, name, images)


def _size(path: Path) -> tuple[int, int]:
    with PILImage.open(path) as image:
        return image.size


async def test_large_image_is_sent_as_a_downscaled_copy(
    hass: HomeAssistant, people_dir: Path
) -> None:
    """The longer edge shrinks to the limit, the aspect ratio stays."""
    original = _write_image(people_dir / "Alice" / "face.png", (1500, 1000))

    [prepared] = await hass.async_add_executor_job(_prepare, hass, "Alice", [original])

    assert prepared.parent == get_reference_cache_dir(hass, "Alice")
    assert prepared.suffix == ".jpg"
    assert _size(prepared) == (MAX_SIZE, round(MAX_SIZE * 1000 / 1500))
    assert _size(original) == (1500, 1000)


async def test_small_and_undecodable_images_are_sent_as_they_are(
    hass: HomeAssistant, people_dir: Path
) -> None:
    """Images within the limit, and files Pillow can't read, are not copied."""
    small = _write_image(people_dir / "Alice" / "small.jpg", (300, 300))
    edge = _write_image(people_dir / "Alice" / "edge.jpg", (MAX_SIZE, 200))
    broken = people_dir / "Alice" / "broken.jpg"
    broken.write_bytes(b"not an image")

    prepared = await hass.async_add_executor_job(
        _prepare, hass, "Alice", [small, edge, broken]
    )

    assert prepared == [small, edge, broken]
    assert not get_reference_cache_dir(hass, "Alice").exists()


async def test_copy_is_reused_rebuilt_and_removed(
    hass: HomeAssistant, people_dir: Path
) -> None:
    """A copy is made once per version of the original and dropped with it."""
    original = _write_image(people_dir / "Alice" / "face.jpg", (1000, 1000))

    [first] = await hass.async_add_executor_job(_prepare, hass, "Alice", [original])
    built = first.stat().st_mtime_ns
    [again] = await hass.async_add_executor_job(_prepare, hass, "Alice", [original])
    assert again == first
    assert again.stat().st_mtime_ns == built

    # Replacing the original rebuilds the copy and drops the old one.
    _write_image(original, (800, 1600))
    stat = original.stat()
    os.utime(original, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    [rebuilt] = await hass.async_add_executor_job(_prepare, hass, "Alice", [original])
    assert rebuilt != first
    assert not first.exists()
    assert _size(rebuilt) == (MAX_SIZE // 2, MAX_SIZE)

    # Without the original, its copy goes too.
    assert await hass.async_add_executor_job(_prepare, hass, "Alice", []) == []
    assert not rebuilt.exists()


async def test_exif_orientation_is_applied(
    hass: HomeAssistant, people_dir: Path
) -> None:
    """A rotated phone photo keeps its upright orientation in the copy."""
    exif = PILImage.Exif()
    exif[0x0112] = 6  # Orientation: rotate 90° clockwise to display.
    original = _write_image(
        people_dir / "Alice" / "phone.jpg", (1600, 800), exif=exif.tobytes()
    )

    [prepared] = await hass.async_add_executor_job(_prepare, hass, "Alice", [original])

    assert _size(prepared) == (MAX_SIZE // 2, MAX_SIZE)


async def test_cache_write_failure_sends_the_original(
    hass: HomeAssistant, people_dir: Path
) -> None:
    """When the copy can't be written, the original is sent."""
    original = _write_image(people_dir / "Alice" / "face.jpg", (1000, 1000))

    with patch.object(PILImage.Image, "save", side_effect=OSError("disk full")):
        prepared = await hass.async_add_executor_job(
            _prepare, hass, "Alice", [original]
        )

    assert prepared == [original]


async def test_run_attaches_the_copy_and_it_resolves(
    hass: HomeAssistant, default_ai_task: None, people_dir: Path
) -> None:
    """A run attaches the copy of a large image, which resolves to its file."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")
    _write_image(people_dir / "Alice Smith" / "big.jpg", (1500, 1500))
    small = _write_image(people_dir / "Alice Smith" / "small.jpg", (300, 300))
    result = GenDataTaskResult(
        conversation_id="test-conversation",
        data={"persons": [{"name": "Alice Smith", "confidence": 0.9}]},
    )

    with (
        patch(
            "custom_components.person_tracker.detection.async_generate_data",
            new=AsyncMock(return_value=result),
        ) as ai_task_mock,
        patch(
            "custom_components.person_tracker.detection.async_get_image",
            new=AsyncMock(
                return_value=Image(content_type="image/jpeg", content=b"\xff\xd8")
            ),
        ),
    ):
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice Smith")]
        ):
            hass.states.async_set(TRIGGER, "on")
            await hass.async_block_till_done()

            ids = [
                attachment["media_content_id"]
                for attachment in ai_task_mock.call_args.kwargs["attachments"][1:]
            ]
            [copy] = get_reference_cache_dir(hass, "Alice Smith").iterdir()
            assert ids == [
                f"media-source://{DOMAIN}/{const.REFERENCE_CACHE_DIR}/"
                f"Alice Smith/{copy.name}",
                f"media-source://{DOMAIN}/{const.PEOPLE_DIR}/Alice Smith/small.jpg",
            ]
            media = await media_source.async_resolve_media(hass, ids[0], None)
            assert media.path == copy
            assert media.mime_type == "image/jpeg"
            assert small.exists()
