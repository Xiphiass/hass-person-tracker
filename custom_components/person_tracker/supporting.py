"""Pure resolution rules for the Supporting tracker (Issue #29 / ADR 0008).

These functions resolve the supporting entities' states to HA areas and
floors without any HA dependency, so they can be unit-tested in isolation.
The caller passes the registry entries as :class:`Place` values.

Resolution
----------
An ``area_id``/``floor_id`` attribute naming a known place wins. Otherwise
the state is matched against place IDs, then names, then aliases, ignoring
case. A state that reports no place (``unknown``, ``unavailable``,
``not_home``, ``away``) or matches nothing resolves to no place.

Agreement
---------
``supporting_agrees`` compares the supporting location with the current
camera sighting: the same area, only the same floor, or neither.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from homeassistant.const import STATE_NOT_HOME, STATE_UNAVAILABLE, STATE_UNKNOWN

AGREES_AREA = "area"
AGREES_FLOOR = "floor"
AGREES_NONE = "none"

# States in which a supporting entity reports no place at all.
_NO_PLACE_STATES = frozenset({STATE_UNKNOWN, STATE_UNAVAILABLE, STATE_NOT_HOME, "away"})


@dataclass(frozen=True)
class Place:
    """An HA area or floor, as far as resolution is concerned."""

    id: str
    name: str
    aliases: frozenset[str]


def resolve_place(
    state: str | None, attribute_id: Any, places: Iterable[Place]
) -> str | None:
    """The ID of the place a supporting entity's *state* names, if any.

    *attribute_id* is the entity's ``area_id``/``floor_id`` attribute; it
    wins when it names one of *places*.
    """
    if not state or state.lower() in _NO_PLACE_STATES:
        return None
    places = list(places)
    if attribute_id is not None:
        for place in places:
            if place.id == attribute_id:
                return place.id
    wanted = state.strip().casefold()
    for matches in (
        lambda place: place.id.casefold() == wanted,
        lambda place: place.name.casefold() == wanted,
        lambda place: any(alias.casefold() == wanted for alias in place.aliases),
    ):
        for place in places:
            if matches(place):
                return place.id
    return None


def supporting_agrees(
    camera_area_id: str | None,
    camera_floor_id: str | None,
    supporting_area_id: str | None,
    supporting_floor_id: str | None,
) -> str | None:
    """Whether the supporting location agrees with the camera sighting.

    ``area`` for the same area, ``floor`` for another area (or no area) on
    the same floor, ``none`` otherwise. None when either side is missing:
    no current camera sighting, or no supporting area or floor.
    """
    if camera_area_id is None or (
        supporting_area_id is None and supporting_floor_id is None
    ):
        return None
    if supporting_area_id == camera_area_id:
        return AGREES_AREA
    if supporting_floor_id is not None and supporting_floor_id == camera_floor_id:
        return AGREES_FLOOR
    return AGREES_NONE
