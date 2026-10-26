"""Shared fixtures for person_tracker tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from homeassistant.components.ai_task.const import DATA_PREFERENCES
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.person_tracker import const
from custom_components.person_tracker.models import person_slug

DOMAIN = const.DOMAIN


@pytest.fixture(autouse=True)
async def homeassistant_core(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    """Set up ``homeassistant`` component first.

    Conversation (or ai_task) registers a started-callback that
    reads ``hass.data[DATA_EXPOSED_ENTITIES]``, only once the
    ``homeassistant`` component is set up.
    """
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
        if hass.config_entries.async_get_entry(config_entry_id) is None:
            MockConfigEntry(domain="test", entry_id=config_entry_id).add_to_hass(hass)
        device = dr.async_get(hass).async_get_or_create(
            config_entry_id=config_entry_id,
            identifiers={(DOMAIN, f"cam-device-{entry.entity_id}")},
            name=f"Camera {entry.entity_id}",
        )
        dr.async_get(hass).async_update_device(device.id, area_id=area.id)
        er.async_get(hass).async_update_entity(entry.entity_id, device_id=device.id)


def entry_data(
    *,
    cameras: list[dict[str, Any]] | None = None,
    ai_task_entity: str | None = "ai_task.openai",
    cooldown_seconds: int | None = None,
    min_confidence: float | None = None,
    recheck: bool | None = None,
    move_check: bool | None = None,
) -> dict[str, Any]:
    """Build person_tracker config entry data (the global settings)."""
    data: dict[str, Any] = {
        const.CONF_CAMERAS: cameras
        or [
            {
                const.CONF_CAMERA: "camera.front_door",
                const.CONF_TRIGGERS: ["binary_sensor.front_door_person"],
            }
        ],
        const.CONF_AI_TASK_ENTITY: ai_task_entity,
    }
    if cooldown_seconds is not None:
        data[const.CONF_COOLDOWN] = cooldown_seconds
    if min_confidence is not None:
        data[const.CONF_MIN_CONFIDENCE] = min_confidence
    if recheck is not None:
        data[const.CONF_RECHECK] = recheck
    if move_check is not None:
        data[const.CONF_MOVE_CHECK] = move_check
    return data


def person(
    name: str,
    *,
    ttl: int = 30,
    authoritative_tracker: str | None = None,
    description: str | None = None,
    supporting_area_entity: str | None = None,
    supporting_floor_entity: str | None = None,
) -> ConfigSubentryData:
    """Build a person subentry."""
    data: dict[str, Any] = {const.CONF_PERSON_NAME: name, const.CONF_TTL: ttl}
    if authoritative_tracker:
        data[const.CONF_AUTHORITATIVE_TRACKER] = authoritative_tracker
    if description:
        data[const.CONF_DESCRIPTION] = description
    if supporting_area_entity:
        data[const.CONF_SUPPORTING_AREA_ENTITY] = supporting_area_entity
    if supporting_floor_entity:
        data[const.CONF_SUPPORTING_FLOOR_ENTITY] = supporting_floor_entity
    return ConfigSubentryData(
        data=data,
        subentry_type=const.SUBENTRY_PERSON,
        title=name,
        unique_id=person_slug(name),
    )


def create_entry(
    hass: HomeAssistant,
    data: dict[str, Any] | None = None,
    people: list[ConfigSubentryData] | None = None,
) -> MockConfigEntry:
    """Create (and add to hass) a person_tracker config entry.

    Defaults to the default camera map and one person, Alice.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=data or entry_data(),
        subentries_data=[person("Alice")] if people is None else people,
    )
    entry.add_to_hass(hass)
    return entry


@asynccontextmanager
async def setup_entry(
    hass: HomeAssistant,
    data: dict[str, Any] | None = None,
    people: list[ConfigSubentryData] | None = None,
) -> AsyncGenerator[MockConfigEntry, None]:
    """Create, set up, and tear down a person_tracker config entry."""
    entry = create_entry(hass, data, people)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    yield entry
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
