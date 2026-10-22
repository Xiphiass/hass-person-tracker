"""Tests for the person_tracker config and person subentry flows."""

from __future__ import annotations

from typing import Any

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.person_tracker import const

from .conftest import add_camera, create_entry, entry_data, person, setup_entry

DOMAIN = const.DOMAIN

FRONT_DOOR = {
    const.CONF_CAMERA: "camera.front_door",
    const.CONF_TRIGGERS: ["binary_sensor.front_door_person"],
}
BACK_DOOR = {
    const.CONF_CAMERA: "camera.back_door",
    const.CONF_TRIGGERS: ["binary_sensor.back_door_motion"],
}


@pytest.fixture
def cameras(hass: HomeAssistant) -> None:
    """Two cameras in areas, each with a trigger."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    add_camera(hass, "camera.back_door", device_area_name="Hallway")
    hass.states.async_set("binary_sensor.front_door_person", "off")
    hass.states.async_set("binary_sensor.back_door_motion", "off")


async def _configure(hass: HomeAssistant, result: dict, data: dict) -> dict:
    return await hass.config_entries.flow.async_configure(result["flow_id"], data)


async def _init_user_flow(hass: HomeAssistant) -> dict:
    """Start a fresh user-initiated flow."""
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})


async def _to_camera_form(hass: HomeAssistant, entry: MockConfigEntry) -> dict:
    """Start a reconfigure flow and choose a menu option."""
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.MENU
    return result


# Initial setup: cameras -> AI task.


async def test_flow_creates_entry(
    hass: HomeAssistant, default_ai_task: None, cameras: None
) -> None:
    """The happy path creates an entry; the AI task is prefilled, not implied."""
    result = await _init_user_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await _configure(hass, result, FRONT_DOOR)
    assert result["step_id"] == "cameras_more"
    result = await _configure(hass, result, {const.CONF_ADD_CAMERA: False})
    assert result["step_id"] == "ai_task"
    schema_key = next(iter(result["data_schema"].schema))
    assert schema_key.description == {"suggested_value": "ai_task.openai"}

    result = await _configure(
        hass, result, {const.CONF_AI_TASK_ENTITY: "ai_task.openai"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "cooldown"
    cooldown_key = next(iter(result["data_schema"].schema))
    assert cooldown_key.description == {"suggested_value": 30}

    result = await _configure(hass, result, {const.CONF_COOLDOWN: 45})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Person Tracker"
    assert result["data"] == {
        const.CONF_CAMERAS: [FRONT_DOOR],
        const.CONF_AI_TASK_ENTITY: "ai_task.openai",
        const.CONF_COOLDOWN: 45,
    }
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.data == result["data"]
    assert not entry.subentries


async def test_flow_multiple_cameras(
    hass: HomeAssistant, default_ai_task: None, cameras: None
) -> None:
    """Cameras can be added in a loop."""
    result = await _init_user_flow(hass)
    result = await _configure(hass, result, FRONT_DOOR)
    result = await _configure(hass, result, {const.CONF_ADD_CAMERA: True})
    assert result["step_id"] == "camera_add"
    result = await _configure(hass, result, BACK_DOOR)
    assert result["step_id"] == "cameras_more"
    result = await _configure(hass, result, {const.CONF_ADD_CAMERA: False})
    result = await _configure(
        hass, result, {const.CONF_AI_TASK_ENTITY: "ai_task.openai"}
    )
    result = await _configure(hass, result, {const.CONF_COOLDOWN: 30})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        const.CONF_CAMERAS: [FRONT_DOOR, BACK_DOOR],
        const.CONF_AI_TASK_ENTITY: "ai_task.openai",
        const.CONF_COOLDOWN: 30,
    }


async def test_flow_camera_without_area(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """A camera without an HA area is rejected with a visible error."""
    add_camera(hass, "camera.front_door")  # no area
    hass.states.async_set("binary_sensor.front_door_person", "off")

    result = await _init_user_flow(hass)
    result = await _configure(hass, result, FRONT_DOOR)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {const.CONF_CAMERA: const.ERR_CAMERA_WITHOUT_AREA}


async def test_flow_selectors_restrict_domains(
    hass: HomeAssistant, default_ai_task: None, cameras: None
) -> None:
    """Non-camera cameras and non-binary-sensor triggers fail the form schema."""
    hass.states.async_set("sensor.front_door", "1")
    result = await _init_user_flow(hass)
    with pytest.raises(InvalidData):
        await _configure(hass, result, {**FRONT_DOOR, const.CONF_CAMERA: "sensor.x"})
    with pytest.raises(InvalidData):
        await _configure(hass, result, {**FRONT_DOOR, const.CONF_TRIGGERS: ["s.x"]})


async def test_flow_duplicate_camera(
    hass: HomeAssistant, default_ai_task: None, cameras: None
) -> None:
    """Adding the same camera twice is rejected."""
    result = await _init_user_flow(hass)
    result = await _configure(hass, result, FRONT_DOOR)
    result = await _configure(hass, result, {const.CONF_ADD_CAMERA: True})
    result = await _configure(hass, result, FRONT_DOOR)
    assert result["step_id"] == "camera_add"
    assert result["errors"] == {const.CONF_CAMERA: const.ERR_DUPLICATE_CAMERA}


@pytest.mark.parametrize(
    ("camera", "errors"),
    [
        (
            {**FRONT_DOOR, const.CONF_CAMERA: "camera.ghost"},
            {const.CONF_CAMERA: const.ERR_INVALID_CAMERA},
        ),
        (
            {**FRONT_DOOR, const.CONF_TRIGGERS: ["binary_sensor.ghost"]},
            {const.CONF_TRIGGERS: const.ERR_INVALID_TRIGGER},
        ),
        (
            {**FRONT_DOOR, const.CONF_TRIGGERS: []},
            {const.CONF_TRIGGERS: const.ERR_NO_TRIGGERS},
        ),
    ],
)
async def test_flow_rejects_invalid_camera(
    hass: HomeAssistant,
    default_ai_task: None,
    cameras: None,
    camera: dict[str, Any],
    errors: dict[str, str],
) -> None:
    """Missing cameras/triggers and empty trigger lists are rejected."""
    result = await _init_user_flow(hass)
    result = await _configure(hass, result, camera)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == errors


async def test_flow_no_ai_task_entity_available(
    hass: HomeAssistant, cameras: None
) -> None:
    """Without any AI task entity the flow aborts before asking for cameras."""
    result = await _init_user_flow(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == const.ABORT_NO_AI_TASK_ENTITY


async def test_flow_invalid_ai_task_entity(
    hass: HomeAssistant, default_ai_task: None, cameras: None
) -> None:
    """An AI task entity that does not exist is rejected."""
    result = await _init_user_flow(hass)
    result = await _configure(hass, result, FRONT_DOOR)
    result = await _configure(hass, result, {const.CONF_ADD_CAMERA: False})
    result = await _configure(hass, result, {const.CONF_AI_TASK_ENTITY: "ai_task.gone"})
    assert result["errors"] == {
        const.CONF_AI_TASK_ENTITY: const.ERR_INVALID_AI_TASK_ENTITY
    }


async def test_flow_single_instance_only(
    hass: HomeAssistant, default_ai_task: None, cameras: None
) -> None:
    """A second configuration is aborted."""
    create_entry(hass)
    result = await _init_user_flow(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == const.ABORT_SINGLE_INSTANCE


# Reconfigure the global settings.


async def test_reconfigure_add_camera(
    hass: HomeAssistant, default_ai_task: None, cameras: None
) -> None:
    """A camera added via Reconfigure is stored and the entry reloaded."""
    async with setup_entry(hass) as entry:
        result = await _to_camera_form(hass, entry)
        result = await _configure(hass, result, {"next_step_id": "camera_add"})
        assert result["step_id"] == "camera_add"
        result = await _configure(hass, result, FRONT_DOOR)
        assert result["errors"] == {const.CONF_CAMERA: const.ERR_DUPLICATE_CAMERA}
        result = await _configure(hass, result, BACK_DOOR)
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reconfigure_successful"
        await hass.async_block_till_done()

        assert entry.data[const.CONF_CAMERAS] == [FRONT_DOOR, BACK_DOOR]
        assert entry.state is ConfigEntryState.LOADED
        assert set(entry.runtime_data.cameras) == {
            "camera.front_door",
            "camera.back_door",
        }


async def test_reconfigure_edit_camera_triggers(
    hass: HomeAssistant, default_ai_task: None, cameras: None
) -> None:
    """A camera's triggers can be changed; the form is prefilled."""
    hass.states.async_set("binary_sensor.front_door_motion", "off")
    async with setup_entry(hass) as entry:
        result = await _to_camera_form(hass, entry)
        result = await _configure(hass, result, {"next_step_id": "camera_edit"})
        assert result["step_id"] == "camera_edit"
        result = await _configure(
            hass, result, {const.CONF_CAMERA: "camera.front_door"}
        )
        assert result["step_id"] == "camera_triggers"
        schema_key = next(iter(result["data_schema"].schema))
        assert schema_key.description == {
            "suggested_value": FRONT_DOOR[const.CONF_TRIGGERS]
        }

        result = await _configure(hass, result, {const.CONF_TRIGGERS: []})
        assert result["errors"] == {const.CONF_TRIGGERS: const.ERR_NO_TRIGGERS}

        triggers = ["binary_sensor.front_door_motion"]
        result = await _configure(hass, result, {const.CONF_TRIGGERS: triggers})
        assert result["reason"] == "reconfigure_successful"
        await hass.async_block_till_done()
        assert entry.runtime_data.cameras["camera.front_door"].triggers == triggers


