"""Identification through ``ai_task.evaluate`` (ADR 0010).

HA 2026.11 added ``ai_task.evaluate``: typed questions answered by the
user's decision model with probabilities, instead of free-form structured
output. Identification asks one yes/no ("noul") question per Person ("is
this Person in the camera snapshot?") and, on a detection run, one more
about anyone else, then translates the answers into the ``generate_data``
response shape, so everything after Identification (corroboration, the
minimum confidence, unknown snapshots, re-checks and move checks) is the
same for both methods. The decision model gives no box, so last-seen
thumbnails keep the full frame.

The action does not exist before HA 2026.11; ``evaluate_available`` tells
whether it can be offered at all.
"""

from __future__ import annotations

from logging import getLogger
from typing import Any

from homeassistant.const import ATTR_SUPPORTED_FEATURES
from homeassistant.core import HomeAssistant

from .const import (
    AI_TASK_NAME,
    EVALUATE_HIT_FLOOR,
    EVALUATE_UNKNOWN_PROBABILITY,
)
from .models import Person

_LOGGER = getLogger(__package__)

try:  # HA 2026.11+
    from homeassistant.components.ai_task.task import async_evaluate
except ImportError:  # pragma: no cover - depends on the installed HA
    async_evaluate = None  # type: ignore[assignment]

# AITaskEntityFeature.EVALUATE; the member is missing before HA 2026.11.
EVALUATE_FEATURE = 8

# The question about anyone who is not one of the asked Persons.
UNKNOWN_QUESTION = "unknown_persons"


def evaluate_available() -> bool:
    """Whether the installed HA has ``ai_task.evaluate``."""
    return async_evaluate is not None


def supports_evaluate(hass: HomeAssistant, entity_id: str) -> bool:
    """Whether AI task entity *entity_id* answers ``ai_task.evaluate``."""
    state = hass.states.get(entity_id)
    if state is None:
        return False
    return bool(
        int(state.attributes.get(ATTR_SUPPORTED_FEATURES) or 0) & EVALUATE_FEATURE
    )


def reference_ranges(people: list[Person]) -> dict[str, str]:
    """Which attached images are each of *people*'s reference photos.

    The reference images come first, each person's in order, and the camera
    snapshot last (see ``snapshot_image``), e.g. ``{"Alice": "images 1-2",
    "Bob": "image 3"}``. People without reference images are left out.
    """
    ranges: dict[str, str] = {}
    position = 1
    for person in people:
        if count := len(person.reference_images):
            last = position + count - 1
            ranges[person.name] = (
                f"image {position}" if count == 1 else f"images {position}-{last}"
            )
            position = last + 1
    return ranges


def snapshot_image(people: list[Person]) -> str:
    """Which attached image is the camera snapshot: the last one.

    It follows all of *people*'s reference images, so a request starts with
    what stays the same from run to run (ADR 0012).
    """
    return f"image {sum(len(p.reference_images) for p in people) + 1}"


def _person_question(person: Person, images: str | None, preamble: str | None) -> str:
    """The yes/no question whether *person* is in the camera snapshot.

    Neutral on purpose: a bar such as "only when clearly matching" turns
    the question into "is it a clear match?", and a likely but not clear
    match then gets a low probability. The minimum confidence is the bar.
    """
    parts = [preamble] if preamble else []
    if images:
        parts.append(
            f"The reference photos of {person.name} are {images}; the last "
            f"attached image is the camera snapshot. Is {person.name}, shown in "
            "the reference photos, visible in the camera snapshot?"
        )
    else:
        parts.append(
            "The last attached image is the camera snapshot. Is "
            f"{person.name} visible in the camera snapshot? There are no "
            f"reference photos of {person.name}."
        )
    if person.description:
        parts.append(
            f"Description of {person.name}, to tell household members apart: "
            f"{person.description}."
        )
    return " ".join(parts)


def _unknown_question(people: list[Person]) -> str:
    """The yes/no question whether anyone else is in the camera snapshot."""
    names = ", ".join(sorted(person.name for person in people)) or "no one"
    return (
        "The last attached image is the camera snapshot; the images before it "
        f"are reference photos of the household members {names}. Is anyone "
        "visible in the camera snapshot who is not one of these household "
        "members, or whom you cannot recognize as one of them?"
    )


