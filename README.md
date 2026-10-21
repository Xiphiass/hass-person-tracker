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
   - Configure the detection cooldown period.
4. After initial setup, you can add, edit, or remove persons from the integration's configuration page.
5. For each person, place reference images in your Home Assistant configuration directory under `person_tracker/people/<person_name>/`.

## Requirements

- Home Assistant Core 2025.8 or later (for AI Task integration support).
- A configured AI provider integration (e.g., OpenAI, Google Generative AI, Ollama) that exposes AI Task entities.
- Cameras assigned to Home Assistant areas.
- Binary sensors to act as detection triggers (e.g., motion or person-detected sensors).

## Documentation

For more detailed information, see the [ADRs](docs/adr/) in this repository.
