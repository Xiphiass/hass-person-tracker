"""Runtime data model for the person_tracker integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import (
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_MIN_CONFIDENCE,
    DEFAULT_RECHECK,
    DEFAULT_TTL_MINUTES,
    DOMAIN,
)

if TYPE_CHECKING:
    from .detection import DetectionCoordinator

type PersonTrackerConfigEntry = ConfigEntry[PersonTrackerData]


def person_slug(name: str) -> str:
    """Return the filesystem- and slug-safe key for person *name*."""
    return "".join(c if c.isalnum() else "-" for c in name.lower())


@dataclass
class Person:
    """One known person and their runtime location state.

    The detection pipeline (later tickets) updates ``location``/``last_seen``/
    ``last_camera``; ticket #2 establishes identity only.
    """

    # The person's config subentry; stable across renames.
    subentry_id: str
    name: str
    ttl_minutes: int = DEFAULT_TTL_MINUTES
    authoritative_tracker: str | None = None
    # Optional distinguishing features (height, build, hair, ...), sent to
    # the AI alongside the reference images.
    description: str | None = None
    # Optional Supporting tracker (ADR 0008): entities whose states name an
    # HA area and an HA floor.
    supporting_area_entity: str | None = None
    supporting_floor_entity: str | None = None
    reference_images: list[Path] = field(default_factory=list)

    # Detection results — filled in by later tickets.
    location: str | None = None
    area_id: str | None = None
    last_seen: datetime | None = None
    last_camera: str | None = None
    # Saved image of the last detection (Issue #3): the per-person
    # thumbnail file (the snapshot cropped tight around the person) and when
    # it was written. Shown by the person's image entity.
    last_thumbnail: Path | None = None
    last_thumbnail_updated: datetime | None = None

    @property
    def ttl(self) -> timedelta:
        """How long a sighting stays valid before the state becomes unknown."""
        return timedelta(minutes=self.ttl_minutes)

    @property
    def device_info(self) -> DeviceInfo:
        """The person's device, shared by all of their entities (ADR 0002)."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.subentry_id)},
            name=self.name,
            entry_type=DeviceEntryType.SERVICE,
        )


@dataclass
class Camera:
    """A camera that passed setup validation.

    Its HA area is not stored; it is looked up when a trigger fires (ADR 0003).
    """

    entity_id: str
    triggers: list[str] = field(default_factory=list)


@dataclass
class PersonTrackerData:
    """Runtime data of the config entry, shared with the entity platforms."""

    ai_task_entity_id: str
    # Keyed by subentry ID.
    people: dict[str, Person]
    # Keyed by camera entity ID.
    cameras: dict[str, Camera]
    # Seconds to wait between detection runs of the same camera (Issue #3).
    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS
    # Hits the AI reports below this confidence are not trusted.
    min_confidence: float = DEFAULT_MIN_CONFIDENCE
    # Re-check a sighting on its last camera shortly before the TTL runs out
    # (ADR 0007).
    recheck: bool = DEFAULT_RECHECK
    # The detection loop; set during entry setup.
    coordinator: DetectionCoordinator | None = None

    @property
    def person_names(self) -> set[str]:
        """All known person names."""
        return {person.name for person in self.people.values()}