async def test_reconfigure_remove_cameras(
    hass: HomeAssistant, default_ai_task: None, cameras: None
) -> None:
    """Cameras can be removed, but at least one must remain."""
    async with setup_entry(hass, entry_data(cameras=[FRONT_DOOR, BACK_DOOR])) as entry:
        result = await _to_camera_form(hass, entry)
        result = await _configure(hass, result, {"next_step_id": "camera_remove"})
        assert result["step_id"] == "camera_remove"

        result = await _configure(
            hass,
            result,
            {const.CONF_REMOVE_CAMERAS: ["camera.front_door", "camera.back_door"]},
        )
        assert result["errors"] == {const.CONF_REMOVE_CAMERAS: const.ERR_NO_CAMERAS}

        result = await _configure(
            hass, result, {const.CONF_REMOVE_CAMERAS: ["camera.front_door"]}
        )
        assert result["reason"] == "reconfigure_successful"
        await hass.async_block_till_done()
        assert entry.data[const.CONF_CAMERAS] == [BACK_DOOR]
        assert list(entry.runtime_data.cameras) == ["camera.back_door"]


async def test_reconfigure_ai_task(
    hass: HomeAssistant, default_ai_task: None, cameras: None
) -> None:
    """The AI task entity can be changed; the form shows the current one."""
    hass.states.async_set("ai_task.other", "idle")
    async with setup_entry(hass) as entry:
        result = await _to_camera_form(hass, entry)
        result = await _configure(hass, result, {"next_step_id": "ai_task"})
        assert result["step_id"] == "ai_task"
        result = await _configure(
            hass, result, {const.CONF_AI_TASK_ENTITY: "ai_task.other"}
        )
        assert result["reason"] == "reconfigure_successful"
        await hass.async_block_till_done()
        assert entry.runtime_data.ai_task_entity_id == "ai_task.other"

        result = await _to_camera_form(hass, entry)
        result = await _configure(hass, result, {"next_step_id": "ai_task"})
        schema_key = next(iter(result["data_schema"].schema))
        assert schema_key.description == {"suggested_value": "ai_task.other"}


