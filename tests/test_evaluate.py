"""Identification through ai_task.evaluate (ADR 0010).

The installed test harness may predate ai_task.evaluate (HA 2026.11), so the
action is patched in and its availability is forced where needed.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.components.camera import Image
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_SUPPORTED_FEATURES
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.person_tracker import const
from custom_components.person_tracker.evaluation import (
    EVALUATE_FEATURE,
    UNKNOWN_QUESTION,
    answers_to_data,
    build_questions,
)
from custom_components.person_tracker.models import Person

from .conftest import add_camera, create_entry, entry_data, person, setup_entry

DOMAIN = const.DOMAIN
TRIGGER = "binary_sensor.front_door_person"
CAMERA = "camera.front_door"
EVALUATE_ENTITY = "ai_task.decider"


def _person(name: str, images: int = 0, description: str | None = None) -> Person:
    subject = Person(subentry_id=name, name=name, description=description)
    subject.reference_images = [Path(f"{name}-{i}.jpg") for i in range(images)]
    return subject


def _answers(**probabilities: float) -> SimpleNamespace:
    """An EvaluationTaskResult with a noul answer per question."""
    return SimpleNamespace(
        answers={
            key: SimpleNamespace(noul=value, type="noul")
            for key, value in probabilities.items()
        }
    )


@pytest.fixture
def evaluate_entity(hass: HomeAssistant, default_ai_task: None) -> None:
    """An AI task entity that supports evaluate, with evaluate available."""
    hass.states.async_set(
        EVALUATE_ENTITY, "idle", {ATTR_SUPPORTED_FEATURES: EVALUATE_FEATURE | 1}
    )
    with (
        patch(
            "custom_components.person_tracker.validation.evaluate_available",
            return_value=True,
        ),
        patch(
            "custom_components.person_tracker.config_flow.evaluate_available",
            return_value=True,
        ),
    ):
        yield


def _mock_evaluate(result: Any = None, **kwargs: Any):
    return patch(
        "custom_components.person_tracker.evaluation.async_evaluate",
        new=AsyncMock(return_value=result, **kwargs),
    )


def _mock_generate_data():
    return patch(
        "custom_components.person_tracker.detection.async_generate_data",
        new=AsyncMock(side_effect=AssertionError("generate_data must not run")),
    )


def _mock_image():
    return patch(
        "custom_components.person_tracker.detection.async_get_image",
        new=AsyncMock(
            return_value=Image(content_type="image/jpeg", content=b"\xff\xd8x\xff\xd9")
        ),
    )


def _evaluate_data(**kwargs: Any) -> dict[str, Any]:
    return entry_data(ai_task_entity=EVALUATE_ENTITY, **kwargs) | {
        const.CONF_AI_METHOD: const.AI_METHOD_EVALUATE
    }


def _tracker_state(hass: HomeAssistant, entry_id: str, name: str) -> str:
    entry = hass.config_entries.async_get_entry(entry_id)
    subentry_ids = {s.title: s.subentry_id for s in entry.subentries.values()}
    entity_id = er.async_get(hass).async_get_entity_id(
        "device_tracker", DOMAIN, subentry_ids[name]
    )
    return hass.states.get(entity_id).state


async def _fire_edge(hass: HomeAssistant) -> None:
    hass.states.async_set(TRIGGER, "off")
    hass.states.async_set(TRIGGER, "on")
    await hass.async_block_till_done()


# --- Questions and answers ---------------------------------------------------


def test_questions_name_each_persons_reference_photos() -> None:
    """One noul question per person, pointing at their reference photos."""
    alice = _person("Alice", images=2, description="long dark hair")
    bob = _person("Bob", images=1)
    questions, asked = build_questions([alice, bob], ask_unknown=True)

    assert asked == {"person_0": alice, "person_1": bob}
    assert set(questions) == {"person_0", "person_1", UNKNOWN_QUESTION}
    assert all(question["type"] == "noul" for question in questions.values())
    assert "Is Alice, shown in the reference photos, visible" in (
        questions["person_0"]["instructions"]
    )
    assert "images 2-3" in questions["person_0"]["instructions"]
    assert "long dark hair" in questions["person_0"]["instructions"]
    assert "image 4" in questions["person_1"]["instructions"]
    assert "Alice, Bob" in questions[UNKNOWN_QUESTION]["instructions"]


def test_questions_carry_the_preamble_without_the_unknown_question() -> None:
    """A re-check or move check frames each question and skips anyone else."""
    questions, _ = build_questions(
        [_person("Alice", images=1)], preamble="Re-check.", ask_unknown=False
    )
    assert set(questions) == {"person_0"}
    assert questions["person_0"]["instructions"].startswith("Re-check. ")


def test_questions_set_no_bar_for_a_match() -> None:
    """The minimum confidence is the bar, not the question's wording.

    "Only when clearly matching" turned a likely match (0.65) into a low
    probability (0.15) on a real decision model.
    """
    questions, _ = build_questions(
        [_person("Alice", images=1, description="tall")], ask_unknown=False
    )
    text = questions["person_0"]["instructions"].lower()
    for bar in ("clearly", "resemble", "only when", "uncertain"):
        assert bar not in text


def test_answers_become_the_generate_data_shape() -> None:
    """Probabilities become confidences; unlikely people are not reported."""
    alice, bob = _person("Alice"), _person("Bob")
    data = answers_to_data(
        _answers(person_0=0.92, person_1=0.05, unknown_persons=0.6).answers,
        {"person_0": alice, "person_1": bob},
        ask_unknown=True,
    )
    assert data == {
        "persons": [{"name": "Alice", "confidence": 0.92}],
        "unknown_persons": 1,
    }


def test_answers_accept_plain_dicts() -> None:
    """The action response form (asdict) is understood as well."""
    data = answers_to_data(
        {"person_0": {"type": "noul", "noul": 0.3}},
        {"person_0": _person("Alice")},
        ask_unknown=False,
    )
    assert data == {
        "persons": [{"name": "Alice", "confidence": 0.3}],
        "unknown_persons": 0,
    }


def test_every_answer_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """Probabilities below the floor are not reported, but they are logged."""
    caplog.set_level("DEBUG", logger="custom_components.person_tracker")
    answers_to_data(
        _answers(person_0=0.05, person_1=0.6, unknown_persons=0.1).answers,
        {"person_0": _person("Alice"), "person_1": _person("Bob")},
        ask_unknown=True,
    )
    assert "Evaluate answers: Alice 0.05, Bob 0.60; anyone else: 0.10" in caplog.text


@pytest.mark.parametrize(
    "answers", [{}, {"person_0": SimpleNamespace(noul=1.5)}, {"person_0": None}]
)
def test_missing_or_invalid_answers_are_malformed(answers: dict) -> None:
    """A missing or out-of-range probability is a failed Identification."""
    with pytest.raises(ValueError):
        answers_to_data(answers, {"person_0": _person("Alice")}, ask_unknown=False)


# --- Detection runs ----------------------------------------------------------


async def test_detection_identifies_through_evaluate(
    hass: HomeAssistant, evaluate_entity: None
) -> None:
    """The evaluate method asks per person and applies the probabilities."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    result = _answers(person_0=0.95, person_1=0.4, unknown_persons=0.1)
    with _mock_evaluate(result) as evaluate, _mock_generate_data(), _mock_image():
        async with setup_entry(
            hass,
            _evaluate_data(cooldown_seconds=0),
            people=[person("Alice"), person("Bob")],
        ) as entry:
            await _fire_edge(hass)

            evaluate.assert_awaited_once()
            _, kwargs = evaluate.call_args
            assert kwargs["entity_id"] == EVALUATE_ENTITY
            assert kwargs["task_name"] == const.AI_TASK_NAME
            assert set(kwargs["questions"]) == {
                "person_0",
                "person_1",
                UNKNOWN_QUESTION,
            }
            assert kwargs["attachments"][0]["media_content_id"].startswith(
                f"media-source://{DOMAIN}/frames/"
            )
            assert kwargs["state"]["camera_snapshot"] == "image 1"

            # Bob's 0.4 is below the default minimum confidence of 0.7.
            assert _tracker_state(hass, entry.entry_id, "Alice") == "home"
            assert _tracker_state(hass, entry.entry_id, "Bob") == "unknown"


