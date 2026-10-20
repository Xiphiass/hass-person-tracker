"""Tests for person_tracker entry setup."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Generator
from datetime import timedelta
from pathlib import Path

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_RESTORED, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType
from tests.common import MockConfigEntry

from custom_components.person_tracker import const
from custom_components.person_tracker.models import PersonTrackerData

from .conftest import add_camera, entry_data, setup_entry

DOMAIN = const.DOMAIN
LOGGER_NAME = f"custom_components.{DOMAIN}"


@pytest.fixture
def reference_images_dir(hass: HomeAssistant) -> Generator[Path]:
    """Provide an empty reference-images root, removed after the test."""
    root = Path(hass.config.path(DOMAIN))
    people = root / "people"
    people.mkdir(parents=True)
    yield people
    shutil.rmtree(root)


async def test_setup_creates_device_and_entity_per_person(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """Each person gets a SERVICE device and one device_tracker entity."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    hass.states.async_set("binary_sensor.front_door_person", "off")

    async with setup_entry(
        hass,
        entry_data(
            people=[
                {const.CONF_PERSON_NAME: "Alice", const.CONF_TTL: 30},
                {const.CONF_PERSON_NAME: "BobSmith", const.CONF_TTL: 45},
            ]
        ),
    ) as entry:
        assert entry.state is ConfigEntryState.LOADED

        devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
        alice = next(device for device in devices if device.name == "Alice")
        bob = next(device for device in devices if device.name == "BobSmith")
        assert {alice.entry_type, bob.entry_type} == {DeviceEntryType.SERVICE}
        assert alice.config_entry_id == entry.entry_id
        assert bob.config_entry_id == entry.entry_id

        entities = er.async_get(hass)
        alice_entity = entities.async_get_entity_id(
            "device_tracker", DOMAIN, f"{entry.entry_id}:alice"
        )
        bob_entity = entities.async_get_entity_id(
            "device_tracker", DOMAIN, f"{entry.entry_id}:bobsmith"
        )
        assert alice_entity is not None
        assert bob_entity is not None
        assert entities.async_get(alice_entity).device_id == alice.id
        assert entities.async_get(bob_entity).device_id == bob.id

        # Entities are registered but have no data yet: state is unknown.
        assert hass.states.get(alice_entity).state == "unknown"
        assert hass.states.get(bob_entity).state == "unknown"

        data = hass.data[DOMAIN][entry.entry_id]
        assert isinstance(data, PersonTrackerData)
        assert data.person_names == {"Alice", "BobSmith"}
        assert data.ai_task_entity_id == "ai_task.openai"
        assert data.cameras["camera.front_door"].triggers == [
            "binary_sensor.front_door_person"
        ]