async def test_reconfigure_cooldown(
    hass: HomeAssistant, default_ai_task: None, cameras: None
) -> None:
    """The detection cooldown is a global setting, editable via Reconfigure."""
    async with setup_entry(hass) as entry:
        result = await _to_camera_form(hass, entry)
        result = await _configure(hass, result, {"next_step_id": "cooldown"})
        assert result["step_id"] == "cooldown"
        # No cooldown stored yet: the default is suggested.
        cooldown_key = next(iter(result["data_schema"].schema))
        assert cooldown_key.description == {"suggested_value": 30}

        result = await _configure(hass, result, {const.CONF_COOLDOWN: 120})
        assert result["reason"] == "reconfigure_successful"
        await hass.async_block_till_done()
        assert entry.data[const.CONF_COOLDOWN] == 120
        assert entry.runtime_data.cooldown_seconds == 120

        # The stored value is now suggested.
        result = await _to_camera_form(hass, entry)
        result = await _configure(hass, result, {"next_step_id": "cooldown"})
        cooldown_key = next(iter(result["data_schema"].schema))
        assert cooldown_key.description == {"suggested_value": 120}


async def test_reconfigure_entry_in_setup_error(
    hass: HomeAssistant, cameras: None
) -> None:
    """Reconfigure fixes an entry that failed setup (no listener registered)."""
    async with setup_entry(hass) as entry:
        assert entry.state is ConfigEntryState.SETUP_ERROR
        hass.states.async_set("ai_task.other", "idle")

        result = await _to_camera_form(hass, entry)
        result = await _configure(hass, result, {"next_step_id": "ai_task"})
        result = await _configure(
            hass, result, {const.CONF_AI_TASK_ENTITY: "ai_task.other"}
        )
        assert result["reason"] == "reconfigure_successful"
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED


