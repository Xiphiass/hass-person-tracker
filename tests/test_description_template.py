"""Person descriptions as HA templates, and descriptions that rule out."""

from __future__ import annotations

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.person_tracker import const
from custom_components.person_tracker.detection import _build_instructions
from custom_components.person_tracker.evaluation import build_questions, build_state
from custom_components.person_tracker.models import Person

from .conftest import add_camera, entry_data, person, setup_entry
from .test_config_flow import _configure_person, _init_person_flow
from .test_detection import _ai_result, _fire_edge, _mock_ai, _mock_image

TRIGGER = "binary_sensor.front_door_person"
CAMERA = "camera.front_door"
AGE = "Toddler, {{ now().year - 2023 }} years old."


def _age() -> str:
    return f"Toddler, {dt_util.now().year - 2023} years old."


def test_prompt_lets_a_description_rule_out() -> None:
    """References lead; a contradicting description rules a member out."""
    text = _build_instructions(
        [Person(subentry_id="k", name="Kid", description="raw")],
        descriptions={"Kid": "Toddler, 3 years old."},
    )
    assert "Kid: Toddler, 3 years old." in text
    assert "raw" not in text
    assert "mainly by comparing them with the reference photos" in text
    assert "is not that member, however similar they look" in text


def test_evaluate_question_uses_the_rendered_description() -> None:
    kid = Person(subentry_id="k", name="Kid", description="raw")
    questions, _ = build_questions(
        [kid], ask_unknown=False, descriptions={"Kid": "Toddler, 3 years old."}
    )
    text = questions["person_0"]["instructions"]
    assert "Toddler, 3 years old." in text
    assert "plainly contradicts it" in text
    assert "is not Kid, however similar they look" in text
    state = build_state([kid], {"Kid": "Toddler, 3 years old."})
    assert state["household_members"][0]["description"] == "Toddler, 3 years old."


async def test_template_is_rendered_on_each_detection(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """The stored template is rendered before it reaches the AI."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")
    with _mock_ai(_ai_result([])) as ai, _mock_image():
        async with setup_entry(
            hass,
            entry_data(cooldown_seconds=0),
            people=[person("Kid", description=AGE)],
        ) as entry:
            await _fire_edge(hass)
            (kid,) = entry.runtime_data.people.values()
            assert kid.description == AGE

    instructions = ai.call_args.kwargs["instructions"]
    assert f"Kid: {_age()}" in instructions
    assert "{{" not in instructions


async def test_failing_template_is_sent_as_text_and_warned_once(
    hass: HomeAssistant, default_ai_task: None, caplog: pytest.LogCaptureFixture
) -> None:
    """A template that fails at detection time never stops Identification."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")
    broken = "Toddler, {{ states('sensor.gone') | int }} years old"
    with _mock_ai(_ai_result([])) as ai, _mock_image():
        async with setup_entry(
            hass,
            entry_data(cooldown_seconds=0),
            people=[person("Kid", description=broken)],
        ):
            await _fire_edge(hass)
            await _fire_edge(hass)

    assert ai.await_count == 2
    assert f"Kid: {broken}" in ai.call_args.kwargs["instructions"]
    warnings = [r for r in caplog.records if "description as plain text" in r.message]
    assert len(warnings) == 1


async def test_flow_stores_the_template_and_rejects_a_broken_one(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    add_camera(hass, CAMERA, area_name="Garden")
    async with setup_entry(hass, people=[]) as entry:
        result = await _init_person_flow(hass, entry)
        result = await _configure_person(
            hass,
            result,
            {const.CONF_PERSON_NAME: "Kid", const.CONF_DESCRIPTION: "{{ now( }}"},
        )
        assert result["errors"] == {const.CONF_DESCRIPTION: "invalid_description"}
        result = await _configure_person(
            hass,
            result,
            {const.CONF_PERSON_NAME: "Kid", const.CONF_DESCRIPTION: AGE},
        )
        assert result["data"][const.CONF_DESCRIPTION] == AGE
        await hass.async_block_till_done()