async def test_setup_fails_when_no_ai_task_available(
    hass: HomeAssistant,
) -> None:
    """Without any AI task entity the entry fails to set up with a reason."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    hass.states.async_set("binary_sensor.front_door_person", "off")

    entry = MockConfigEntry(domain=DOMAIN, data=entry_data())
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.reason == const.ERR_NO_AI_TASK_ENTITY
    assert not hass.states.async_entity_ids("device_tracker")

    await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_fails_on_invalid_ai_task_entity(
    hass: HomeAssistant,
) -> None:
    """An AI task override pointing at a non-existent entity fails setup."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    hass.states.async_set("binary_sensor.front_door_person", "off")

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=entry_data(ai_task_entity="ai_task.gone"),
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.reason == const.ERR_INVALID_AI_TASK_ENTITY

    await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_uses_default_ai_task(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """No override in the entry data resolves to the HA default AI task."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    hass.states.async_set("binary_sensor.front_door_person", "off")

    async with setup_entry(hass, entry_data()) as entry:
        data = hass.data[DOMAIN][entry.entry_id]
        assert data.ai_task_entity_id == "ai_task.openai"


async def test_setup_skips_camera_without_area(
    hass: HomeAssistant, default_ai_task: None, caplog: pytest.LogCaptureFixture
) -> None:
    """Cameras without an HA area are skipped at setup with a visible error."""
    add_camera(hass, "camera.front_door")  # no area
    add_camera(hass, "camera.back_door", area_name="Hallway")
    hass.states.async_set("binary_sensor.front_door_person", "off")
    hass.states.async_set("binary_sensor.back_door_motion", "off")

    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    async with setup_entry(
        hass,
        entry_data(
            cameras=[
                {
                    const.CONF_CAMERA: "camera.front_door",
                    const.CONF_TRIGGERS: ["binary_sensor.front_door_person"],
                },
                {
                    const.CONF_CAMERA: "camera.back_door",
                    const.CONF_TRIGGERS: ["binary_sensor.back_door_motion"],
                },
            ]
        ),
    ) as entry:
        assert entry.state is ConfigEntryState.LOADED
        data = hass.data[DOMAIN][entry.entry_id]
        assert list(data.cameras) == ["camera.back_door"]
        assert any("camera.front_door" in record.message for record in caplog.records)


async def test_setup_skips_invalid_triggers(
    hass: HomeAssistant, default_ai_task: None, caplog: pytest.LogCaptureFixture
) -> None:
    """Trigger states that are not binary sensors are dropped at setup."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    hass.states.async_set("binary_sensor.good", "off")
    hass.states.async_set("sensor.bad", "1")

    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    async with setup_entry(
        hass,
        entry_data(
            cameras=[
                {
                    const.CONF_CAMERA: "camera.front_door",
                    const.CONF_TRIGGERS: ["binary_sensor.good", "sensor.bad"],
                }
            ]
        ),
    ) as entry:
        assert entry.state is ConfigEntryState.LOADED
        data = hass.data[DOMAIN][entry.entry_id]
        assert data.cameras["camera.front_door"].triggers == ["binary_sensor.good"]
        assert any("sensor.bad" in record.message for record in caplog.records)


async def test_setup_skips_camera_with_no_valid_triggers(
    hass: HomeAssistant, default_ai_task: None, caplog: pytest.LogCaptureFixture
) -> None:
    """A camera whose triggers are all invalid is skipped entirely."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    hass.states.async_set("sensor.bad", "1")

    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    async with setup_entry(
        hass,
        entry_data(
            cameras=[
                {
                    const.CONF_CAMERA: "camera.front_door",
                    const.CONF_TRIGGERS: ["sensor.bad"],
                }
            ]
        ),
    ) as entry:
        assert entry.state is ConfigEntryState.LOADED
        data = hass.data[DOMAIN][entry.entry_id]
        assert data.cameras == {}


async def test_setup_discovers_reference_images(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path
) -> None:
    """Reference images in the person's folder are discovered at setup time."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    hass.states.async_set("binary_sensor.front_door_person", "off")

    alice_dir = reference_images_dir / "Alice"
    bob_dir = reference_images_dir / "Bob"
    alice_dir.mkdir()
    bob_dir.mkdir()
    for filename in ("a.png", "photo.jpg", "notes.txt"):
        (alice_dir / filename).touch()
    (bob_dir / "face.png").touch()
    # Carol has no folder yet.

    async with setup_entry(
        hass,
        entry_data(
            people=[
                {const.CONF_PERSON_NAME: "Alice", const.CONF_TTL: 30},
                {const.CONF_PERSON_NAME: "Bob", const.CONF_TTL: 30},
                {const.CONF_PERSON_NAME: "Carol", const.CONF_TTL: 30},
            ]
        ),
    ) as entry:
        data = hass.data[DOMAIN][entry.entry_id]
        alice_images = [path.name for path in data.people["Alice"].reference_images]
        assert alice_images == ["a.png", "photo.jpg"]
        assert [path.name for path in data.people["Bob"].reference_images] == [
            "face.png"
        ]
        # Person with no folder yet.
        assert data.people["Carol"].reference_images == []
        assert data.people["Carol"].last_seen is None
        assert data.people["Carol"].area_id is None


async def test_reference_images_reload_picks_up_new_files(
    hass: HomeAssistant, default_ai_task: None, reference_images_dir: Path
) -> None:
    """Reference images are re-discovered when the entry is reloaded."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    hass.states.async_set("binary_sensor.front_door_person", "off")

    alice_dir = reference_images_dir / "Alice"
    alice_dir.mkdir()
    first = alice_dir / "first.png"
    first.touch()

    async with setup_entry(hass, entry_data()) as entry:
        data = hass.data[DOMAIN][entry.entry_id]
        assert [p.name for p in data.people["Alice"].reference_images] == ["first.png"]

        # Add another image, remove the first, then reload.
        (alice_dir / "second.jpg").touch()
        first.unlink()
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

        data = hass.data[DOMAIN][entry.entry_id]
        assert [p.name for p in data.people["Alice"].reference_images] == ["second.jpg"]


async def test_unload_removes_entities(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """Unloading the entry removes the tracker entities and their data."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    hass.states.async_set("binary_sensor.front_door_person", "off")

    entry = MockConfigEntry(domain=DOMAIN, data=entry_data())
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert hass.data[DOMAIN][entry.entry_id] is not None

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert entry.entry_id not in hass.data[DOMAIN]
    # Registered entities stay in the state machine as unavailable.
    assert all(
        state.state == STATE_UNAVAILABLE
        for state in hass.states.async_all("device_tracker")
    )


async def test_ttl_field_is_parsed(hass: HomeAssistant, default_ai_task: None) -> None:
    """Person TTL is stored as a timedelta on the runtime model."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    hass.states.async_set("binary_sensor.front_door_person", "off")

    async with setup_entry(hass, entry_data()) as entry:
        data = hass.data[DOMAIN][entry.entry_id]
        assert data.people["Alice"].ttl == timedelta(minutes=30)
        assert data.people["Alice"].authoritative_tracker is None


@pytest.mark.parametrize("restored", [False, True])
async def test_setup_retries_while_camera_not_loaded(
    hass: HomeAssistant, default_ai_task: None, restored: bool
) -> None:
    """A registered camera without a live state yet makes setup retry."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    hass.states.async_set("binary_sensor.front_door_person", "off")
    if restored:
        hass.states.async_set(
            "camera.front_door", STATE_UNAVAILABLE, {ATTR_RESTORED: True}
        )
    else:
        hass.states.async_remove("camera.front_door")

    entry = MockConfigEntry(domain=DOMAIN, data=entry_data())
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert "camera.front_door" in entry.reason

    await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_retries_while_ai_task_not_loaded(hass: HomeAssistant) -> None:
    """A registered AI task override without a state yet makes setup retry."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    add_camera(hass, "ai_task.openai")
    hass.states.async_remove("ai_task.openai")
    hass.states.async_set("binary_sensor.front_door_person", "off")

    entry = MockConfigEntry(
        domain=DOMAIN, data=entry_data(ai_task_entity="ai_task.openai")
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY

    await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_skips_person_with_colliding_slug(
    hass: HomeAssistant, default_ai_task: None, caplog: pytest.LogCaptureFixture
) -> None:
    """Names that differ only in case share a slug; the later one is skipped."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    hass.states.async_set("binary_sensor.front_door_person", "off")

    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    async with setup_entry(
        hass,
        entry_data(
            people=[
                {const.CONF_PERSON_NAME: "Alice", const.CONF_TTL: 30},
                {const.CONF_PERSON_NAME: "alice", const.CONF_TTL: 30},
            ]
        ),
    ) as entry:
        assert entry.state is ConfigEntryState.LOADED
        assert hass.data[DOMAIN][entry.entry_id].person_names == {"Alice"}
        assert any("'alice'" in record.message for record in caplog.records)
