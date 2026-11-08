"""Snapshot source per camera: a frame of the video stream (Issue #47)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.components.ai_task.task import GenDataTaskResult
from homeassistant.components.camera import Image
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.person_tracker import const, helpers
from custom_components.person_tracker.detection import _ffmpeg_input

from .conftest import add_camera, entry_data, person, setup_entry

DOMAIN = const.DOMAIN
TRIGGER = "binary_sensor.front_door_person"
CAMERA = "camera.front_door"
STREAM = "rtsps://192.168.1.1:7441/abc?enableSrtp"
STREAM_FRAME = b"\xff\xd8stream-frame\xff\xd9"
STILL = b"\xff\xd8still\xff\xd9"
DETECTION = "custom_components.person_tracker.detection"


def _stream_camera() -> dict[str, Any]:
    return {
        const.CONF_CAMERA: CAMERA,
        const.CONF_TRIGGERS: [TRIGGER],
        const.CONF_SNAPSHOT_SOURCE: const.SNAPSHOT_SOURCE_STREAM,
    }


def _mock_ai():
    return patch(
        f"{DETECTION}.async_generate_data",
        new=AsyncMock(
            return_value=GenDataTaskResult(
                conversation_id="test",
                data={"persons": [{"name": "Alice", "confidence": 0.9}]},
            )
        ),
    )


def _mock_still():
    return patch(
        f"{DETECTION}.async_get_image",
        new=AsyncMock(return_value=Image(content_type="image/jpeg", content=STILL)),
    )


def _mock_stream(source: Any = STREAM, frame: bytes | None = STREAM_FRAME):
    source_mock = (
        AsyncMock(side_effect=source)
        if isinstance(source, Exception)
        else AsyncMock(return_value=source)
    )
    return (
        patch(f"{DETECTION}.async_get_stream_source", new=source_mock),
        patch(f"{DETECTION}.ffmpeg.async_get_image", new=AsyncMock(return_value=frame)),
    )


def _tracker_state(hass: HomeAssistant, entry_id: str) -> str:
    entry = hass.config_entries.async_get_entry(entry_id)
    (subentry_id,) = entry.subentries
    entity_id = er.async_get(hass).async_get_entity_id(
        "device_tracker", DOMAIN, subentry_id
    )
    return hass.states.get(entity_id).state


async def _fire_edge(hass: HomeAssistant) -> None:
    hass.states.async_set(TRIGGER, "off")
    hass.states.async_set(TRIGGER, "on")
    await hass.async_block_till_done()


def test_rtsp_goes_over_tcp_and_the_url_is_quoted() -> None:
    """RTSP gets TCP transport; other URLs pass through unchanged."""
    assert _ffmpeg_input(STREAM) == f"-rtsp_transport tcp -i '{STREAM}'"
    assert _ffmpeg_input("http://cam/stream.m3u8") == "http://cam/stream.m3u8"


async def test_stream_frame_is_the_frame(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """A stream camera sends a frame of its stream to the AI, not its still."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")
    source_patch, ffmpeg_patch = _mock_stream()
    # The saved frame is exactly what the AI gets; capture its bytes.
    saved: list[tuple[bytes, str]] = []

    def _capture(hass_, camera_id, content, content_type):
        saved.append((content, content_type))
        return helpers.save_frame(hass_, camera_id, content, content_type)

    with (
        _mock_ai(),
        _mock_still() as still,
        source_patch,
        ffmpeg_patch as grab,
        patch(f"{DETECTION}.save_frame", side_effect=_capture),
    ):
        async with setup_entry(
            hass,
            entry_data(cameras=[_stream_camera()], cooldown_seconds=0),
            people=[person("Alice")],
        ) as entry:
            await _fire_edge(hass)
            assert _tracker_state(hass, entry.entry_id) == "home"

    still.assert_not_awaited()
    grab.assert_awaited_once()
    args, kwargs = grab.call_args
    assert args[1] == f"-rtsp_transport tcp -i '{STREAM}'"
    assert kwargs["extra_cmd"] == const.STREAM_FRAME_FFMPEG_OUTPUT
    assert saved == [(STREAM_FRAME, "image/jpeg")]


@pytest.mark.parametrize(
    ("source", "frame", "reason"),
    [
        (None, STREAM_FRAME, "it has no video stream"),
        (HomeAssistantError("offline"), STREAM_FRAME, "unavailable (offline)"),
        (STREAM, None, "no frame could be read"),
    ],
    ids=["no-stream", "stream-error", "no-frame"],
)
async def test_falls_back_to_the_still_and_warns_once(
    hass: HomeAssistant,
    default_ai_task: None,
    caplog: pytest.LogCaptureFixture,
    source: Any,
    frame: bytes | None,
    reason: str,
) -> None:
    """Without a stream frame the still is used; detection still runs."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")
    source_patch, ffmpeg_patch = _mock_stream(source, frame)

    with _mock_ai(), _mock_still() as still, source_patch, ffmpeg_patch:
        async with setup_entry(
            hass,
            entry_data(cameras=[_stream_camera()], cooldown_seconds=0),
            people=[person("Alice")],
        ) as entry:
            await _fire_edge(hass)
            await _fire_edge(hass)
            assert _tracker_state(hass, entry.entry_id) == "home"

    assert still.await_count == 2
    warnings = [r for r in caplog.records if "instead of a stream frame" in r.message]
    assert len(warnings) == 1
    assert reason in warnings[0].message
    assert STREAM not in caplog.text


async def test_camera_source_never_touches_the_stream(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """The default snapshot source only uses the camera's still."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")
    source_patch, ffmpeg_patch = _mock_stream()

    with _mock_ai(), _mock_still() as still, source_patch as source, ffmpeg_patch:
        async with setup_entry(
            hass, entry_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            await _fire_edge(hass)

    still.assert_awaited_once()
    source.assert_not_awaited()


async def test_reconfigure_sets_the_snapshot_source(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """Editing a camera changes its snapshot source; old entries default."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")
    async with setup_entry(hass) as entry:
        assert (
            entry.runtime_data.cameras[CAMERA].snapshot_source
            == const.SNAPSHOT_SOURCE_CAMERA
        )
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "camera_edit"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {const.CONF_CAMERA: CAMERA}
        )
        assert result["step_id"] == "camera_triggers"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                const.CONF_TRIGGERS: [TRIGGER],
                const.CONF_SNAPSHOT_SOURCE: const.SNAPSHOT_SOURCE_STREAM,
            },
        )
        assert result["reason"] == "reconfigure_successful"
        await hass.async_block_till_done()
        assert entry.data[const.CONF_CAMERAS] == [
            {**_stream_camera(), const.CONF_SNAPSHOT_SIZE: const.SNAPSHOT_SIZE_NATIVE}
        ]
        assert (
            entry.runtime_data.cameras[CAMERA].snapshot_source
            == const.SNAPSHOT_SOURCE_STREAM
        )
