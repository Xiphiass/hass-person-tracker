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
    AreaOrFloor,
    resolve_area_or_floor,
    supporting_agrees,
)

AREAS_OR_FLOORS = [
    AreaOrFloor("living_room", "Living Room", frozenset({"Lounge"})),
    AreaOrFloor("kitchen", "Kitchen", frozenset()),
    # An ID that is also another one's name: ID matches win over names.
    AreaOrFloor("office", "Study", frozenset()),
    AreaOrFloor("study", "Library", frozenset({"Office"})),
]


class TestResolveAreaOrFloor:
    """Unit tests for supporting.resolve_area_or_floor."""

    def test_attribute_naming_known_one_wins(self) -> None:
        assert (
            resolve_area_or_floor("Kitchen", "living_room", AREAS_OR_FLOORS)
            == "living_room"
        )

    def test_attribute_naming_unknown_one_falls_back_to_state(self) -> None:
        assert resolve_area_or_floor("Kitchen", "garage", AREAS_OR_FLOORS) == "kitchen"

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
        assert resolve_area_or_floor(state, None, AREAS_OR_FLOORS) == expected

    def test_id_match_beats_name_and_alias(self) -> None:
        # "office" is the ID of Study and an alias of Library.
        assert resolve_area_or_floor("Office", None, AREAS_OR_FLOORS) == "office"

    def test_name_match_beats_alias(self) -> None:
        candidates = [
            AreaOrFloor("a", "Den", frozenset()),
            AreaOrFloor("b", "Snug", frozenset({"Den"})),
        ]
        assert resolve_area_or_floor("den", None, list(reversed(candidates))) == "a"

    @pytest.mark.parametrize(
        "state",
        [None, "", "unknown", " Unavailable ", "not_home", "away", "Garage"],
    )
    def test_no_match(self, state: str | None) -> None:
        assert resolve_area_or_floor(state, None, AREAS_OR_FLOORS) is None

    @pytest.mark.parametrize("state", ["unknown", "unavailable", "not_home", "away"])
    def test_absent_state_ignores_stale_attribute(self, state: str) -> None:
        assert resolve_area_or_floor(state, "kitchen", AREAS_OR_FLOORS) is None


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
