"""Unit tests for the AI task response validator (Issue #3)."""

from __future__ import annotations

from typing import Any

from custom_components.person_tracker.detection import (
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


def test_unparseable_top_level_yields_empty() -> None:
    """Anything that is not a dict or list yields no hits, never an error."""
    for data in (None, 3.14, "Alice", {"foo": 1}, {"persons": "Alice"}, {}):
        assert validate_ai_response(data) == [], data


def test_persons_not_a_list() -> None:
    """A non-list 'persons' value yields no hits."""
    assert validate_ai_response({"persons": {"name": "Alice"}}) == []
    assert validate_ai_response({"persons": 42}) == []


def test_response_shape_matches_what_detection_expects() -> None:
    """Every hit has exactly the keys the detection loop reads."""
    data: Any = {"persons": [{"name": "Alice", "confidence": 0.9}]}
    for hit in validate_ai_response(data):
        assert set(hit) == {"name", "confidence"}


def test_unknown_person_count() -> None:
    """A valid count is returned; anything else means "no usable count"."""
    assert unknown_person_count({"persons": [], "unknown_persons": 0}) == 0
    assert unknown_person_count({"persons": [], "unknown_persons": 2}) == 2
    assert unknown_person_count({"persons": []}) is None
    assert unknown_person_count({"unknown_persons": -1}) is None
    assert unknown_person_count({"unknown_persons": True}) is None
    assert unknown_person_count({"unknown_persons": "1"}) is None
    assert unknown_person_count(["Alice"]) is None
