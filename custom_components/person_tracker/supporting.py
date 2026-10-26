"""Pure resolution rules for the Supporting tracker (Issue #29 / ADR 0008).

These functions resolve the supporting entities' states to HA areas and
floors without any HA dependency, so they can be unit-tested in isolation.
The caller passes the registry entries as :class:`AreaOrFloor` values.

Resolution
----------
An ``area_id``/``floor_id`` attribute naming a known area or floor wins.
Otherwise the state is matched against their IDs, then names, then aliases,
ignoring case. A state that reports no location (``unknown``,
``unavailable``, ``not_home``, ``away``), even with a stale attribute, or
that matches nothing resolves to none.

Agreement
---------
``supporting_agrees`` compares the supporting location with the current
camera sighting: the same area, only the same floor, or neither.

Corroboration
-------------
``boosted_confidence`` raises an Identification hit's confidence when the
camera's area agrees with the supporting tracker (ADR 0009): the area boost
for the same area, else the floor boost for the same floor. Boosts never add
up, never lower the confidence and are capped at 1.0.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from homeassistant.const import STATE_NOT_HOME, STATE_UNAVAILABLE, STATE_UNKNOWN

from .const import SUPPORTING_AREA_BOOST, SUPPORTING_FLOOR_BOOST

AGREES_AREA = "area"
AGREES_FLOOR = "floor"
AGREES_NONE = "none"

# States in which a supporting entity reports no location at all.
_ABSENT_STATES = frozenset({STATE_UNKNOWN, STATE_UNAVAILABLE, STATE_NOT_HOME, "away"})


@dataclass(frozen=True)
class AreaOrFloor:
    """An HA area or floor, as far as resolution is concerned."""

    id: str
    name: str
    aliases: frozenset[str]


def resolve_area_or_floor(
    state: str | None, attribute_id: Any, candidates: Iterable[AreaOrFloor]
) -> str | None:
    """The ID of the area or floor a supporting entity's *state* names.

    *attribute_id* is the entity's ``area_id``/``floor_id`` attribute; it
    wins when it names one of *candidates*. None when nothing matches.
    """
    wanted = (state or "").strip().casefold()
    if not wanted or wanted in _ABSENT_STATES:
        return None
    candidates = list(candidates)
    if attribute_id is not None:
        for candidate in candidates:
            if candidate.id == attribute_id:
                return candidate.id
    for matches in (
        lambda candidate: candidate.id.casefold() == wanted,
        lambda candidate: candidate.name.casefold() == wanted,
        lambda candidate: any(
            alias.casefold() == wanted for alias in candidate.aliases
        ),
    ):
        for candidate in candidates:
            if matches(candidate):
                return candidate.id
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


def boosted_confidence(
    confidence: float,
    camera_area_id: str,
    camera_floor_id: str | None,
    supporting_area_id: str | None,
    supporting_floor_id: str | None,
) -> float:
    """*confidence* of a hit on a camera in *camera_area_id*, corroborated.

    The area boost when the supporting area is the camera's area, else the
    floor boost when the supporting floor is the camera area's floor, capped
    at 1.0; unchanged otherwise, and never lowered.
    """
    boost = {
        AGREES_AREA: SUPPORTING_AREA_BOOST,
        AGREES_FLOOR: SUPPORTING_FLOOR_BOOST,
    }.get(
        supporting_agrees(
            camera_area_id, camera_floor_id, supporting_area_id, supporting_floor_id
        ),
        0.0,
    )
    if not boost:
        return confidence
    return max(confidence, min(1.0, confidence + boost))
