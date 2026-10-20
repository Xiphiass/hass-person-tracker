"""Translations stay in sync with the English source strings."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

TRANSLATIONS = (
    Path(__file__).parent.parent / "custom_components/person_tracker/translations"
)
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _flatten(strings: dict[str, Any], prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    for key, value in strings.items():
        if isinstance(value, dict):
            flat |= _flatten(value, f"{prefix}{key}.")
        else:
            flat[f"{prefix}{key}"] = value
    return flat


def _load(language: str) -> dict[str, str]:
    return _flatten(json.loads((TRANSLATIONS / f"{language}.json").read_text()))


@pytest.mark.parametrize(
    "language", [p.stem for p in TRANSLATIONS.glob("*.json") if p.stem != "en"]
)
def test_translation_matches_english(language: str) -> None:
    """Every translation has exactly the English keys and placeholders."""
    english = _load("en")
    translated = _load(language)
    assert translated.keys() == english.keys()
    for key, text in english.items():
        assert set(_PLACEHOLDER.findall(translated[key])) == set(
            _PLACEHOLDER.findall(text)
        ), key
