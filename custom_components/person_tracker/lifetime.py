"""Pure decision rules for tracker state lifetime (Issue #6 / ADR 0004).

These functions encode the state-lifetime logic without any HA dependency so
they can be unit-tested in isolation.

TTL expiry
----------
A tracker expires to ``unknown`` when no new sighting has arrived within the
person's TTL (default 30 minutes). A person with no sighting at all is
always expired.

Authoritative demotion
----------------------
If the person's optional authoritative tracker (e.g. their phone
``device_tracker``) reports the person away from home, the camera tracker is
immediately demoted to ``unknown``.

Leading source
--------------
The current camera sighting leads; once it is not current, a supporting
tracker that resolves to an area leads instead (ADR 0008). A supporting
floor alone gives no presence. The authoritative tracker reporting the
person away outranks both: no source leads and the tracker is ``unknown``.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.const import STATE_HOME, STATE_UNKNOWN

SOURCE_CAMERA = "camera"
SOURCE_SUPPORTING = "supporting"


def is_expired(last_seen: datetime | None, ttl: timedelta, now: datetime) -> bool:
    """Return whether *last_seen* is older than *ttl* as of *now*.

    A person with no sighting (``last_seen is None``) is always expired.
    ``ttl`` of zero or less means "no expiry" (never expires).
    """
    if last_seen is None:
        return True
    if ttl <= timedelta(0):
        return False
    return (now - last_seen) > ttl


def authoritative_reports_away(state: str | None) -> bool:
    """Whether an authoritative tracker reports its person away from home.

    "home" means the person is at home (no demotion). "unknown" (or no state
    at all) is the absence of a report, not a report of being away, so it
    does not demote. Any other state — "not_home" or a named zone — is a
    report that the person is away from home and therefore demotes the
    camera tracker.
    """
    if state is None or state == STATE_UNKNOWN:
        return False
    return state != STATE_HOME


def leading_source(
    *, authoritative_away: bool, camera_current: bool, has_supporting_area: bool
) -> str | None:
    """Which source places the person at home, if any.

    ``camera`` while the camera sighting is current, else ``supporting``
    while the supporting tracker resolves to an area, else None (unknown).
    None whenever the authoritative tracker reports the person away.
    """
    if authoritative_away:
        return None
    if camera_current:
        return SOURCE_CAMERA
    if has_supporting_area:
        return SOURCE_SUPPORTING
    return None
