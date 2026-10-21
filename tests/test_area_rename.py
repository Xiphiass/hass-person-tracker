"""Tests for live area renames (Issue #6 / ADR 0004).

Renaming an HA area must be reflected in the tracker state it refers to,
without a restart or reconfiguration: the state property resolves the area
live, and the entity re-publishes on ``EVENT_AREA_REGISTRY_UPDATED``.
"""

from __future__ import annotations

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.util import dt as dt_util

from .conftest import add_camera, person, setup_entry
from .test_state_lifetime import _sight, _tracker_state


async def test_area_registry_rename_event(hass: HomeAssistant) -> None:
    """Renaming an area fires ``EVENT_AREA_REGISTRY_UPDATED`` with the ID."""
    registry = ar.async_get(hass)
    area = registry.async_get_or_create("Old Name")

    renamed: list[str] = []

    @callback
    def listener(event: Event[ar.EventAreaRegistryUpdatedData]) -> None:
        if event.data.get("action") == "update":
            renamed.append(str(event.data.get("area_id")))

    unsub = hass.bus.async_listen(ar.EVENT_AREA_REGISTRY_UPDATED, listener)

    registry.async_update(area.id, name="New Name")
    await hass.async_block_till_done()

    unsub()
    assert str(area.id) in renamed
    assert registry.async_get_area(area.id).name == "New Name"


async def test_tracker_state_reflects_area_rename(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """Renaming the area a person was last seen in flips the tracker state."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    garden = ar.async_get(hass).async_get_or_create("Garden")

    async with setup_entry(hass, people=[person("Alice")]) as entry:
        _sight(hass, entry, "Alice", dt_util.now(), garden.id)
        await hass.async_block_till_done()
        assert _tracker_state(hass, "Alice") == "Garden"

        ar.async_get(hass).async_update(garden.id, name="Garden Party")
        await hass.async_block_till_done()
        assert _tracker_state(hass, "Alice") == "Garden Party"