def build_questions(
    people: list[Person], *, preamble: str | None = None, ask_unknown: bool
) -> tuple[dict[str, dict[str, Any]], dict[str, Person]]:
    """The evaluate questions for *people*, and which Person each one asks about.

    One question per Person, keyed ``person_<n>`` (Person names need not be
    valid keys), plus the unknown-person question when *ask_unknown*.
    """
    ranges = reference_ranges(people)
    questions: dict[str, dict[str, Any]] = {}
    asked: dict[str, Person] = {}
    for index, person in enumerate(people):
        key = f"person_{index}"
        questions[key] = {
            "type": "noul",
            "instructions": _person_question(person, ranges.get(person.name), preamble),
        }
        asked[key] = person
    if ask_unknown:
        questions[UNKNOWN_QUESTION] = {
            "type": "noul",
            "instructions": _unknown_question(people),
        }
    return questions, asked


def build_state(people: list[Person]) -> dict[str, Any]:
    """The shared state the questions are evaluated against."""
    ranges = reference_ranges(people)
    return {
        "camera_snapshot": snapshot_image(people),
        "household_members": [
            {
                "name": person.name,
                "reference_photos": ranges.get(person.name),
                "description": person.description,
            }
            for person in people
        ],
    }


def _probability(answers: Any, key: str) -> float:
    """The yes probability answered for question *key*.

    Raises ValueError when the answer is missing or not a probability: a
    malformed response is a failed Identification (Issue #5).
    """
    if isinstance(answers, dict):
        answer = answers.get(key)
    else:
        answer = None
    if isinstance(answer, dict):
        value = answer.get("noul")
    else:
        value = getattr(answer, "noul", None)
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not 0 <= value <= 1
    ):
        raise ValueError(f"no probability for {key!r}: {answer!r}")
    return float(value)


def answers_to_data(
    answers: Any, asked: dict[str, Person], *, ask_unknown: bool
) -> dict[str, Any]:
    """Translate evaluate *answers* into the generate_data response shape.

    A Person whose probability reaches ``EVALUATE_HIT_FLOOR`` is reported
    with it as the confidence; the unknown-person question counts as one
    unknown person from ``EVALUATE_UNKNOWN_PROBABILITY``, or as none when it
    was not asked.
    """
    probabilities = {
        person.name: _probability(answers, key) for key, person in asked.items()
    }
    unknown_probability = (
        _probability(answers, UNKNOWN_QUESTION) if ask_unknown else None
    )
    # Every answer, also the ones below the floor that are not reported:
    # without them a decision model that answers low for everyone is silent.
    _LOGGER.debug(
        "Evaluate answers: %s; anyone else: %s",
        ", ".join(f"{name} {value:.2f}" for name, value in probabilities.items())
        or "no one asked",
        "not asked" if unknown_probability is None else f"{unknown_probability:.2f}",
    )
    persons = [
        {"name": name, "confidence": value}
        for name, value in probabilities.items()
        if value >= EVALUATE_HIT_FLOOR
    ]
    unknown = int(
        unknown_probability is not None
        and unknown_probability >= EVALUATE_UNKNOWN_PROBABILITY
    )
    return {"persons": persons, "unknown_persons": unknown}


async def async_identify_by_evaluation(
    hass: HomeAssistant,
    entity_id: str,
    people: list[Person],
    attachments: list[dict[str, str]],
    *,
    preamble: str | None = None,
    ask_unknown: bool,
) -> dict[str, Any]:
    """Ask the decision model which of *people* the snapshot shows.

    *attachments* are *people*'s reference images followed by the snapshot,
    as for generate_data. Returns the generate_data response shape. Errors
    of the AI Task call propagate; a malformed answer raises ValueError.
    """
    if async_evaluate is None:
        raise ValueError("ai_task.evaluate needs Home Assistant 2026.11 or newer")
    questions, asked = build_questions(
        people, preamble=preamble, ask_unknown=ask_unknown
    )
    result = await async_evaluate(
        hass,
        task_name=AI_TASK_NAME,
        entity_id=entity_id,
        questions=questions,
        state=build_state(people),
        attachments=attachments,
    )
    return answers_to_data(result.answers, asked, ask_unknown=ask_unknown)
