"""Shared fixtures for person_tracker tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from homeassistant.components.ai_task.const import DATA_PREFERENCES
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from tests.common import MockConfigEntry, get_test_config_dir

from custom_components.person_tracker import const

DOMAIN = const.DOMAIN


@pytest.fixture(autouse=True)
async def homeassistant_core(hass: HomeAssistant) -> None:
    """Set up ``homeassistant`` component first.

    Conversation (or ai_task) registers a started-callback that
    reads ``hass.data[DATA_EXPOSED_ENTITIES]``, only once the
    ``homeassistant`` component is set up.
    """
    # Force the test harness to use our custom components path
    hass.config.config_dir = get_test_config_dir()
    assert await async_setup_component(hass, "homeassistant", {})


@pytest.fixture
async def default_ai_task(hass: HomeAssistant) -> None:
    """Set up the ai_task integration with a default generate-data entity."""
    assert await async_setup_component(hass, "ai_task", {})
    hass.states.async_set("ai_task.openai", "idle")
    hass.data[DATA_PREFERENCES].async_set_preferences(
        gen_data_entity_id="ai_task.openai"
    )


def add_camera(
    hass: HomeAssistant,
    entity_id: str,
    *,
    area_name: str | None = None,
    device_area_name: str | None = None,
    config_entry_id: str = "test-config-entry",
) -> None:
    """Create a camera state, optionally assigning it to an HA area.

    The area can be assigned to the entity itself or to a device the entity
    belongs to.
    """
    domain, object_id = entity_id.split(".", 1)
    entry = er.async_get(hass).async_get_or_create(
        domain,
        "test",
        unique_id=entity_id,
        suggested_object_id=object_id,
    )
    hass.states.async_set(entry.entity_id, "idle")
    assert entry.entity_id == entity_id
    if area_name:
        area = ar.async_get(hass).async_get_or_create(area_name)
        er.async_get(hass).async_update_entity(entry.entity_id, area_id=area.id)
    elif device_area_name:
        area = ar.async_get(hass).async_get_or_create(device_area_name)
        device = dr.async_get(hass).async_get_or_create(
            config_entry_id=config_entry_id,
            identifiers={(DOMAIN, f"cam-device-{entry.entity_id}")},
            name=f"Camera {entry.entity_id}",
        )
        dr.async_get(hass).async_update_device(device.id, area_id=area.id)
        er.async_get(hass).async_update_entity(entry.entity_id, device_id=device.id)


def entry_data(
    *,
    people: list[dict[str, Any]] | None = None,
    cameras: list[dict[str, Any]] | None = None,
    ai_task_entity: str | None = None,
) -> dict[str, Any]:
    """Build person_tracker config entry data."""
    return {
        const.CONF_PEOPLE: people
        or [{const.CONF_PERSON_NAME: "Alice", const.CONF_TTL: 30}],
        const.CONF_CAMERAS: cameras
        or [
            {
                const.CONF_CAMERA: "camera.front_door",
                const.CONF_TRIGGERS: ["binary_sensor.front_door_person"],
            }
        ],
        const.CONF_AI_TASK_ENTITY: ai_task_entity,
    }


def create_entry(hass: HomeAssistant, data: dict[str, Any]) -> MockConfigEntry:
    """Create (and add to hass) a person_tracker config entry."""
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)
    return entry


@asynccontextmanager
async def setup_entry(
    hass: HomeAssistant, data: dict[str, Any]
) -> AsyncGenerator[MockConfigEntry, None]:
    """Create, set up, and tear down a person_tracker config entry."""
    entry = create_entry(hass, data)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    yield entry
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
