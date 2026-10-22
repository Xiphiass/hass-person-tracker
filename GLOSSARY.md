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

**Unknown snapshot**:
A saved Snapshot in which no known Person was identified. Retained on disk with camera and timestamp metadata; the raw material for future self-learning (out of scope in v1).