# People as subentries.


async def _init_person_flow(hass: HomeAssistant, entry: MockConfigEntry) -> dict:
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, const.SUBENTRY_PERSON), context={"source": "user"}
    )


async def _configure_person(hass: HomeAssistant, result: dict, data: dict) -> dict:
    return await hass.config_entries.subentries.async_configure(result["flow_id"], data)


def _tracker_entity_ids(
    hass: HomeAssistant, entry: MockConfigEntry, domain: str = "device_tracker"
) -> list[str]:
    return [
        entity.entity_id
        for entity in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
        if entity.domain == domain
    ]


async def test_add_person(
    hass: HomeAssistant, default_ai_task: None, cameras: None
) -> None:
    """Adding a person creates a subentry and, after reload, its tracker."""
    hass.states.async_set("device_tracker.bob_phone", "home")
    async with setup_entry(hass) as entry:
        result = await _init_person_flow(hass, entry)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"

        result = await _configure_person(
            hass,
            result,
            {
                const.CONF_PERSON_NAME: "Bob",
                const.CONF_TTL: 45,
                const.CONF_AUTHORITATIVE_TRACKER: "device_tracker.bob_phone",
            },
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()

        bob = next(s for s in entry.subentries.values() if s.title == "Bob")
        assert bob.subentry_type == const.SUBENTRY_PERSON
        assert bob.unique_id == "bob"
        assert bob.data == {
            const.CONF_PERSON_NAME: "Bob",
            const.CONF_TTL: 45,
            const.CONF_AUTHORITATIVE_TRACKER: "device_tracker.bob_phone",
        }
        assert entry.runtime_data.person_names == {"Alice", "Bob"}
        assert len(_tracker_entity_ids(hass, entry)) == 2


async def test_add_person_default_ttl(
    hass: HomeAssistant, default_ai_task: None, cameras: None
) -> None:
    """TTL defaults to 30 minutes."""
    async with setup_entry(hass, people=[]) as entry:
        result = await _init_person_flow(hass, entry)
        result = await _configure_person(hass, result, {const.CONF_PERSON_NAME: "Bob"})
        assert result["data"] == {const.CONF_PERSON_NAME: "Bob", const.CONF_TTL: 30}
        # Adding Bob scheduled an entry reload; let it finish so teardown
        # unloads a settled (not mid-setup) entry.
        await hass.async_block_till_done()


@pytest.mark.parametrize(
    "bad_name",
    ["", "  ", "Alice Smith", "Bad/Name", "Bad\\Name", ".", "..", "a" * 101],
)
async def test_add_person_invalid_name(
    hass: HomeAssistant, default_ai_task: None, cameras: None, bad_name: str
) -> None:
    """Invalid person names are rejected with a visible error."""
    entry = create_entry(hass)
    result = await _init_person_flow(hass, entry)
    result = await _configure_person(hass, result, {const.CONF_PERSON_NAME: bad_name})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {const.CONF_PERSON_NAME: const.ERR_INVALID_PERSON_NAME}


@pytest.mark.parametrize("name", ["Alice", "alice", "ALICE"])
async def test_add_person_duplicate_name(
    hass: HomeAssistant, default_ai_task: None, cameras: None, name: str
) -> None:
    """Adding the same person twice is rejected, regardless of case."""
    entry = create_entry(hass)
    result = await _init_person_flow(hass, entry)
    result = await _configure_person(hass, result, {const.CONF_PERSON_NAME: name})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {const.CONF_PERSON_NAME: const.ERR_UNIQUE_PERSON}


async def test_add_person_invalid_authoritative_tracker(
    hass: HomeAssistant, default_ai_task: None, cameras: None
) -> None:
    """An authoritative tracker must be an existing device_tracker."""
    hass.states.async_set("sensor.not_a_tracker", "1")
    entry = create_entry(hass)
    result = await _init_person_flow(hass, entry)
    # The entity selector restricts the domain, so the form schema rejects it.
    with pytest.raises(InvalidData):
        await _configure_person(
            hass,
            result,
            {
                const.CONF_PERSON_NAME: "Bob",
                const.CONF_AUTHORITATIVE_TRACKER: "sensor.not_a_tracker",
            },
        )
    # A device_tracker that does not exist passes the schema but not validation.
    result = await _configure_person(
        hass,
        result,
        {
            const.CONF_PERSON_NAME: "Bob",
            const.CONF_AUTHORITATIVE_TRACKER: "device_tracker.gone",
        },
    )
    assert result["errors"] == {
        const.CONF_AUTHORITATIVE_TRACKER: const.ERR_INVALID_AUTHORITATIVE_TRACKER
    }


async def test_reconfigure_person_rename_keeps_entity(
    hass: HomeAssistant, default_ai_task: None, cameras: None
) -> None:
    """Renaming a person updates the subentry and keeps its tracker entity."""
    async with setup_entry(hass) as entry:
        (alice_id,) = entry.subentries
        (entity_id,) = _tracker_entity_ids(hass, entry)
        (image_entity_id,) = _tracker_entity_ids(hass, entry, "image")

        result = await entry.start_subentry_reconfigure_flow(hass, alice_id)
        assert result["step_id"] == "reconfigure"
        name_key = next(iter(result["data_schema"].schema))
        assert name_key.description == {"suggested_value": "Alice"}

        result = await _configure_person(
            hass, result, {const.CONF_PERSON_NAME: "Alicia", const.CONF_TTL: 60}
        )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reconfigure_successful"
        await hass.async_block_till_done()

        subentry = entry.subentries[alice_id]
        assert subentry.title == "Alicia"
        assert subentry.unique_id == "alicia"
        assert subentry.data == {const.CONF_PERSON_NAME: "Alicia", const.CONF_TTL: 60}
        assert entry.runtime_data.people[alice_id].name == "Alicia"
        assert _tracker_entity_ids(hass, entry) == [entity_id]
        assert _tracker_entity_ids(hass, entry, "image") == [image_entity_id]
        device = dr.async_get(hass).async_get_device_by_identifier(
            (DOMAIN, alice_id), entry.entry_id
        )
        assert device is not None
        assert device.name == "Alicia"


async def test_reconfigure_person_keeps_own_name(
    hass: HomeAssistant, default_ai_task: None, cameras: None
) -> None:
    """A person may keep their name; other people's names stay taken."""
    entry = create_entry(hass, people=[person("Alice"), person("Bob")])
    alice_id = next(
        s.subentry_id for s in entry.subentries.values() if s.title == "Alice"
    )
    result = await entry.start_subentry_reconfigure_flow(hass, alice_id)
    result = await _configure_person(hass, result, {const.CONF_PERSON_NAME: "bob"})
    assert result["errors"] == {const.CONF_PERSON_NAME: const.ERR_UNIQUE_PERSON}
    result = await _configure_person(
        hass, result, {const.CONF_PERSON_NAME: "Alice", const.CONF_TTL: 10}
    )
    assert result["reason"] == "reconfigure_successful"
    assert entry.subentries[alice_id].data[const.CONF_TTL] == 10


async def test_remove_person_removes_device_and_entity(
    hass: HomeAssistant, default_ai_task: None, cameras: None
) -> None:
    """Deleting a person subentry removes their device and entities."""
    async with setup_entry(hass, people=[person("Alice"), person("Bob")]) as entry:
        bob_id = next(
            s.subentry_id for s in entry.subentries.values() if s.title == "Bob"
        )
        assert len(_tracker_entity_ids(hass, entry)) == 2
        assert len(_tracker_entity_ids(hass, entry, "image")) == 2

        assert hass.config_entries.async_remove_subentry(entry, bob_id)
        await hass.async_block_till_done()

        assert len(_tracker_entity_ids(hass, entry)) == 1
        assert len(_tracker_entity_ids(hass, entry, "image")) == 1
        assert (
            dr.async_get(hass).async_get_device_by_identifier(
                (DOMAIN, bob_id), entry.entry_id
            )
            is None
        )
        assert entry.runtime_data.person_names == {"Alice"}
