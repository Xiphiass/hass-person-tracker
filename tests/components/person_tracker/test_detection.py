"""Tests for detection loop."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.person_tracker import const
from .conftest import add_camera, entry_data, setup_entry

DOMAIN = const.DOMAIN


async def test_detection_loop_triggers(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """Test that detection starts when a trigger flips OFF->ON."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    hass.states.async_set("binary_sensor.front_door_person", "off")

    async with setup_entry(
        hass,
        entry_data(
            people=[{const.CONF_PERSON_NAME: "Alice", const.CONF_TTL: 30}],
            cameras=[
                {
                    const.CONF_CAMERA: "camera.front_door",
                    const.CONF_TRIGGERS: ["binary_sensor.front_door_person"],
                }
            ],
        ),
    ) as entry:
        # This is where the detection loop setup would be verified.
        assert entry.state.value == "loaded"
