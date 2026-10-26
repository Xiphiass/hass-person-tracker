# hass-person-tracker

Tracks the location of known household members using camera snapshots and
LLM-based identification, as a Home Assistant custom integration.

## Language

**Person**:
A known household member who has one or more reference images. The thing we track. Configured as a subentry of the integration entry.
_Avoid_: user, member, face

**Location**:
The HA Area a camera belongs to (e.g. "garden", "living room"). Locations come from HA areas, never from our own configuration.
_Avoid_: place, zone (HA's `zone` is a geofence concept — different thing)

**Camera**:
A Home Assistant camera entity assigned to an HA Area; that Area is the Camera's Location. Any camera integration works; UniFi is one example.

**Detection**:
A binary sensor asserting that a person is currently in a camera's field of view (e.g. UniFi's person-detected sensor, or any motion sensor). Detections are the trigger source.
_Avoid_: trigger, motion (motion may fire for non-persons)

**Snapshot**:
A still image taken from a Camera when a Detection fires. Input to Identification and the material saved for unknown persons.
_Avoid_: frame, capture, picture

**Reference image**:
A known photo of a Person, supplied by the user, used by Identification.
_Avoid_: sample picture, training set

**Identification**:
The AI step that decides which Persons appear in a Snapshot, constrained to the known Persons, using their reference images.
_Avoid_: recognition, classification

**Description**:
Optional free-text distinguishing features of a Person (height, build, hair, glasses, ...), sent to Identification alongside the reference images. Supports telling Persons apart; on its own it never justifies a confident identification.

**Minimum confidence**:
The global threshold (0-1) below which an Identification is not trusted: the Person's tracker is not updated and the Snapshot is kept as an Unknown snapshot.

**Authoritative tracker**:
An optional external device_tracker per Person (e.g. their phone) that outranks the camera tracker: when it reports the Person away, the camera tracker is immediately demoted to unknown.

**Supporting tracker**:
An optional, lower-priority, room-level location source per Person (e.g. a BLE tracker), configured as an area entity and/or a floor entity. The camera sighting outranks it, and the Authoritative tracker can demote it. It leads once the camera sighting has expired, or after a move the cameras did not see, and it can raise an Identification's confidence (ADR 0008, ADR 0009). One per Person.
_Avoid_: secondary tracker, BLE tracker (BLE is one example)

**Re-check**:
An Identification run shortly before a Person's sighting expires, on the Person's last camera and asking about that Person only. A confirmation refreshes the sighting; anything else lets it expire (ADR 0007). Skipped while the Supporting tracker leads after a move (ADR 0008).
_Avoid_: poll, refresh

**Move check**:
An Identification run on the cameras of the area the Supporting tracker has just moved the Person to, asking about that Person only, so a camera can confirm the move. A confirmation is a normal sighting; a miss changes nothing (ADR 0008).

**Unknown snapshot**:
A saved Snapshot in which no known Person was identified. Retained on disk with camera and timestamp metadata; the raw material for future self-learning (out of scope in v1).
