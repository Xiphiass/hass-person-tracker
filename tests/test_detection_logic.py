"""Unit tests for the AI task response validator (Issue #3)."""

from __future__ import annotations

import pytest
from typing import Any

from custom_components.person_tracker.const import AI_TASK_NAME
from custom_components.person_tracker.detection import (
    AI_RESULT_STRUCTURE,
    unknown_person_count,
    validate_ai_response,
)


def test_full_shape() -> None:
    """The documented shape passes through unchanged."""
    data = {
        "persons": [
            {"name": "Alice", "confidence": 0.9},
            {"name": "Bob", "confidence": 0.7},
        ]
    }
    assert validate_ai_response(data) == [
        {"name": "Alice", "confidence": 0.9},
        {"name": "Bob", "confidence": 0.7},
    ]


def test_people_alias_accepted() -> None:
    """The 'people' key is accepted as a synonym for 'persons'."""
    data = {"people": [{"name": "Alice", "confidence": 1.0}]}
    assert validate_ai_response(data) == [{"name": "Alice", "confidence": 1.0}]


def test_bare_list_of_names() -> None:
    """A bare list of names is accepted, confidence defaults to 1.0."""
    assert validate_ai_response(["Alice", "Bob"]) == [
        {"name": "Alice", "confidence": 1.0},
        {"name": "Bob", "confidence": 1.0},
    ]


def test_name_key_aliases() -> None:
    """'person' is accepted as a name alias; missing confidence defaults."""
    assert validate_ai_response({"persons": [{"person": "Alice"}]}) == [
        {"name": "Alice", "confidence": 1.0}
    ]


def test_integer_confidence_coerced() -> None:
    """Integer confidences are coerced to floats."""
    assert validate_ai_response({"persons": [{"name": "Alice", "confidence": 1}]}) == [
        {"name": "Alice", "confidence": 1.0}
    ]


def test_empty_persons_list() -> None:
    """'No one visible' is an empty list, not an error."""
    assert validate_ai_response({"persons": []}) == []


def test_names_stripped() -> None:
    """Surrounding whitespace is stripped from names."""
    assert validate_ai_response({"persons": ["  Alice  "]}) == [
        {"name": "Alice", "confidence": 1.0}
    ]


def test_garbage_entries_dropped() -> None:
    """Entries that are not names are skipped, the rest kept."""
    data = {"persons": [42, None, "", "   ", "Alice", {"confidence": 0.5}]}
    assert validate_ai_response(data) == [{"name": "Alice", "confidence": 1.0}]


def test_bad_confidence_defaults_to_one() -> None:
    """Non-numeric (or boolean) confidences fall back to 1.0."""
    for confidence in ("high", None, True, False):
        data = {"persons": [{"name": "Alice", "confidence": confidence}]}
        assert validate_ai_response(data) == [
            {"name": "Alice", "confidence": 1.0}
        ], confidence


def test_malformed_top_level_raises_value_error() -> None:
    """A malformed response is a failure, never an empty (false-negative) run.

    Anything that is not the documented persons envelope — no 'persons' key,
    a bare list under the wrong key, a non-container top level — raises
    ValueError so the detection run is skipped without touching state
    (Issue #5).
    """
    for data in (None, 3.14, "Alice", {"foo": 1}, {}):
        with pytest.raises(ValueError):
            validate_ai_response(data)


def test_persons_not_a_list_raises_value_error() -> None:
    """A non-list 'persons' value is a failed identification, not no hits."""
    for data in ({"persons": {"name": "Alice"}}, {"persons": 42}, {"people": 42}):
        with pytest.raises(ValueError):
            validate_ai_response(data)


def test_response_shape_matches_what_detection_expects() -> None:
    """Every hit has exactly the keys the detection loop reads."""
    data: Any = {"persons": [{"name": "Alice", "confidence": 0.9}]}
    for hit in validate_ai_response(data):
        assert set(hit) == {"name", "confidence"}


def test_ai_result_structure_uses_task_name_and_persons_array() -> None:
    """The task's result schema is the contract the validator enforces."""
    assert AI_TASK_NAME == "identify_persons"
    # Structure access: probatio.Schema stores its definition in ``.schema``
    spec = AI_RESULT_STRUCTURE.schema
    assert "persons" in spec
    assert spec["persons"][0]["name"] is str
    assert spec["persons"][0]["confidence"] is float
    # An optional tight bounding box around each person (Issue #3).
    assert spec["persons"][0]["bbox"] == [float, float, float, float]
    assert spec["unknown_persons"] is int


def test_valid_bbox_carried_through() -> None:
    """A well-formed bbox is kept, with ints coerced to floats."""
    data = {
        "persons": [
            {"name": "Alice", "confidence": 0.9, "bbox": [10, 10, 40, 50]},
            {"name": "Bob", "confidence": 0.7, "bbox": [0.1, 0.1, 0.5, 0.5]},
        ],
        "unknown_persons": 0,
    }
    assert validate_ai_response(data) == [
        {"name": "Alice", "confidence": 0.9, "bbox": [10.0, 10.0, 40.0, 50.0]},
        {"name": "Bob", "confidence": 0.7, "bbox": [0.1, 0.1, 0.5, 0.5]},
    ]


def test_malformed_bbox_dropped_but_hit_kept() -> None:
    """A bbox we can't crop from must not drop the identification itself."""
    for bad in (
        [1.0, 2.0, 3.0],  # too short
        [1.0, "x", 3.0, 4.0],  # not all numbers
        "1, 2, 3, 4",  # a string
        [[1.0, 2.0, 3.0, 4.0]],  # nested
        [True, 1.0, 2.0, 3.0],  # a boolean is not a coordinate
        None,
    ):
        data = {
            "persons": [{"name": "Alice", "confidence": 0.9, "bbox": bad}],
            "unknown_persons": 0,
        }
        assert validate_ai_response(data) == [{"name": "Alice", "confidence": 0.9}], bad


def test_unknown_person_count() -> None:
    """A valid count is returned; anything else means "no usable count"."""
    assert unknown_person_count({"persons": [], "unknown_persons": 0}) == 0
    assert unknown_person_count({"persons": [], "unknown_persons": 2}) == 2
    assert unknown_person_count({"persons": []}) is None
    assert unknown_person_count({"unknown_persons": -1}) is None
    assert unknown_person_count({"unknown_persons": True}) is None
    assert unknown_person_count({"unknown_persons": "1"}) is None
    assert unknown_person_count(["Alice"]) is None
