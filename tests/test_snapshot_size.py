"""Snapshot size per camera: native, 1080p or 960p (experimental)."""

from __future__ import annotations

import io
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image as PILImage

from homeassistant.components.ai_task.task import GenDataTaskResult
from homeassistant.components.camera import Image
from homeassistant.core import HomeAssistant

from custom_components.person_tracker import const, helpers
from custom_components.person_tracker.helpers import downscale_snapshot

from .conftest import add_camera, entry_data, person, setup_entry

TRIGGER = "binary_sensor.front_door_person"
CAMERA = "camera.front_door"
DETECTION = "custom_components.person_tracker.detection"


def _jpeg(width: int, height: int) -> bytes:
    out = io.BytesIO()
    PILImage.new("RGB", (width, height), (40, 90, 160)).save(out, format="JPEG")
    return out.getvalue()


def _size(content: bytes) -> tuple[int, int]:
    with PILImage.open(io.BytesIO(content)) as image:
        return image.size


@pytest.mark.parametrize(
    ("size", "source", "expected"),
    [
        (const.SNAPSHOT_SIZE_1080, (2688, 1512), (1920, 1080)),
        (const.SNAPSHOT_SIZE_960, (2688, 1512), (1707, 960)),
        (const.SNAPSHOT_SIZE_960, (1280, 1280), (960, 960)),
        # Portrait: the shorter edge is the width.
        (const.SNAPSHOT_SIZE_1080, (1512, 2688), (1080, 1920)),
    ],
)
def test_downscales_the_shorter_edge(
    size: str, source: tuple[int, int], expected: tuple[int, int]
) -> None:
    """The shorter edge is capped; the aspect ratio is kept."""
    content, content_type = downscale_snapshot(_jpeg(*source), "image/png", size)
    assert content_type == "image/jpeg"
    assert _size(content) == expected


@pytest.mark.parametrize(
    "size",
    [const.SNAPSHOT_SIZE_NATIVE, const.SNAPSHOT_SIZE_1080, const.SNAPSHOT_SIZE_960],
)
def test_small_frames_and_native_are_unchanged(size: str) -> None:
    """Never upscaled, never re-encoded when nothing changes."""
    small = _jpeg(640, 360)
    assert downscale_snapshot(small, "image/jpeg", size) == (small, "image/jpeg")


def test_native_keeps_a_large_frame() -> None:
    large = _jpeg(2688, 1512)
    assert downscale_snapshot(large, "image/jpeg", "native") == (large, "image/jpeg")


def test_undecodable_frame_is_sent_as_it_is() -> None:
    """A broken frame is not a reason to skip the detection."""
    assert downscale_snapshot(b"junk", "image/jpeg", "960") == (b"junk", "image/jpeg")


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (None, (2688, 1512)),
        (const.SNAPSHOT_SIZE_1080, (1920, 1080)),
        (const.SNAPSHOT_SIZE_960, (1707, 960)),
    ],
    ids=["default-native", "1080p", "960p"],
)
async def test_the_ai_gets_the_downscaled_frame(
    hass: HomeAssistant,
    default_ai_task: None,
    size: str | None,
    expected: tuple[int, int],
) -> None:
    """The frame saved for the AI (and the thumbnail) is the resized one."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")
    camera: dict[str, Any] = {
        const.CONF_CAMERA: CAMERA,
        const.CONF_TRIGGERS: [TRIGGER],
    }
    if size is not None:
        camera[const.CONF_SNAPSHOT_SIZE] = size
    saved: list[tuple[int, int]] = []

    def _capture(hass_, camera_id, content, content_type):
        saved.append(_size(content))
        return helpers.save_frame(hass_, camera_id, content, content_type)

    with (
        patch(
            f"{DETECTION}.async_generate_data",
            new=AsyncMock(
                return_value=GenDataTaskResult(
                    conversation_id="test",
                    data={"persons": [{"name": "Alice", "confidence": 0.9}]},
                )
            ),
        ),
        patch(
            f"{DETECTION}.async_get_image",
            new=AsyncMock(
                return_value=Image(content_type="image/jpeg", content=_jpeg(2688, 1512))
            ),
        ),
        patch(f"{DETECTION}.save_frame", side_effect=_capture),
    ):
        async with setup_entry(
            hass,
            entry_data(cameras=[camera], cooldown_seconds=0),
            people=[person("Alice")],
        ) as entry:
            assert entry.runtime_data.cameras[CAMERA].snapshot_size == (
                size or const.SNAPSHOT_SIZE_NATIVE
            )
            hass.states.async_set(TRIGGER, "on")
            await hass.async_block_till_done()

    assert saved == [expected]


async def test_reconfigure_sets_the_snapshot_size(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """Editing a camera changes its snapshot size."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")
    async with setup_entry(hass) as entry:
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "camera_edit"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {const.CONF_CAMERA: CAMERA}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                const.CONF_TRIGGERS: [TRIGGER],
                const.CONF_SNAPSHOT_SIZE: const.SNAPSHOT_SIZE_960,
            },
        )
        await hass.async_block_till_done()
        entry = hass.config_entries.async_get_entry(entry.entry_id)
        assert (
            entry.runtime_data.cameras[CAMERA].snapshot_size == const.SNAPSHOT_SIZE_960
        )
