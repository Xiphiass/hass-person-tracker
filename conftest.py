"""Root conftest: wire the product integration into the vendored HA test harness.

The vendored harness (tests/) uses tests/testing_config as its config dir, and
HA's loader discovers custom components under
tests/testing_config/custom_components/. A symlink keeps the tests pointed at
the live product code in custom_components/person_tracker/.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
PRODUCT_DIR = REPO_ROOT / "custom_components" / "person_tracker"
HARNESS_LINK = (
    REPO_ROOT / "tests" / "testing_config" / "custom_components" / "person_tracker"
)


def _ensure_product_linked() -> None:
    """Create or refresh the harness symlink to the product integration."""
    if HARNESS_LINK.is_symlink():
        if HARNESS_LINK.resolve() == PRODUCT_DIR.resolve():
            return
        HARNESS_LINK.unlink()
    elif HARNESS_LINK.exists():
        raise RuntimeError(
            f"{HARNESS_LINK} exists and is not a symlink; refusing to overwrite"
        )
    HARNESS_LINK.parent.mkdir(parents=True, exist_ok=True)
    HARNESS_LINK.symlink_to(PRODUCT_DIR, target_is_directory=True)


_ensure_product_linked()
