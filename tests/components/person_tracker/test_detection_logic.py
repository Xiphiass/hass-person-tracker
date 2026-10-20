"""Unit tests for detection validation logic."""

from custom_components.person_tracker.detection import validate_ai_response


def test_validate_ai_response() -> None:
    """Validation logic test."""
    known = {"Alice", "Bob"}

    # Valid
    assert validate_ai_response(["Alice"], known) == {"Alice"}
    assert validate_ai_response(["Alice", "Bob"], known) == {"Alice", "Bob"}

    # None/empty
    assert validate_ai_response(["none"], known) == set()
    assert validate_ai_response([], known) == set()

    # Invalid/Unknown
    assert validate_ai_response(["Charlie"], known) == set()
    assert validate_ai_response(["Alice", "Charlie"], known) == {"Alice"}

    # Malformed
    assert validate_ai_response("string", known) == set()
    assert validate_ai_response([1, 2], known) == set()
