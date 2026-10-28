<p align="center">
  <img src="custom_components/person_tracker/brand/icon@2x.png" alt="Person Tracker icon" width="128">
</p>

<p align="center">
  <a href="https://github.com/Xiphiass/hass-person-tracker/releases"><img src="https://img.shields.io/github/v/release/Xiphiass/hass-person-tracker" alt="Release"></a>
  <a href="https://github.com/Xiphiass/hass-person-tracker/actions/workflows/ci.yml"><img src="https://github.com/Xiphiass/hass-person-tracker/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://hacs.xyz/docs/faq/custom_repositories/"><img src="https://img.shields.io/badge/HACS-Custom-41BDF5.svg" alt="HACS Custom"></a>
  <img src="https://img.shields.io/badge/Home%20Assistant-2025.8%2B-18BCF2?logo=homeassistant&logoColor=white" alt="Home Assistant 2025.8+">
  <a href="LICENSE"><img src="https://img.shields.io/github/license/Xiphiass/hass-person-tracker" alt="License: MIT"></a>
</p>

# Person Tracker

Tracks household members' locations using camera snapshots and LLM-based identification, as a Home Assistant custom integration.

## Features

- **AI-Powered Identification**: Uses Home Assistant's AI Task infrastructure to identify household members from camera snapshots.
- **Area-Based Tracking**: Automatically maps camera locations to Home Assistant areas.
- **Works with HA persons**: The tracker reports `home` while a person is seen, so linking it to a Home Assistant person marks them at home. The room they were seen in is in the tracker's `area` attribute (and `last_location`, kept after the sighting goes stale). The tracker also takes the linked person's profile picture, so tile cards can show it.
- **Configurable Persistence**: Supports Time-to-Live (TTL) expiry and authoritative tracking (e.g., phone GPS) to manage tracker state reliability.
- **Unknown Person Support**: Automatically captures and retains snapshots of unidentified individuals for review.

## Installation via HACS