async def test_evaluate_unknown_person_saves_an_unknown_snapshot(
    hass: HomeAssistant, evaluate_entity: None, tmp_path: Path
) -> None:
    """Someone else in the frame is kept as an unknown snapshot."""
    hass.config.config_dir = str(tmp_path)
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")
    unknown_dir = tmp_path / DOMAIN / const.UNKNOWN_SNAPSHOTS_DIR

    with (
        _mock_evaluate(_answers(person_0=0.02, unknown_persons=0.9)),
        _mock_image(),
    ):
        async with setup_entry(
            hass, _evaluate_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            await _fire_edge(hass)

    assert len(list(unknown_dir.glob("*.jpg"))) == 1


async def test_evaluate_empty_scene_saves_nothing(
    hass: HomeAssistant, evaluate_entity: None, tmp_path: Path
) -> None:
    """No one at all (every probability low) is a false trigger."""
    hass.config.config_dir = str(tmp_path)
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")
    unknown_dir = tmp_path / DOMAIN / const.UNKNOWN_SNAPSHOTS_DIR

    with (
        _mock_evaluate(_answers(person_0=0.05, unknown_persons=0.05)),
        _mock_image(),
    ):
        async with setup_entry(
            hass, _evaluate_data(cooldown_seconds=0), people=[person("Alice")]
        ):
            await _fire_edge(hass)

    assert not list(unknown_dir.glob("*.jpg"))


@pytest.mark.parametrize(
    "mock",
    [
        {"side_effect": HomeAssistantError("decision model down")},
        {"return_value": _answers(unknown_persons=0.1)},
    ],
    ids=["ai-error", "missing-answer"],
)
async def test_evaluate_failure_keeps_state(
    hass: HomeAssistant,
    evaluate_entity: None,
    mock: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed or malformed evaluation changes no tracker (Issue #5)."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with (
        patch(
            "custom_components.person_tracker.evaluation.async_evaluate",
            new=AsyncMock(**mock),
        ),
        _mock_image(),
    ):
        async with setup_entry(
            hass, _evaluate_data(cooldown_seconds=0), people=[person("Alice")]
        ) as entry:
            await _fire_edge(hass)
            assert _tracker_state(hass, entry.entry_id, "Alice") == "unknown"
    assert "for camera.front_door" in caplog.text


async def test_recheck_uses_the_evaluate_preamble(
    hass: HomeAssistant, evaluate_entity: None
) -> None:
    """A re-check frames the questions neutrally, without the generate data bar."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")

    with _mock_evaluate(_answers(person_0=0.9)) as evaluate, _mock_image():
        async with setup_entry(
            hass, _evaluate_data(cooldown_seconds=0), people=[person("Alice")]
        ) as entry:
            coordinator = entry.runtime_data.coordinator
            camera = entry.runtime_data.cameras[CAMERA]
            people = list(entry.runtime_data.people.values())
            await coordinator._async_identify(
                camera, people, preamble=const.AI_TASK_RECHECK_INSTRUCTIONS
            )

    _, kwargs = evaluate.call_args
    assert set(kwargs["questions"]) == {"person_0"}
    instructions = kwargs["questions"]["person_0"]["instructions"]
    assert instructions.startswith(const.EVALUATE_RECHECK_PREAMBLE)
    assert const.AI_TASK_RECHECK_INSTRUCTIONS not in instructions


# --- Setup and config flow ---------------------------------------------------


async def test_setup_fails_when_the_entity_cannot_evaluate(
    hass: HomeAssistant, evaluate_entity: None
) -> None:
    """An entity without the evaluate feature cannot run the evaluate method."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")
    entry = create_entry(
        hass,
        entry_data()
        | {const.CONF_AI_METHOD: const.AI_METHOD_EVALUATE},  # ai_task.openai
    )
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_setup_fails_without_evaluate_in_ha(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """The evaluate method needs ai_task.evaluate in HA."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")
    entry = create_entry(
        hass, entry_data() | {const.CONF_AI_METHOD: const.AI_METHOD_EVALUATE}
    )
    with patch(
        "custom_components.person_tracker.validation.evaluate_available",
        return_value=False,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def _reconfigure_ai_task(hass: HomeAssistant, entry_id: str) -> dict:
    entry = hass.config_entries.async_get_entry(entry_id)
    result = await entry.start_reconfigure_flow(hass)
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "ai_task"}
    )


async def test_reconfigure_to_evaluate(
    hass: HomeAssistant, evaluate_entity: None
) -> None:
    """The method can be switched to evaluate on a supporting entity."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")
    async with setup_entry(hass) as entry:
        result = await _reconfigure_ai_task(hass, entry.entry_id)
        assert result["step_id"] == "ai_task"
        keys = {str(key): key for key in result["data_schema"].schema}
        assert keys[const.CONF_AI_METHOD].description == {
            "suggested_value": const.AI_METHOD_GENERATE_DATA
        }

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                const.CONF_AI_TASK_ENTITY: "ai_task.openai",
                const.CONF_AI_METHOD: const.AI_METHOD_EVALUATE,
            },
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {
            const.CONF_AI_TASK_ENTITY: const.ERR_EVALUATE_NOT_SUPPORTED
        }

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                const.CONF_AI_TASK_ENTITY: EVALUATE_ENTITY,
                const.CONF_AI_METHOD: const.AI_METHOD_EVALUATE,
            },
        )
        assert result["reason"] == "reconfigure_successful"
        await hass.async_block_till_done()
        assert entry.data[const.CONF_AI_METHOD] == const.AI_METHOD_EVALUATE
        assert entry.runtime_data.ai_method == const.AI_METHOD_EVALUATE
        assert entry.runtime_data.ai_task_entity_id == EVALUATE_ENTITY


async def test_method_is_hidden_without_evaluate_in_ha(
    hass: HomeAssistant, default_ai_task: None
) -> None:
    """Before HA 2026.11 the AI task step only asks for the entity."""
    add_camera(hass, CAMERA, area_name="Garden")
    hass.states.async_set(TRIGGER, "off")
    with patch(
        "custom_components.person_tracker.config_flow.evaluate_available",
        return_value=False,
    ):
        async with setup_entry(hass) as entry:
            result = await _reconfigure_ai_task(hass, entry.entry_id)
            assert [str(key) for key in result["data_schema"].schema] == [
                const.CONF_AI_TASK_ENTITY
            ]
