import pytest
from pathlib import Path
from homeassistant.core import HomeAssistant
from custom_components.person_tracker.helpers import (
    discover_reference_images,
    get_person_images_dir,
)
from custom_components.person_tracker.const import DOMAIN, PEOPLE_DIR


@pytest.fixture
def mock_config_dir(hass: HomeAssistant, tmp_path: Path) -> Path:
    """Set up the config directory in temp storage."""
    hass.config.config_dir = str(tmp_path)
    return tmp_path


async def test_discover_reference_images_creates_folder(
    hass: HomeAssistant, mock_config_dir: Path
) -> None:
    """Verify that discover_reference_images creates the person folder if it doesn't exist."""
    person_name = "TestPerson"
    folder = get_person_images_dir(hass, person_name)

    # Ensure it doesn't exist yet
    assert not folder.exists()

    # Run the function
    images = await hass.async_add_executor_job(
        discover_reference_images, hass, person_name
    )

    # Verify it now exists
    assert folder.exists()
    assert folder.is_dir()
    assert images == []
