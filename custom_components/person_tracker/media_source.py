"""Expose the reference images as a media source.

The AI Task integration only accepts attachments it can resolve through
media_source to a local file, so the per-person reference-image folders are
served as ``media-source://person_tracker/people/<person>/<file>``.
"""

from __future__ import annotations

from homeassistant.components.media_source import local_source
from homeassistant.core import HomeAssistant
from homeassistant.helpers.singleton import singleton

from .const import DOMAIN, PEOPLE_DIR

DATA_MEDIA_SOURCE = f"{DOMAIN}_media_source"


@singleton(DATA_MEDIA_SOURCE, async_=True)
async def async_get_media_source(hass: HomeAssistant) -> local_source.LocalSource:
    """Set up the reference-image media source."""
    return local_source.LocalSource(
        hass,
        DOMAIN,
        "Person Tracker reference images",
        {PEOPLE_DIR: hass.config.path(DOMAIN, PEOPLE_DIR)},
        f"/{DOMAIN}",
    )
