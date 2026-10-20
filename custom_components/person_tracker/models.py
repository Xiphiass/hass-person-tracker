"""Runtime data model for the person_tracker integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .const import DEFAULT_TTL_MINUTES


def person_slug(name: str) -> str:
    """Return the filesystem- and slug-safe key for person *name*."""
    return "".join(c if c.isalnum() else "-" for c in name.lower())


@dataclass
class Person:
    """One known person and their runtime location state.

    The detection pipeline (later tickets) updates ``location``/``last_seen``/
    ``last_camera``; ticket #2 establishes identity only.
    """

    name: str
    ttl_minutes: int = DEFAULT_TTL_MINUTES
    authoritative_tracker: str | None = None
    reference_images: list[Path] = field(default_factory=list)

    # Detection results — filled in by later tickets.
    location: str | None = None
    area_id: str | None = None
    last_seen: datetime | None = None
    last_camera: str | None = None

    @property
    def ttl(self) -> timedelta:
        """How long a sighting stays valid before the state becomes unknown."""
        return timedelta(minutes=self.ttl_minutes)

    @property
    def slug(self) -> str:
        """Filesystem- and slug-safe key for this person."""
        return person_slug(self.name)


@dataclass
class Camera:
    """A camera that passed setup validation, with its resolved HA area."""

    entity_id: str
    area_id: str
    triggers: list[str] = field(default_factory=list)


@dataclass
class PersonTrackerData:
    """Per-config-entry runtime data shared with the entity platforms."""

    entry_id: str
    ai_task_entity_id: str | None
    people: dict[str, Person]
    cameras: dict[str, Camera]

    @property
    def person_names(self) -> set[str]:
        """All known person names (identity data from the config entry)."""
        return set(self.people)
