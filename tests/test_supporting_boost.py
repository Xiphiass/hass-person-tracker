"""Plain unit tests of the Supporting tracker confidence boost (Issue #32).

After Identification, a hit whose camera agrees with the Person's
supporting tracker gets a boost before the minimum confidence comparison
(ADR 0009): the area boost for the same area, else the floor boost for the
same floor. Boosts never add up, never lower and are capped at 1.0.
"""

from __future__ import annotations

import pytest

from custom_components.person_tracker.supporting import boosted_confidence


class TestBoostedConfidence:
    """Unit tests for supporting.boosted_confidence."""

    def test_same_area_adds_the_area_boost(self) -> None:
        assert boosted_confidence(
            0.6, "kitchen", "ground", "kitchen", "ground"
        ) == pytest.approx(0.75)

    def test_same_floor_adds_the_floor_boost(self) -> None:
        assert boosted_confidence(
            0.6, "kitchen", "ground", "hall", "ground"
        ) == pytest.approx(0.65)

    def test_floor_only_supporting_on_same_floor(self) -> None:
        assert boosted_confidence(
            0.6, "kitchen", "ground", None, "ground"
        ) == pytest.approx(0.65)

    def test_area_and_floor_match_is_not_cumulative(self) -> None:
        # The same area is also the same floor: only the area boost applies.
        assert boosted_confidence(
            0.5, "kitchen", "ground", "kitchen", "ground"
        ) == pytest.approx(0.65)

    def test_disagreement_changes_nothing(self) -> None:
        assert boosted_confidence(0.6, "kitchen", "ground", "bedroom", "up") == 0.6

    def test_no_supporting_data_changes_nothing(self) -> None:
        assert boosted_confidence(0.6, "kitchen", "ground", None, None) == 0.6

    def test_capped_at_one(self) -> None:
        assert boosted_confidence(0.95, "kitchen", "ground", "kitchen", None) == 1.0

    def test_never_lowers_a_confidence_above_one(self) -> None:
        # A provider may answer above 1; the cap must not pull it down.
        assert boosted_confidence(1.2, "kitchen", "ground", "kitchen", None) == 1.2
