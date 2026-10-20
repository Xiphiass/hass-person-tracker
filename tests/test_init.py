"""Tests for person_tracker entry setup."""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_RESTORED, STATE_UNAVAILABLE
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType

from custom_components.person_tracker import const
from custom_components.person_tracker.helpers import get_camera_area
from custom_components.person_tracker.models import Person, PersonTrackerData

from .conftest import add_camera, create_entry, entry_data, person, setup_entry

DOMAIN = const.DOMAIN
LOGGER_NAME = f"custom_components.{DOMAIN}"


def _by_name(data: PersonTrackerData) -> dict[str, Person]:
    """Index the runtime people by name."""
    return {person.name: person for person in data.people.values()}


@pytest.fixture
def reference_images_dir(hass: HomeAssistant, tmp_path: Path) -> Path:
    """Point the HA config dir at *tmp_path*; return the reference-images root."""
    hass.config.config_dir = str(tmp_path)
    people = tmp_path / DOMAIN / "people"
    people.mkdir(parents=True)
    return people


async def test_setup_creates_device_and_entity_per_person(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """Each person gets a SERVICE device and one device_tracker entity."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    hass.states.async_set("binary_sensor.front_door_person", "off")

    async with setup_entry(
        hass,
        entry_data(),
        people=[person("Alice"), person("BobSmith", ttl=45)],
    ) as entry:
        assert entry.state is ConfigEntryState.LOADED

        devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
        alice = next(device for device in devices if device.name == "Alice")
        bob = next(device for device in devices if device.name == "BobSmith")
        assert {alice.entry_type, bob.entry_type} == {DeviceEntryType.SERVICE}
        assert alice.config_entry_id == entry.entry_id
        assert bob.config_entry_id == entry.entry_id

        subentry_ids = {s.title: s.subentry_id for s in entry.subentries.values()}
        assert alice.config_subentry_id == subentry_ids["Alice"]

        # Entities are keyed on the person's subentry ID, stable across renames.
        entities = er.async_get(hass)
        alice_entity = entities.async_get_entity_id(
            "device_tracker", DOMAIN, subentry_ids["Alice"]
        )
        bob_entity = entities.async_get_entity_id(
            "device_tracker", DOMAIN, subentry_ids["BobSmith"]
        )
        assert alice_entity is not None
        assert bob_entity is not None
        assert entities.async_get(alice_entity).device_id == alice.id
        assert entities.async_get(bob_entity).device_id == bob.id

        # Entities are registered but have no data yet: state is unknown.
        assert hass.states.get(alice_entity).state == "unknown"
        assert hass.states.get(bob_entity).state == "unknown"

        data = entry.runtime_data
        assert isinstance(data, PersonTrackerData)
        assert data.person_names == {"Alice", "BobSmith"}
        assert set(data.people) == set(subentry_ids.values())
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

    entry = create_entry(hass)
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

    entry = create_entry(hass, entry_data(ai_task_entity="ai_task.gone"))
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
        data = entry.runtime_data
        assert data.ai_task_entity_id == "ai_task.openai"


async def test_setup_keeps_camera_without_area(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """Areas are not checked at setup; they are looked up at trigger time."""
    add_camera(hass, "camera.front_door")  # no area
    hass.states.async_set("binary_sensor.front_door_person", "off")

    async with setup_entry(hass) as entry:
        assert entry.state is ConfigEntryState.LOADED
        assert list(entry.runtime_data.cameras) == ["camera.front_door"]
        assert get_camera_area(hass, "camera.front_door") is None


async def test_camera_area_follows_registry_changes(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """Moving a camera to another area applies without reloading the entry."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    hass.states.async_set("binary_sensor.front_door_person", "off")

    async with setup_entry(hass) as entry:
        assert get_camera_area(hass, "camera.front_door").name == "Garden"

        hallway = ar.async_get(hass).async_get_or_create("Hallway")
        er.async_get(hass).async_update_entity("camera.front_door", area_id=hallway.id)
        await hass.async_block_till_done()

        assert get_camera_area(hass, "camera.front_door") == hallway
        assert entry.state is ConfigEntryState.LOADED


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
        data = entry.runtime_data
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
        data = entry.runtime_data
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
        entry_data(),
        people=[person("Alice"), person("Bob"), person("Carol")],
    ) as entry:
        data = entry.runtime_data
        alice_images = [path.name for path in _by_name(data)["Alice"].reference_images]
        assert alice_images == ["a.png", "photo.jpg"]
        assert [path.name for path in _by_name(data)["Bob"].reference_images] == [
            "face.png"
        ]
        # Person with no folder yet.
        assert _by_name(data)["Carol"].reference_images == []
        assert _by_name(data)["Carol"].last_seen is None
        assert _by_name(data)["Carol"].area_id is None


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
        data = entry.runtime_data
        assert [p.name for p in _by_name(data)["Alice"].reference_images] == [
            "first.png"
        ]

        # Add another image, remove the first, then reload.
        (alice_dir / "second.jpg").touch()
        first.unlink()
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

        data = entry.runtime_data
        assert [p.name for p in _by_name(data)["Alice"].reference_images] == [
            "second.jpg"
        ]


async def test_unload_removes_entities(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """Unloading the entry makes the tracker entities unavailable."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    hass.states.async_set("binary_sensor.front_door_person", "off")

    entry = create_entry(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not None

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
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
        data = entry.runtime_data
        assert _by_name(data)["Alice"].ttl == timedelta(minutes=30)
        assert _by_name(data)["Alice"].authoritative_tracker is None


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

    entry = create_entry(hass)
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

    entry = create_entry(hass, entry_data(ai_task_entity="ai_task.openai"))
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY

    await hass.config_entries.async_unload(entry.entry_id)


async def test_person_subentry_slugs_are_unique(hass: HomeAssistant) -> None:
    """Names that differ only in case share a slug and cannot coexist."""
    entry = create_entry(hass)
    with pytest.raises(HomeAssistantError):
        hass.config_entries.async_add_subentry(entry, ConfigSubentry(**person("alice")))


async def test_subentry_changes_reload_entry(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """Adding, renaming and removing people reloads the entry."""
    add_camera(hass, "camera.front_door", area_name="Garden")
    hass.states.async_set("binary_sensor.front_door_person", "off")

    async with setup_entry(hass) as entry:
        (alice_id,) = entry.subentries
        (entity_id,) = hass.states.async_entity_ids("device_tracker")

        bob = ConfigSubentry(**person("Bob"))
        hass.config_entries.async_add_subentry(entry, bob)
        await hass.async_block_till_done()
        assert entry.runtime_data.person_names == {"Alice", "Bob"}

        hass.config_entries.async_update_subentry(
            entry,
            entry.subentries[alice_id],
            title="Alicia",
            data={const.CONF_PERSON_NAME: "Alicia", const.CONF_TTL: 30},
            unique_id="alicia",
        )
        await hass.async_block_till_done()
        assert entry.runtime_data.people[alice_id].name == "Alicia"
        # The renamed person keeps their entity (and its history).
        assert er.async_get(hass).async_get(entity_id).config_subentry_id == alice_id

        hass.config_entries.async_remove_subentry(entry, bob.subentry_id)
        await hass.async_block_till_done()
        assert entry.runtime_data.person_names == {"Alicia"}