1. Ensure you have [HACS](https://hacs.xyz/) installed in your Home Assistant instance.
2. Go to HACS -> Integrations.
3. Click the three dots in the top right corner and select **Custom repositories**.
4. Paste this repository URL (`https://github.com/Xiphiass/hass-person-tracker`) and select **Integration** as the category.
5. Click **Add**.
6. Find "Person Tracker" in the list and click **Download**.
7. Restart Home Assistant.

## Configuration

The integration is configured entirely through the Home Assistant UI:

1. Go to **Settings** -> **Devices & Services**.
2. Click **Add Integration** and search for **Person Tracker**.
3. Follow the instructions to:
   - Select the cameras to monitor and assign trigger binary sensors to them.
   - Choose the AI Task entity for identification.
   - Configure the detection cooldown period and the minimum confidence (default 0.7). Identifications the AI reports below the minimum confidence update no tracker; the snapshot is kept as an unknown snapshot instead. Raise the value if people get mixed up.
   - Choose whether to re-check before expiry (default on). A person who sits still triggers no new detection, so shortly before their sighting expires the camera that last saw them takes one more snapshot and the AI is asked about that person only. A confirmation keeps them at home; otherwise the sighting expires as usual. This costs one AI call per expiry while someone stays in view. With a supporting tracker, a person whose supporting area changed after the sighting and has held for two minutes is not re-checked, since the camera would only show the room they left.
   - Choose whether to run a move check (default on). It only affects persons with a supporting area entity (see below). When a person's supporting area changes to another area that has cameras, holds there for two minutes, and no camera has seen them since the change, every camera of that area takes a snapshot and the AI is asked about that person only. A confirmation is a normal camera sighting, so the camera leads again. A miss or a failed run changes nothing, and the move check is not retried. Cameras that ran within the cooldown are left out, since they just answered, and the move check claims the cooldown of the cameras it uses. It never saves an unknown snapshot. It is skipped when the authoritative tracker reports the person away or the person has no reference images, and persons whose moves into the same area settle within ten seconds of each other share one run per camera. A return to the area the supporting tracker last settled in, such as after it was briefly `unavailable`, is no move. A move that has not settled when the integration loads (for example right after a reload or a restart) is still checked. This costs at most one AI call per camera of the new area per move.
4. After initial setup, you can add, edit, or remove persons from the integration's configuration page. Each person can have an optional description of distinguishing features (height, build, hair, glasses, ...), which is sent to the AI to help tell household members apart when the face is not visible.
   Each person can also have an optional **supporting tracker**: a lower-priority, room-level location source such as a BLE tracker (e.g. Bermuda, ESPresense). It is configured as two optional entities from any integration:
   - **Supporting area entity**: an entity whose state names a Home Assistant area (e.g. Bermuda's `sensor.<device>_area`). An `area_id` attribute naming a known area wins; otherwise the state is matched against area IDs, then names, then aliases, ignoring case.
   - **Supporting floor entity**: an entity whose state names a Home Assistant floor, resolved the same way (`floor_id` attribute, then ID, name, aliases). Without it, the supporting floor is the floor of the supporting area.

   States such as `unknown`, `unavailable`, `not_home` or `away`, or a state that matches no area or floor, mean no supporting location, even if an `area_id`/`floor_id` attribute is still set.

   The camera leads while its sighting is current. Once the sighting has expired (or before the person was ever seen), a supporting area keeps the tracker `home` in that area, so the person stays at home while out of the cameras' view. A supporting floor without an area gives no presence, as a floor alone is too coarse. The authoritative tracker still outranks everything: when it reports the person away, the tracker is `unknown` even if the supporting tracker still reports an area (e.g. a BLE tag left at home).

   The supporting tracker can also corroborate a camera identification. After the AI has answered, a hit on a camera in the supporting area gains +0.15 confidence, or +0.05 if only the supporting floor matches the camera area's floor, before it is compared with the minimum confidence. The boosts don't add up, the result is capped at 1.0, and a supporting tracker that disagrees never lowers the confidence. This applies to detection runs and presence re-checks, but not to move checks: there the supporting tracker already chose the camera and the person, so its agreement would only confirm itself. The supporting tracker is never mentioned to the AI, so the camera stays an independent check. Debug logging shows the raw and boosted confidence whenever a boost applies.
5. For each person, place reference images in your Home Assistant configuration directory under `person_tracker/people/<person_name>/`. They are sent to the AI along with every camera snapshot to recognize who is who. Changes apply on the next detection, no reload needed. Each reference image adds to the size, and so the cost, of every AI request, so a few clear photos per person work best. AI providers bill images by their pixel size, so an image larger than 512 px on its longer edge is sent as a downscaled copy. The copies are kept under `person_tracker/reference_cache/` and rebuilt when you replace an image; your originals are never changed.

## Tracker attributes

Each person's tracker (`device_tracker.<person>`) is `home` while a camera sighting is current or, after that, while the supporting tracker reports an area. It is `unknown` otherwise, and whenever the authoritative tracker reports the person away. It carries these attributes:

| Attribute | Description | Present |
| --- | --- | --- |
| `source` | `camera` while the camera sighting leads, otherwise the ID of the supporting area entity | Only while `home` |
| `area` | Display name of the area the person is in: the camera's area, or the supporting area while the supporting tracker leads | Only while `home` |
| `floor` | Display name of that area's floor: the camera area's floor, or the supporting floor while the supporting tracker leads | Only while `home`, and only if there is a floor |
| `last_location` | Display name of the area the person was last seen in by a camera | Kept after expiry |
| `area_id` | ID of that area | Kept after expiry |
| `last_floor` | Display name of that area's floor | Kept after expiry, if the area is on a floor |
| `floor_id` | ID of that floor | Kept after expiry, if the area is on a floor |
| `last_seen` | Time of the last sighting | Kept after expiry |
| `last_camera` | Camera of the last sighting | Kept after expiry |
| `supporting_area` | Display name of the supporting tracker's area | While the supporting area entity resolves to an area |
| `supporting_area_id` | ID of that area | Same as `supporting_area` |
| `supporting_floor` | Display name of the supporting tracker's floor | While a supporting floor resolves |
| `supporting_floor_id` | ID of that floor | Same as `supporting_floor` |
| `supporting_agrees` | `area` if the supporting area is the camera's area, `floor` if only the floor matches, otherwise `none` | Only while the camera leads and a supporting area or floor resolves |

The `last_*`, `area_id` and `floor_id` attributes always describe the last camera sighting; the supporting tracker never overwrites them. Area and floor are looked up live from the last seen area, so renaming an area or floor, or moving the area to another floor, shows on the tracker without a reload, also after expiry.

The supporting attributes are resolved live too: they update whenever a supporting entity changes state or an area or floor is renamed.

## Requirements

- Home Assistant Core 2025.8 or later (for AI Task integration support). The integration's own icon shows from Home Assistant 2026.3 on, which loads brand images shipped with custom integrations.
- A configured AI provider integration (e.g., OpenAI, Google Generative AI, Ollama) that exposes AI Task entities.
- Cameras assigned to Home Assistant areas.
- Binary sensors to act as detection triggers (e.g., motion or person-detected sensors).

## Documentation

For more detailed information, see the [ADRs](docs/adr/) in this repository.

## License

[MIT](LICENSE)
