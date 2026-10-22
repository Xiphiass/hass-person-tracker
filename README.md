# Person Tracker

Tracks household members' locations using camera snapshots and LLM-based identification, as a Home Assistant custom integration.

## Features

- **AI-Powered Identification**: Uses Home Assistant's AI Task infrastructure to identify household members from camera snapshots.
- **Area-Based Tracking**: Automatically maps camera locations to Home Assistant areas.
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
4. After initial setup, you can add, edit, or remove persons from the integration's configuration page. Each person can have an optional description of distinguishing features (height, build, hair, glasses, ...), which is sent to the AI to help tell household members apart when the face is not visible.
5. For each person, place reference images in your Home Assistant configuration directory under `person_tracker/people/<person_name>/`. They are sent to the AI along with every camera snapshot to recognize who is who. Changes apply on the next detection, no reload needed. Each reference image adds to the size, and so the cost, of every AI request, so a few clear photos per person work best.

## Requirements

- Home Assistant Core 2025.8 or later (for AI Task integration support).
- A configured AI provider integration (e.g., OpenAI, Google Generative AI, Ollama) that exposes AI Task entities.
- Cameras assigned to Home Assistant areas.
- Binary sensors to act as detection triggers (e.g., motion or person-detected sensors).

## Documentation

For more detailed information, see the [ADRs](docs/adr/) in this repository.
