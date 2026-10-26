"""Plain unit tests of Supporting tracker resolution (Issue #29 / ADR 0008).

The supporting area and floor entities' states are resolved to HA areas and
floors: an ``area_id``/``floor_id`` attribute naming a known one first, then
the state matched against ID, name and aliases (case-insensitive).
"""

from __future__ import annotations

import pytest

from custom_components.person_tracker.supporting import (
    AGREES_AREA,
    AGREES_FLOOR,
    AGREES_NONE,
    Place,
    resolve_place,
    supporting_agrees,
)

PLACES = [
    Place("living_room", "Living Room", frozenset({"Lounge"})),
    Place("kitchen", "Kitchen", frozenset()),
    # An ID that is also another place's name: ID matches win over names.
    Place("office", "Study", frozenset()),
    Place("study", "Library", frozenset({"Office"})),
]


class TestResolvePlace:
    """Unit tests for supporting.resolve_place."""

    def test_attribute_naming_known_place_wins(self) -> None:
        assert resolve_place("Kitchen", "living_room", PLACES) == "living_room"

    def test_attribute_naming_unknown_place_falls_back_to_state(self) -> None:
        assert resolve_place("Kitchen", "garage", PLACES) == "kitchen"

    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            ("kitchen", "kitchen"),
            ("KITCHEN", "kitchen"),
            ("living room", "living_room"),
            ("Living Room", "living_room"),
            ("lounge", "living_room"),
            ("Library", "study"),
        ],
    )
    def test_state_matches_id_name_or_alias(self, state: str, expected: str) -> None:
        assert resolve_place(state, None, PLACES) == expected

    def test_id_match_beats_name_and_alias(self) -> None:
        # "office" is the ID of Study and an alias of Library.
        assert resolve_place("Office", None, PLACES) == "office"

    def test_name_match_beats_alias(self) -> None:
        places = [
            Place("a", "Den", frozenset()),
            Place("b", "Snug", frozenset({"Den"})),
        ]
        assert resolve_place("den", None, list(reversed(places))) == "a"

    @pytest.mark.parametrize(
        "state", [None, "", "unknown", "unavailable", "not_home", "away", "Garage"]
    )
    def test_no_place(self, state: str | None) -> None:
        assert resolve_place(state, None, PLACES) is None

    @pytest.mark.parametrize("state", ["unknown", "unavailable", "not_home", "away"])
    def test_absent_state_ignores_stale_attribute(self, state: str) -> None:
        assert resolve_place(state, "kitchen", PLACES) is None


class TestSupportingAgrees:
    """Unit tests for supporting.supporting_agrees."""

    def test_same_area(self) -> None:
        assert supporting_agrees("kitchen", "ground", "kitchen", "ground") == (
            AGREES_AREA
        )

    def test_same_floor_other_area(self) -> None:
        assert supporting_agrees("kitchen", "ground", "hall", "ground") == (
            AGREES_FLOOR
        )

    def test_floor_only_supporting_on_same_floor(self) -> None:
        assert supporting_agrees("kitchen", "ground", None, "ground") == AGREES_FLOOR

    def test_different_area_and_floor(self) -> None:
        assert supporting_agrees("kitchen", "ground", "bedroom", "upstairs") == (
            AGREES_NONE
        )

    def test_no_floors_on_either_side_is_none(self) -> None:
        assert supporting_agrees("kitchen", None, "hall", None) == AGREES_NONE

    def test_missing_camera_sighting(self) -> None:
        assert supporting_agrees(None, None, "kitchen", "ground") is None

    def test_missing_supporting_location(self) -> None:
        assert supporting_agrees("kitchen", "ground", None, None) is None
