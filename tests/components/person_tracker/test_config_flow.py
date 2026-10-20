"""Tests for the person_tracker config flow."""

from __future__ import annotations

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData

from custom_components.person_tracker import const

from .conftest import add_camera, entry_data

DOMAIN = const.DOMAIN


async def _init_user_flow(hass: HomeAssistant) -> dict:
    """Start a fresh user-initiated flow."""
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})


async def test_flow_creates_entry_with_default_ai_task(
    hass: HomeAssistant, enable_custom_integrations: None, default_ai_task: None
) -> None:
    """The full happy path creates an entry using the HA default AI task."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    hass.states.async_set("binary_sensor.front_door_person", "off")

    result = await _init_user_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_PERSON_NAME: "Alice", const.CONF_TTL: 30}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "person_more"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_ADD_PERSON: False}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "camera_first"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            const.CONF_CAMERA: "camera.front_door",
            const.CONF_TRIGGERS: ["binary_sensor.front_door_person"],
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "cameras_more"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_ADD_CAMERA: False}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "ai_task"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Person Tracker"
    assert result["data"] == {
        const.CONF_PEOPLE: [{const.CONF_PERSON_NAME: "Alice", const.CONF_TTL: 30}],
        const.CONF_CAMERAS: [
            {
                const.CONF_CAMERA: "camera.front_door",
                const.CONF_TRIGGERS: ["binary_sensor.front_door_person"],
            }
        ],
        const.CONF_AI_TASK_ENTITY: None,
    }

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.data == result["data"]


async def test_flow_multiple_people_and_cameras(
    hass: HomeAssistant, enable_custom_integrations: None, default_ai_task: None
) -> None:
    """People and cameras can be added in loops; TTL defaults to 30."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    add_camera(hass, "camera.back_door", device_area_name="Hallway")
    hass.states.async_set("binary_sensor.front_door_person", "off")
    hass.states.async_set("binary_sensor.back_door_motion", "off")
    hass.states.async_set("device_tracker.alice_phone", "home")

    result = await _init_user_flow(hass)

    # Person 1 (with authoritative tracker).
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            const.CONF_PERSON_NAME: "Alice",
            const.CONF_TTL: 45,
            const.CONF_AUTHORITATIVE_TRACKER: "device_tracker.alice_phone",
        },
    )
    assert result["step_id"] == "person_more"
    # Person 2 (default TTL).
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_ADD_PERSON: True}
    )
    assert result["step_id"] == "person_add"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_PERSON_NAME: "BobSmith"}
    )
    assert result["step_id"] == "person_more"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_ADD_PERSON: False}
    )
    assert result["step_id"] == "camera_first"

    # Camera 1.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            const.CONF_CAMERA: "camera.front_door",
            const.CONF_TRIGGERS: ["binary_sensor.front_door_person"],
        },
    )
    assert result["step_id"] == "cameras_more"
    # Camera 2 (device-level area).
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_ADD_CAMERA: True}
    )
    assert result["step_id"] == "camera_add"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            const.CONF_CAMERA: "camera.back_door",
            const.CONF_TRIGGERS: ["binary_sensor.back_door_motion"],
        },
    )
    assert result["step_id"] == "cameras_more"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_ADD_CAMERA: False}
    )
    assert result["step_id"] == "ai_task"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][const.CONF_PEOPLE] == [
        {
            const.CONF_PERSON_NAME: "Alice",
            const.CONF_TTL: 45,
            const.CONF_AUTHORITATIVE_TRACKER: "device_tracker.alice_phone",
        },
        {const.CONF_PERSON_NAME: "BobSmith", const.CONF_TTL: 30},
    ]
    assert result["data"][const.CONF_CAMERAS] == [
        {
            const.CONF_CAMERA: "camera.front_door",
            const.CONF_TRIGGERS: ["binary_sensor.front_door_person"],
        },
        {
            const.CONF_CAMERA: "camera.back_door",
            const.CONF_TRIGGERS: ["binary_sensor.back_door_motion"],
        },
    ]


async def test_flow_ai_task_override_stored(
    hass: HomeAssistant, enable_custom_integrations: None, default_ai_task: None
) -> None:
    """An explicitly selected AI task entity is stored in the entry data."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    hass.states.async_set("binary_sensor.front_door_person", "off")
    hass.states.async_set("ai_task.openai", "idle")

    result = await _init_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_PERSON_NAME: "Alice"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_ADD_PERSON: False}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            const.CONF_CAMERA: "camera.front_door",
            const.CONF_TRIGGERS: ["binary_sensor.front_door_person"],
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_ADD_CAMERA: False}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_AI_TASK_ENTITY: "ai_task.openai"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][const.CONF_AI_TASK_ENTITY] == "ai_task.openai"


@pytest.mark.parametrize(
    ("bad_name"),
    ["", "  ", "Alice Smith", "Bad/Name", "Bad\\Name", ".", "..", "a" * 101],
)
async def test_flow_invalid_person_name(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    default_ai_task: None,
    bad_name: str,
) -> None:
    """Invalid person names are rejected with a visible error."""
    result = await _init_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_PERSON_NAME: bad_name}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert const.ERR_INVALID_PERSON_NAME in result["errors"]
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_flow_duplicate_person_name(
    hass: HomeAssistant, enable_custom_integrations: None, default_ai_task: None
) -> None:
    """Adding the same person twice is rejected."""
    result = await _init_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_PERSON_NAME: "Alice"}
    )
    assert result["step_id"] == "person_more"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_ADD_PERSON: True}
    )
    assert result["step_id"] == "person_add"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_PERSON_NAME: "Alice"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "person_add"
    assert const.ERR_UNIQUE_PERSON in result["errors"]


async def test_flow_invalid_authoritative_tracker(
    hass: HomeAssistant, enable_custom_integrations: None, default_ai_task: None
) -> None:
    """An authoritative tracker that is not a device_tracker is rejected."""
    hass.states.async_set("sensor.not_a_tracker", "1")
    result = await _init_user_flow(hass)
    # The entity selector restricts the domain, so the form schema rejects it.
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                const.CONF_PERSON_NAME: "Alice",
                const.CONF_AUTHORITATIVE_TRACKER: "sensor.not_a_tracker",
            },
        )

    # A device_tracker that does not exist passes the schema but not validation.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            const.CONF_PERSON_NAME: "Alice",
            const.CONF_AUTHORITATIVE_TRACKER: "device_tracker.gone",
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert const.ERR_INVALID_AUTHORITATIVE_TRACKER in result["errors"]


async def test_flow_camera_without_area(
    hass: HomeAssistant, enable_custom_integrations: None, default_ai_task: None
) -> None:
    """A camera without an HA area is rejected in the flow with a visible error."""
    add_camera(hass, "camera.front_door")  # no area
    hass.states.async_set("binary_sensor.front_door_person", "off")

    result = await _init_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_PERSON_NAME: "Alice"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_ADD_PERSON: False}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            const.CONF_CAMERA: "camera.front_door",
            const.CONF_TRIGGERS: ["binary_sensor.front_door_person"],
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "camera_first"
    assert const.ERR_CAMERA_WITHOUT_AREA in result["errors"]


async def test_flow_camera_must_be_camera_entity(
    hass: HomeAssistant, enable_custom_integrations: None, default_ai_task: None
) -> None:
    """Selecting a non-camera entity is rejected."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    hass.states.async_set("sensor.front_door", "1")
    hass.states.async_set("binary_sensor.front_door_person", "off")

    result = await _init_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_PERSON_NAME: "Alice"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_ADD_PERSON: False}
    )
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                const.CONF_CAMERA: "sensor.front_door",
                const.CONF_TRIGGERS: ["binary_sensor.front_door_person"],
            },
        )


async def test_flow_duplicate_camera(
    hass: HomeAssistant, enable_custom_integrations: None, default_ai_task: None
) -> None:
    """Adding the same camera twice is rejected."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    hass.states.async_set("binary_sensor.front_door_person", "off")

    result = await _init_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_PERSON_NAME: "Alice"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_ADD_PERSON: False}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            const.CONF_CAMERA: "camera.front_door",
            const.CONF_TRIGGERS: ["binary_sensor.front_door_person"],
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_ADD_CAMERA: True}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            const.CONF_CAMERA: "camera.front_door",
            const.CONF_TRIGGERS: ["binary_sensor.front_door_person"],
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert const.ERR_DUPLICATE_CAMERA in result["errors"]


async def test_flow_trigger_must_be_binary_sensor(
    hass: HomeAssistant, enable_custom_integrations: None, default_ai_task: None
) -> None:
    """Triggers that are not binary sensors are rejected."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    hass.states.async_set("sensor.not_a_sensor", "1")

    result = await _init_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_PERSON_NAME: "Alice"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_ADD_PERSON: False}
    )
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                const.CONF_CAMERA: "camera.front_door",
                const.CONF_TRIGGERS: ["sensor.not_a_sensor"],
            },
        )


async def test_flow_camera_needs_at_least_one_trigger(
    hass: HomeAssistant, enable_custom_integrations: None, default_ai_task: None
) -> None:
    """A camera with no triggers is rejected."""
    add_camera(hass, "camera.front_door", area_name="Garden")

    result = await _init_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_PERSON_NAME: "Alice"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_ADD_PERSON: False}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {const.CONF_CAMERA: "camera.front_door", const.CONF_TRIGGERS: []},
    )
    assert result["type"] is FlowResultType.FORM
    assert const.ERR_NO_TRIGGERS in result["errors"]


async def test_flow_no_ai_task_entity_available(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    """No default AI task entity and no override fails the ai_task step."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    hass.states.async_set("binary_sensor.front_door_person", "off")

    result = await _init_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_PERSON_NAME: "Alice"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_ADD_PERSON: False}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            const.CONF_CAMERA: "camera.front_door",
            const.CONF_TRIGGERS: ["binary_sensor.front_door_person"],
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_ADD_CAMERA: False}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "ai_task"
    assert const.ERR_NO_AI_TASK_ENTITY in result["errors"]
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_flow_invalid_ai_task_entity(
    hass: HomeAssistant, enable_custom_integrations: None, default_ai_task: None
) -> None:
    """An AI task override that does not exist is rejected."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    hass.states.async_set("binary_sensor.front_door_person", "off")

    result = await _init_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_PERSON_NAME: "Alice"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_ADD_PERSON: False}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            const.CONF_CAMERA: "camera.front_door",
            const.CONF_TRIGGERS: ["binary_sensor.front_door_person"],
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_ADD_CAMERA: False}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_AI_TASK_ENTITY: "ai_task.gone"}
    )
    assert result["type"] is FlowResultType.FORM
    assert const.ERR_INVALID_AI_TASK_ENTITY in result["errors"]


async def test_flow_single_instance_only(
    hass: HomeAssistant, enable_custom_integrations: None, default_ai_task: None
) -> None:
    """A second configuration is aborted."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    hass.states.async_set("binary_sensor.front_door_person", "off")

    from .conftest import create_entry, setup_entry

    async with setup_entry(hass, entry_data()) as entry:
        assert entry.state.name == "LOADED"

    result = await _init_user_flow(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == const.ABORT_SINGLE_INSTANCE
